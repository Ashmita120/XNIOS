"""The request -> plan surface, end to end.

Walks the planner through the cases that matter: a plain request, an account
whose tier is looked up rather than typed, a named data object, the three
timing intents, admission control against a filling ledger, and the two ways a
request can fail (nothing in range, and nothing left before the deadline).

Run:  python experiments/planner_demo.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_benchmark import build_config, SCENARIO_PROFILES

from xnios.config import scenario_from_config
from xnios.planner import (Planner, Customer, DataObject, CommRequest, TimingIntent)


def rule(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    profile = [p for p in SCENARIO_PROFILES if p["name"] == "baseline"][0]
    cfg, _ = build_config("india8", profile, 0)
    scn = scenario_from_config(cfg)

    planner = Planner(scn, t0=0.0, horizon_s=86400.0)
    print(f"network: {len(scn.satellites)} satellites, {len(scn.stations)} stations")
    print(f"horizon: {planner.look.stats()['passes']} contacts precomputed in "
          f"{planner.look.build_ms:.0f} ms")

    # configured once, not retyped per job
    planner.register_customer(Customer("CUSTOMER-17", "Orbital Imaging Ltd",
                                       tier="military", sla_availability=0.999))
    planner.register_customer(Customer("CUSTOMER-04", "Research Consortium",
                                       tier="research", sla_availability=0.95))
    # Pick the satellite with the soonest contact, so the demo exercises the
    # near-term paths (TRANSMIT_NOW, deadlines that bite) rather than a bird
    # whose first pass is eleven hours out.
    sat = min((s.id for s in scn.satellites),
              key=lambda sid: (planner.look.next_contact(sid, 0.0) or {}).get("wait_s", 1e18))
    nxt = planner.look.next_contact(sat, 0.0)
    print(f"chosen satellite: {sat}, next contact {nxt['station']} in {nxt['wait_s']:.0f} s")
    planner.register_object(DataObject("IMG-8472", sat, 18.4, "wide-swath imagery"))

    rule("1. Plain request - the operator supplies four fields")
    p = planner.plan(CommRequest(satellite_id=sat, data_volume_gbit=18.4,
                                 customer_id="CUSTOMER-17", priority="high"), t_now=0.0)
    print(p.card())

    rule("2. Same job by data object - size resolved from the payload registry")
    p2 = planner.plan(CommRequest(satellite_id=sat, data_object_id="IMG-8472",
                                  customer_id="CUSTOMER-17"), t_now=0.0)
    print(f"  IMG-8472 -> {p2.data_volume_gbit} Gbit, tier from account: {p2.tier} "
          f"(priority {p2.priority}), decision {p2.decision.value}")

    rule("3. Timing intent changes the answer for an identical payload")
    for intent, dl in [(TimingIntent.ASAP, None),
                       (TimingIntent.BY_DEADLINE, 1200.0),
                       (TimingIntent.BY_DEADLINE, 200.0),
                       (TimingIntent.FLEXIBLE, None)]:
        r = CommRequest(satellite_id=sat, data_volume_gbit=40.0,
                        customer_id="CUSTOMER-17", timing=intent, deadline_s=dl)
        pl = planner.plan(r, t_now=0.0)
        label = f"{intent.value}" + (f" (deadline t+{dl:.0f}s)" if dl else "")
        start = (f"t+{pl.recommendation.t_start:.0f}s @ {pl.recommendation.station}"
                 if pl.recommendation else "-")
        print(f"  {label:32s} -> {pl.decision.value:13s} start {start:28s} "
              f"{pl.scheduled_gbit:5.1f}/{pl.data_volume_gbit:.0f} Gbit "
              f"across {len(pl.schedule)} contact(s)")
    print("\n  ASAP takes the soonest contacts; FLEXIBLE takes the fattest ones,")
    print("  leaving the short scarce passes for requests that have no choice.")

    rule("4. Admission control - the ledger fills and later requests see it")
    fresh = Planner(scn, t0=0.0, horizon_s=86400.0)
    fresh.register_customer(Customer("CUSTOMER-17", tier="military"))
    window_s = 14400.0        # four hours: several contacts for this satellite
    print(f"  {'#':>3s} {'request':10s} {'decision':14s} {'booked':>8s} "
          f"{'short':>7s} {'commitments':>12s}")
    print("  " + "-" * 62)
    for i in range(1, 7):
        r = CommRequest(satellite_id=sat, data_volume_gbit=60.0,
                        customer_id="CUSTOMER-17", timing=TimingIntent.BY_DEADLINE,
                        deadline_s=window_s)
        pl = fresh.plan(r, t_now=0.0)
        fresh.accept(pl)
        print(f"  {i:3d} {pl.request_id:10s} {pl.decision.value:14s} "
              f"{pl.scheduled_gbit:7.1f}G {pl.shortfall_gbit:6.1f}G "
              f"{len(fresh.commitments):12d}")
    print("\n  Capacity is consumed, not re-promised. Once the station's beams are")
    print("  fully booked across the deadline, further requests are refused.")
    print(f"  reason code on the last one: {pl.reason_code}")

    rule("5. Rejections carry a machine-readable reason")
    bad = planner.plan(CommRequest(satellite_id="SAT-999", data_volume_gbit=1.0), 0.0)
    print(f"  unknown satellite   -> {bad.decision.value:8s} [{bad.reason_code}]")
    tight = planner.plan(CommRequest(satellite_id=sat, data_volume_gbit=5000.0,
                                     timing=TimingIntent.BY_DEADLINE,
                                     deadline_s=600.0), 0.0)
    print(f"  5000 Gbit in 600 s  -> {tight.decision.value:8s} [{tight.reason_code}] "
          f"{tight.scheduled_gbit:.1f} of {tight.data_volume_gbit:.0f} Gbit")
    for e in tight.explanation:
        print(f"      - {e}")

    rule("6. What the plan does NOT claim")
    b = p.beam_requirement
    print(f"  frequency : {p.frequency['channel']}  ({p.frequency['note']})")
    print(f"  beam      : count={b['count']}, az {b['az_deg']:.1f}, el {b['elev_deg']:.1f}, "
          f"scan {b['scan_angle_deg']:.1f} deg, width {b['beamwidth_deg']:.2f} deg")
    print("              - a requirement to synthesise, not an index to select.")


if __name__ == "__main__":
    main()
