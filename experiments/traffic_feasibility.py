"""V2 workstream A — does a traffic arrival process create real ML headroom?

    python experiments/traffic_feasibility.py
    python experiments/traffic_feasibility.py --runs 10

Stage 2 (§15.10) killed every candidate target because the twin had no arrival
process: demand was handed out at t=0 and only drained. Workstream A adds one.
But adding *variance* is not the same as adding *predictability* — station
failures already prove that, being memoryless and therefore unlearnable.

So this runs the Stage 2 gate against three worlds that differ only in their
arrival process:

    none       V1 — no arrivals at all            (regression check: must be bit-identical)
    poisson    memoryless arrivals                (CONTROL — expect NO headroom)
    bursty     Markov ON/OFF arrivals with dwell  (expect headroom)

The control is the point. "We added traffic and got headroom" is a claim about
the model; "structured traffic gave headroom while unstructured traffic gave
none" is a claim about the *process*, and only the second one is evidence.

Part 1 validates the processes themselves (mean rate, memory, latent state).
Part 2 re-runs the gate on the demand / queue / throughput targets.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios.allocators import make_allocator, make_freq_allocator, make_power_allocator
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler
from xnios.simulator import Simulator
from xnios.telemetry import TelemetryRecorder, MemorySink
from xnios.traffic import make_traffic

from api.presets import all_presets

try:
    from sklearn.ensemble import HistGradientBoostingRegressor
    HAVE_SK = True
except ImportError:                                       # pragma: no cover
    HAVE_SK = False

HORIZONS = [60.0, 180.0, 300.0]
LAGS = [1, 2, 3, 6, 12]                     # steps of arrival history given to the model

TRAFFIC = {
    "none": None,
    "poisson": {"model": "poisson", "mean_gbit_per_hour": 20.0, "chunk_gbit": 0.5},
    "bursty": {"model": "bursty", "mean_gbit_per_hour": 20.0, "chunk_gbit": 0.04,
               "burst_ratio": 6.0, "on_dwell_s": 240.0, "off_dwell_s": 600.0},
}


def build_cfg(preset: str, variant: str, seed: int, vary_world: bool = True) -> dict:
    """One scenario. `seed` alone does NOT change the constellation — the India
    presets specify satellites explicitly, so every seed shares the same orbits
    and therefore the same deterministic visibility backbone.

    That makes a run-level split useless: the model memorises the backbone from
    the training runs and meets it again, unchanged, in the held-out ones. So
    each run also gets its own constellation phasing and backlog draw, and the
    split then separates *worlds* rather than just traffic realisations.
    """
    cfg = copy.deepcopy(all_presets()[preset])
    cfg["seed"] = seed
    t = TRAFFIC[variant]
    if t is not None:
        cfg["traffic"] = {**t, "seed": seed}
    else:
        cfg.pop("traffic", None)

    if vary_world:
        import random as _r
        rng = _r.Random(f"world-{seed}")
        sats = cfg.get("satellites", {})
        for s in sats.get("list", []):
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


# --------------------------------------------------------------- part 1: process

def part1_process_checks(preset, out):
    """The arrival processes must do what they claim, before anything is inferred."""
    # --- regression: traffic off must reproduce V1 exactly
    a = run(build_cfg(preset, "none", 0, vary_world=False))[3].summary
    b = run(build_cfg(preset, "none", 0, vary_world=False))[3].summary
    keys = [k for k in a if isinstance(a[k], (int, float))
            and "ms" not in k]                                   # wall-clock varies
    same = all(abs(a[k] - b[k]) < 1e-12 for k in keys)
    out.append(("traffic=none is deterministic", "identical KPIs",
                "identical" if same else "DIFFER", same))

    # --- mean rate: long-run arrivals should match the configured rate
    for variant in ("poisson", "bursty"):
        cfg = build_cfg(preset, variant, 0)
        scn, simcfg, recs, _ = run(cfg)
        # arrivals = Dqueue + delivered  (conservation over the whole run)
        n0, n1 = recs[0].network, recs[-1].network
        arrived = (n1.queue_bits - n0.queue_bits) + n1.bits_delivered_total
        hours = simcfg.duration_s / 3600.0
        want = TRAFFIC[variant]["mean_gbit_per_hour"] * 1e9 * len(scn.satellites) * hours
        err = abs(arrived - want) / want
        out.append((f"{variant}: long-run mean rate", "within 25% of configured",
                    f"{arrived/1e9:.0f} Gb vs {want/1e9:.0f} Gb ({100*err:+.0f}%)",
                    err < 0.25))

    # --- memory: the discriminating property
    for variant, expect_memory in (("poisson", False), ("bursty", True)):
        series = []
        for seed in range(4):
            scn, simcfg, recs, _ = run(build_cfg(preset, variant, seed))
            q = np.array([r.network.queue_bits for r in recs])
            d = np.array([r.network.bits_delivered_total for r in recs])
            arr = np.diff(q) + np.diff(d)                        # arrivals per step
            if arr.std() > 0:
                a = arr - arr.mean()
                ac = float(np.correlate(a, a, "full")[len(a):][:6] @ np.ones(6) / (6 * (a @ a)))
                series.append(ac)
        lag_ac = float(np.mean(series)) if series else float("nan")
        ok = (lag_ac > 0.10) if expect_memory else (abs(lag_ac) < 0.10)
        out.append((f"{variant}: arrival autocorrelation",
                    "> 0.10 (has memory)" if expect_memory else "|r| < 0.10 (memoryless)",
                    f"{lag_ac:+.3f}", ok))

    # --- the latent burst state must not leak into telemetry
    scn, simcfg, recs, _ = run(build_cfg(preset, "bursty", 0))
    leaked = any("burst" in f.lower() or "traffic" in f.lower()
                 for f in vars(recs[0].satellites[0]))
    out.append(("bursty: latent state hidden", "absent from telemetry",
                "leaked" if leaked else "absent", not leaked))


# ------------------------------------------------------------------ part 2: gate

def samples(recs, run_id, cap_info=None):
    """Network rows with arrival history, and the demand / queue / throughput targets."""
    q = np.array([r.network.queue_bits for r in recs])
    d = np.array([r.network.bits_delivered_total for r in recs])
    arr = np.concatenate([[0.0], np.diff(q) + np.diff(d)])       # arrivals in each step
    ts = np.array([r.t for r in recs])
    dt = ts[1] - ts[0] if len(ts) > 1 else 10.0
    rows = []
    for i, r in enumerate(recs):
        n = r.network
        row = {"run": run_id, "t": r.t,
               "f_queue": n.queue_bits, "f_thr": n.throughput_bps,
               "f_util": n.beam_utilization, "f_cov": n.coverage,
               "f_pairs": n.n_visible_pairs, "f_backlogged": n.n_backlogged,
               "f_wait": n.n_waiting, "f_arr_now": arr[i]}
        # arrival history — the only way a memory-bearing process is observable
        for L in LAGS:
            j = max(0, i - L + 1)
            row[f"f_arr_mean{L}"] = float(arr[j:i + 1].mean())
        # physics-consistent deliverable-bits forecast (Stage 1 contact windows)
        if cap_info is not None:
            grid, cap, gdt = cap_info
            j = int(np.argmin(np.abs(grid - r.t)))
            cap_now = float(cap[j])
            row["f_cap_now"] = cap_now
            # observed delivery efficiency: the network never achieves the raw
            # capacity bound (the scheduler does not saturate every beam), so
            # scale the forecast by the efficiency visible right now. A ratio
            # estimator from the current row — no constant fitted on training data.
            eff = (n.throughput_bps / cap_now) if cap_now > 0 else 0.0
            row["f_eff_now"] = eff
            for h in HORIZONS:
                dl = deliverable_bits(grid, cap, gdt, r.t, h)
                row[f"f_deliverable{int(h)}"] = dl
                row[f"f_deliv_eff{int(h)}"] = dl * min(1.0, max(0.0, eff))
        for h in HORIZONS:
            k = i + int(h / dt)
            if k >= len(recs):
                row[f"y_arr{int(h)}"] = row[f"y_queue{int(h)}"] = np.nan
                row[f"y_thr{int(h)}"] = np.nan
            else:
                row[f"y_arr{int(h)}"] = float(arr[i + 1:k + 1].sum())
                row[f"y_queue{int(h)}"] = recs[k].network.queue_bits
                row[f"y_thr{int(h)}"] = recs[k].network.throughput_bps
        rows.append(row)
    return rows


def sat_samples(recs, run_id):
    """Per-satellite rows.

    Network-level demand sums 20 independent ON/OFF chains, and the law of large
    numbers flattens exactly the burst structure the process was added to create.
    A single satellite's arrivals stay coherent, so this is where the memory is
    visible — and it is also the level at which a scheduler would use a demand
    forecast.
    """
    ids = [s.sat_id for s in recs[0].satellites]
    ts = np.array([r.t for r in recs])
    dt = ts[1] - ts[0] if len(ts) > 1 else 10.0
    hist = {sid: [] for sid in ids}
    for r in recs:
        by = {s.sat_id: s for s in r.satellites}
        for sid in ids:
            s = by.get(sid)
            hist[sid].append((s.backlog_bits, s.delivered_bits) if s else (0.0, 0.0))

    rows = []
    for sid in ids:
        bk = np.array([h[0] for h in hist[sid]], float)
        dl = np.array([h[1] for h in hist[sid]], float)
        arr = np.concatenate([[0.0], np.diff(bk) + np.diff(dl)])
        for i in range(len(recs)):
            row = {"run": run_id, "sat": sid, "t": recs[i].t,
                   "f_backlog": bk[i], "f_delivered": dl[i], "f_arr_now": arr[i]}
            for L in LAGS:
                j = max(0, i - L + 1)
                row[f"f_arr_mean{L}"] = float(arr[j:i + 1].mean())
                row[f"f_arr_max{L}"] = float(arr[j:i + 1].max())
            # "how long since data last arrived" — the most direct observable of
            # an ON/OFF state, and meaningless for a memoryless process
            gap = 0
            for k in range(i, -1, -1):
                if arr[k] > 0:
                    break
                gap += 1
            row["f_gap"] = float(gap)
            for h in HORIZONS:
                k = i + int(h / dt)
                row[f"y_arr{int(h)}"] = (float(arr[i + 1:k + 1].sum())
                                         if k < len(recs) else np.nan)
            rows.append(row)
    return rows


def capacity_series(scn, recs):
    """Deliverable bits/s at each record time, from the Stage 1 forecaster.

    The naive queue baseline multiplies the *instantaneous* throughput by the
    horizon, which quietly assumes the current contact lasts forever. With a
    ~5-minute pass and a 180 s horizon that is wrong by most of the horizon, and
    the error inflates apparent ML headroom exactly the way the SNR baseline did.

    This integrates the real future instead: at each instant, the top-N visible
    links by achievable rate (N = beams available), from the actual contact
    windows. Precomputed on the record grid so the lookup is O(1).
    """
    from experiments.feasibility_study import Analytics
    horizon = recs[-1].t + max(HORIZONS) + 60.0
    an = Analytics(scn, horizon)
    beams = recs[0].network.beams_total
    ts = np.array([r.t for r in recs], float)
    dt = ts[1] - ts[0] if len(ts) > 1 else 10.0
    grid = np.arange(ts[0], ts[-1] + max(HORIZONS) + dt, dt)
    cap = np.array([an.capacity_bps_at(float(t), beams) for t in grid], float)
    return grid, cap, dt


def deliverable_bits(grid, cap, dt, t, h):
    """Bits the network could deliver in (t, t+h] given the real contact windows."""
    m = (grid > t) & (grid <= t + h)
    return float(cap[m].sum() * dt)


def _r2(y, p):
    y, p = np.asarray(y, float), np.asarray(p, float)
    m = np.isfinite(y) & np.isfinite(p)
    if m.sum() < 5:
        return float("nan")
    y, p = y[m], p[m]
    sst = float(np.sum((y - y.mean()) ** 2))
    return float("nan") if sst <= 0 else 1.0 - float(np.sum((y - p) ** 2)) / sst


def gate(rows, target, persist_fn, analytic_fn, label, out):
    y = np.array([r.get(target, np.nan) for r in rows], float)
    runs = np.array([r["run"] for r in rows])
    keys = sorted({k for r in rows for k in r if k.startswith("f_")})
    X = np.array([[r.get(k, np.nan) for k in keys] for r in rows], float)
    per = np.array([persist_fn(r) for r in rows], float)
    ana = np.array([analytic_fn(r) for r in rows], float)

    ok = np.isfinite(y) & np.isfinite(per) & np.isfinite(ana)
    y, X, runs, per, ana = y[ok], X[ok], runs[ok], per[ok], ana[ok]
    uniq = sorted(set(runs.tolist()))
    te_runs = set(uniq[::3])
    te = np.array([r in te_runs for r in runs])
    tr = ~te
    if tr.sum() < 40 or te.sum() < 20:
        out.append((label, 0, *[float("nan")] * 3, "split too small"))
        return
    r2p, r2a = _r2(y[te], per[te]), _r2(y[te], ana[te])
    r2m = float("nan")
    if HAVE_SK:
        m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.08, random_state=0)
        m.fit(X[tr], y[tr])
        r2m = _r2(y[te], m.predict(X[te]))
    base = np.nanmax([r2p, r2a])
    head = r2m - base if np.isfinite(r2m) and np.isfinite(base) else float("nan")
    out.append((label, int(te.sum()), r2p, r2a, r2m, f"{head:+.3f}"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preset", default="india4-nominal")
    ap.add_argument("--runs", type=int, default=9)
    args = ap.parse_args()

    print(f"\n  preset {args.preset} · {args.runs} seeds per variant\n")

    checks = []
    part1_process_checks(args.preset, checks)
    w = max(len(c[0]) for c in checks)
    w1 = max(len(str(c[1])) for c in checks)
    print("  PART 1 — do the arrival processes behave as specified?")
    print(f"  {'check'.ljust(w)}  {'expected'.ljust(w1)}  actual")
    print(f"  {'-'*w}  {'-'*w1}  ------")
    for name, exp, act, ok in checks:
        print(f"  {name.ljust(w)}  {str(exp).ljust(w1)}  {act}   [{'PASS' if ok else 'FAIL'}]")
    failed = sum(1 for c in checks if not c[3])
    print(f"\n  {len(checks)-failed}/{len(checks)} passed\n")

    print("  PART 2 — the Stage 2 gate, re-run per arrival process")
    for variant in ("poisson", "bursty"):
        rows, srows = [], []
        for seed in range(args.runs):
            scn, simcfg, recs, _ = run(build_cfg(args.preset, variant, seed))
            cap_info = capacity_series(scn, recs)
            rows += samples(recs, f"{variant}#{seed}", cap_info)
            srows += sat_samples(recs, f"{variant}#{seed}")
        out = []
        for h in HORIZONS:
            gate(srows, f"y_arr{int(h)}",
                 lambda r, h=h: r["f_arr_mean3"] * (h / 10.0),
                 lambda r, h=h: r["f_arr_mean12"] * (h / 10.0),
                 f"PER-SAT demand +{int(h)}s", out)
        for h in HORIZONS:
            gate(rows, f"y_arr{int(h)}",
                 lambda r, h=h: r["f_arr_mean3"] * (h / 10.0),
                 lambda r, h=h: r["f_arr_mean12"] * (h / 10.0),
                 f"network demand +{int(h)}s", out)
        # queue, judged against BOTH baselines so the difference is visible:
        #   naive   = current throughput held for the whole horizon
        #   physics = deliverable bits from the real future contact windows
        for h in HORIZONS:
            gate(rows, f"y_queue{int(h)}", lambda r: r["f_queue"],
                 lambda r, h=h: r["f_queue"] + r["f_arr_mean12"] * (h / 10.0)
                 - r["f_thr"] * h, f"queue +{int(h)}s [naive]", out)
        for h in HORIZONS:
            gate(rows, f"y_queue{int(h)}", lambda r: r["f_queue"],
                 lambda r, h=h: max(0.0, r["f_queue"] + r["f_arr_mean12"] * (h / 10.0)
                                    - r.get(f"f_deliverable{int(h)}", 0.0)),
                 f"queue +{int(h)}s [physics]", out)
        for h in HORIZONS:
            gate(rows, f"y_queue{int(h)}", lambda r: r["f_queue"],
                 lambda r, h=h: max(0.0, r["f_queue"] + r["f_arr_mean12"] * (h / 10.0)
                                    - r.get(f"f_deliv_eff{int(h)}", 0.0)),
                 f"queue +{int(h)}s [phys*eff]", out)
        print(f"\n  --- traffic = {variant} "
              f"({'CONTROL: expect no headroom' if variant == 'poisson' else 'has memory'}) ---")
        print(f"  {'target':<22} {'n':>6} {'persist':>9} {'analytic':>9} {'learned':>9}  headroom")
        print(f"  {'-'*22} {'-'*6} {'-'*9} {'-'*9} {'-'*9}  --------")
        for label, n, a, b, c, note in out:
            f = lambda v: "   --  " if not np.isfinite(v) else f"{v:>9.3f}"
            print(f"  {label:<22} {n:>6} {f(a)} {f(b)} {f(c)}  {note}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
