"""Experiment 4 -- station placement optimization (see the plan for full rationale).

Generalizes the india4-vs-india8 finding from the first benchmark (station geographic
diversity, not just beam-count ratio, drove queueing delay) across six placement
strategies for a fixed 6-station budget: random, grid, clustered, coastal (real cities as
a stand-in), equatorial, and a greedy coverage-maximizing "optimized" placement.

For a FAIR comparison, each strategy gets its own orbital planes tuned per-station via
xnios.orbit.find_orbit_for_elevation (target ~80 deg) -- so no strategy is starved by
accidental geometry the way the first benchmark's arbitrary RAAN choice was. This isolates
the placement PATTERN's effect on queueing/completion, not a coverage-gap confound. Full
orbit arg_lat spread (180deg) + 100-min duration, matching phase_benchmark.py's realistic-
coverage style (not exp1-3's deliberately clustered/tight style).

Run:  python experiments/exp4_placement.py            (full sweep)
      python experiments/exp4_placement.py --smoke     (tiny grid, fast sanity check)
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import orbit as orb
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler, KPI_KEYS
from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from xnios.oracle import optimal_throughput
from experiments.bench_common import phased_station, run_kpis, CsvWriter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "experiments", "results", "exp4_placement.csv")

N_STATIONS = 6
N_SATS = 48                # divisible by 6 -> 8 satellites/plane
ALT_KM = 600.0
DURATION_S = 6000.0        # 100 min, matches phase_benchmark.py's realistic-coverage style
DT_S = 10.0
SEEDS = [0, 1, 2]
PLACEMENT_SEED = 0         # station layout itself is fixed; only satellite phasing varies by seed


# --------------------------------------------------------------------------- #
# Placement strategies -> list of (lat, lon)
# --------------------------------------------------------------------------- #
def random_placement() -> list:
    rng = random.Random(PLACEMENT_SEED)
    return [(rng.uniform(-55, 55), rng.uniform(-180, 180)) for _ in range(N_STATIONS)]


def grid_placement() -> list:
    lats = [-20.0, 20.0]
    lons = [-100.0, 20.0, 140.0]
    return [(lat, lon) for lat in lats for lon in lons]


def clustered_placement() -> list:
    rng = random.Random(PLACEMENT_SEED)
    center_lat, center_lon = 20.0, 80.0
    return [(center_lat + rng.uniform(-5, 5), center_lon + rng.uniform(-5, 5))
            for _ in range(N_STATIONS)]


def coastal_placement() -> list:
    # real coastal cities as a stand-in for a coastline-aware placement
    return [(19.08, 72.88), (1.35, 103.82), (51.92, 4.48),
            (49.28, -123.12), (-33.87, 151.21), (-33.92, 18.42)]


def equatorial_placement() -> list:
    rng = random.Random(PLACEMENT_SEED)
    return [(rng.uniform(-8, 8), i * 60.0 - 150.0 + rng.uniform(-10, 10)) for i in range(N_STATIONS)]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


COVERAGE_RADIUS_KM = 2000.0     # approx ground-coverage radius, 600km alt, ~10deg elev mask


def optimized_placement() -> list:
    """Greedy max-coverage: repeatedly add the candidate that covers the most
    still-uncovered points on a dense target grid (classic greedy set-cover)."""
    candidates = [(lat, lon) for lat in range(-60, 61, 20) for lon in range(-180, 180, 20)]
    targets = [(lat, lon) for lat in range(-60, 61, 15) for lon in range(-180, 180, 15)]
    covered, chosen = set(), []
    for _ in range(N_STATIONS):
        best, best_new, best_gain = None, None, -1
        for c in candidates:
            if c in chosen:
                continue
            new_cov = {t for t in targets if t not in covered
                      and _haversine_km(c[0], c[1], t[0], t[1]) <= COVERAGE_RADIUS_KM}
            if len(new_cov) > best_gain:
                best, best_new, best_gain = c, new_cov, len(new_cov)
        chosen.append(best)
        covered |= best_new
    return chosen


STRATEGIES = {
    "random": random_placement,
    "grid": grid_placement,
    "clustered": clustered_placement,
    "coastal": coastal_placement,
    "equatorial": equatorial_placement,
    "optimized": optimized_placement,
}


# --------------------------------------------------------------------------- #
def planes_for_stations(stations: list) -> list:
    """One orbital plane per station, each tuned (via find_orbit_for_elevation) to bring
    that station's own ground track close to overhead (~80deg) -- so every strategy gets
    a fair shot at coverage regardless of where its stations happen to sit."""
    planes = []
    for lat, lon in stations:
        inc = 53.0 if abs(lat) <= 50.0 else 97.6   # reach high-latitude stations too
        r = orb.find_orbit_for_elevation(lat, lon, inc, 80.0, ALT_KM)
        planes.append({"inc": inc, "raan": r["raan_deg"], "altitude_km": ALT_KM})
    return planes


def build_config(strategy_name: str, stations: list, seed: int) -> dict:
    planes = planes_for_stations(stations)
    station_cfgs = [phased_station(f"GS-{i}", lat, lon, num_beams=4)
                    for i, (lat, lon) in enumerate(stations)]
    return {
        "name": f"exp4-{strategy_name}-seed{seed}", "seed": seed, "t_mid": DURATION_S / 2.0,
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


FIELDNAMES = (["strategy", "n_stations", "n_satellites", "seed"] + list(KPI_KEYS)
              + ["pct_optimal", "oracle_delivered_gbit", "wall_time_s"])


def run_sweep(smoke: bool = False) -> None:
    strategies = dict(list(STRATEGIES.items())[:2]) if smoke else STRATEGIES
    seeds = SEEDS[:1] if smoke else SEEDS

    total = len(strategies) * (len(seeds) + 1)   # +1 per strategy for the oracle solve
    print("=" * 88)
    print(f"Exp4 placement: {len(strategies)} strategies x ({len(seeds)} seeds + 1 oracle) "
          f"= {total} total runs")
    print("=" * 88)

    alloc = make_allocator("equal")
    palloc = make_power_allocator("fixed")
    falloc = make_freq_allocator("coloring")

    writer = CsvWriter(RESULTS_PATH, FIELDNAMES)
    t_start = time.time()
    run_idx = 0
    try:
        for name, fn in strategies.items():
            stations = fn()
            oracle_seed = seeds[0]
            cfg = build_config(name, stations, oracle_seed)
            scn = scenario_from_config(cfg)
            sim_cfg = sim_config_from_config(cfg)

            run_idx += 1
            t0 = time.perf_counter()
            oracle = optimal_throughput(scn, sim_cfg.duration_s, slot_s=20.0)
            wt = time.perf_counter() - t0
            print(f"[{run_idx}/{total}] {name:11s} | ORACLE ceiling -> "
                  f"{oracle.delivered_gbit:7.1f} Gb ({wt:.1f}s)")

            for seed in seeds:
                if seed == oracle_seed:
                    scn_s, sim_cfg_s = scn, sim_cfg
                else:
                    cfg_s = build_config(name, stations, seed)
                    scn_s = scenario_from_config(cfg_s)
                    sim_cfg_s = sim_config_from_config(cfg_s)

                run_idx += 1
                row, _res = run_kpis(scn_s, sim_cfg_s, make_scheduler("fcfs/strongest"),
                                     alloc, palloc, falloc)
                pct_opt = (row["delivered_gbit"] / oracle.delivered_gbit
                          if oracle.delivered_gbit > 0 else "")
                out = dict(strategy=name, n_stations=N_STATIONS, n_satellites=N_SATS,
                          seed=seed, pct_optimal=pct_opt,
                          oracle_delivered_gbit=oracle.delivered_gbit, **row)
                writer.write(out)
                print(f"[{run_idx}/{total}] {name:11s} | seed={seed} -> "
                      f"delivered={row['delivered_gbit']:7.1f}Gb "
                      f"completion={row['completion_rate']*100:5.1f}% "
                      f"sla={row['sla_compliance']*100:5.1f}% "
                      f"wait={row['mean_wait_s']:7.1f}s "
                      f"({row['wall_time_s']:.2f}s)")
    finally:
        writer.close()

    elapsed = time.time() - t_start
    print("=" * 88)
    print(f"Done: {run_idx} runs in {elapsed/60:.1f} min. Results -> {RESULTS_PATH}")
    print("=" * 88)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exp4: station placement optimization.")
    ap.add_argument("--smoke", action="store_true", help="tiny grid for a fast sanity check")
    args = ap.parse_args()
    run_sweep(smoke=args.smoke)
