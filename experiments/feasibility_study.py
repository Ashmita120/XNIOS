"""V2 Stage 2 — Target Feasibility & Predictability Study (the hard gate).

    python experiments/feasibility_study.py
    python experiments/feasibility_study.py --runs 8 --quick

No ML target is committed to before this passes. For every candidate target the
study measures three numbers on held-out runs:

    persistence   what you get for free by assuming nothing changes
    analytical    what physics + the Stage 1 forecaster already know
    learned       what a strong tabular model gets with every feature available

and separately:

    ceiling       how much of the future is irreducibly uncertain, measured by
                  branching the same world into N stochastic futures

The decision rule is the gap:

    headroom = learned - max(persistence, analytical)

A target with no headroom is not an ML problem, however appealing it sounds. A
target whose analytical baseline already sits at the ceiling is *finished* — the
correct implementation is the closed form, not a model.

Metrics: R^2 on held-out runs for continuous targets; AUC + Brier for binary ones.
Splits are always **by run**, never by row — rows inside a run are massively
autocorrelated and a random row split reports a spectacular, fake score.
"""

from __future__ import annotations

import argparse
import copy
import math
import os
import sys
import warnings

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import forecast as fc
from xnios.allocators import make_allocator, make_freq_allocator, make_power_allocator
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler
from xnios.link import achievable_rate_bps, snr_linear
from xnios.simulator import Simulator
from xnios.telemetry import TelemetryRecorder, MemorySink
from xnios.weather import DynamicWeatherModel

from api.presets import all_presets

warnings.filterwarnings("ignore", category=UserWarning)

try:
    from sklearn.ensemble import HistGradientBoostingRegressor, HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score, brier_score_loss
    HAVE_SK = True
except ImportError:                                        # pragma: no cover
    HAVE_SK = False


# --------------------------------------------------------------------- running

def run_once(cfg, seed=None, scheduler="fcfs/strongest"):
    """One telemetered run. `seed` overrides the config seed (new world + weather)."""
    cfg = copy.deepcopy(cfg)
    if seed is not None:
        cfg["seed"] = seed
    scn = scenario_from_config(cfg)
    simcfg = sim_config_from_config(cfg)
    rec = TelemetryRecorder(sink=MemorySink())
    Simulator(scn, make_scheduler(scheduler), simcfg,
              allocator=make_allocator("equal"),
              power_allocator=make_power_allocator("adaptive"),
              freq_allocator=make_freq_allocator("coloring"),
              telemetry=rec).run()
    return scn, simcfg, rec.sink.records


# ------------------------------------------------------- analytical forecasting

