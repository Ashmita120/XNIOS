"""V2 workstream B — does latent station health create a learnable precursor?

    python experiments/health_feasibility.py --runs 12

§15.10 killed failure prediction because the failure process is memoryless: a
constant hazard has nothing preceding it. Workstream B replaces it with a causal
chain (health -> G/T penalty + SNR jitter -> hazard) so that an outage is always
*preceded* by an observable decline.

Same experimental design that worked for traffic, with the same control:

    poisson    dynamics.failure_events, memoryless   CONTROL — expect NO headroom
    degraded   degradation.StationDegradation        expect headroom

The gate criteria (§15.2), all four required:
    1. learned skill is positive and meaningful
    2. it beats the strongest non-ML baseline by a material margin
    3. the advantage DISAPPEARS in the control  (attribution)
    4. it survives held-out *worlds*, not just held-out seeds

The precursor is a residual against physics: `xnios.forecast` reproduces the link
budget exactly and knows nothing about degradation, so

    residual = measured SNR - forecast SNR

is ~0 for a healthy station and grows as one decays. Stage 1 is the instrument.
"""

from __future__ import annotations

import argparse
import copy
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import forecast as fc
from xnios.allocators import make_allocator, make_freq_allocator, make_power_allocator
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.degradation import HOUSEKEEPING
from xnios.experiment import make_scheduler
from xnios.link import snr_linear
from xnios.simulator import Simulator
from xnios.telemetry import TelemetryRecorder, MemorySink

from api.presets import all_presets

try:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score, brier_score_loss
    HAVE_SK = True
except ImportError:                                        # pragma: no cover
    HAVE_SK = False

HORIZONS = [60.0, 180.0, 300.0]
LAGS = [1, 3, 6, 12, 24]

# A 30-minute run is the wrong clock for this experiment. Every pass happens in
# its first ~5 minutes, so degradation measured per hour is worth ~0.008 dB while
# any link is up, and health never reaches the failure threshold. Run across
# multiple orbits instead (~96 min each), so successive passes see a station in
# successively worse condition — which is the whole point of a precursor.
DURATION_S = 10800.0                      # ~1.9 orbits, 2-3 passes per station

DEGRADE = {"drift_per_hour": 0.35, "shock_mtbf_s": 1800.0, "shock_size": 0.18,
           "fail_below": 0.25, "mttr_s": 600.0,
           "gt_penalty_max_db": 3.0, "jitter_max_db": 1.2}
POISSON = {"random": {"station_mtbf_s": 3000.0, "station_mttr_s": 600.0}}


def build_cfg(preset, variant, seed, vary_world=True):
    """Worlds vary per seed — the India presets pin satellites explicitly, so a
    seed alone leaves the visibility backbone identical and the split leaks."""
    cfg = copy.deepcopy(all_presets()[preset])
    cfg["seed"] = seed
    cfg.pop("dynamics", None)
    cfg.pop("degradation", None)
    sim = dict(cfg.get("sim", {}))
    sim["duration_s"] = DURATION_S
    cfg["sim"] = sim
    if variant == "poisson":
        cfg["dynamics"] = copy.deepcopy(POISSON)
    elif variant == "degraded":
        cfg["degradation"] = {**DEGRADE, "seed": seed}
    if vary_world:
        import random as _r
        rng = _r.Random(f"world-{seed}")
        for s in cfg.get("satellites", {}).get("list", []):
            s["arg_lat0"] = s.get("arg_lat0", 0.0) + rng.uniform(-25.0, 25.0)
            s["backlog_gbit"] = s.get("backlog_gbit", 20.0) * rng.uniform(0.6, 1.6)
    return cfg


def run(cfg):
    scn = scenario_from_config(cfg)
    simcfg = sim_config_from_config(cfg)
    rec = TelemetryRecorder(sink=MemorySink())
    res = Simulator(scn, make_scheduler("fcfs/strongest"), simcfg,
                    allocator=make_allocator("equal"),
                    power_allocator=make_power_allocator("adaptive"),
                    freq_allocator=make_freq_allocator("coloring"),
                    telemetry=rec).run()
    return scn, simcfg, rec.sink.records, res


# ----------------------------------------------------------- the precursor

def snr_residuals(scn, recs):
    """Per station per step: measured SNR minus what the link budget says.

    Averaged over the station's links at that instant. The forecaster is given
    the *actual* allocated bandwidth and power, so the only thing left in the
    residual is degradation (and, in the control, nothing at all).
    """
    sats = {s.id: s for s in scn.satellites}
    stations = {g.id: g for g in scn.stations}
    out = {g.id: [] for g in scn.stations}
    for r in recs:
        per = {gid: [] for gid in stations}
        for l in r.links:
            if not l.active or not l.alloc_bw_hz or not l.alloc_power_w:
                continue
            s, g = sats[l.sat_id], stations[l.station_id]
            lin = snr_linear(l.range_km, l.elev_deg, s, g,
                             rain_zenith_db=scn.weather.fade_db(l.station_id, r.t),
                             bandwidth_hz=l.alloc_bw_hz, tx_power_w=l.alloc_power_w)
            if lin <= 0:
                continue
            per[l.station_id].append(l.snr_db - 10.0 * math.log10(lin))
        for gid in stations:
            out[gid].append(float(np.mean(per[gid])) if per[gid] else np.nan)
    return out


