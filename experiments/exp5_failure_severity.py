"""Experiment 5 -- failure severity sweep (see the plan for full rationale).

Severity here means the FRACTION OF TIME a station is down (a duty cycle), not the
fraction of stations dead forever -- needed for the recovery metrics to mean anything.
For a Poisson failure process with fixed repair time (MTTR), the long-run downtime
fraction is MTTR/(MTBF+MTTR) (standard renewal-reward result), so MTBF is solved from the
target severity with MTTR fixed at 300s. severity=0% disables dynamics; severity=100% is
a distinct scripted "every station permanently down from t=0" event (the Poisson formula
blows up at severity=1, and "always down" isn't really a Poisson process anyway).

4 real, globally-spread stations (Delhi, Singapore, Rotterdam, Sydney), each with its own
tuned orbital plane (reusing exp4_placement's planes_for_stations), ~40 satellites,
100-min duration so the duty cycle plays out more than once per station.

Run:  python experiments/exp5_failure_severity.py            (full sweep)
      python experiments/exp5_failure_severity.py --smoke     (tiny grid, fast sanity check)
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
from experiments.exp4_placement import planes_for_stations
from experiments.bench_common import phased_station, run_kpis, CsvWriter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "experiments", "results", "exp5_failure_severity.csv")

STATIONS = [(28.61, 77.21), (1.35, 103.82), (51.92, 4.48), (-33.87, 151.21)]   # Delhi, Singapore, Rotterdam, Sydney
N_SATS = 40
ALT_KM = 600.0
DURATION_S = 6000.0        # 100 min -> duty cycle plays out more than once
DT_S = 10.0
SEEDS = [0, 1, 2]

SEVERITIES = [0.0, 0.10, 0.20, 0.40, 0.60, 0.80, 1.00]
MTTR_S = 300.0
SCHEDULERS = ["fcfs/strongest", "mip"]


def dynamics_for_severity(severity: float, station_ids: list):
    if severity <= 0.0:
        return None
    if severity >= 1.0:
        return {"events": [{"t": 0, "station": sid, "action": "station_fail"} for sid in station_ids]}
    mtbf = MTTR_S * (1.0 - severity) / severity
    return {"random": {"station_mtbf_s": mtbf, "station_mttr_s": MTTR_S,
                       "beam_mtbf_s": 0.0, "beam_mttr_s": 0.0}}


def build_config(severity: float, seed: int) -> dict:
    planes = planes_for_stations(STATIONS)
    station_cfgs = [phased_station(f"GS-{i}", lat, lon, num_beams=4)
                    for i, (lat, lon) in enumerate(STATIONS)]
    cfg = {
        "name": f"exp5-sev{severity}-seed{seed}", "seed": seed, "t_mid": DURATION_S / 2.0,
        "sim": {"duration_s": DURATION_S, "dt_s": DT_S, "decision_interval_s": DT_S},
        "stations": station_cfgs,
        "satellites": {
            "mode": "generate", "count": N_SATS, "planes": planes, "arg_lat_spread_deg": 180.0,
            "freq_ghz": 8.2, "bandwidth_mhz": 50, "tx_power_w": 5,
            "backlog_gbit": {"classes": [2, 20, 80], "weights": [0.35, 0.4, 0.25]},
            "tiers": ["research", "commercial", "commercial", "military", "emergency"],
            "tier_deadline_s": {"emergency": 300, "military": 600, "commercial": 1200, "research": 2400},
        },
    }
    dyn = dynamics_for_severity(severity, [s["id"] for s in station_cfgs])
    if dyn:
        cfg["dynamics"] = dyn
    return cfg


FIELDNAMES = (["severity_pct", "station_mtbf_s", "station_mttr_s", "scheduler",
              "n_satellites", "n_stations", "seed"] + list(KPI_KEYS) + ["wall_time_s"])


def run_sweep(smoke: bool = False) -> None:
    severities = SEVERITIES[:2] if smoke else SEVERITIES
    schedulers = SCHEDULERS[:1] if smoke else SCHEDULERS
    seeds = SEEDS[:1] if smoke else SEEDS

    total = len(severities) * len(schedulers) * len(seeds)
    print("=" * 88)
    print(f"Exp5 failure severity: {len(severities)} level(s) x {len(schedulers)} "
          f"scheduler(s) x {len(seeds)} seed(s) = {total} runs")
    print("=" * 88)

    alloc = make_allocator("equal")
    palloc = make_power_allocator("fixed")
    falloc = make_freq_allocator("coloring")

    writer = CsvWriter(RESULTS_PATH, FIELDNAMES)
    t_start = time.time()
    run_idx = 0
    try:
        for severity in severities:
            mtbf = (MTTR_S * (1 - severity) / severity) if 0 < severity < 1 else ""
            mttr = MTTR_S if 0 < severity < 1 else ""
            for sched_name in schedulers:
                for seed in seeds:
                    cfg = build_config(severity, seed)
                    scn = scenario_from_config(cfg)
                    sim_cfg = sim_config_from_config(cfg)
                    scheduler = make_scheduler(sched_name)

                    run_idx += 1
                    row, _res = run_kpis(scn, sim_cfg, scheduler, alloc, palloc, falloc)
                    out = dict(severity_pct=severity * 100, station_mtbf_s=mtbf,
                              station_mttr_s=mttr, scheduler=sched_name,
                              n_satellites=N_SATS, n_stations=len(STATIONS), seed=seed, **row)
                    writer.write(out)

                    print(f"[{run_idx}/{total}] severity={severity*100:5.1f}% "
                          f"sched={sched_name:14s} seed={seed} -> "
                          f"completion={row['completion_rate']*100:5.1f}% "
                          f"sla={row['sla_compliance']*100:5.1f}% "
                          f"drop={row['drop_rate']*100:5.1f}% "
                          f"interrupted={row['sessions_interrupted']:3.0f} "
                          f"recovery={row['mean_recovery_s']:7.1f}s "
                          f"({row['wall_time_s']:.2f}s)")
    finally:
        writer.close()

    elapsed = time.time() - t_start
    print("=" * 88)
    print(f"Done: {run_idx} runs in {elapsed/60:.1f} min. Results -> {RESULTS_PATH}")
    print("=" * 88)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exp5: failure severity sweep.")
    ap.add_argument("--smoke", action="store_true", help="tiny grid for a fast sanity check")
    args = ap.parse_args()
    run_sweep(smoke=args.smoke)