class Analytics:
    """Stage 1 forecaster, precomputed per run.

    `contact_windows` costs ~0.5 ms per call; a dataset needs the answer for every
    (pair, step, horizon). Computing the whole schedule once per run turns that
    into a dictionary lookup, which is the only reason this study is cheap enough
    to run over dozens of scenarios.
    """

    def __init__(self, scn, horizon_s):
        self.scn = scn
        self.sats = {s.id: s for s in scn.satellites}
        self.stations = {g.id: g for g in scn.stations}
        self.win = {}
        for s in scn.satellites:
            for g in scn.stations:
                rain = scn.weather.fade_db(g.id, 0.0)
                self.win[(s.id, g.id)] = fc.contact_windows(
                    s, g, 0.0, horizon_s, step_s=10.0, rain_zenith_db=rain)

    def time_to_los(self, sid, gid, t):
        for w in self.win.get((sid, gid), ()):
            if w.t_rise <= t <= w.t_set:
                return w.t_set - t
        return None

    def visible_at(self, sid, gid, t) -> bool:
        return any(w.contains(t) for w in self.win.get((sid, gid), ()))

    def snr_db_at(self, sid, gid, t, bw_hz=None, pw_w=None) -> float | None:
        """The link budget's interference-free SNR at a future instant.

        This is the *proper* analytical baseline for future SNR, and it must be
        computed from the budget directly rather than inverted from the rate —
        `rate_from_sinr` caps spectral efficiency at 5.5 bps/Hz, so inverting it
        silently clamps every strong link to ~16.5 dB.

        `telemetry.LinkRecord.snr_db` is measured over the bandwidth the allocator
        granted, so the honest "physics + policy unchanged" forecast holds the
        current bandwidth and power fixed and moves only the geometry.
        """
        s, g = self.sats[sid], self.stations[gid]
        if not self.visible_at(sid, gid, t):
            return None
        elev, rng = fc.elevation_series(s, g, np.array([t]))
        rain = self.scn.weather.fade_db(gid, 0.0)
        lin = snr_linear(float(rng[0]), float(elev[0]), s, g, rain_zenith_db=rain,
                         bandwidth_hz=bw_hz, tx_power_w=pw_w)
        return 10.0 * math.log10(lin) if lin > 0 else None

    def capacity_bps_at(self, t, beams_total) -> float:
        """Best achievable aggregate rate at `t`: the top-N visible links by rate,
        N = total beams. A capacity bound, not a prediction of scheduler behaviour."""
        rates = []
        for (sid, gid), ws in self.win.items():
            if not any(w.contains(t) for w in ws):
                continue
            s, g = self.sats[sid], self.stations[gid]
            elev, rng = fc.elevation_series(s, g, np.array([t]))
            rain = self.scn.weather.fade_db(gid, 0.0)
            rates.append(achievable_rate_bps(float(rng[0]), float(elev[0]), s, g,
                                             rain_zenith_db=rain))
        rates.sort(reverse=True)
        return float(sum(rates[:beams_total]))


# ------------------------------------------------------------- dataset building

LINK_HORIZONS = [30.0, 60.0, 120.0]
NET_HORIZONS = [60.0, 180.0, 300.0]


