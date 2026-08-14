"""Does priority-aware allocation actually decide anything? A positive control.

The planner resolves a request's tier and reports it, then allocates strictly
first-come-first-served. Before building a joint optimiser to fix that, this
establishes whether there is anything to fix — the same discipline that turned
three earlier scheduler studies into honest null results.

Three policies over identical worlds:

    fcfs        requests booked in arrival order (today's behaviour)
    priority    booked by (deadline, -tier, volume) — earliest deadline first,
                ties to the higher tier, then the smaller job
    oracle      the MILP in xnios.request_oracle, solved twice: once for
                throughput and once for weighted completion

Three contention regimes, calibrated rather than guessed. Total network capacity
over the horizon is measured analytically first, then demand is set to a
multiple of it:

    A  slack     demand ~0.5x capacity   everything should fit
    B  real      demand ~1.5x capacity   the positive control
    C  severe    demand ~3.0x capacity   does ordering still matter under stress?

Regime A is the experiment's own control: if the policies differ there, the
harness is manufacturing differences and nothing downstream can be trusted.

PAIRED WORLDS. Each world randomises orbital phasing, request volumes,
deadlines and tiers, and every policy runs against that same world — never
"fcfs on seed 1, oracle on seed 2". Differences are then within-world, and the
spread across worlds says whether they generalise.

Run:  python experiments/multirequest_control.py
      python experiments/multirequest_control.py --worlds 3 --smoke
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_benchmark import INDIA8, _phased_station

from xnios.config import scenario_from_config
from xnios.entities import TIERS
from xnios.planner import Planner, Customer, CommRequest, TimingIntent
from xnios.request_oracle import optimal_allocation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(ROOT, "experiments", "results")

HORIZON_S = 3600.0          # one hour of planning
SLOT_S = 30.0               # oracle time discretisation
# 36 satellites so that ~24 are reachable in the horizon — comfortably more than
# N_REQUESTS. With fewer, two requests share a satellite and conflict over its
# single link even when aggregate demand is low, which stops regime A from being
# the control it is supposed to be.
N_SATS = 36
N_REQUESTS = 14
BEAMS_PER_STATION = 1       # the contention knob: one link per aperture

# (name, demand as a multiple of measured capacity, deadline mode)
#
# "0-trivial" exists to validate the harness itself: tiny demand AND deadlines at
# the end of the horizon, so nothing competes for anything. Every policy must
# score ~100% there. Calibrating demand against aggregate capacity alone is not
# enough to guarantee slack, because tier-scaled deadlines concentrate demand
# into the early part of the horizon -- which is exactly why A-slack still shows
# a gap and cannot serve as the control on its own.
REGIMES = [("0-trivial", 0.15, "loose"), ("A-slack", 0.5, "tiered"),
           ("B-real", 1.5, "tiered"), ("C-severe", 3.0, "tiered")]
TIER_NAMES = ["research", "commercial", "military", "emergency"]


# --------------------------------------------------------------------------- #
# world construction
# --------------------------------------------------------------------------- #
def build_world(seed: int) -> dict:
    """A network + satellites. Orbital phasing is randomised per world."""
    rng = random.Random(1000 + seed)
    stations = [_phased_station(s) for s in INDIA8[:4]]
    for st in stations:
        st["num_beams"] = BEAMS_PER_STATION
    return {
        "name": f"world-{seed}",
        "seed": seed,
        "sim": {"duration_s": HORIZON_S, "dt_s": 10.0, "decision_interval_s": 10.0},
        "stations": stations,
        "satellites": {
            "mode": "generate", "count": N_SATS,
            "planes": [
                {"inc": 53.0, "raan": rng.uniform(255.0, 300.0), "altitude_km": 600.0},
                {"inc": 53.0, "raan": rng.uniform(255.0, 300.0), "altitude_km": 600.0},
            ],
            "arg_lat_spread_deg": 360.0,
            "freq_ghz": 8.2, "bandwidth_mhz": 50, "tx_power_w": 5,
            "backlog_gbit": {"classes": [1], "weights": [1]},   # demand comes from requests
        },
    }


def network_capacity_gbit(planner: Planner, sat_ids, t_now: float = 0.0) -> float:
    """Deliverable data in the horizon, capped by beam-seconds.

    Summing every pass would double-count moments when one station sees several
    satellites but has one beam. Sweep the horizon instead and take, per slot,
    the best `num_beams` links each station could carry.
    """
    stations = planner.stations
    total = 0.0
    n = int(np.ceil(HORIZON_S / SLOT_S))
    for s in range(n):
        a, b = t_now + s * SLOT_S, t_now + (s + 1) * SLOT_S
        for gid, g in stations.items():
            offers = []
            for sid in sat_ids:
                bits = sum(p.bits_until(a, b)
                           for p in planner.look.passes.get((sid, gid), ())
                           if p.t_set > a and p.t_rise < b)
                if bits > 0:
                    offers.append(bits)
            offers.sort(reverse=True)
            total += sum(offers[:g.num_beams])
    return total / 1e9


def _solo_capacity(planner: Planner, sat_id: str, deadline_s: float) -> float:
    """Gbit this satellite could deliver by `deadline_s` with the whole network
    to itself — the ceiling for a request in isolation."""
    total = 0.0
    for gid in planner.stations:
        for p in planner.look.passes.get((sat_id, gid), ()):
            if p.t_rise >= deadline_s:
                break
            total += p.bits_until(0.0, deadline_s)
    return total / 1e9


def build_requests(planner: Planner, seed: int, demand_gbit: float,
                   deadline_mode: str = "tiered") -> list:
    """`N_REQUESTS` jobs, EVERY ONE FEASIBLE ON ITS OWN, summing toward `demand_gbit`.

    Feasibility in isolation is the point. If a request's deadline falls before
    its satellite's first pass, every policy refuses it identically and the
    comparison is diluted by ties nobody had a choice about — worse, regime A
    then shows differences and stops being a control. So each volume is capped
    at 80% of what that satellite could deliver alone by its own deadline, and
    any failure observed later is caused by contention, which is the thing under
    study.

    The regime multiplier therefore acts on *aggregate* demand while per-request
    feasibility is preserved: at high multipliers volumes clamp and the surplus
    shows up as more requests chasing the same passes.
    """
    rng = random.Random(7000 + seed)
    reachable = [sid for sid in planner.sats
                 if any(p.t_rise < HORIZON_S
                        for p in planner.look.by_sat.get(sid, ()))]
    rng.shuffle(reachable)
    if not reachable:
        return []
    # one satellite per request where possible, so regime A isolates aggregate
    # scarcity from satellite-level conflict
    distinct = len(reachable) >= N_REQUESTS

    shares = np.array([rng.uniform(0.5, 2.0) for _ in range(N_REQUESTS)])
    shares = shares / shares.sum() * demand_gbit

    out = []
    for i in range(N_REQUESTS):
        tier = rng.choices(TIER_NAMES, weights=[0.35, 0.35, 0.20, 0.10])[0]
        sat = reachable[i] if distinct else reachable[i % len(reachable)]
        # tighter deadlines for higher tiers, which is what makes them compete
        frac = ({"research": (0.6, 1.0), "commercial": (0.45, 0.9),
                 "military": (0.3, 0.7), "emergency": (0.2, 0.5)}[tier]
                if deadline_mode == "tiered" else (0.9, 1.0))
        deadline = float(HORIZON_S * rng.uniform(*frac))
        solo = _solo_capacity(planner, sat, deadline)
        if solo <= 1e-6:
            continue                       # no contact before this deadline at all
        volume = float(min(shares[i], 0.8 * solo))
        if volume < 0.5:
            continue
        out.append({
            "request_id": f"REQ-{i:02d}",
            "satellite_id": sat,
            "volume_gbit": volume,
            "deadline_s": deadline,
            "tier": tier,
            "weight": float(TIERS[tier]),
            "solo_capacity_gbit": solo,
            "arrival": i,                     # FCFS order
        })
    return out


# --------------------------------------------------------------------------- #
# policies
# --------------------------------------------------------------------------- #
def order_fcfs(reqs: list) -> list:
    return sorted(reqs, key=lambda r: r["arrival"])


def order_priority(reqs: list) -> list:
    """Earliest deadline, then higher tier, then the smaller job."""
    return sorted(reqs, key=lambda r: (r["deadline_s"], -r["weight"], r["volume_gbit"]))


def run_policy(cfg: dict, reqs: list, order_fn) -> dict:
    """Book every request through the planner in the policy's order."""
    scn = scenario_from_config(cfg)
    planner = Planner(scn, t0=0.0, horizon_s=HORIZON_S + 1800.0)
    for tier in TIER_NAMES:
        planner.register_customer(Customer(f"CUST-{tier}", tier=tier))

    delivered, met = {}, {}
    t0 = time.perf_counter()
    for r in order_fn(reqs):
        plan = planner.plan(CommRequest(
            satellite_id=r["satellite_id"], data_volume_gbit=r["volume_gbit"],
            customer_id=f"CUST-{r['tier']}", timing=TimingIntent.BY_DEADLINE,
            deadline_s=r["deadline_s"]), t_now=0.0)
        planner.accept(plan, allow_partial=True)     # under contention, take what fits
        delivered[r["request_id"]] = plan.scheduled_gbit
        met[r["request_id"]] = plan.shortfall_gbit <= 1e-6
    wall = time.perf_counter() - t0
    return _score(reqs, delivered, met, wall_s=wall)


