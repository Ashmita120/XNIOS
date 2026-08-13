"""Phase 1 - Validate the simulator.

Research question: does the digital twin behave correctly? Each experiment has an
unambiguous expected outcome; we assert it. Only once all four pass should any
optimisation/AI experiment be trusted on this world.

  E1  one sat / one station        -> 100% successful communication
  E2  one station / two sats       -> one is served, the other waits
  E3  five stations / one sat      -> nearest station is selected
  E4  visibility expires           -> communication stops at LOS

Run:  python experiments/phase1_validation.py   (from the repo root e:\\Antenna)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import scenarios
from xnios.simulator import Simulator, SimConfig
from xnios.schedulers import FCFS, GreedyScheduler

PASS = "\033[92mPASS\033[0m" if os.environ.get("TERM") else "PASS"
FAIL = "\033[91mFAIL\033[0m" if os.environ.get("TERM") else "FAIL"

results = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ->  {detail}" if detail else ""))
    results.append(ok)
    return ok


# --------------------------------------------------------------------------- E1
def e1():
    print("\nE1: one satellite -> one station  (expect 100% completion)")
    scn = scenarios.e1_one_sat_one_station(t_mid=600.0)
    res = Simulator(scn, FCFS(), SimConfig(duration_s=1200, dt_s=5)).run()
    print(res)
    check("all data downlinked (completion_rate == 100%)",
          abs(res.summary["completion_rate"] - 1.0) < 1e-9,
          f"completion={res.summary['completion_rate']*100:.0f}%")
    check("throughput is positive", res.summary["delivered_gbit"] > 0,
          f"{res.summary['delivered_gbit']:.2f} Gbit delivered")


# --------------------------------------------------------------------------- E2
def e2():
    print("\nE2: two satellites -> one single-beam station  (expect one waits)")
    scn = scenarios.e2_one_station_two_sats(t_mid=600.0)
    res = Simulator(scn, FCFS(), SimConfig(duration_s=1200, dt_s=5)).run()
    print(res)
    waited = [sid for sid, d in res.per_sat.items() if d["wait_s"] > 0]
    check("at least one satellite had to wait", len(waited) >= 1,
          f"waited: {', '.join(waited) or 'none'}")
    # single-beam station can never serve two at once -> both funnel through GS-1
    served_on = {sid: d["served_on"] for sid, d in res.per_sat.items()}
    check("both served through the single station", set(served_on.values()) <= {"GS-1"},
          f"served_on={served_on}")
    # validate the beam allocator: the single beam was used, but never double-booked
    # (a double-book bug would push peak utilisation to 2.0 since total_beams == 1)
    peak = res.summary["peak_beam_utilization"]
    check("beam used but never double-booked (peak busy beams == 1)",
          0.99 <= peak <= 1.01,
          f"peak beam util={peak*100:.0f}% of the station's 1 beam")


# --------------------------------------------------------------------------- E3
def e3():
    print("\nE3: one satellite -> five stations  (expect nearest/overhead GS-0)")
    # Validate the station-selection RULE on a snapshot where all 5 stations see
    # the satellite simultaneously (t_mid = overhead GS-0). This isolates the rule
    # from acquisition/stickiness effects (which are tested elsewhere).
    scn = scenarios.e3_five_stations_one_sat(t_mid=600.0)
    sim = Simulator(scn, GreedyScheduler(station_key="nearest"), SimConfig())
    state = sim.snapshot(600.0)

    vis = state.visible_for("SAT-1")
    seen = {v.station_id: v for v in vis}
    check("all five stations see the satellite at peak", len(seen) == 5,
          f"visible: {sorted(seen)}")
    nearest = min(vis, key=lambda v: v.range_km).station_id
    check("GS-0 (overhead) is geometrically nearest", nearest == "GS-0",
          f"ranges(km): " + ", ".join(f"{v.station_id}={v.range_km:.0f}" for v in
                                       sorted(vis, key=lambda v: v.range_km)))

    picked_near = GreedyScheduler(station_key="nearest").decide(state)
    check("'nearest' scheduler selects GS-0",
          picked_near and picked_near[0].station_id == "GS-0",
          f"picked {picked_near[0].station_id if picked_near else None}")
    picked_elev = GreedyScheduler(station_key="highest_elev").decide(state)
    check("'highest elevation' scheduler also selects GS-0",
          picked_elev and picked_elev[0].station_id == "GS-0",
          f"picked {picked_elev[0].station_id if picked_elev else None}")


# --------------------------------------------------------------------------- E4
def e4():
    print("\nE4: visibility expires  (expect transfer stops at LOS, no completion)")
    scn = scenarios.e4_visibility_expiry(t_mid=400.0)
    cfg = SimConfig(duration_s=1200, dt_s=5, trace=True)
    sim = Simulator(scn, FCFS(), cfg)
    res = sim.run()
    print(res)

    check("satellite did NOT complete (buffer too big for one pass)",
          res.summary["completion_rate"] < 1e-9,
          f"completion={res.summary['completion_rate']*100:.0f}%")
    check("some data moved during the pass", res.summary["delivered_gbit"] > 0,
          f"{res.summary['delivered_gbit']:.2f} Gbit")

    # trace rows are (t, delivered_by_sat, busy_beam_count)
    trace = sim.trace
    deliv = [(t, d["SAT-1"]) for t, d, _ in trace]
    last_growth_t = 0.0
    for i in range(1, len(deliv)):
        if deliv[i][1] - deliv[i - 1][1] > 1.0:
            last_growth_t = deliv[i][0]
    final = deliv[-1][1]
    plateau = all(abs(v - final) < 1.0 for t, v in deliv if t > last_growth_t + 1e-6)
    check("transfer stopped and stayed stopped after LOS", plateau,
          f"last transfer at t={last_growth_t:.0f}s, then flat until t={deliv[-1][0]:.0f}s")

    # the beam must be RELEASED after LOS (not held idle) -> 0 busy beams afterwards
    busy_after = [busy for t, _d, busy in trace if t > last_growth_t + 1e-6]
    check("beam released after LOS (0 busy beams once the pass ends)",
          all(b == 0 for b in busy_after),
          f"max busy beams after LOS = {max(busy_after) if busy_after else 0}")


if __name__ == "__main__":
    print("=" * 68)
    print("X-NioS digital twin - Phase 1 validation (E1-E4)")
    print("=" * 68)
    e1(); e2(); e3(); e4()
    print("\n" + "=" * 68)
    ok = all(results)
    print(f"RESULT: {sum(results)}/{len(results)} checks passed"
          f"  ->  {'SIMULATOR VALIDATED' if ok else 'VALIDATION FAILED'}")
    print("=" * 68)
    sys.exit(0 if ok else 1)