def build_samples(scn, records, run_id, dt_s):
    """Feature rows + every candidate target, for one run."""
    horizon = records[-1].t + max(NET_HORIZONS) + max(LINK_HORIZONS)
    an = Analytics(scn, horizon)
    by_t = {r.t: r for r in records}
    times = sorted(by_t)
    beams_total = records[0].network.beams_total
    backlog = {}
    for r in records:
        for s in r.satellites:
            backlog.setdefault(r.t, {})[s.sat_id] = s.backlog_bits

    link_rows, net_rows = [], []

    # ---- link level: one row per visible pair per step
    for r in records:
        for l in r.links:
            ttl = an.time_to_los(l.sat_id, l.station_id, r.t)
            row = {
                "run": run_id, "t": r.t, "sat": l.sat_id, "station": l.station_id,
                # observed now
                "f_elev": l.elev_deg, "f_snr": l.snr_db, "f_sinr": l.sinr_db,
                "f_range": l.range_km, "f_scan": l.scan_deg, "f_fade": l.rain_fade_db,
                "f_rate": l.rate_bps, "f_active": float(l.active),
                "f_inr": l.inr_db, "f_bw": l.alloc_bw_hz, "f_pwr": l.alloc_power_w,
                "f_beams_active": r.network.beams_active,
                "f_beams_avail": r.network.beams_available,
                "f_contention": r.network.contention_ratio,
                "f_nwait": r.network.n_waiting,
                # analytical forecast — the physics half of the feature set
                "f_ttl": ttl if ttl is not None else -1.0,
            }
            for h in LINK_HORIZONS:
                row[f"f_elev_p{int(h)}"] = fc.elevation_at(
                    an.sats[l.sat_id], an.stations[l.station_id], r.t + h)
                row[f"f_vis_p{int(h)}"] = float(an.visible_at(l.sat_id, l.station_id, r.t + h))

            # ---- analytical predictions (the baseline, computed per row)
            for h in LINK_HORIZONS:
                a = an.snr_db_at(l.sat_id, l.station_id, r.t + h,
                                 bw_hz=l.alloc_bw_hz or None,
                                 pw_w=l.alloc_power_w or None)
                row[f"a_snr{int(h)}"] = a if a is not None else np.nan
                row[f"a_loss{int(h)}"] = 1.0 - row[f"f_vis_p{int(h)}"]

            # ---- targets
            #
            # A link row vanishing is NOT the same as a link being lost: the
            # simulator stops emitting a pair once the satellite drains its
            # buffer, so counting that as "loss" would train a model to predict
            # the scheduler finishing rather than the pass ending. Samples whose
            # satellite completes inside the horizon are dropped instead.
            for h in LINK_HORIZONS:
                fut = by_t.get(r.t + h)
                if fut is None:
                    row[f"y_loss{int(h)}"] = np.nan
                    row[f"y_snr{int(h)}"] = np.nan
                    continue
                still_needs = backlog.get(r.t + h, {}).get(l.sat_id, 0.0) > 0
                nxt = next((x for x in fut.links
                            if x.sat_id == l.sat_id and x.station_id == l.station_id), None)
                row[f"y_loss{int(h)}"] = (0.0 if nxt is not None else 1.0) if still_needs else np.nan
                row[f"y_snr{int(h)}"] = nxt.snr_db if nxt is not None else np.nan
            link_rows.append(row)

    # ---- network level: one row per step
    for r in records:
        n = r.network
        row = {
            "run": run_id, "t": r.t,
            "f_throughput": n.throughput_bps, "f_queue": n.queue_bits,
            "f_beam_util": n.beam_utilization, "f_bw_util": n.bandwidth_utilization,
            "f_coverage": n.coverage, "f_contention": n.contention_ratio,
            "f_nwait": n.n_waiting, "f_backlogged": n.n_backlogged,
            "f_active": n.beams_active, "f_avail": n.beams_available,
            "f_power": n.power_w, "f_sinr": n.mean_sinr_db,
            "f_sats_link": n.n_sats_with_link, "f_pairs": n.n_visible_pairs,
        }
        for h in NET_HORIZONS:
            row[f"f_cap_p{int(h)}"] = an.capacity_bps_at(r.t + h, beams_total)
            row[f"f_pairs_p{int(h)}"] = float(sum(
                1 for k in an.win if an.visible_at(k[0], k[1], r.t + h)))
        for h in NET_HORIZONS:
            fut = by_t.get(r.t + h)
            if fut is None:
                for k in ("thr", "queue", "util", "energy"):
                    row[f"y_{k}{int(h)}"] = np.nan
                continue
            row[f"y_thr{int(h)}"] = fut.network.throughput_bps
            row[f"y_queue{int(h)}"] = fut.network.queue_bits
            row[f"y_util{int(h)}"] = fut.network.beam_utilization
            row[f"y_energy{int(h)}"] = fut.network.energy_j_total - n.energy_j_total
        net_rows.append(row)

    return link_rows, net_rows


# ------------------------------------------------------------------- evaluation

def _r2(y, pred):
    y, pred = np.asarray(y, float), np.asarray(pred, float)
    ok = np.isfinite(y) & np.isfinite(pred)
    if ok.sum() < 3:
        return float("nan")
    y, pred = y[ok], pred[ok]
    sst = float(np.sum((y - y.mean()) ** 2))
    if sst <= 0:
        return float("nan")
    return 1.0 - float(np.sum((y - pred) ** 2)) / sst


def _cols(rows, prefixes=("f_", "a_")):
    """Feature matrix. The analytical predictions (`a_*`) are included on purpose:
    the fair test of headroom is whether a model can beat the closed form *given*
    the closed form, which is the residual-learning setup the plan recommends."""
    keys = sorted({k for r in rows for k in r if k.startswith(prefixes)})
    return keys, np.array([[r.get(k, np.nan) for k in keys] for r in rows], float)


