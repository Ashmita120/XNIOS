"""V2 Stage 1 validation — does the analytical forecaster match the twin exactly?

    python experiments/forecast_validation.py
    python experiments/forecast_validation.py --preset india4-congested --duration 3600

The forecaster is not a model, so "accuracy" is the wrong frame. It re-derives the
same geometry the simulator uses, which means **any disagreement is a bug**. Each
test below therefore declares a hard threshold up front and passes or fails against
it — no scores to interpret.

  T1  vectorised geometry == the scalar reference in orbit.py     (float precision)
  T2  bisected window edges == a brute-force dense scan           (root finding)
  T3  predicted windows == visibility actually observed in a run  (end to end)
  T4  predicted time-to-LOS == the LOS that actually happened     (handover trigger)
  T5  cost of forecasting vs cost of simulating forward           (the speed claim)

T4 is the operational one: proactive handover is only as good as this number.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import forecast as fc
from xnios import orbit as orb
from xnios.allocators import make_allocator, make_freq_allocator, make_power_allocator
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler
from xnios.simulator import Simulator
from xnios.telemetry import TelemetryRecorder, MemorySink

from api.presets import all_presets


# --------------------------------------------------------------------- helpers

class Result:
    def __init__(self):
        self.rows = []
        self.failed = 0

    def check(self, name: str, expected: str, actual: str, ok: bool):
        self.rows.append((name, expected, actual, ok))
        if not ok:
            self.failed += 1

    def report(self) -> int:
        w0 = max(len(r[0]) for r in self.rows)
        w1 = max(len(r[1]) for r in self.rows)
        w2 = max(len(r[2]) for r in self.rows)
        print()
        print(f"  {'CHECK'.ljust(w0)}  {'EXPECTED'.ljust(w1)}  {'ACTUAL'.ljust(w2)}  ")
        print(f"  {'-' * w0}  {'-' * w1}  {'-' * w2}  ----")
        for name, exp, act, ok in self.rows:
            print(f"  {name.ljust(w0)}  {exp.ljust(w1)}  {act.ljust(w2)}  "
                  f"{'PASS' if ok else 'FAIL'}")
        print()
        print(f"  {len(self.rows) - self.failed}/{len(self.rows)} passed")
        return 1 if self.failed else 0


def _with_sim_overrides(cfg, duration_s, dt_s) -> dict:
    sim_cfg = dict(cfg.get("sim", {}))
    if duration_s is not None:
        sim_cfg["duration_s"] = duration_s
    if dt_s is not None:
        sim_cfg["dt_s"] = dt_s
        sim_cfg["decision_interval_s"] = dt_s
    return {**cfg, "sim": sim_cfg}


def _run_sim(cfg, duration_s=None, dt_s=None, telemetry=True):
    """Run the twin (optionally with telemetry on); return (scn, simcfg, records)."""
    cfg = _with_sim_overrides(cfg, duration_s, dt_s)
    scn = scenario_from_config(cfg)
    simcfg = sim_config_from_config(cfg)
    rec = TelemetryRecorder(sink=MemorySink()) if telemetry else None
    sim = Simulator(scn, make_scheduler("fcfs/strongest"), simcfg,
                    allocator=make_allocator("equal"),
                    power_allocator=make_power_allocator("adaptive"),
                    freq_allocator=make_freq_allocator("coloring"),
                    telemetry=rec)
    sim.run()
    return scn, simcfg, (rec.sink.records if rec else [])


# ------------------------------------------------------------------------ T1

def t1_geometry_precision(scn, res: Result, n_samples=4000, seed=0):
    """The vectorised propagator must reproduce orbit.py to float precision."""
    rng = np.random.default_rng(seed)
    max_elev_err = 0.0
    max_rng_err = 0.0
    sats, stations = scn.satellites, scn.stations
    for _ in range(n_samples // 20):
        s = sats[rng.integers(len(sats))]
        g = stations[rng.integers(len(stations))]
        ts = rng.uniform(0.0, 6000.0, size=20)
        elev_v, rng_v = fc.elevation_series(s, g, ts)
        gs = orb.gs_position_ecef(g.lat_deg, g.lon_deg, g.alt_km)
        for i, t in enumerate(ts):
            p = orb.sat_position_ecef(s.orbit, float(t))
            e_ref, _az, r_ref = orb.elevation_azimuth_range(gs, g.lat_deg, g.lon_deg, p)
            max_elev_err = max(max_elev_err, abs(elev_v[i] - e_ref))
            max_rng_err = max(max_rng_err, abs(rng_v[i] - r_ref))

    res.check("T1 elevation vs orbit.py", "< 1e-9 deg", f"{max_elev_err:.2e} deg",
              max_elev_err < 1e-9)
    res.check("T1 range vs orbit.py", "< 1e-9 km", f"{max_rng_err:.2e} km",
              max_rng_err < 1e-9)


# ------------------------------------------------------------------------ T2

def t2_root_finding(scn, res: Result, horizon_s=3600.0, dense_step=0.25):
    """Bisected edges must match a brute-force dense scan of the same predicate."""
    sats, stations = scn.satellites, scn.stations
    worst = 0.0
    n_windows = 0
    n_pairs = 0
    for s in sats[:6]:
        for g in stations:
            n_pairs += 1
            windows = fc.contact_windows(s, g, 0.0, horizon_s, step_s=10.0)
            if not windows:
                continue
            grid = np.arange(0.0, horizon_s + dense_step, dense_step)
            elev, rngs = fc.elevation_series(s, g, grid)
            ok = fc._usable_mask(s, g, elev, rngs, 0.0)
            d = np.diff(ok.astype(np.int8))
            rises = grid[np.flatnonzero(d == 1) + 1]
            sets = grid[np.flatnonzero(d == -1)]
            for w in windows:
                n_windows += 1
                if not w.open_start and len(rises):
                    worst = max(worst, float(np.min(np.abs(rises - w.t_rise))))
                if not w.open_end and len(sets):
                    worst = max(worst, float(np.min(np.abs(sets - w.t_set))))

    res.check("T2 window edges vs dense scan",
              f"<= {dense_step:.2f} s (scan resolution)",
              f"{worst:.3f} s over {n_windows} windows / {n_pairs} pairs",
              worst <= dense_step + 1e-6)


# ------------------------------------------------------------------------ T3

def t3_vs_telemetry(scn, records, res: Result, dt_s: float):
    """Windows predicted at t=0 must match the visibility the run actually saw.

    Compared only while a satellite still has backlog: `Simulator._visibility`
    skips drained satellites, so a geometric window with no data left to send
    correctly produces no link row and is not a disagreement.
    """
    sats = {s.id: s for s in scn.satellites}
    stations = {g.id: g for g in scn.stations}
    horizon = records[-1].t + dt_s

    # observed: (sat, station) -> set of step times where the pair was visible
    observed = {}
    backlog_at = {}
    for r in records:
        for s in r.satellites:
            backlog_at.setdefault(s.sat_id, {})[r.t] = s.backlog_bits
        for l in r.links:
            observed.setdefault((l.sat_id, l.station_id), set()).add(r.t)

    # predicted: windows from the forecaster, using each station's actual weather
    predicted = {}
    for sid, s in sats.items():
        for gid, g in stations.items():
            rain = scn.weather.fade_db(gid, 0.0)
            predicted[(sid, gid)] = fc.contact_windows(s, g, 0.0, horizon,
                                                       step_s=10.0,
                                                       rain_zenith_db=rain)

    fp = fn = tp = 0
    checked = 0
    for (sid, gid), windows in predicted.items():
        for r_t in [r.t for r in records]:
            has_backlog = backlog_at.get(sid, {}).get(r_t, 0.0) > 0
            if not has_backlog:
                continue
            checked += 1
            pred = any(w.contains(r_t) for w in windows)
            obs = r_t in observed.get((sid, gid), ())
            if pred and obs:
                tp += 1
            elif pred and not obs:
                fp += 1
            elif obs and not pred:
                fn += 1

    total_dis = fp + fn
    res.check("T3 predicted vs observed visibility", "0 disagreements",
              f"{total_dis} of {checked} samples ({tp} agree-visible, "
              f"{fp} predicted-only, {fn} observed-only)",
              total_dis == 0)


# ------------------------------------------------------------------------ T4

def t4_time_to_los(scn, records, res: Result, dt_s: float, tol_s: float):
    """Predicted seconds-to-LOS vs the LOS the run actually experienced.

    Ground truth needs care. A link row disappears for two quite different
    reasons — the pass ended (a real LOS) or the satellite drained its buffer and
    `Simulator._visibility` stopped emitting it. Only the first is a LOS, so runs
    that end because the satellite finished, and runs still open when the record
    ends, are both excluded. Counting them would score the forecaster against the
    scheduler's behaviour rather than against geometry.

    The surviving truth is an interval, not a point: the row is present at
    `t_last` and gone at `t_last + dt`, so the true LOS lies in
    `(t_last, t_last + dt]`. A prediction inside that interval is exactly right.
    """
    sats = {s.id: s for s in scn.satellites}
    stations = {g.id: g for g in scn.stations}
    times_all = sorted({r.t for r in records})
    t_end = times_all[-1]

    backlog = {}
    seen = {}
    for r in records:
        for s in r.satellites:
            backlog.setdefault(s.sat_id, {})[r.t] = s.backlog_bits
        for l in r.links:
            seen.setdefault((l.sat_id, l.station_id), []).append(r.t)

    errs = []
    n_runs = n_drained = n_open = 0
    for (sid, gid), times in seen.items():
        times = sorted(times)
        runs, cur = [], [times[0]]
        for a, b in zip(times, times[1:]):
            if b - a <= dt_s * 1.5:
                cur.append(b)
            else:
                runs.append(cur)
                cur = [b]
        runs.append(cur)

        rain = scn.weather.fade_db(gid, 0.0)
        for run in runs:
            n_runs += 1
            t_last = run[-1]
            if t_last >= t_end - 1e-9:            # still visible when the run ended
                n_open += 1
                continue
            b_next = backlog.get(sid, {}).get(t_last + dt_s)
            if backlog.get(sid, {}).get(t_last, 0.0) <= 0 or (b_next is not None and b_next <= 0):
                n_drained += 1                    # buffer emptied, not a lost pass
                continue

            lo_true = t_last                      # true LOS in (t_last, t_last + dt]
            hi_true = t_last + dt_s
            for t in run:
                pred = fc.time_to_los(sats[sid], stations[gid], t, rain_zenith_db=rain)
                if pred is None:
                    errs.append(float("inf"))
                    continue
                lo, hi = lo_true - t, hi_true - t
                errs.append(0.0 if lo <= pred <= hi else min(abs(pred - lo), abs(pred - hi)))

    if not errs:
        res.check("T4 time-to-LOS", "samples > 0",
                  f"no usable LOS runs ({n_runs} runs: {n_drained} drained, {n_open} open)",
                  False)
        return

    errs = np.array(errs)
    worst = float(np.max(errs))
    p95 = float(np.percentile(errs, 95))
    exact = int(np.sum(errs == 0.0))
    res.check("T4 time-to-LOS error (max)", f"<= {tol_s:.1f} s",
              f"{worst:.2f} s over {len(errs)} samples from "
              f"{n_runs - n_drained - n_open}/{n_runs} runs "
              f"({n_drained} drained, {n_open} open, excluded)",
              worst <= tol_s)
    res.check("T4 time-to-LOS inside truth interval", ">= 95% of samples",
              f"{100.0 * exact / len(errs):.1f}% exact, p95 err {p95:.2f} s",
              exact >= 0.95 * len(errs))


# ------------------------------------------------------------------------ T5

def t5_speed(scn, cfg, res: Result, records, duration_s, dt_s):
    """Forecasting the horizon must be far cheaper than simulating it."""
    sats, stations = scn.satellites, scn.stations

    t0 = time.perf_counter()
    sched = fc.contact_schedule(sats, stations, 0.0, duration_s, step_s=10.0)
    t_fc = time.perf_counter() - t0

    t0 = time.perf_counter()
    _run_sim(cfg, duration_s, dt_s, telemetry=False)   # telemetry off: fairest baseline
    t_sim = time.perf_counter() - t0

    # single time-to-LOS call — the cost a controller pays per link per decision
    s, g = sats[0], stations[0]
    t0 = time.perf_counter()
    for _ in range(200):
        fc.time_to_los(s, g, 60.0)
    t_ttl = (time.perf_counter() - t0) / 200.0

    speedup = t_sim / t_fc if t_fc > 0 else float("inf")
    res.check("T5 full schedule vs one sim run", "forecaster faster",
              f"{t_fc * 1e3:.1f} ms vs {t_sim * 1e3:.1f} ms ({speedup:.1f}x), "
              f"{len(sched)} windows", speedup > 1.0)
    # the number that matters for a controller: answering "when does this link
    # drop?" by re-simulating costs a whole run; the forecaster answers directly
    per_query = t_sim / t_ttl if t_ttl > 0 else float("inf")
    res.check("T5 one time_to_los vs re-simulating", "> 50x cheaper",
              f"{t_ttl * 1e3:.2f} ms vs {t_sim * 1e3:.1f} ms ({per_query:.0f}x)",
              per_query > 50.0)


# ----------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preset", default="india4-nominal")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--dt", type=float, default=None)
    args = ap.parse_args()

    presets = all_presets()
    if args.preset not in presets:
        print(f"unknown preset {args.preset!r}. available: {', '.join(presets)}")
        return 2
    cfg = _with_sim_overrides(presets[args.preset], args.duration, args.dt)
    scn, simcfg, records = _run_sim(cfg)
    dt_s = simcfg.dt_s
    duration_s = simcfg.duration_s

    print(f"\n  preset {args.preset} · {len(scn.satellites)} sats · "
          f"{len(scn.stations)} stations · {duration_s:.0f}s @ dt={dt_s:.0f}s "
          f"· {len(records)} records")
    masks = {g.id: (g.elevation_mask_deg, fc.effective_mask_deg(g)) for g in scn.stations}
    for gid, (cfg_mask, eff) in masks.items():
        note = "  <- scan-limited" if eff > cfg_mask else ""
        print(f"    {gid:<20} mask {cfg_mask:>5.1f} deg -> effective {eff:>5.1f} deg{note}")

    res = Result()
    t1_geometry_precision(scn, res)
    t2_root_finding(scn, res)
    t3_vs_telemetry(scn, records, res, dt_s)
    t4_time_to_los(scn, records, res, dt_s, tol_s=dt_s)
    t5_speed(scn, cfg, res, records, duration_s, dt_s)
    return res.report()


if __name__ == "__main__":
    raise SystemExit(main())