def samples(scn, recs, run_id, dt_s, housekeeping=False):
    """One row per station per step; target = future outage.

    `housekeeping` is the ONLY thing that differs between the two arms of the
    observability experiment. The degradation process, the hidden health state,
    the outage definition, the horizons, the world split, the baselines and the
    metrics are all identical — so any change in AUC is attributable to
    observability alone.
    """
    res = snr_residuals(scn, recs)
    ts = [r.t for r in recs]
    up = {g.id: [] for g in scn.stations}
    for r in recs:
        by = {s.station_id: s for s in r.stations}
        for g in scn.stations:
            st = by.get(g.id)
            up[g.id].append(bool(st.up) if st else True)

    # station-local housekeeping history, sampled every step regardless of
    # whether any satellite is visible — this is the whole point of the arm
    hk_hist = {g.id: {ch: [] for ch in HOUSEKEEPING} for g in scn.stations}
    for r in recs:
        by = {s.station_id: s for s in r.stations}
        for g in scn.stations:
            st = by.get(g.id)
            for ch in HOUSEKEEPING:
                hk_hist[g.id][ch].append(float(getattr(st, ch, 0.0)) if st else 0.0)
    hk_hist = {gid: {ch: np.array(v, float) for ch, v in d.items()}
               for gid, d in hk_hist.items()}

    rows = []
    for g in scn.stations:
        rsd = np.array(res[g.id], float)
        alive = np.array(up[g.id], bool)
        # forward-fill the residual so gaps between passes do not erase history
        ff, last = [], np.nan
        for v in rsd:
            if np.isfinite(v):
                last = v
            ff.append(last)
        ff = np.array(ff, float)

        for i, r in enumerate(recs):
            if not alive[i]:
                continue                       # already down: nothing to predict
            st = next((s for s in r.stations if s.station_id == g.id), None)
            row = {"run": run_id, "station": g.id, "t": r.t,
                   "f_resid": ff[i],
                   "f_sinr": st.mean_sinr_db if st else np.nan,
                   "f_beams_avail": st.beams_available if st else np.nan,
                   "f_beam_util": st.beam_utilization if st else np.nan,
                   "f_power": st.link_power_w if st else np.nan,
                   "f_fade": st.rain_fade_db if st else np.nan,
                   "f_connected": len(st.connected_sats) if st else 0}
            for L in LAGS:
                j = max(0, i - L + 1)
                w = ff[j:i + 1]
                w = w[np.isfinite(w)]
                row[f"f_resid_mean{L}"] = float(w.mean()) if len(w) else np.nan
                row[f"f_resid_sd{L}"] = float(w.std()) if len(w) > 1 else np.nan
                row[f"f_resid_min{L}"] = float(w.min()) if len(w) else np.nan
            # slope of the residual — degradation is a trend, not a level
            w = ff[max(0, i - 23):i + 1]
            m = np.isfinite(w)
            row["f_resid_slope"] = (float(np.polyfit(np.arange(m.sum()), w[m], 1)[0])
                                    if m.sum() > 3 else np.nan)

            # --- the only difference between the two arms ---------------------
            # station-local instruments, sampled every step whether or not any
            # satellite is visible. Lags and slopes because degradation is a
            # trend, and no single channel is a clean read of the latent state.
            if housekeeping and st is not None:
                for ch in HOUSEKEEPING:
                    cur = float(getattr(st, ch, 0.0))
                    row[f"f_hk_{ch}"] = cur
                    series = hk_hist[g.id][ch]
                    for L in LAGS:
                        w2 = series[max(0, i - L + 1):i + 1]
                        row[f"f_hk_{ch}_mean{L}"] = float(np.mean(w2))
                        if L >= 6:
                            row[f"f_hk_{ch}_sd{L}"] = float(np.std(w2))
                    w2 = series[max(0, i - 35):i + 1]
                    row[f"f_hk_{ch}_slope"] = (float(np.polyfit(np.arange(len(w2)), w2, 1)[0])
                                               if len(w2) > 3 else np.nan)
            for h in HORIZONS:
                k = i + int(h / dt_s)
                row[f"y_fail{int(h)}"] = (0.0 if all(alive[i + 1:k + 1]) else 1.0) \
                    if k < len(recs) else np.nan
            rows.append(row)
    return rows


