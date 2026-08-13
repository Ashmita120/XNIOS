"""V2 — does the analytical LOS trigger beat the V1 elevation heuristic?

    python experiments/handover_ab.py --runs 12

One variable. Identical scenarios, seeds, scheduler, allocators and worlds; the
only difference is how proactive handover decides a pass is ending:

    A  "elevation"  V1: elevation at t+lead vs the station's CONFIGURED mask
    B  "forecast"   V2: exact seconds-to-LOS from xnios.forecast

The concrete defect A has: `elevation_mask_deg` is 10 deg on these stations, but
they are phased arrays with `max_scan_deg=60`, so a beam cannot be formed below
**30 deg** at all (`link.beam_reachable`). The V1 test therefore reports the pass
as continuing while the link is already unusable, and the session is dropped as an
interruption instead of being handed over. B asks the forecaster, which folds in
the mask, the steering limit and the SNR floor together.

Primary metric: **sessions_interrupted** (lower is better). Everything else is
reported so an improvement bought by thrashing is visible rather than hidden —
if B merely hands over far more often, that is a cost, not a win.
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
from xnios import forecast as fc

from api.presets import all_presets

METRICS = ["sessions_interrupted", "proactive_handovers", "delivered_gbit",
           "completion_rate", "sla_compliance", "mean_wait_s"]


def build_cfg(preset, seed, mode, lead=30.0):
    cfg = copy.deepcopy(all_presets()[preset])
    cfg["seed"] = seed
    sim = dict(cfg.get("sim", {}))
    sim["handover"] = True                     # both arms hand over; only the trigger differs
    sim["handover_lead_s"] = lead
    sim["handover_mode"] = mode
    cfg["sim"] = sim
    import random as _r
    rng = _r.Random(f"world-{seed}")
    for s in cfg.get("satellites", {}).get("list", []):
        s["arg_lat0"] = s.get("arg_lat0", 0.0) + rng.uniform(-25.0, 25.0)
        s["backlog_gbit"] = s.get("backlog_gbit", 20.0) * rng.uniform(0.6, 1.6)
    return cfg


def run(cfg):
    scn = scenario_from_config(cfg)
    simcfg = sim_config_from_config(cfg)
    return Simulator(scn, make_scheduler("fcfs/strongest"), simcfg,
                     allocator=make_allocator("equal"),
                     power_allocator=make_power_allocator("adaptive"),
                     freq_allocator=make_freq_allocator("coloring")).run()


def show_defect(preset):
    """Quantify the gap the two triggers disagree over, before measuring KPIs."""
    scn = scenario_from_config(build_cfg(preset, 0, "elevation"))
    g = scn.stations[0]
    eff = fc.effective_mask_deg(g)
    print(f"  {g.id}: configured mask {g.elevation_mask_deg:.0f} deg, "
          f"phased_array={g.phased_array}, max_scan {g.max_scan_deg:.0f} deg "
          f"-> effective mask {eff:.0f} deg")
    print(f"  the V1 trigger treats {g.elevation_mask_deg:.0f}-{eff:.0f} deg as usable; "
          f"the link is not")
    # The dead band must be measured over the GEOMETRIC window (above the
    # configured mask), not the usable one — `contact_windows` returns only usable
    # time, so measuring inside it is circular and always reports 0%.
    s = scn.satellites[2]
    grid = np.arange(0.0, 6000.0, 1.0)
    elev, _ = fc.elevation_series(s, g, grid)
    above_cfg = elev >= g.elevation_mask_deg
    usable = elev >= eff
    dead = above_cfg & ~usable
    print(f"  {s.id}: {int(above_cfg.sum())}s look visible to the V1 trigger, "
          f"{int(usable.sum())}s are usable")
    print(f"  dead band: {int(dead.sum())}s = "
          f"{100 * dead.sum() / max(1, above_cfg.sum()):.0f}% of apparent visibility\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preset", default="india4-storm")
    ap.add_argument("--runs", type=int, default=12)
    ap.add_argument("--lead", type=float, default=30.0)
    args = ap.parse_args()

    print(f"\n  {args.preset} · {args.runs} worlds · handover lead {args.lead:.0f}s\n")
    show_defect(args.preset)

    rows = {"elevation": [], "forecast": []}
    for seed in range(args.runs):
        for mode in rows:
            rows[mode].append(run(build_cfg(args.preset, seed, mode, args.lead)).summary)

    print(f"  {'metric':<22} {'A elevation':>12} {'B forecast':>12} {'delta':>10}"
          f"  {'worlds better':>13}")
    print(f"  {'-'*22} {'-'*12} {'-'*12} {'-'*10}  {'-'*13}")
    for k in METRICS:
        a = np.array([s[k] for s in rows["elevation"]], float)
        b = np.array([s[k] for s in rows["forecast"]], float)
        lower_better = k in ("sessions_interrupted", "mean_wait_s")
        better = int(np.sum(b < a)) if lower_better else int(np.sum(b > a))
        print(f"  {k:<22} {a.mean():>12.3f} {b.mean():>12.3f} {b.mean()-a.mean():>+10.3f}"
              f"  {better:>6}/{len(a):<6}")

    a = np.array([s["sessions_interrupted"] for s in rows["elevation"]], float)
    b = np.array([s["sessions_interrupted"] for s in rows["forecast"]], float)
    d = a - b                                   # positive = fewer interruptions with B
    sem = float(np.std(d, ddof=1) / np.sqrt(len(d))) if len(d) > 1 else 0.0
    print(f"\n  interruptions avoided by the forecast trigger: "
          f"{d.mean():+.2f} per run (sem {sem:.2f})")
    ha = np.array([s["proactive_handovers"] for s in rows["elevation"]], float)
    hb = np.array([s["proactive_handovers"] for s in rows["forecast"]], float)
    print(f"  handovers performed: {ha.mean():.2f} -> {hb.mean():.2f} "
          f"({hb.mean()-ha.mean():+.2f})")
    if d.mean() > 2 * sem and sem > 0:
        print("  -> the analytical trigger measurably reduces interruptions.")
    elif abs(d.mean()) <= max(2 * sem, 1e-9):
        print("  -> no measurable difference; the heuristic is good enough here.")
    else:
        print("  -> the analytical trigger is WORSE; investigate before adopting.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