def eval_continuous(rows, target, persist_key, analytic_fn, label, out):
    y = np.array([r.get(target, np.nan) for r in rows], float)
    runs = np.array([r["run"] for r in rows])
    keys, X = _cols(rows)
    persist = np.array([r.get(persist_key, np.nan) for r in rows], float)
    analytic = np.array([analytic_fn(r) for r in rows], float)

    # every baseline is scored on the SAME rows, or the comparison is meaningless
    ok = np.isfinite(y) & np.isfinite(persist) & np.isfinite(analytic)
    y, X, runs, persist, analytic = y[ok], X[ok], runs[ok], persist[ok], analytic[ok]
    if len(y) < 50:
        out.append((label, len(y), float("nan"), float("nan"), float("nan"), "too few samples"))
        return

    uniq = sorted(set(runs.tolist()))
    test_runs = set(uniq[::3]) or {uniq[-1]}          # every 3rd run held out
    te = np.array([r in test_runs for r in runs])
    tr = ~te
    if tr.sum() < 30 or te.sum() < 10:
        out.append((label, len(y), float("nan"), float("nan"), float("nan"), "split too small"))
        return

    r2_p = _r2(y[te], persist[te])
    r2_a = _r2(y[te], analytic[te])
    r2_m = float("nan")
    pred_m = None
    if HAVE_SK:
        m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.08,
                                          random_state=0)
        m.fit(X[tr], y[tr])
        pred_m = m.predict(X[te])
        r2_m = _r2(y[te], pred_m)
    best_base = np.nanmax([r2_p, r2_a])
    head = r2_m - best_base if np.isfinite(r2_m) and np.isfinite(best_base) else float("nan")

    # How much of a good R^2 is just "predict zero"? After the pass ends this
    # network is idle for most of the run, so a target that is mostly zero can
    # look well predicted while carrying no information about the busy period.
    yt = y[te]
    zero_frac = float(np.mean(np.abs(yt) < 1e-9))
    act = np.abs(yt) > 1e-9
    r2_act = _r2(yt[act], pred_m[act]) if (pred_m is not None and act.sum() > 10) else float("nan")
    note = f"{head:+.3f}   zero {100 * zero_frac:>4.0f}%"
    if np.isfinite(r2_act):
        note += f"  active R2 {r2_act:+.3f}"
    out.append((label, int(te.sum()), r2_p, r2_a, r2_m, note))


def eval_binary(rows, target, analytic_fn, label, out):
    y = np.array([r.get(target, np.nan) for r in rows], float)
    runs = np.array([r["run"] for r in rows])
    keys, X = _cols(rows)
    analytic = np.array([analytic_fn(r) for r in rows], float)

    ok = np.isfinite(y) & np.isfinite(analytic)
    y, X, runs, analytic = y[ok], X[ok], runs[ok], analytic[ok]
    uniq = sorted(set(runs.tolist()))
    test_runs = set(uniq[::3]) or {uniq[-1]}
    te = np.array([r in test_runs for r in runs])
    tr = ~te
    base_rate = float(y.mean())
    if len(set(y[te].tolist())) < 2 or tr.sum() < 30:
        out.append((label, int(te.sum()), float("nan"), float("nan"), float("nan"),
                    f"base rate {base_rate:.2f} — degenerate"))
        return

    auc_a = roc_auc_score(y[te], analytic[te]) if HAVE_SK else float("nan")
    brier_a = brier_score_loss(y[te], np.clip(analytic[te], 0, 1)) if HAVE_SK else float("nan")
    auc_m = brier_m = float("nan")
    if HAVE_SK:
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, random_state=0)
        m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]
        auc_m = roc_auc_score(y[te], p)
        brier_m = brier_score_loss(y[te], p)
    out.append((label, int(te.sum()), base_rate, auc_a, auc_m,
                f"brier {brier_a:.4f} -> {brier_m:.4f}"))


