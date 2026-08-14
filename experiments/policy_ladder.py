"""How much of the oracle gap does each ordering rule close, and how cheaply?

The positive control established a real decision gap (11.5 pp in A-slack) that a
hand-written (deadline, tier, volume) ordering does not close. Before reaching
for a solver, this asks the cheaper question: is the gap a *hard* combinatorial
problem, or just a poorly chosen sort key?

The ladder, in order of what each rule is allowed to know:

  fcfs            arrival order — today's planner
  edf             earliest deadline first
  tier-deadline   tier, then deadline, then volume
  deadline-tier   deadline, then tier, then volume
  density         weight / volume, largest first — the weighted-knapsack ratio.
                  Objective is weighted COMPLETION, so a small emergency job is
                  worth more per beam-second than a huge research one, and no
                  ordering keyed on deadline alone can see that.
  slack           DYNAMIC: least (deadline - earliest completion) first, re-quoted
                  against the live ledger after every booking
  ratio           DYNAMIC: oppcost / volume — adds the cost side of the knapsack
                  ratio, which is oppcost's one identified failure mode
  w_avail         DYNAMIC: weight / capacity-still-available
  oppcost         DYNAMIC: weight x (volume / capacity still available to this
                  request before its own deadline). Highest first — the request
                  about to lose its opportunity, scaled by what it is worth.
                  Uses the exact future capacity the lookahead already has.
  oracle/*        the MILP reference, both objectives

Static rules sort once. Dynamic rules re-evaluate every remaining request against
the current ledger after each booking, which costs O(n^2) quotes — worth
measuring, since planning latency is an operational metric and a rule that needs
196 quotes to beat a sort is not obviously cheaper than a solver.

`gap closed` is the headline: (policy - fcfs) / (oracle - fcfs). 100% means the
rule is as good as optimal; 0% means it adds nothing over arrival order.

Run:  python experiments/policy_ladder.py --worlds 5
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from multirequest_control import (HORIZON_S, REGIMES, RESULTS_DIR, SLOT_S,
                                  TIER_NAMES, build_requests, build_world,
                                  network_capacity_gbit, _score)

from xnios.config import scenario_from_config
from xnios.planner import Planner, Customer, CommRequest, TimingIntent
from xnios.request_oracle import optimal_allocation

BIG_GBIT = 1.0e6            # "quote me everything you have" probe


def _planner(cfg: dict) -> Planner:
    scn = scenario_from_config(cfg)
    p = Planner(scn, t0=0.0, horizon_s=HORIZON_S + 1800.0)
    for tier in TIER_NAMES:
        p.register_customer(Customer(f"CUST-{tier}", tier=tier))
    return p


def _quote(planner: Planner, r: dict, volume: float | None = None):
    return planner.plan(CommRequest(
        satellite_id=r["satellite_id"],
        data_volume_gbit=volume if volume is not None else r["volume_gbit"],
        customer_id=f"CUST-{r['tier']}", timing=TimingIntent.BY_DEADLINE,
        deadline_s=r["deadline_s"]), t_now=0.0)


# --------------------------------------------------------------------------- #
# static orderings
# --------------------------------------------------------------------------- #
STATIC = {
    "fcfs":          lambda r: (r["arrival"],),
    "edf":           lambda r: (r["deadline_s"], r["arrival"]),
    "tier-deadline": lambda r: (-r["weight"], r["deadline_s"], r["volume_gbit"]),
    "deadline-tier": lambda r: (r["deadline_s"], -r["weight"], r["volume_gbit"]),
    "density":       lambda r: (-r["weight"] / max(r["volume_gbit"], 1e-9),
                                r["deadline_s"]),
}


def run_static(cfg: dict, reqs: list, key) -> dict:
    planner = _planner(cfg)
    delivered, met = {}, {}
    n_quotes = 0
    t0 = time.perf_counter()
    for r in sorted(reqs, key=key):
        plan = _quote(planner, r)
        n_quotes += 1
        planner.accept(plan, allow_partial=True)
        delivered[r["request_id"]] = plan.scheduled_gbit
        met[r["request_id"]] = plan.shortfall_gbit <= 1e-6
    row = _score(reqs, delivered, met, wall_s=time.perf_counter() - t0)
    row["n_quotes"] = n_quotes
    return row


# --------------------------------------------------------------------------- #
# dynamic orderings
# --------------------------------------------------------------------------- #
def _score_slack(planner: Planner, r: dict, quote) -> tuple:
    """Least remaining slack first; anything that can no longer complete goes last."""
    if quote.shortfall_gbit > 1e-6 or quote.completes_at_s is None:
        return (1, 0.0)
    return (0, r["deadline_s"] - quote.completes_at_s)


def _score_oppcost(planner: Planner, r: dict, quote) -> tuple:
    """Weighted share of the opportunity this request still has.

    `avail` is what the network would give this request if it asked for
    everything — the capacity its satellite can still reach before its own
    deadline, after existing bookings. volume/avail near 1 means the chance is
    about to disappear; scaled by tier so a scarce emergency outranks a scarce
    research job. Negated so the sort is ascending like the others.
    """
    if quote.shortfall_gbit > 1e-6:
        return (1, 0.0)
    avail = _quote(planner, r, BIG_GBIT).scheduled_gbit
    ratio = r["volume_gbit"] / max(avail, 1e-9)
    return (0, -(r["weight"] * min(ratio, 1.0)))


def _score_ratio(planner: Planner, r: dict, quote) -> tuple:
    """oppcost divided by volume — value x risk per Gbit consumed.

    The obvious fix for oppcost's one failure, where it books a large request
    and thereby displaces two smaller ones of equal weight: a greedy knapsack
    rule should divide value by cost. It reorders correctly and still loses,
    which is the point of keeping it here. See `_score_w_avail`.
    """
    if quote.shortfall_gbit > 1e-6:
        return (1, 0.0)
    avail = _quote(planner, r, BIG_GBIT).scheduled_gbit
    risk = min(r["volume_gbit"] / max(avail, 1e-9), 1.0)
    return (0, -(r["weight"] * risk / max(r["volume_gbit"], 1e-9)))


def _score_w_avail(planner: Planner, r: dict, quote) -> tuple:
    """weight / capacity-still-available — value per Gbit of opportunity spent."""
    if quote.shortfall_gbit > 1e-6:
        return (1, 0.0)
    avail = _quote(planner, r, BIG_GBIT).scheduled_gbit
    return (0, -(r["weight"] / max(avail, 1e-9)))


DYNAMIC = {"slack": _score_slack, "oppcost": _score_oppcost,
           "ratio": _score_ratio, "w_avail": _score_w_avail}


def run_dynamic(cfg: dict, reqs: list, score_fn) -> dict:
    """Re-quote every remaining request after each booking, then take the best."""
    planner = _planner(cfg)
    delivered, met = {}, {}
    remaining = list(reqs)
    n_quotes = 0
    t0 = time.perf_counter()
    while remaining:
        scored = []
        for r in remaining:
            q = _quote(planner, r)
            n_quotes += 1
            key = score_fn(planner, r, q)
            if score_fn is _score_oppcost and key[0] == 0:
                n_quotes += 1               # the probe quote inside the scorer
            scored.append((key, r["arrival"], r, q))
        scored.sort(key=lambda x: (x[0], x[1]))
        _key, _arr, r, plan = scored[0]
        planner.accept(plan, allow_partial=True)
        delivered[r["request_id"]] = plan.scheduled_gbit
        met[r["request_id"]] = plan.shortfall_gbit <= 1e-6
        remaining.remove(r)
    row = _score(reqs, delivered, met, wall_s=time.perf_counter() - t0)
    row["n_quotes"] = n_quotes
    return row


def run_oracle(cfg: dict, reqs: list, objective: str, time_limit_s: float) -> dict:
    planner = _planner(cfg)
    res = optimal_allocation(planner.look, planner.stations, reqs, t_now=0.0,
                             slot_s=SLOT_S, objective=objective,
                             time_limit_s=time_limit_s)
    row = _score(reqs, res.delivered, res.met, wall_s=res.solve_ms / 1e3)
    row["optimal"] = res.optimal
    row["n_quotes"] = 0
    return row


LADDER = list(STATIC) + list(DYNAMIC)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worlds", type=int, default=5)
    ap.add_argument("--time-limit", type=float, default=60.0)
    args = ap.parse_args()

    rows = []
    t_start = time.time()
    for w in range(args.worlds):
        cfg = build_world(w)
        probe = _planner(cfg)
        cap = network_capacity_gbit(probe, list(probe.sats))
        for rname, mult, dmode in REGIMES:
            reqs = build_requests(probe, w, cap * mult, dmode)
            if not reqs:
                continue
            for pname in LADDER:
                fn = (run_static(cfg, reqs, STATIC[pname]) if pname in STATIC
                      else run_dynamic(cfg, reqs, DYNAMIC[pname]))
                rows.append(dict(world=w, regime=rname, policy=pname, **fn))
            for oname, obj in [("oracle/throughput", "throughput"),
                               ("oracle/priority", "priority")]:
                rows.append(dict(world=w, regime=rname, policy=oname,
                                 **run_oracle(cfg, reqs, obj, args.time_limit)))
        print(f"  world {w} done ({time.time() - t_start:.0f}s)")

    _report(rows, args.worlds)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, "policy_ladder.csv")
    keys = list({k: None for r in rows for k in r}.keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        wc = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        wc.writeheader()
        wc.writerows(rows)
    print(f"\n  -> {path}")
    print(f"Done in {time.time() - t_start:.0f} s.")


def _report(rows: list, n_worlds: int) -> None:
    print()
    print("=" * 104)
    print("POLICY LADDER - weighted completion, and the share of the fcfs->oracle gap each rule closes")
    print("=" * 104)
    for rname, _m, _d in REGIMES:
        sub = [x for x in rows if x["regime"] == rname]
        if not sub:
            continue

        def mean(p, k="weighted_met"):
            v = [x[k] for x in sub if x["policy"] == p]
            return float(np.mean(v)) if v else float("nan")

        base, best = mean("fcfs"), mean("oracle/priority")
        span = best - base
        print(f"\n  regime {rname}   fcfs {base * 100:.1f}%  ->  oracle {best * 100:.1f}%  "
              f"(gap {span * 100:.1f} pp)")
        hdr = (f"    {'policy':16s} {'wtd met':>8s} {'gap closed':>11s} {'complete':>9s} "
               f"{'Gbit':>8s} {'fair':>6s} {'quotes':>7s} {'ms':>7s} {'worlds better':>14s}")
        print(hdr)
        print("    " + "-" * (len(hdr) - 4))
        for p in LADDER + ["oracle/throughput", "oracle/priority"]:
            ps = [x for x in sub if x["policy"] == p]
            if not ps:
                continue
            m = mean(p)
            closed = (m - base) / span * 100 if abs(span) > 1e-9 else float("nan")
            # paired: in how many worlds does this rule beat fcfs?
            better = 0
            for wd in {x["world"] for x in sub}:
                f = next((x for x in sub if x["world"] == wd and x["policy"] == "fcfs"), None)
                c = next((x for x in sub if x["world"] == wd and x["policy"] == p), None)
                if f and c and c["weighted_met"] > f["weighted_met"] + 1e-9:
                    better += 1
            worse = 0
            for wd in {x["world"] for x in sub}:
                f = next((x for x in sub if x["world"] == wd and x["policy"] == "fcfs"), None)
                c = next((x for x in sub if x["world"] == wd and x["policy"] == p), None)
                if f and c and c["weighted_met"] < f["weighted_met"] - 1e-9:
                    worse += 1
            print(f"    {p:16s} {m * 100:7.1f}% {closed:10.0f}% "
                  f"{mean(p, 'completion_rate') * 100:8.1f}% "
                  f"{mean(p, 'delivered_gbit'):8.1f} {mean(p, 'fairness'):6.3f} "
                  f"{mean(p, 'n_quotes'):7.0f} {mean(p, 'wall_s') * 1e3:7.1f} "
                  f"{f'{better} up / {worse} down':>14s}")


if __name__ == "__main__":
    main()
