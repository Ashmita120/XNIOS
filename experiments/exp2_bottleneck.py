"""Experiment 2 -- beam & bandwidth bottleneck sweep (see the plan for full rationale).

Reuses exp1's 2-station contended geometry at a fixed 80-satellite (4x) congestion level.
Two sub-sweeps written to one CSV (distinguished by a `sweep_type` column):

  bandwidth: bandwidth_mhz in {500 (uncontended baseline), 100, 50} x
             bandwidth_allocator in {equal, lp}, scheduler fixed, beams fixed at 4
             -> shows whether `lp` actually beats `equal` once the pool is scarce
             (at 500 MHz it never did -- 200 MHz max demand never touched the pool).

  beams:     num_beams in {1, 2, 4, 8}, scheduler in {fcfs/strongest, mip},
             bandwidth fixed at 500 (uncontended, isolates the beam-count effect)
             -> queue growth / wait-time scaling curve.

Run:  python experiments/exp2_bottleneck.py            (full sweep)
      python experiments/exp2_bottleneck.py --smoke     (tiny grid, fast sanity check)
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
RESULTS_PATH = os.path.join(ROOT, "experiments", "results", "exp2_bottleneck.csv")

N_SATS = 80             # exp1's "4x" congestion level -- known to be genuinely contended
N_STATIONS = 2
DURATION_S = 1200.0
DT_S = 5.0
T_MID = 600.0
SEEDS = [0, 1, 2]

BANDWIDTH_LEVELS_MHZ = [500, 100, 50]
BANDWIDTH_ALLOCATORS = ["equal", "lp"]

BEAM_LEVELS = [1, 2, 4, 8]
BEAM_SCHEDULERS = ["fcfs/strongest", "mip"]

# Controlled heterogeneous-power sub-sweep: the main bandwidth sweep above found `lp`
# tied with (or slightly worse than) `equal` even at 50 MHz, because satellites clustered
# on one tight pass have near-identical link quality -- there's no asymmetry for LP to
# exploit by shifting bandwidth around. This isolates the textbook case: 4 co-located,
# CONCURRENTLY active satellites (guaranteed sharing one pool, unlike the large pool's
# organic scheduling) with deliberately different tx_power (2 weak @ 1W, 2 strong @ 5W).
HETERO_BW_LEVELS_MHZ = [500, 100, 50, 20]
HETERO_POWERS_W = [1.0, 1.0, 5.0, 5.0]


def build_hetero_config(bandwidth_mhz: float) -> dict:
    sats = []
    for i, pw in enumerate(HETERO_POWERS_W):
        sats.append({"id": f"SAT-{i}", "inc": 53.0, "raan": 270.0, "arg_lat0": 154.5 + i * 0.3,
                     "altitude_km": 600.0, "backlog_gbit": 60.0, "tier": "commercial",
                     "tx_power_w": pw, "tx_power_max_w": pw})
    return {
        "name": f"exp2-hetero-bw{bandwidth_mhz}", "seed": 0, "t_mid": 600.0,
        "sim": {"duration_s": 1200.0, "dt_s": 5.0, "decision_interval_s": 5.0},
        "stations": [{"id": "GS-0", "lat": 13.03, "lon": 77.51, "num_beams": 4,
                     "g_over_t_dbk": 24, "weather": "clear", "bandwidth_mhz": bandwidth_mhz,
                     "phased_array": False}],
        "satellites": {"mode": "explicit", "list": sats, "freq_ghz": 8.2, "bandwidth_mhz": 50},
    }


def build_config(n_beams: int, bandwidth_mhz: float, seed: int) -> dict:
    return {
        "name": f"exp2-beams{n_beams}-bw{bandwidth_mhz}-seed{seed}",
        "seed": seed,
        "t_mid": T_MID,
        "sim": {"duration_s": DURATION_S, "dt_s": DT_S, "decision_interval_s": DT_S},
        "stations": [
            {"id": "GS-0", "place_under": {"plane": 0, "dlat": 0.0, "dlon": 0.0},
             "num_beams": n_beams, "bandwidth_mhz": bandwidth_mhz},
            {"id": "GS-1", "place_under": {"plane": 0, "dlat": 3.0, "dlon": -3.0},
             "num_beams": n_beams, "bandwidth_mhz": bandwidth_mhz},
        ],
        "satellites": {
            "mode": "generate", "count": N_SATS,
            "planes": [{"inc": 53.0, "raan": 0.0, "altitude_km": 600.0}],
            "arg_lat_spread_deg": 8.0,
            "freq_ghz": 8.2, "bandwidth_mhz": 50, "tx_power_w": 5,
            "backlog_gbit": {"classes": [2, 20, 80], "weights": [0.35, 0.4, 0.25]},
            "tiers": ["research", "commercial", "commercial", "military", "emergency"],
            "tier_deadline_s": {"emergency": 90, "military": 180, "commercial": 300, "research": 550},
        },
    }


FIELDNAMES = (["sweep_type", "bandwidth_mhz", "bandwidth_allocator", "num_beams",
              "n_beams_total", "scheduler", "n_satellites", "seed"] + list(KPI_KEYS)
              + ["starvation_pct", "wall_time_s"])


def _run_one(n_beams, bandwidth_mhz, sched_name, bw_alloc_name, seed):
    cfg = build_config(n_beams, bandwidth_mhz, seed)
    scn = scenario_from_config(cfg)
    sim_cfg = sim_config_from_config(cfg)
    scheduler = make_scheduler(sched_name)
    alloc = make_allocator(bw_alloc_name)
    palloc = make_power_allocator("fixed")
    falloc = make_freq_allocator("coloring")
    row, res = run_kpis(scn, sim_cfg, scheduler, alloc, palloc, falloc)
    row["starvation_pct"] = starvation_pct(res.per_sat)
    return row


def run_sweep(smoke: bool = False) -> None:
    bw_levels = BANDWIDTH_LEVELS_MHZ[:1] if smoke else BANDWIDTH_LEVELS_MHZ
    bw_allocs = BANDWIDTH_ALLOCATORS if not smoke else BANDWIDTH_ALLOCATORS[:1]
    beam_levels = BEAM_LEVELS[:2] if smoke else BEAM_LEVELS
    beam_scheds = BEAM_SCHEDULERS[:1] if smoke else BEAM_SCHEDULERS
    seeds = SEEDS[:1] if smoke else SEEDS

    hetero_levels = HETERO_BW_LEVELS_MHZ[:1] if smoke else HETERO_BW_LEVELS_MHZ

    n_bw_runs = len(bw_levels) * len(bw_allocs) * len(seeds)
    n_beam_runs = len(beam_levels) * len(beam_scheds) * len(seeds)
    n_hetero_runs = len(hetero_levels) * 2   # equal + lp
    total = n_bw_runs + n_beam_runs + n_hetero_runs
    print("=" * 88)
    print(f"Exp2 bottleneck: bandwidth sweep {n_bw_runs} + beam sweep {n_beam_runs} + "
          f"hetero-power sweep {n_hetero_runs} = {total} total")
    print("=" * 88)

    writer = CsvWriter(RESULTS_PATH, FIELDNAMES)
    t_start = time.time()
    run_idx = 0
    try:
        # --- bandwidth sweep: beams fixed at 4, scheduler fixed at fcfs/strongest ---
        for bw_mhz in bw_levels:
            for bw_alloc in bw_allocs:
                for seed in seeds:
                    run_idx += 1
                    row = _run_one(4, bw_mhz, "fcfs/strongest", bw_alloc, seed)
                    out = dict(sweep_type="bandwidth", bandwidth_mhz=bw_mhz,
                              bandwidth_allocator=bw_alloc, num_beams=4,
                              n_beams_total=N_STATIONS * 4, scheduler="fcfs/strongest",
                              n_satellites=N_SATS, seed=seed, **row)
                    writer.write(out)
                    print(f"[{run_idx}/{total}] bandwidth bw={bw_mhz:4.0f}MHz "
                          f"alloc={bw_alloc:6s} seed={seed} -> "
                          f"delivered={row['delivered_gbit']:7.2f}Gb "
                          f"fairness={row['fairness']:.3f} "
                          f"sla={row['sla_compliance']*100:5.1f}% "
                          f"starve={row['starvation_pct']*100:5.1f}% "
                          f"({row['wall_time_s']:.2f}s)")

        # --- beam sweep: bandwidth fixed at 500 (uncontended) ---
        for n_beams in beam_levels:
            for sched_name in beam_scheds:
                for seed in seeds:
                    run_idx += 1
                    row = _run_one(n_beams, 500, sched_name, "equal", seed)
                    out = dict(sweep_type="beams", bandwidth_mhz=500,
                              bandwidth_allocator="equal", num_beams=n_beams,
                              n_beams_total=N_STATIONS * n_beams, scheduler=sched_name,
                              n_satellites=N_SATS, seed=seed, **row)
                    writer.write(out)
                    print(f"[{run_idx}/{total}] beams    beams={n_beams:2d}/station "
                          f"sched={sched_name:22s} seed={seed} -> "
                          f"delivered={row['delivered_gbit']:7.2f}Gb "
                          f"wait={row['mean_wait_s']:7.1f}s "
                          f"beam_util={row['beam_utilization']*100:5.1f}% "
                          f"({row['wall_time_s']:.2f}s)")

        # --- heterogeneous-power sweep: 4 co-located sats (2 weak@1W, 2 strong@5W),
        #     guaranteed concurrently sharing one pool -- isolates whether lp can beat
        #     equal when there's real link-quality asymmetry to exploit ---
        for bw_mhz in hetero_levels:
            for bw_alloc in ["equal", "lp"]:
                cfg = build_hetero_config(bw_mhz)
                scn = scenario_from_config(cfg)
                sim_cfg = sim_config_from_config(cfg)
                scheduler = make_scheduler("fcfs/strongest")
                alloc = make_allocator(bw_alloc)
                palloc = make_power_allocator("fixed")
                falloc = make_freq_allocator("coloring")

                run_idx += 1
                row, res = run_kpis(scn, sim_cfg, scheduler, alloc, palloc, falloc)
                row["starvation_pct"] = starvation_pct(res.per_sat)
                out = dict(sweep_type="bandwidth_hetero", bandwidth_mhz=bw_mhz,
                          bandwidth_allocator=bw_alloc, num_beams=4, n_beams_total=4,
                          scheduler="fcfs/strongest", n_satellites=4, seed=0, **row)
                writer.write(out)
                print(f"[{run_idx}/{total}] hetero   bw={bw_mhz:4.0f}MHz alloc={bw_alloc:6s} "
                      f"(2x1W+2x5W) -> delivered={row['delivered_gbit']:7.2f}Gb "
                      f"fairness={row['fairness']:.3f} ({row['wall_time_s']:.2f}s)")
    finally:
        writer.close()

    elapsed = time.time() - t_start
    print("=" * 88)
    print(f"Done: {run_idx} runs in {elapsed/60:.1f} min. Results -> {RESULTS_PATH}")
    print("=" * 88)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exp2: beam & bandwidth bottleneck sweep.")
    ap.add_argument("--smoke", action="store_true", help="tiny grid for a fast sanity check")
    args = ap.parse_args()
    run_sweep(smoke=args.smoke)