# ------------------------------------------------- irreducible uncertainty (C)

def ceiling_study(cfg, branch_times, horizons, n_seeds=24):
    """Branch one world into N stochastic futures and measure the spread.

    The satellites, stations, orbits and backlogs are held **identical**; only the
    weather walk after the branch point is redrawn. That isolates aleatoric
    uncertainty — the part of the future that is unknowable rather than merely
    unknown — which is the ceiling no model can pass.

    Failures are deliberately not branched: they are a memoryless Poisson process,
    so by construction nothing in the observable state predicts them, and their
    contribution is pure irreducible variance on top of what is measured here.
    """
    cfg = copy.deepcopy(cfg)
    wcfg = dict(cfg.get("weather", {}))
    wcfg["provider"] = "dynamic"
    # The dwell must be SHORT and the branch EARLY, or the redrawn weather only
    # starts after the pass is over and every future is trivially identical —
    # a measurement artifact, not a finding. Passes here last ~5 minutes.
    wcfg["dwell_s"] = 30.0
    cfg["weather"] = wcfg

    base_scn = scenario_from_config(cfg)
    simcfg = sim_config_from_config(cfg)
    dwell = wcfg["dwell_s"]

    out = {}
    for t_branch in branch_times:
        kb = int(t_branch / dwell)
        series = []
        for k in range(n_seeds):
            scn = copy.deepcopy(base_scn)
            alt = DynamicWeatherModel(scn.stations, seed=1000 + k, dwell_s=dwell,
                                      horizon_s=simcfg.duration_s + 600.0)
            for gid, walk in scn.weather.walk.items():
                other = alt.walk[gid]
                n = max(len(walk), len(other))
                merged = [walk[min(i, len(walk) - 1)] if i <= kb
                          else other[min(i, len(other) - 1)] for i in range(n)]
                scn.weather.walk[gid] = merged          # shared past, divergent future
            rec = TelemetryRecorder(sink=MemorySink())
            Simulator(scn, make_scheduler("fcfs/strongest"), simcfg,
                      allocator=make_allocator("equal"),
                      power_allocator=make_power_allocator("adaptive"),
                      freq_allocator=make_freq_allocator("coloring"),
                      telemetry=rec).run()
            series.append({r.t: r.network for r in rec.sink.records})

        for h in horizons:
            t = t_branch + h
            thr = [s[t].throughput_bps for s in series if t in s]
            que = [s[t].queue_bits for s in series if t in s]
            if len(thr) < 3:
                continue
            out.setdefault(("throughput", h), []).append((np.mean(thr), np.std(thr)))
            out.setdefault(("queue", h), []).append((np.mean(que), np.std(que)))
    return out


