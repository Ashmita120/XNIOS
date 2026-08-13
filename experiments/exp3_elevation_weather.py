"""Experiment 3 -- low-elevation + weather-severity sweep (see the plan for rationale).

The first benchmark's passes were an accidental 83-86deg near-overhead -- unrealistically
easy, and why adaptive power never showed a gain (link margin was enormous even under
storm). This uses the new xnios.orbit.find_orbit_for_elevation() helper to build
CONTROLLED, non-degenerate elevation passes (20/30/40/50/60/80deg) over a real station,
crossed with the new weather severity tiers (clear..extreme), to find where adaptive
power actually earns its keep.

Geometry note: find_orbit_for_elevation() finds the peak at t=0 (GMST=0 convention), but
Simulator.run() only advances t forward from 0 -- so the arg_lat0 it returns is shifted
here by -mean_motion*T_MID so the pass instead peaks at t=T_MID, mid-simulation (giving a
full rise-and-set pass to observe, not just the descending half).

Run:  python experiments/exp3_elevation_weather.py            (full sweep)
      python experiments/exp3_elevation_weather.py --smoke     (tiny grid, fast sanity check)
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import orbit as orb
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler, KPI_KEYS
from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from experiments.bench_common import run_kpis, CsvWriter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "experiments", "results", "exp3_elevation_weather.csv")
GAIN_PATH = os.path.join(ROOT, "experiments", "results", "exp3_adaptive_gain.csv")

STATION_LAT, STATION_LON = 13.03, 77.51    # Bengaluru-ISTRAC (real; reused from configs/india.json)
INC_DEG = 53.0
ALT_KM = 600.0
T_MID = 600.0
DURATION_S = 1200.0
DT_S = 5.0

ELEVATIONS = [20.0, 30.0, 40.0, 50.0, 60.0, 80.0]
WEATHER_STATES = ["clear", "light_rain", "heavy_rain", "storm", "extreme"]
POWER_ALLOCATORS = ["fixed", "adaptive"]
SEEDS = [0, 1, 2]
N_SATS_PER_CELL = 8
BACKLOG_GBIT = 60.0             # large -> a single pass never fully drains it
JITTER_DEG = 3.0                # small per-satellite phase spread around the peak


def orbit_params_for_elevation(target_elev: float):
    r = orb.find_orbit_for_elevation(STATION_LAT, STATION_LON, INC_DEG, target_elev, ALT_KM)
    n = orb.mean_motion(ALT_KM)
    shift_deg = math.degrees(n * T_MID)
    arg_lat0_at_tmid = (r["arg_lat0_deg"] - shift_deg) % 360.0
    return r["raan_deg"], arg_lat0_at_tmid, r["achieved_elev_deg"]


def build_config(target_elev: float, weather_state: str, power_alloc: str, seed: int):
    raan, arg_lat0_base, achieved = orbit_params_for_elevation(target_elev)
    rng = random.Random(seed)
    sats = []
    for i in range(N_SATS_PER_CELL):
        jitter = rng.uniform(-JITTER_DEG, JITTER_DEG)
        sats.append({
            "id": f"SAT-{i}", "inc": INC_DEG, "raan": raan,
            "arg_lat0": (arg_lat0_base + jitter) % 360.0, "altitude_km": ALT_KM,
            "backlog_gbit": BACKLOG_GBIT, "tier": "commercial",
        })
    cfg = {
        "name": f"exp3-elev{target_elev}-{weather_state}-{power_alloc}-seed{seed}",
        "seed": seed, "t_mid": T_MID,
        "sim": {"duration_s": DURATION_S, "dt_s": DT_S, "decision_interval_s": DT_S},
        "stations": [
            {"id": "GS-0", "lat": STATION_LAT, "lon": STATION_LON,
             "num_beams": N_SATS_PER_CELL + 2, "g_over_t_dbk": 24,
             "weather": weather_state, "bandwidth_mhz": 500, "phased_array": False},
        ],
        "satellites": {"mode": "explicit", "list": sats, "freq_ghz": 8.2,
                       "bandwidth_mhz": 50, "tx_power_w": 5, "tx_power_max_w": 10},
    }
    return cfg, achieved


FIELDNAMES = (["target_elev_deg", "achieved_elev_deg", "weather_state", "power_allocator",
              "seed"] + list(KPI_KEYS) + ["wall_time_s"])


def run_sweep(smoke: bool = False) -> None:
    elevations = ELEVATIONS[:2] if smoke else ELEVATIONS
    weathers = WEATHER_STATES[:2] if smoke else WEATHER_STATES
    powers = POWER_ALLOCATORS
    seeds = SEEDS[:1] if smoke else SEEDS

    total = len(elevations) * len(weathers) * len(powers) * len(seeds)
    print("=" * 88)
    print(f"Exp3 elevation x weather: {len(elevations)} elev x {len(weathers)} weather x "
          f"{len(powers)} power x {len(seeds)} seed(s) = {total} runs")
    print("=" * 88)

    alloc = make_allocator("equal")
    falloc = make_freq_allocator("coloring")

    writer = CsvWriter(RESULTS_PATH, FIELDNAMES)
    t_start = time.time()
    run_idx = 0
    raw_rows = []
    try:
        for elev in elevations:
            for weather in weathers:
                for power_name in powers:
                    palloc = make_power_allocator(power_name)
                    for seed in seeds:
                        cfg, achieved = build_config(elev, weather, power_name, seed)
                        scn = scenario_from_config(cfg)
                        sim_cfg = sim_config_from_config(cfg)
                        scheduler = make_scheduler("fcfs/strongest")   # no contention (beams > sats)

                        run_idx += 1
                        row, _res = run_kpis(scn, sim_cfg, scheduler, alloc, palloc, falloc)
                        out = dict(target_elev_deg=elev, achieved_elev_deg=achieved,
                                  weather_state=weather, power_allocator=power_name,
                                  seed=seed, **row)
                        writer.write(out)
                        raw_rows.append(out)

                        print(f"[{run_idx}/{total}] elev={elev:4.0f}(~{achieved:5.1f}) "
                              f"weather={weather:10s} power={power_name:8s} seed={seed} -> "
                              f"delivered={row['delivered_gbit']:6.2f}Gb "
                              f"energy={row['energy_kj']:5.1f}kJ "
                              f"gb_per_kj={row['gb_per_kj']:5.2f} "
                              f"({row['wall_time_s']:.2f}s)")
    finally:
        writer.close()

    elapsed = time.time() - t_start
    print("=" * 88)
    print(f"Done: {run_idx} runs in {elapsed/60:.1f} min. Results -> {RESULTS_PATH}")

    _write_gain_summary(raw_rows)
    print(f"Adaptive-gain summary -> {GAIN_PATH}")
    print("=" * 88)


def _write_gain_summary(rows: list) -> None:
    """For each (elevation, weather) cell, adaptive_gain_pct = (adaptive - fixed) / fixed,
    averaged over seeds -- the number that answers 'does adaptive power actually help
    here'."""
    import csv as csv_mod
    groups = defaultdict(dict)   # (elev, weather) -> {power: [delivered_gbit,...]}
    for r in rows:
        key = (r["target_elev_deg"], r["weather_state"])
        groups[key].setdefault(r["power_allocator"], []).append(r["delivered_gbit"])

    with open(GAIN_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv_mod.DictWriter(f, fieldnames=["target_elev_deg", "weather_state",
                                              "fixed_delivered_gbit", "adaptive_delivered_gbit",
                                              "adaptive_gain_pct"])
        w.writeheader()
        for (elev, weather), by_power in sorted(groups.items()):
            fixed = by_power.get("fixed", [])
            adaptive = by_power.get("adaptive", [])
            if not fixed or not adaptive:
                continue
            fixed_mean = sum(fixed) / len(fixed)
            adaptive_mean = sum(adaptive) / len(adaptive)
            gain = ((adaptive_mean - fixed_mean) / fixed_mean * 100.0) if fixed_mean > 0 else 0.0
            w.writerow({"target_elev_deg": elev, "weather_state": weather,
                       "fixed_delivered_gbit": fixed_mean, "adaptive_delivered_gbit": adaptive_mean,
                       "adaptive_gain_pct": gain})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exp3: low-elevation + weather-severity sweep.")
    ap.add_argument("--smoke", action="store_true", help="tiny grid for a fast sanity check")
    args = ap.parse_args()
    run_sweep(smoke=args.smoke)
