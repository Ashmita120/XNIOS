"""What actually makes X-NioS deliver more data — measured, in priority order.

The question this answers is not "which scheduler is cleverest" but "where is the
throughput going, and which knob gets it back". It runs three sections:

  budget   Can the controller decide in time? Per-step wall cost and decision
           latency (mean / p50 / p99 / max) against the control interval.

  policy   Every scheduler against the offline MILP oracle, at several contention
           levels. Reports % of optimal, not just Gbit, so a tie at 99% is
           visibly a tie rather than a mystery.

  levers   One-factor-at-a-time sweeps of the things that are NOT the scheduler:
           the phased-array scan envelope, beams per station, satellite
           bandwidth, proactive handover, power allocation. Ranked by measured
           delta so the biggest win is at the top.

The sections are deliberately in that order: there is no point tuning a policy
that runs in 0.4 ms inside a 10 s budget until you know the policy is what is
limiting you. On the shipped India/global presets it is not — see the levers
table, which is where the double-digit numbers live.

Run:
    python experiments/realtime_benchmark.py                # all three
    python experiments/realtime_benchmark.py --section levers
    python experiments/realtime_benchmark.py --smoke        # fast sanity pass
"""

from __future__ import annotations

import argparse
import copy
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_benchmark import build_config, SCENARIO_PROFILES

from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler
from xnios.oracle import optimal_throughput
from xnios.simulator import Simulator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(ROOT, "experiments", "results")

FREQ_ALLOCATOR = "coloring"
BW_ALLOCATOR = "equal"
PW_ALLOCATOR = "adaptive"

POLICIES = [
    "fcfs/strongest", "ljf/strongest", "sjf/strongest", "edf/strongest",
    "priority/strongest", "priority/least_loaded",
    "hungarian/throughput", "mip",
    "horizon/throughput", "horizon/urgency", "horizon/sla",
]


def _profile(name: str) -> dict:
    return [p for p in SCENARIO_PROFILES if p["name"] == name][0]


def _run(cfg: dict, policy: str, bw: str = BW_ALLOCATOR, pw: str = PW_ALLOCATOR):
    """One simulation. Returns (summary, wall_seconds, scheduler)."""
    scn = scenario_from_config(cfg)
    sim_cfg = sim_config_from_config(cfg)
    sched = make_scheduler(policy)
    t0 = time.perf_counter()
    res = Simulator(scn, sched, sim_cfg,
                    allocator=make_allocator(bw),
                    power_allocator=make_power_allocator(pw),
                    freq_allocator=make_freq_allocator(FREQ_ALLOCATOR)).run()
    return res.summary, time.perf_counter() - t0, sched