def _score(reqs: list, delivered: dict, met: dict, wall_s: float = 0.0) -> dict:
    w_total = sum(r["weight"] for r in reqs) or 1.0
    fracs = [min(1.0, delivered[r["request_id"]] / r["volume_gbit"]) for r in reqs]
    s, s2 = sum(fracs), sum(f * f for f in fracs)
    return {
        "delivered_gbit": sum(delivered.values()),
        "completion_rate": sum(met.values()) / len(reqs),
        "weighted_met": sum(r["weight"] for r in reqs if met[r["request_id"]]) / w_total,
        "n_met": sum(met.values()),
        "n_partial": sum(1 for r in reqs
                         if not met[r["request_id"]] and delivered[r["request_id"]] > 1e-6),
        "n_rejected": sum(1 for r in reqs if delivered[r["request_id"]] <= 1e-6),
        "fairness": (s * s) / (len(fracs) * s2) if s2 > 0 else 1.0,
        "wall_s": wall_s,
    }


def run_oracle(cfg: dict, reqs: list, objective: str, time_limit_s: float) -> dict:
    scn = scenario_from_config(cfg)
    planner = Planner(scn, t0=0.0, horizon_s=HORIZON_S + 1800.0)
    res = optimal_allocation(planner.look, planner.stations, reqs, t_now=0.0,
                             slot_s=SLOT_S, objective=objective,
                             time_limit_s=time_limit_s)
    row = _score(reqs, res.delivered, res.met, wall_s=res.solve_ms / 1e3)
    row["optimal"] = res.optimal
    row["n_vars"] = res.n_vars
    return row


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #
POLICIES = [("fcfs", order_fcfs), ("priority", order_priority)]
ORACLES = [("oracle/throughput", "throughput"), ("oracle/priority", "priority")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worlds", type=int, default=5)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--time-limit", type=float, default=60.0)
    args = ap.parse_args()
    worlds = 2 if args.smoke else args.worlds
    regimes = REGIMES[:2] if args.smoke else REGIMES

    rows = []
    t_start = time.time()
    for w in range(worlds):
        cfg = build_world(w)
        scn = scenario_from_config(cfg)
        probe = Planner(scn, t0=0.0, horizon_s=HORIZON_S + 1800.0)
        cap = network_capacity_gbit(probe, list(probe.sats))

        print()
        print("=" * 100)
        print(f"WORLD {w} — {len(scn.stations)} stations x {BEAMS_PER_STATION} beam, "
              f"{N_SATS} satellites, {HORIZON_S / 60:.0f} min horizon")
        print(f"  measured network capacity: {cap:.1f} Gbit")
        print("=" * 100)

        for rname, mult, dmode in regimes:
            reqs = build_requests(probe, w, cap * mult, dmode)
            if not reqs:
                print(f"  {rname}: no reachable satellite, skipped")
                continue
            demand = sum(r["volume_gbit"] for r in reqs)
            print(f"\n  regime {rname}  demand {demand:.1f} Gbit "
                  f"({demand / cap:.1f}x capacity), {len(reqs)} requests")
            hdr = (f"    {'policy':18s} {'Gbit':>8s} {'complete':>9s} {'wtd met':>8s} "
                   f"{'met':>4s} {'part':>5s} {'rej':>4s} {'fair':>6s} {'solve s':>8s}")
            print(hdr)
            print("    " + "-" * (len(hdr) - 4))

            for pname, fn in POLICIES:
                r = run_policy(cfg, reqs, fn)
                rows.append(dict(world=w, regime=rname, demand_gbit=demand,
                                 capacity_gbit=cap, policy=pname, **r))
                print(f"    {pname:18s} {r['delivered_gbit']:8.1f} "
                      f"{r['completion_rate'] * 100:8.1f}% {r['weighted_met'] * 100:7.1f}% "
                      f"{r['n_met']:4d} {r['n_partial']:5d} {r['n_rejected']:4d} "
                      f"{r['fairness']:6.3f} {r['wall_s']:8.2f}")

            for oname, obj in ORACLES:
                r = run_oracle(cfg, reqs, obj, args.time_limit)
                rows.append(dict(world=w, regime=rname, demand_gbit=demand,
                                 capacity_gbit=cap, policy=oname, **r))
                star = "" if r.get("optimal") else "  (time limit)"
                print(f"    {oname:18s} {r['delivered_gbit']:8.1f} "
                      f"{r['completion_rate'] * 100:8.1f}% {r['weighted_met'] * 100:7.1f}% "
                      f"{r['n_met']:4d} {r['n_partial']:5d} {r['n_rejected']:4d} "
                      f"{r['fairness']:6.3f} {r['wall_s']:8.2f}{star}")

    _summarise(rows)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, "multirequest_control.csv")
    keys = list({k: None for r in rows for k in r}.keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        wcsv = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        wcsv.writeheader()
        wcsv.writerows(rows)
    print(f"\n  -> {path}")
    print(f"Done in {time.time() - t_start:.0f} s.")


def _summarise(rows: list) -> None:
    """Paired within-world comparison, which is the only one that means anything."""
    print()
    print("=" * 100)
    print("PAIRED SUMMARY — mean across worlds, and priority minus fcfs within each world")
    print("=" * 100)
    regimes = [r for r, _m, _d in REGIMES if any(x["regime"] == r for x in rows)]
    for rname in regimes:
        sub = [x for x in rows if x["regime"] == rname]
        worlds = sorted({x["world"] for x in sub})
        print(f"\n  regime {rname}")
        hdr = (f"    {'policy':18s} {'Gbit':>8s} {'complete':>9s} {'wtd met':>9s} "
               f"{'fair':>7s}")
        print(hdr)
        print("    " + "-" * (len(hdr) - 4))
        for pname in [p for p, _ in POLICIES] + [o for o, _ in ORACLES]:
            ps = [x for x in sub if x["policy"] == pname]
            if not ps:
                continue
            print(f"    {pname:18s} {np.mean([x['delivered_gbit'] for x in ps]):8.1f} "
                  f"{np.mean([x['completion_rate'] for x in ps]) * 100:8.1f}% "
                  f"{np.mean([x['weighted_met'] for x in ps]) * 100:8.1f}% "
                  f"{np.mean([x['fairness'] for x in ps]):7.3f}")

        deltas = []
        for w in worlds:
            f = next((x for x in sub if x["world"] == w and x["policy"] == "fcfs"), None)
            p = next((x for x in sub if x["world"] == w and x["policy"] == "priority"), None)
            if f and p:
                deltas.append((p["weighted_met"] - f["weighted_met"]) * 100)
        if deltas:
            wins = sum(1 for d in deltas if d > 0.5)
            losses = sum(1 for d in deltas if d < -0.5)
            print(f"    priority - fcfs on weighted completion: "
                  f"mean {np.mean(deltas):+.1f} pp, per-world {[f'{d:+.1f}' for d in deltas]}")
            print(f"    priority wins {wins}/{len(deltas)} worlds, loses {losses}")


if __name__ == "__main__":
    main()
