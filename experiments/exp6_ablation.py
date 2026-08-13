"""Experiment 6 -- ablation + multi-seed robustness (see the plan for full rationale).

Base scenario deliberately NOT the first benchmark's too-easy 83-86deg near-overhead case:
uses xnios.orbit.find_orbit_for_elevation for a moderate ~35deg peak elevation (real link
margin, where weather/power actually matter), and TWO stations close together (~2.5deg
offset) so their visibility windows overlap -- giving proactive handover an actual
alternative station to switch to (the first benchmark's handover never fired at all).

Six CUMULATIVE stages, each adding one feature to the previous:
  baseline -> +adaptive_power -> +weather -> +failure_recovery -> +handover -> +dynamic_scheduler

The SAME seed is reused across all 6 stages for a given seed index (only the stage's
feature settings change, not the underlying satellite population/orbits), which is what
makes the paired t-test between consecutive stages valid.

Run:  python experiments/exp6_ablation.py            (full: 15 seeds/stage)
      python experiments/exp6_ablation.py --smoke     (tiny grid, fast sanity check)
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import orbit as orb
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler, KPI_KEYS
from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from experiments.bench_common import phased_station, run_kpis, CsvWriter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_PATH = os.path.join(ROOT, "experiments", "results", "exp6_ablation.csv")
STATS_PATH = os.path.join(ROOT, "experiments", "results", "exp6_ablation_stats.csv")

STATION_A = (20.0, 80.0)
STATION_B = (24.0, 84.0)          # offset along the ground-track direction -> B's LOS lags
                                   # A's by ~95s (verified), giving proactive handover an
                                   # actual window to switch into before A loses the satellite
INC_DEG = 53.0
TARGET_ELEV_DEG = 35.0            # moderate -- real link margin, unlike the 83-86deg case
ALT_KM = 600.0
T_MID = 1500.0
DURATION_S = 3000.0               # 50 min
DT_S = 10.0
N_SATS = 20
JITTER_DEG = 3.0                  # spreads pass timing slightly (jitter>10ish flips which
                                   # station a satellite sees first, breaking the staggered
                                   # GS-A-then-GS-B geometry the handover design depends on)
N_BEAMS_PER_STATION = N_SATS       # must be >= N_SATS: verified that ANY beam contention
                                   # (num_beams < n_sats) causes satellites queued waiting
                                   # for GS-B to always beat GS-A's migrating satellites to
                                   # its free beams (they become eligible for GS-B earlier),
                                   # blocking proactive handover entirely -- an all-or-
                                   # nothing threshold, not a gradual effect. Contention is
                                   # already covered by exp1/exp2; this experiment isolates
                                   # the ablation/handover mechanism itself.

SEEDS_FULL = list(range(15))      # 15 seeds, within the approved 10-30 range
FAILURE_MTBF_S = 1500.0           # matches exp5's "40%-severity" mid-range finding
FAILURE_MTTR_S = 300.0

# NOTE on ordering: +handover is deliberately tested right after +adaptive_power, BEFORE
# +weather and +failure_recovery (swapped from the originally-planned order). Verified
# empirically, in order of discovery:
#   1. Once station failures are active, nearly every session gets interrupted by a failure
#      before it ever approaches a natural LOS, so proactive handover (which only fires
#      near LOS) never gets a chance to act.
#   2. Even with failures off, storm-level weather fade kills the GS-A link (via SNR
#      dropping below MIN_SNR_DB) at a HIGHER elevation than the nominal elevation_mask_deg
#      -- but the proactive-handover check only anticipates the geometric mask, not a
#      weather-induced early death, so the reactive "link already dead" path always wins.
# Both are all-or-nothing starvation of the mechanism, not partial effects. Testing
# handover first (clean baseline + adaptive power only) lets its own mechanism actually be
# exercised; weather and failures are layered on afterward, showing what they cost even
# WITH handover already active.
STAGES = [
    ("baseline",           dict(power="fixed",    weather=False, failures=False, handover=False, scheduler="fcfs/strongest")),
    ("+adaptive_power",    dict(power="adaptive", weather=False, failures=False, handover=False, scheduler="fcfs/strongest")),
    ("+handover",          dict(power="adaptive", weather=False, failures=False, handover=True,  scheduler="fcfs/strongest")),
    ("+weather",           dict(power="adaptive", weather=True,  failures=False, handover=True,  scheduler="fcfs/strongest")),
    ("+failure_recovery",  dict(power="adaptive", weather=True,  failures=True,  handover=True,  scheduler="fcfs/strongest")),
    ("+dynamic_scheduler", dict(power="adaptive", weather=True,  failures=True,  handover=True,  scheduler="mip")),
]


def _base_orbit():
    n = orb.mean_motion(ALT_KM)
    r = orb.find_orbit_for_elevation(STATION_A[0], STATION_A[1], INC_DEG, TARGET_ELEV_DEG, ALT_KM)
    shift_deg = math.degrees(n * T_MID)
    arg_lat0_at_tmid = (r["arg_lat0_deg"] - shift_deg) % 360.0
    return r["raan_deg"], arg_lat0_at_tmid, r["achieved_elev_deg"]


def build_config(stage_settings: dict, seed: int) -> dict:
    import random
    raan, arg_lat0_base, _achieved = _base_orbit()
    rng = random.Random(seed)
    sats = []
    for i in range(N_SATS):
        jitter = rng.uniform(-JITTER_DEG, JITTER_DEG)
        tier = rng.choice(["research", "commercial", "commercial", "military", "emergency"])
        tier_dl = {"emergency": 300, "military": 600, "commercial": 1200, "research": 2400}
        # backlog deliberately large (a full pass at this elevation delivers ~150Gb) so a
        # satellite can't drain and free its session on the ASCENDING half of its pass --
        # otherwise it never reaches the low-elevation window where handover would matter
        # (verified: 60Gb drained mid-rise at ~19deg elevation, before LOS was ever close)
        backlog = rng.choices([300e9, 400e9, 500e9], weights=[0.35, 0.4, 0.25])[0] * rng.uniform(0.8, 1.2)
        sats.append({"id": f"SAT-{i:02d}", "inc": INC_DEG, "raan": raan,
                    "arg_lat0": (arg_lat0_base + jitter) % 360.0, "altitude_km": ALT_KM,
                    "backlog_gbit": backlog / 1e9, "tier": tier,
                    "deadline_s": T_MID + tier_dl[tier] * rng.uniform(0.8, 1.2)})

    weather_a = "storm" if stage_settings["weather"] else "clear"
    weather_b = "rain" if stage_settings["weather"] else "clear"
    # phased_array=False (plain dish): proactive handover only anticipates the nominal
    # elevation_mask_deg (10deg), not a phased array's stricter max_scan_deg reachability
    # cutoff (60deg -> elev>=30) -- with phased_array=True and a 35deg target peak, every
    # session was reactively killed by the scan-limit cutoff (~28-30deg) before the
    # proactive check (watching for the 10deg mask) ever anticipated it. Verified via
    # tracing: sessions consistently ended at ~27.8-28.9deg, matching the scan-limit
    # boundary exactly, not the nominal mask.
    # num_beams generous relative to N_SATS: verified that num_beams < N_SATS causes
    # "waiting" satellites (queued for GS-B since GS-A was already full) to always beat
    # "migrating" satellites (GS-A sessions trying to move before their own LOS) to GS-B's
    # free beams -- an all-or-nothing block on proactive handover, not a gradual effect.
    stations = [
        phased_station("GS-A", STATION_A[0], STATION_A[1], num_beams=N_BEAMS_PER_STATION,
                       weather=weather_a, phased_array=False),
        phased_station("GS-B", STATION_B[0], STATION_B[1], num_beams=N_BEAMS_PER_STATION,
                       weather=weather_b, phased_array=False),
    ]

    cfg = {
        "name": f"exp6-seed{seed}", "seed": seed, "t_mid": T_MID,
        "sim": {"duration_s": DURATION_S, "dt_s": DT_S, "decision_interval_s": DT_S,
               "handover": stage_settings["handover"], "handover_lead_s": 40.0},
        "stations": stations,
        "satellites": {"mode": "explicit", "list": sats, "freq_ghz": 8.2,
                       "bandwidth_mhz": 50, "tx_power_w": 5, "tx_power_max_w": 10},
    }
    if stage_settings["failures"]:
        cfg["dynamics"] = {"random": {"station_mtbf_s": FAILURE_MTBF_S,
                                      "station_mttr_s": FAILURE_MTTR_S,
                                      "beam_mtbf_s": 0.0, "beam_mttr_s": 0.0}}
    return cfg


FIELDNAMES = ["stage", "seed"] + list(KPI_KEYS) + ["wall_time_s"]


def run_sweep(smoke: bool = False) -> None:
    stages = STAGES[:2] if smoke else STAGES
    seeds = SEEDS_FULL[:2] if smoke else SEEDS_FULL

    total = len(stages) * len(seeds)
    print("=" * 88)
    print(f"Exp6 ablation: {len(stages)} stage(s) x {len(seeds)} seed(s) = {total} runs")
    print("=" * 88)

    alloc = make_allocator("equal")
    falloc = make_freq_allocator("coloring")

    writer = CsvWriter(RESULTS_PATH, FIELDNAMES)
    t_start = time.time()
    run_idx = 0
    by_stage = defaultdict(list)
    try:
        for stage_name, settings in stages:
            palloc = make_power_allocator(settings["power"])
            scheduler_name = settings["scheduler"]
            for seed in seeds:
                cfg = build_config(settings, seed)
                scn = scenario_from_config(cfg)
                sim_cfg = sim_config_from_config(cfg)
                scheduler = make_scheduler(scheduler_name)

                run_idx += 1
                row, _res = run_kpis(scn, sim_cfg, scheduler, alloc, palloc, falloc)
                out = dict(stage=stage_name, seed=seed, **row)
                writer.write(out)
                by_stage[stage_name].append(out)

                print(f"[{run_idx}/{total}] {stage_name:20s} seed={seed:2d} -> "
                      f"delivered={row['delivered_gbit']:6.2f}Gb "
                      f"completion={row['completion_rate']*100:5.1f}% "
                      f"proactive_ho={row['proactive_handovers']:2.0f} "
                      f"interrupted={row['sessions_interrupted']:2.0f} "
                      f"({row['wall_time_s']:.2f}s)")
    finally:
        writer.close()

    elapsed = time.time() - t_start
    print("=" * 88)
    print(f"Done: {run_idx} runs in {elapsed/60:.1f} min. Results -> {RESULTS_PATH}")

    _write_stats([s for s, _ in stages], by_stage)
    print(f"Stage-transition stats -> {STATS_PATH}")
    print("=" * 88)


def _write_stats(stage_order: list, by_stage: dict) -> None:
    import csv as csv_mod
    import statistics
    try:
        from scipy import stats as scipy_stats
        have_scipy = True
    except ImportError:
        have_scipy = False

    def ci95(xs):
        if len(xs) < 2:
            return (xs[0], xs[0]) if xs else (0.0, 0.0)
        m = statistics.mean(xs)
        se = statistics.stdev(xs) / math.sqrt(len(xs))
        return (m - 1.96 * se, m + 1.96 * se)

    fieldnames = ["stage", "n_seeds", "delivered_gbit_mean", "delivered_gbit_std",
                 "delivered_gbit_ci95_lo", "delivered_gbit_ci95_hi",
                 "completion_rate_mean", "completion_rate_std",
                 "vs_previous_stage", "delivered_gbit_pvalue_ttest_rel"]
    with open(STATS_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv_mod.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        prev_delivered = None
        prev_name = None
        for name in stage_order:
            rows = by_stage[name]
            delivered = [r["delivered_gbit"] for r in rows]
            completion = [r["completion_rate"] for r in rows]
            lo, hi = ci95(delivered)
            pval = ""
            if have_scipy and prev_delivered is not None and len(prev_delivered) == len(delivered) and len(delivered) > 1:
                try:
                    pval = float(scipy_stats.ttest_rel(delivered, prev_delivered).pvalue)
                except Exception:
                    pval = ""
            w.writerow({
                "stage": name, "n_seeds": len(rows),
                "delivered_gbit_mean": statistics.mean(delivered),
                "delivered_gbit_std": statistics.stdev(delivered) if len(delivered) > 1 else 0.0,
                "delivered_gbit_ci95_lo": lo, "delivered_gbit_ci95_hi": hi,
                "completion_rate_mean": statistics.mean(completion),
                "completion_rate_std": statistics.stdev(completion) if len(completion) > 1 else 0.0,
                "vs_previous_stage": prev_name or "",
                "delivered_gbit_pvalue_ttest_rel": pval,
            })
            prev_delivered, prev_name = delivered, name


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exp6: ablation + multi-seed robustness.")
    ap.add_argument("--smoke", action="store_true", help="tiny grid for a fast sanity check")
    args = ap.parse_args()
    run_sweep(smoke=args.smoke)