def _write_csv(path: str, rows: list) -> None:
    if not rows:
        return
    os.makedirs(RESULTS_DIR, exist_ok=True)
    keys = list({k: None for r in rows for k in r}.keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  -> {path}")


def _rule(title: str) -> None:
    print()
    print("=" * 96)
    print(title)
    print("=" * 96)


# --------------------------------------------------------------------------- #
# 1. real-time budget
# --------------------------------------------------------------------------- #
def section_budget(smoke: bool) -> list:
    """Is the control loop fast enough? Decision latency vs the interval it has."""
    _rule("1. REAL-TIME BUDGET - can the controller answer inside its control interval?")
    cases = [("india8", "baseline"), ("india8", "congested"), ("global6", "stress_all")]
    policies = ["fcfs/strongest", "hungarian/throughput", "horizon/urgency"]
    if smoke:
        cases, policies = cases[:1], policies[:2]

    hdr = (f"{'network':9s} {'profile':11s} {'policy':21s} {'sats':>5s} "
           f"{'step ms':>8s} {'mean':>7s} {'p50':>7s} {'p99':>7s} {'max':>7s} "
           f"{'setup ms':>9s} {'budget':>8s} {'headroom':>9s}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for net, pname in cases:
        cfg, n_sats = build_config(net, _profile(pname), 0)
        sim_cfg = sim_config_from_config(cfg)
        steps = int(sim_cfg.duration_s / sim_cfg.dt_s)
        budget_ms = sim_cfg.decision_interval_s * 1e3
        for policy in policies:
            s, wall, sched = _run(cfg, policy)
            step_ms = wall / steps * 1e3
            setup_ms = getattr(getattr(sched, "look", None), "build_ms", 0.0)
            head = budget_ms / max(s["max_decision_ms"], 1e-6)
            print(f"{net:9s} {pname:11s} {policy:21s} {n_sats:5d} "
                  f"{step_ms:8.2f} {s['mean_decision_ms']:7.3f} {s['p50_decision_ms']:7.3f} "
                  f"{s['p99_decision_ms']:7.3f} {s['max_decision_ms']:7.3f} "
                  f"{setup_ms:9.1f} {budget_ms:7.0f}ms {head:8.0f}x")
            rows.append(dict(network=net, profile=pname, policy=policy, n_sats=n_sats,
                             step_ms=step_ms, mean_decision_ms=s["mean_decision_ms"],
                             p50_decision_ms=s["p50_decision_ms"],
                             p99_decision_ms=s["p99_decision_ms"],
                             max_decision_ms=s["max_decision_ms"],
                             setup_ms=setup_ms, budget_ms=budget_ms, headroom_x=head))

    print("\n  step ms  = whole simulation step (physics + allocation + decide).")
    print("  setup ms = one-time cost at bind (horizon/* precomputes its contact windows).")
    print("  headroom = control interval / worst observed decision.")
    _write_csv(os.path.join(RESULTS_DIR, "realtime_budget.csv"), rows)
    return rows


# --------------------------------------------------------------------------- #
# 2. policy vs oracle
# --------------------------------------------------------------------------- #
def section_policy(smoke: bool) -> list:
    """Every scheduler against the MILP ceiling, at rising contention."""
    _rule("2. POLICY vs ORACLE - how much of the optimum does each scheduler capture?")
    # beams_per_station is the contention knob: the shipped presets ship 4, which
    # is more than the number of satellites usually visible at one station.
    cases = [("india8", "baseline", 4), ("india8", "congested", 4),
             ("india8", "congested", 1), ("global6", "congested", 1)]
    policies = list(POLICIES)
    if smoke:
        cases = cases[:1]
        policies = ["fcfs/strongest", "hungarian/throughput", "horizon/urgency"]

    rows = []
    for net, pname, beams in cases:
        cfg, n_sats = build_config(net, _profile(pname), 0)
        for st in cfg["stations"]:
            st["num_beams"] = beams
        scn = scenario_from_config(cfg)
        sim_cfg = sim_config_from_config(cfg)
        t0 = time.perf_counter()
        oracle = optimal_throughput(scn, sim_cfg.duration_s, slot_s=20.0)
        orc_s = time.perf_counter() - t0

        print(f"\n  {net} / {pname} / {beams} beam(s) per station - {n_sats} satellites, "
              f"{beams * len(cfg['stations'])} beams total")
        print(f"  oracle ceiling {oracle.delivered_gbit:.1f} Gbit  ({orc_s:.1f} s solve)")
        hdr = (f"  {'policy':21s} {'Gbit':>8s} {'%opt':>7s} {'compl':>7s} {'sla':>7s} "
               f"{'fair':>6s} {'util':>6s} {'wait s':>7s} {'p99 ms':>7s}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))

        for policy in policies:
            s, wall, _ = _run(cfg, policy)
            pct = (s["delivered_gbit"] / oracle.delivered_gbit * 100
                   if oracle.delivered_gbit > 0 else float("nan"))
            print(f"  {policy:21s} {s['delivered_gbit']:8.1f} {pct:6.1f}% "
                  f"{s['completion_rate']*100:6.1f}% {s['sla_compliance']*100:6.1f}% "
                  f"{s['fairness']:6.3f} {s['beam_utilization']*100:5.1f}% "
                  f"{s['mean_wait_s']:7.0f} {s['p99_decision_ms']:7.3f}")
            rows.append(dict(network=net, profile=pname, beams_per_station=beams,
                             n_sats=n_sats, policy=policy,
                             oracle_gbit=oracle.delivered_gbit, pct_optimal=pct,
                             wall_s=wall, **s))

    _write_csv(os.path.join(RESULTS_DIR, "realtime_policy.csv"), rows)
    return rows


# --------------------------------------------------------------------------- #
# 3. levers
# --------------------------------------------------------------------------- #
def _set_station(cfg: dict, key: str, value) -> dict:
    out = copy.deepcopy(cfg)
    for st in out["stations"]:
        st[key] = value
    return out


def _set_sat(cfg: dict, key: str, value) -> dict:
    out = copy.deepcopy(cfg)
    out["satellites"][key] = value
    return out


def _set_sim(cfg: dict, **kw) -> dict:
    out = copy.deepcopy(cfg)
    out["sim"].update(kw)
    return out


def section_levers(smoke: bool) -> list:
    """One factor at a time, against a fixed baseline config and policy.

    The policy is held at fcfs/strongest on purpose. Section 2 shows the policy
    choice is worth a fraction of a percent here, so holding it fixed isolates
    the factor under test instead of confounding it.
    """
    _rule("3. LEVERS - one factor at a time, ranked by measured effect on delivered data")
    net, pname, policy = "india8", "baseline", "fcfs/strongest"
    base_cfg, n_sats = build_config(net, _profile(pname), 0)

    base, _, _ = _run(base_cfg, policy)
    print(f"  baseline: {net} / {pname} / {policy} / {n_sats} sats "
          f"-> {base['delivered_gbit']:.1f} Gbit, "
          f"completion {base['completion_rate']*100:.1f}%, "
          f"SLA {base['sla_compliance']*100:.1f}%, "
          f"beam util {base['beam_utilization']*100:.1f}%\n")

    variants = [
        # (family, label, config)
        ("scan envelope", "max_scan 65 deg", _set_station(base_cfg, "max_scan_deg", 65)),
        ("scan envelope", "max_scan 70 deg", _set_station(base_cfg, "max_scan_deg", 70)),
        ("scan envelope", "max_scan 75 deg", _set_station(base_cfg, "max_scan_deg", 75)),
        ("scan envelope", "max_scan 80 deg", _set_station(base_cfg, "max_scan_deg", 80)),
        ("beams",         "1 beam/station",  _set_station(base_cfg, "num_beams", 1)),
        ("beams",         "2 beams/station", _set_station(base_cfg, "num_beams", 2)),
        ("beams",         "8 beams/station", _set_station(base_cfg, "num_beams", 8)),
        ("sat bandwidth", "100 MHz",         _set_sat(base_cfg, "bandwidth_mhz", 100)),
        ("sat bandwidth", "200 MHz",         _set_sat(base_cfg, "bandwidth_mhz", 200)),
        ("sat power",     "tx 10 W",         _set_sat(base_cfg, "tx_power_w", 10)),
        ("handover",      "elevation mode",  _set_sim(base_cfg, handover=True,
                                                      handover_mode="elevation",
                                                      handover_lead_s=40.0)),
        ("handover",      "forecast mode",   _set_sim(base_cfg, handover=True,
                                                      handover_mode="forecast",
                                                      handover_lead_s=40.0)),
        ("station G/T",   "+3 dB",           _set_station(base_cfg, "g_over_t_dbk", 27)),
        ("policy",        "horizon/urgency", base_cfg),
    ]
    if smoke:
        variants = [v for v in variants if v[0] in ("scan envelope", "policy")][:4]

    rows = []
    for family, label, cfg in variants:
        pol = "horizon/urgency" if family == "policy" else policy
        s, wall, _ = _run(cfg, pol)
        d = s["delivered_gbit"]
        rows.append(dict(family=family, lever=label, policy=pol,
                         delivered_gbit=d,
                         delta_pct=(d / base["delivered_gbit"] - 1) * 100,
                         completion_rate=s["completion_rate"],
                         sla_compliance=s["sla_compliance"],
                         beam_utilization=s["beam_utilization"],
                         fairness=s["fairness"], wall_s=wall))

    rows.sort(key=lambda r: -r["delta_pct"])
    hdr = (f"  {'lever':34s} {'Gbit':>8s} {'delta':>8s} {'compl':>7s} {'sla':>7s} {'util':>7s}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        print(f"  {r['family'] + ': ' + r['lever']:34s} {r['delivered_gbit']:8.1f} "
              f"{r['delta_pct']:+7.1f}% {r['completion_rate']*100:6.1f}% "
              f"{r['sla_compliance']*100:6.1f}% {r['beam_utilization']*100:6.1f}%")

    _write_csv(os.path.join(RESULTS_DIR, "realtime_levers.csv"),
               [dict(baseline_gbit=base["delivered_gbit"], **r) for r in rows])
    return rows


SECTIONS = {"budget": section_budget, "policy": section_policy, "levers": section_levers}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--section", choices=list(SECTIONS) + ["all"], default="all")
    ap.add_argument("--smoke", action="store_true", help="tiny grid, fast sanity check")
    args = ap.parse_args()

    t0 = time.time()
    names = list(SECTIONS) if args.section == "all" else [args.section]
    for name in names:
        SECTIONS[name](args.smoke)
    print(f"\nDone in {time.time() - t0:.0f} s.")


if __name__ == "__main__":
    main()
