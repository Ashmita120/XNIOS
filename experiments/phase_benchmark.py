"""Benchmark sweep — real ground-station networks x scenarios x policies.

Design rationale lives in testing.md (repo root). Reuses the existing xnios library
end-to-end (config/simulator/experiment/allocators/oracle) — no changes to xnios/ itself.

Run:  python experiments/phase_benchmark.py            (full sweep, writes CSVs)
      python experiments/phase_benchmark.py --smoke     (tiny grid, fast sanity check)
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios.config import scenario_from_config, sim_config_from_config
from xnios.simulator import Simulator
from xnios.experiment import make_scheduler, KPI_KEYS
from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from xnios.oracle import optimal_throughput

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(ROOT, "experiments", "results")

# --------------------------------------------------------------------------- #
# Real ground-station networks (approx public lat/lon for well-known real sites)
# --------------------------------------------------------------------------- #
INDIA8 = [
    {"id": "Delhi",              "lat": 28.61, "lon": 77.21,   "g_over_t_dbk": 24, "weather": "clear"},
    {"id": "Bengaluru-ISTRAC",   "lat": 13.03, "lon": 77.51,   "g_over_t_dbk": 27, "weather": "clear"},
    {"id": "Ahmedabad-SAC",      "lat": 23.03, "lon": 72.58,   "g_over_t_dbk": 24, "weather": "clear"},
    {"id": "Hyderabad-NRSC",     "lat": 17.03, "lon": 78.18,   "g_over_t_dbk": 26, "weather": "clear"},
    {"id": "Guwahati",           "lat": 26.14, "lon": 91.74,   "g_over_t_dbk": 22, "weather": "clear"},
    {"id": "Thiruvananthapuram", "lat": 8.52,  "lon": 76.94,   "g_over_t_dbk": 23, "weather": "clear"},
    {"id": "Lucknow-ISTRAC",     "lat": 26.85, "lon": 80.95,   "g_over_t_dbk": 22, "weather": "clear"},
    {"id": "Port-Blair",         "lat": 11.62, "lon": 92.73,   "g_over_t_dbk": 22, "weather": "clear"},
]
INDIA4_IDS = {"Delhi", "Bengaluru-ISTRAC", "Guwahati", "Port-Blair"}
INDIA4 = [s for s in INDIA8 if s["id"] in INDIA4_IDS]

GLOBAL6 = [
    {"id": "Svalbard",       "lat": 78.23,  "lon": 15.41,   "g_over_t_dbk": 25, "weather": "cloudy"},
    {"id": "Fairbanks",      "lat": 64.84,  "lon": -147.72, "g_over_t_dbk": 24, "weather": "clear"},
    {"id": "PuntaArenas",    "lat": -53.16, "lon": -70.91,  "g_over_t_dbk": 23, "weather": "cloudy"},
    {"id": "Awarua-NZ",      "lat": -46.53, "lon": 168.38,  "g_over_t_dbk": 24, "weather": "rain"},
    {"id": "Hartebeesthoek", "lat": -25.89, "lon": 27.68,   "g_over_t_dbk": 26, "weather": "clear"},
    {"id": "Singapore",      "lat": 1.35,   "lon": 103.82,  "g_over_t_dbk": 22, "weather": "rain"},
]

# network name -> (station list, baseline satellite count)
STATION_NETWORKS = {
    "india8": (INDIA8, 40),
    "india4": (INDIA4, 20),
    "global6": (GLOBAL6, 40),
}


def _phased_station(s: dict) -> dict:
    return {
        "id": s["id"], "lat": s["lat"], "lon": s["lon"],
        "num_beams": 4, "g_over_t_dbk": s["g_over_t_dbk"], "weather": s["weather"],
        "bandwidth_mhz": 500, "phased_array": True, "beamwidth_deg": 3.0,
        "n_channels": 4, "dual_pol": True, "max_scan_deg": 60, "setup_time_s": 0.05,
    }


# Per-network orbital planes. RAAN values were found by a numeric search (see
# testing.md diagnosis) for the (inclination, RAAN) that brings each region's ground
# track closest to overhead -- an arbitrary RAAN choice was found to miss every real
# station entirely (best elevation 19 deg, below the phased array's 30 deg reachable
# floor -> 0 Gb delivered everywhere). This is standard constellation design practice
# (a regional network's planes are oriented to actually serve its region); satellites
# still get a full-orbit arg_lat spread (no per-satellite anchoring), so WHEN within
# the window each one passes overhead is still effectively random.
NETWORK_PLANES = {
    "india8": [
        {"inc": 53.0, "raan": 270.0, "altitude_km": 600.0},
        {"inc": 53.0, "raan": 282.0, "altitude_km": 600.0},
    ],
    "india4": [
        {"inc": 53.0, "raan": 270.0, "altitude_km": 600.0},
        {"inc": 53.0, "raan": 282.0, "altitude_km": 600.0},
    ],
    "global6": [
        {"inc": 53.0, "raan": 50.0,  "altitude_km": 600.0},   # Hartebeesthoek
        {"inc": 53.0, "raan": 102.0, "altitude_km": 600.0},   # Singapore
        {"inc": 53.0, "raan": 222.0, "altitude_km": 600.0},   # Awarua-NZ
        {"inc": 97.6, "raan": 152.0, "altitude_km": 600.0},   # Svalbard
        {"inc": 97.6, "raan": 16.0,  "altitude_km": 600.0},   # Fairbanks
        {"inc": 97.6, "raan": 120.0, "altitude_km": 600.0},   # Punta Arenas
    ],
}

DURATION_S = 6000.0   # 100 min > one ~96.5 min LEO period at 600km -> real contact chances
DT_S = 10.0

# --------------------------------------------------------------------------- #
# Scenario profiles
# --------------------------------------------------------------------------- #
FAILURE_PARAMS = dict(station_mtbf_s=2000.0, station_mttr_s=600.0,
                      beam_mtbf_s=1500.0, beam_mttr_s=500.0)

SCENARIO_PROFILES = [
    dict(name="baseline",        sat_mult=1, failures=False, handover=False, weather="static"),
    dict(name="congested",       sat_mult=2, failures=False, handover=False, weather="static"),
    dict(name="failures",        sat_mult=1, failures=True,  handover=False, weather="static"),
    dict(name="handover",        sat_mult=1, failures=False, handover=True,  weather="static"),
    dict(name="weather_dynamic", sat_mult=1, failures=False, handover=False, weather="dynamic"),
    dict(name="stress_all",      sat_mult=2, failures=True,  handover=True,  weather="dynamic"),
]

POLICY_GRID = [
    (sched, bw, pw)
    for sched in ["fcfs/strongest", "edf/strongest", "hungarian/throughput", "mip"]
    for bw in ["equal", "lp"]
    for pw in ["fixed", "adaptive"]
]
FREQ_ALLOCATOR = "coloring"


def build_config(network_name: str, profile: dict, seed: int) -> tuple[dict, int]:
    stations_raw, base_count = STATION_NETWORKS[network_name]
    stations = [_phased_station(s) for s in stations_raw]
    n_sats = base_count * profile["sat_mult"]

    cfg = {
        "name": f"{network_name}-{profile['name']}-seed{seed}",
        "seed": seed,
        "t_mid": DURATION_S / 2.0,
        "sim": {
            "duration_s": DURATION_S, "dt_s": DT_S, "decision_interval_s": DT_S,
            "handover": profile["handover"], "handover_lead_s": 40.0,
        },
        "stations": stations,
        "satellites": {
            "mode": "generate", "count": n_sats, "planes": NETWORK_PLANES[network_name],
            "arg_lat_spread_deg": 180.0,
            "freq_ghz": 8.2, "bandwidth_mhz": 50, "tx_power_w": 5,
            "backlog_gbit": {"classes": [2, 20, 80], "weights": [0.35, 0.4, 0.25]},
            "tiers": ["research", "commercial", "commercial", "military", "emergency"],
            "tier_deadline_s": {"emergency": 300, "military": 600, "commercial": 1200, "research": 2400},
        },
    }
    if profile["weather"] == "dynamic":
        cfg["weather"] = {"provider": "dynamic", "dwell_s": 300.0}
    if profile["failures"]:
        cfg["dynamics"] = {"random": dict(FAILURE_PARAMS)}
    return cfg, n_sats


INPUT_COLS = [
    "station_network", "n_stations", "n_beams_total", "n_satellites",
    "scenario_profile", "congestion_level", "failures_enabled",
    "station_mtbf_s", "station_mttr_s", "beam_mtbf_s", "beam_mttr_s",
    "handover_enabled", "weather_mode", "duration_s", "dt_s", "seed",
    "scheduler", "bandwidth_allocator", "power_allocator", "freq_allocator",
]
OUTPUT_COLS = list(KPI_KEYS) + ["pct_optimal", "oracle_delivered_gbit", "wall_time_s"]
FIELDNAMES = INPUT_COLS + OUTPUT_COLS


def run_sweep(smoke: bool = False) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    results_path = os.path.join(RESULTS_DIR, "benchmark_results.csv")
    summary_path = os.path.join(RESULTS_DIR, "benchmark_summary.csv")

    networks = list(STATION_NETWORKS.keys())
    profiles = SCENARIO_PROFILES
    policies = POLICY_GRID
    seeds = [0]

    if smoke:
        networks = networks[:1]
        profiles = profiles[:1]
        policies = policies[:2]

    n_scen = len(networks) * len(profiles)
    total_policy_runs = n_scen * len(policies) * len(seeds)
    total_runs = total_policy_runs + n_scen
    print("=" * 88)
    print(f"X-NioS benchmark sweep: {len(networks)} network(s) x {len(profiles)} profile(s) x "
          f"{len(policies)} polic(y/ies) x {len(seeds)} seed(s)")
    print(f"  = {total_policy_runs} policy runs + {n_scen} oracle solves = {total_runs} total runs")
    print("=" * 88)

    t_start = time.time()
    run_idx = 0
    summary_rows = []

    with open(results_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        f.flush()

        for network_name in networks:
            stations_raw, _ = STATION_NETWORKS[network_name]
            n_stations = len(stations_raw)

            for profile in profiles:
                oracle_seed = seeds[0]
                cfg, n_sats = build_config(network_name, profile, oracle_seed)
                scn = scenario_from_config(cfg)
                sim_cfg = sim_config_from_config(cfg)
                n_beams_total = sum(g.num_beams for g in scn.stations)

                base_input = dict(
                    station_network=network_name, n_stations=n_stations,
                    n_beams_total=n_beams_total, n_satellites=n_sats,
                    scenario_profile=profile["name"],
                    congestion_level="high" if profile["sat_mult"] > 1 else "normal",
                    failures_enabled=profile["failures"],
                    station_mtbf_s=FAILURE_PARAMS["station_mtbf_s"] if profile["failures"] else "",
                    station_mttr_s=FAILURE_PARAMS["station_mttr_s"] if profile["failures"] else "",
                    beam_mtbf_s=FAILURE_PARAMS["beam_mtbf_s"] if profile["failures"] else "",
                    beam_mttr_s=FAILURE_PARAMS["beam_mttr_s"] if profile["failures"] else "",
                    handover_enabled=profile["handover"],
                    weather_mode=profile["weather"],
                    duration_s=DURATION_S, dt_s=DT_S,
                )

                # --- oracle ceiling, once per (network, profile) ---
                run_idx += 1
                t0 = time.perf_counter()
                oracle = optimal_throughput(scn, sim_cfg.duration_s, slot_s=20.0)
                wt = time.perf_counter() - t0
                print(f"[{run_idx}/{total_runs}] {network_name:8s} | {profile['name']:15s} | "
                      f"ORACLE ceiling -> {oracle.delivered_gbit:7.1f} Gb  ({wt:5.1f}s)")

                oracle_row = dict(base_input, seed=oracle_seed, scheduler="oracle_ceiling",
                                  bandwidth_allocator="", power_allocator="", freq_allocator="",
                                  pct_optimal=(1.0 if oracle.delivered_gbit > 0 else ""),
                                  oracle_delivered_gbit=oracle.delivered_gbit, wall_time_s=wt)
                for k in KPI_KEYS:
                    oracle_row[k] = oracle.delivered_gbit if k == "delivered_gbit" else ""
                writer.writerow(oracle_row)
                f.flush()

                for sched_name, bw_name, pw_name in policies:
                    for seed in seeds:
                        if seed == oracle_seed:
                            scn_s, sim_cfg_s = scn, sim_cfg
                        else:
                            cfg_s, _ = build_config(network_name, profile, seed)
                            scn_s = scenario_from_config(cfg_s)
                            sim_cfg_s = sim_config_from_config(cfg_s)

                        scheduler = make_scheduler(sched_name)
                        alloc = make_allocator(bw_name)
                        palloc = make_power_allocator(pw_name)
                        falloc = make_freq_allocator(FREQ_ALLOCATOR)

                        run_idx += 1
                        t0 = time.perf_counter()
                        res = Simulator(scn_s, scheduler, sim_cfg_s, allocator=alloc,
                                        power_allocator=palloc, freq_allocator=falloc).run()
                        wt = time.perf_counter() - t0

                        pct_opt = (res.summary["delivered_gbit"] / oracle.delivered_gbit
                                   if oracle.delivered_gbit > 0 else "")
                        row = dict(base_input, seed=seed, scheduler=sched_name,
                                  bandwidth_allocator=bw_name, power_allocator=pw_name,
                                  freq_allocator=FREQ_ALLOCATOR,
                                  pct_optimal=pct_opt, oracle_delivered_gbit=oracle.delivered_gbit,
                                  wall_time_s=wt)
                        for k in KPI_KEYS:
                            row[k] = res.summary[k]
                        writer.writerow(row)
                        f.flush()

                        policy_label = f"{sched_name}+{bw_name}+{pw_name}+{FREQ_ALLOCATOR}"
                        print(f"[{run_idx}/{total_runs}] {network_name:8s} | {profile['name']:15s} | "
                              f"{policy_label:42s} seed={seed} -> "
                              f"{res.summary['delivered_gbit']:7.1f} Gb  ({wt:5.1f}s)")

                        summary_rows.append(dict(row, policy_label=policy_label))

    elapsed = time.time() - t_start
    print("=" * 88)
    print(f"Done: {run_idx} runs in {elapsed/60:.1f} min. Results -> {results_path}")

    _write_summary(summary_rows, summary_path)
    print(f"Summary  -> {summary_path}")
    print("=" * 88)


def _write_summary(rows: list, path: str) -> None:
    groups = defaultdict(list)
    for r in rows:
        groups[(r["station_network"], r["scenario_profile"])].append(r)

    objectives = [
        ("best_throughput", "delivered_gbit", max),
        ("best_completion", "completion_rate", max),
        ("best_sla", "sla_compliance", max),
        ("lowest_wait", "mean_wait_s", min),
        ("best_fairness", "fairness", max),
        ("best_gb_per_kj", "gb_per_kj", max),
        ("lowest_drop_rate", "drop_rate", min),
    ]
    fieldnames = ["station_network", "scenario_profile", "n_satellites", "n_stations",
                 "oracle_delivered_gbit"]
    for label, _key, _fn in objectives:
        fieldnames += [f"{label}_policy", f"{label}_value"]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for (network, profile), rs in sorted(groups.items()):
            out = dict(station_network=network, scenario_profile=profile,
                      n_satellites=rs[0]["n_satellites"], n_stations=rs[0]["n_stations"],
                      oracle_delivered_gbit=rs[0]["oracle_delivered_gbit"])
            for label, key, fn in objectives:
                best = fn(rs, key=lambda r: r[key])
                out[f"{label}_policy"] = best["policy_label"]
                out[f"{label}_value"] = best[key]
            writer.writerow(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="X-NioS benchmark sweep (see testing.md).")
    ap.add_argument("--smoke", action="store_true",
                    help="tiny grid (1 network x 1 profile x 2 policies) for a fast sanity check")
    args = ap.parse_args()
    run_sweep(smoke=args.smoke)