# ------------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=6, help="stochastic seeds per preset")
    ap.add_argument("--seeds", type=int, default=16, help="branches for the ceiling study")
    ap.add_argument("--quick", action="store_true", help="skip the ceiling study")
    args = ap.parse_args()

    if not HAVE_SK:
        print("  scikit-learn not installed — the 'learned' column will be blank.")

    presets = all_presets()
    chosen = ["india4-nominal", "india4-congested", "india4-storm"]

    link_rows, net_rows = [], []
    print()
    for name in chosen:
        for k in range(args.runs):
            scn, simcfg, records = run_once(presets[name], seed=k)
            lr, nr = build_samples(scn, records, f"{name}#{k}", simcfg.dt_s)
            link_rows += lr
            net_rows += nr
            print(f"  ran {name:<20} seed {k}  ->  {len(lr):>5} link rows, "
                  f"{len(nr):>4} network rows")
    print(f"\n  dataset: {len(link_rows)} link samples, {len(net_rows)} network samples, "
          f"{len(chosen) * args.runs} runs (every 3rd held out)\n")

    out = []
    # ---- link level
    for h in LINK_HORIZONS:
        eval_binary(link_rows, f"y_loss{int(h)}",
                    lambda r, h=h: r.get(f"a_loss{int(h)}", np.nan),
                    f"link loss <= {int(h)}s", out)
    for h in LINK_HORIZONS:
        eval_continuous(link_rows, f"y_snr{int(h)}", "f_snr",
                        lambda r, h=h: r.get(f"a_snr{int(h)}", np.nan),
                        f"link SNR @ +{int(h)}s", out)
    # ---- network level
    for h in NET_HORIZONS:
        eval_continuous(net_rows, f"y_thr{int(h)}", "f_throughput",
                        lambda r, h=h: r.get(f"f_cap_p{int(h)}", np.nan),
                        f"throughput @ +{int(h)}s", out)
    for h in NET_HORIZONS:
        eval_continuous(net_rows, f"y_queue{int(h)}", "f_queue",
                        lambda r, h=h: max(0.0, r.get("f_queue", 0.0)
                                           - r.get(f"f_cap_p{int(h)}", 0.0) * h),
                        f"queue @ +{int(h)}s", out)
    for h in NET_HORIZONS:
        eval_continuous(net_rows, f"y_util{int(h)}", "f_beam_util",
                        lambda r, h=h: min(1.0, r.get(f"f_pairs_p{int(h)}", 0.0)
                                           / max(1.0, r.get("f_avail", 1.0))),
                        f"beam util @ +{int(h)}s", out)
    for h in NET_HORIZONS:
        eval_continuous(net_rows, f"y_energy{int(h)}", "f_power",
                        lambda r, h=h: r.get("f_power", 0.0) * h,
                        f"energy over +{int(h)}s", out)

    # ---- report
    print("  CONTINUOUS TARGETS — R^2 on held-out runs")
    print(f"  {'target':<26} {'n':>6} {'persist':>9} {'analytic':>9} {'learned':>9}  headroom")
    print(f"  {'-'*26} {'-'*6} {'-'*9} {'-'*9} {'-'*9}  {'-'*9}")
    for label, n, a, b, c, note in out:
        if label.startswith("link loss"):
            continue
        f = lambda v: "   --  " if not np.isfinite(v) else f"{v:>9.3f}"
        print(f"  {label:<26} {n:>6} {f(a)} {f(b)} {f(c)}  {note}")

    print("\n  BINARY TARGETS — AUC on held-out runs")
    print(f"  {'target':<26} {'n':>6} {'base':>9} {'analytic':>9} {'learned':>9}  brier")
    print(f"  {'-'*26} {'-'*6} {'-'*9} {'-'*9} {'-'*9}  {'-'*9}")
    for label, n, a, b, c, note in out:
        if not label.startswith("link loss"):
            continue
        f = lambda v: "   --  " if not np.isfinite(v) else f"{v:>9.3f}"
        print(f"  {label:<26} {n:>6} {f(a)} {f(b)} {f(c)}  {note}")

    if not args.quick:
        print("\n  IRREDUCIBLE UNCERTAINTY — same world, "
              f"{args.seeds} redrawn weather futures")
        ceil = ceiling_study(presets["india4-storm"], branch_times=[30.0, 60.0, 90.0],
                             horizons=[30.0, 60.0, 120.0, 180.0], n_seeds=args.seeds)
        print(f"  {'target':<20} {'horizon':>8} {'mean':>14} {'across-seed sd':>16} {'cv':>8}")
        print(f"  {'-'*20} {'-'*8} {'-'*14} {'-'*16} {'-'*8}")
        for (tgt, h), vals in sorted(ceil.items()):
            mean = float(np.mean([m for m, _ in vals]))
            sd = float(np.mean([s for _, s in vals]))
            cv = sd / mean if mean > 0 else 0.0
            unit = 1e9
            print(f"  {tgt:<20} {int(h):>7}s {mean/unit:>12.3f}G {sd/unit:>14.4f}G {cv:>8.4f}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
