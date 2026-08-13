"""Experiment 1 -- scheduler contention sweep (see the plan for full rationale).

The first benchmark found every scheduler tied because stations were plentiful (6-8) and
satellites spread across a full orbit, so no more than a couple were ever simultaneously
free at one station. This forces the opposite: 2 dish stations, 2 beams each (4 total
slots), satellites tightly clustered on one pass (arg_lat_spread=8deg, the same trick
xnios/scenarios.py's congested_scenario uses) so 20-160 satellites contend for 4 beams at
once. Sweeps satellite count (congestion level) x scheduler.

Run:  python experiments/exp1_contention.py            (full sweep)
      python experiments/exp1_contention.py --smoke     (tiny grid, fast sanity check)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler, KPI_KEYS
from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from experiments.bench_common import run_kpis, starvation_pct, CsvWriter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "experiments", "results", "exp1_contention.csv")

BASE_SATS = 20
CONGESTION_MULTS = [1, 2, 4, 6, 8]              # -> 20/40/80/120/160 satellites
SCHEDULERS = ["fcfs/strongest", "edf/strongest", "priority/strongest",
             "hungarian/throughput", "mip"]
SEEDS = [0, 1, 2]

N_BEAMS_PER_STATION = 2
N_STATIONS = 2
DURATION_S = 1200.0
DT_S = 5.0
T_MID = 600.0


def build_config(n_sats: int, seed: int) -> dict:
    return {
        "name": f"exp1-n{n_sats}-seed{seed}",
        "seed": seed,
        "t_mid": T_MID,
        "sim": {"duration_s": DURATION_S, "dt_s": DT_S, "decision_interval_s": DT_S},
        "stations": [
            {"id": "GS-0", "place_under": {"plane": 0, "dlat": 0.0, "dlon": 0.0},
             "num_beams": N_BEAMS_PER_STATION},
            {"id": "GS-1", "place_under": {"plane": 0, "dlat": 3.0, "dlon": -3.0},
             "num_beams": N_BEAMS_PER_STATION},
        ],
        "satellites": {
            "mode": "generate", "count": n_sats,
            "planes": [{"inc": 53.0, "raan": 0.0, "altitude_km": 600.0}],
            "arg_lat_spread_deg": 8.0,
            "freq_ghz": 8.2, "bandwidth_mhz": 50, "tx_power_w": 5,
            "backlog_gbit": {"classes": [2, 20, 80], "weights": [0.35, 0.4, 0.25]},
            "tiers": ["research", "commercial", "commercial", "military", "emergency"],
            "tier_deadline_s": {"emergency": 90, "military": 180, "commercial": 300, "research": 550},
        },
    }


FIELDNAMES = (["congestion_level", "congestion_mult", "n_satellites", "n_stations",
              "n_beams_total", "scheduler", "seed"] + list(KPI_KEYS)
              + ["starvation_pct", "wall_time_s"])


def run_sweep(smoke: bool = False) -> None:
    mults = CONGESTION_MULTS[:1] if smoke else CONGESTION_MULTS
    schedulers = SCHEDULERS[:2] if smoke else SCHEDULERS
    seeds = SEEDS[:1] if smoke else SEEDS

    total = len(mults) * len(schedulers) * len(seeds)
    print("=" * 88)
    print(f"Exp1 scheduler contention: {len(mults)} level(s) x {len(schedulers)} "
          f"scheduler(s) x {len(seeds)} seed(s) = {total} runs")
    print("=" * 88)

    alloc = make_allocator("equal")
    palloc = make_power_allocator("fixed")
    falloc = make_freq_allocator("coloring")

    writer = CsvWriter(RESULTS_PATH, FIELDNAMES)
    t_start = time.time()
    run_idx = 0
    try:
        for mult in mults:
            n_sats = BASE_SATS * mult
            for sched_name in schedulers:
                for seed in seeds:
                    cfg = build_config(n_sats, seed)
                    scn = scenario_from_config(cfg)
                    sim_cfg = sim_config_from_config(cfg)
                    scheduler = make_scheduler(sched_name)

                    run_idx += 1
                    row, res = run_kpis(scn, sim_cfg, scheduler, alloc, palloc, falloc)
                    row["starvation_pct"] = starvation_pct(res.per_sat)

                    out = dict(congestion_level=f"{mult}x", congestion_mult=mult,
                              n_satellites=n_sats, n_stations=N_STATIONS,
                              n_beams_total=N_STATIONS * N_BEAMS_PER_STATION,
                              scheduler=sched_name, seed=seed, **row)
                    writer.write(out)

                    print(f"[{run_idx}/{total}] {mult}x ({n_sats:3d} sats) | "
                          f"{sched_name:22s} seed={seed} -> "
                          f"delivered={row['delivered_gbit']:7.2f}Gb "
                          f"completion={row['completion_rate']*100:5.1f}% "
                          f"sla={row['sla_compliance']*100:5.1f}% "
                          f"wait={row['mean_wait_s']:6.1f}s "
                          f"dec={row['mean_decision_ms']:.4f}ms "
                          f"({row['wall_time_s']:.2f}s)")
    finally:
        writer.close()

    elapsed = time.time() - t_start
    print("=" * 88)
    print(f"Done: {run_idx} runs in {elapsed/60:.1f} min. Results -> {RESULTS_PATH}")
    print("=" * 88)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exp1: scheduler contention sweep.")
    ap.add_argument("--smoke", action="store_true", help="tiny grid for a fast sanity check")
    args = ap.parse_args()
    run_sweep(smoke=args.smoke)