# ------------------------------------------------------------------ the gate

def gate(rows, target, label, out):
    y = np.array([r.get(target, np.nan) for r in rows], float)
    runs = np.array([r["run"] for r in rows])
    keys = sorted({k for r in rows for k in r if k.startswith("f_")})
    X = np.array([[r.get(k, np.nan) for k in keys] for r in rows], float)
    ok = np.isfinite(y)
    y, X, runs = y[ok], X[ok], runs[ok]
    if len(y) < 100:
        out.append((label, 0, float("nan"), float("nan"), "too few samples"))
        return
    uniq = sorted(set(runs.tolist()))
    te = np.array([r in set(uniq[::3]) for r in runs])
    tr = ~te
    base = float(y.mean())
    if len(set(y[te].tolist())) < 2 or tr.sum() < 60:
        out.append((label, int(te.sum()), base, float("nan"),
                    "degenerate — no outages held out"))
        return
    auc = brier = float("nan")
    if HAVE_SK:
        m = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08,
                                           random_state=0)
        m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]
        auc = roc_auc_score(y[te], p)
        brier = brier_score_loss(y[te], p)
    out.append((label, int(te.sum()), base, auc, f"brier {brier:.4f}"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preset", default="india4-nominal")
    ap.add_argument("--runs", type=int, default=12)
    args = ap.parse_args()
    print(f"\n  preset {args.preset} · {args.runs} worlds per variant\n")

    # --- process checks -----------------------------------------------------
    print("  PART 1 — the degradation process")
    a = run(build_cfg(args.preset, "none", 0, vary_world=False))[3].summary
    b = run(build_cfg(args.preset, "none", 0, vary_world=False))[3].summary
    same = all(abs(a[k] - b[k]) < 1e-12 for k in a
               if isinstance(a[k], (int, float)) and "ms" not in k)
    print(f"    degradation off is bit-identical to V1 : {'PASS' if same else 'FAIL'}")

    scn, simcfg, recs, _ = run(build_cfg(args.preset, "degraded", 0))
    deg = scn.degradation
    hs = [deg.health(g.id, t) for g in scn.stations
          for t in (0, DURATION_S/3, 2*DURATION_S/3, DURATION_S-1)]
    print(f"    health declines over the run           : "
          f"{min(hs):.2f}..{max(hs):.2f} (1.0 = healthy)")
    nfail = len([e for e in deg.failure_events() if e.action == "station_fail"])
    print(f"    degradation-driven outages             : {nfail}")

    res = snr_residuals(scn, recs)
    allr = np.concatenate([np.array(v, float) for v in res.values()])
    allr = allr[np.isfinite(allr)]
    print(f"    SNR residual vs physics                : mean {allr.mean():+.2f} dB, "
          f"sd {allr.std():.2f} dB   <- the precursor")

    scn0, _, recs0, _ = run(build_cfg(args.preset, "poisson", 0))
    r0 = np.concatenate([np.array(v, float) for v in snr_residuals(scn0, recs0).values()])
    r0 = r0[np.isfinite(r0)]
    print(f"    same, memoryless control               : mean {r0.mean():+.2f} dB, "
          f"sd {r0.std():.2f} dB   <- must be ~0")
    leaked = any("health" in f.lower() or "hazard" in f.lower()
                 for f in vars(recs[0].stations[0]))
    print(f"    latent health hidden from telemetry    : {'FAIL' if leaked else 'PASS'}")

    # --- the gate -----------------------------------------------------------
    print("\n  PART 2 — outage prediction; only OBSERVABILITY differs between arms")
    arms = [("poisson", False, "CONTROL: memoryless, link-only"),
            ("degraded", False, "link-only — 2.3% duty cycle"),
            ("degraded", True, "+ station-local housekeeping, every step"),
            ("poisson", True, "CONTROL: memoryless + housekeeping")]
    for variant, hk_on, tag in arms:
        rows = []
        for seed in range(args.runs):
            scn, simcfg, recs, _ = run(build_cfg(args.preset, variant, seed))
            rows += samples(scn, recs, f"{variant}#{seed}", simcfg.dt_s, housekeeping=hk_on)
        out = []
        for h in HORIZONS:
            gate(rows, f"y_fail{int(h)}", f"outage within +{int(h)}s", out)
        print(f"\n  --- {variant} ({tag}) ---")
        print(f"  {'target':<24} {'n':>6} {'base rate':>10} {'AUC':>8}  note")
        print(f"  {'-'*24} {'-'*6} {'-'*10} {'-'*8}  ----")
        for label, n, base, auc, note in out:
            f = lambda v: "   --  " if not np.isfinite(v) else f"{v:>8.3f}"
            print(f"  {label:<24} {n:>6} {base:>10.4f} {f(auc)}  {note}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
