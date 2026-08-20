"""Does the executor actually honour what the planner promised?

The planner and the simulator encode the network's constraints in two separate
places. That already produced one unexecutable plan (a satellite booked on two
stations at once, caught only because a demo printed it), and nothing structural
stops the two drifting apart again. This test makes the agreement checkable.

Two halves.

STRUCTURAL — re-derive the constraints from `xnios.forecast` directly, rather
than from the `Lookahead` the planner itself used, so a bug in the lookahead
cannot hide behind itself:

  C1  one link per satellite at a time
  C2  a station never exceeds its beam count
  C3  every booked window lies inside a genuinely usable contact
  C4  the promised volume fits the link budget over that interval
  C5  the pointing stays inside the array's scan envelope

EXECUTION — hand the booked ledger to the simulator through a scheduler that
follows it literally, and compare delivered against promised. Exact equality is
not expected and would be suspicious: the planner quotes nominal bandwidth and
power with no interference, while the engine applies allocators, co-channel
interference and beam slew. The planner's number is meant to be an upper bound,
so the test is `delivered <= promised` with the ratio reported.

Run:  python experiments/plan_conformance.py
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_benchmark import build_config, SCENARIO_PROFILES

from xnios import forecast as fc
from xnios.config import scenario_from_config, sim_config_from_config
# The production execution path, imported rather than reimplemented. This test
# is the architectural gate on request -> plan -> execute, so it has to exercise
# the same PlanScheduler that POST /api/plan/execute runs; a local copy would
# gate a fiction.
from xnios.execution import PlanScheduler, execution_scenario
from xnios.planner import Planner, Customer, CommRequest, TimingIntent
from xnios.simulator import Simulator


# --------------------------------------------------------------------------- #
# structural conformance
# --------------------------------------------------------------------------- #
def check_structure(planner, scn) -> list:
    """Every constraint, re-derived from physics. Returns a list of violations."""
    sats = {s.id: s for s in scn.satellites}
    stations = {g.id: g for g in scn.stations}
    v = []
    led = planner.commitments

    # C1 one link per satellite
    by_sat = defaultdict(list)
    for c in led:
        by_sat[c.satellite_id].append(c)
    for sid, cs in by_sat.items():
        cs = sorted(cs, key=lambda c: c.t_start)
        for a, b in zip(cs, cs[1:]):
            if a.t_end > b.t_start + 1e-6:
                v.append(f"C1 {sid}: {a.station} [{a.t_start:.1f},{a.t_end:.1f}] overlaps "
                         f"{b.station} [{b.t_start:.1f},{b.t_end:.1f}]")

    # C2 station beam capacity, checked on the event timeline
    by_st = defaultdict(list)
    for c in led:
        by_st[c.station].append(c)
    for gid, cs in by_st.items():
        n_beams = stations[gid].num_beams
        edges = sorted({t for c in cs for t in (c.t_start, c.t_end)})
        for a, b in zip(edges, edges[1:]):
            mid = 0.5 * (a + b)
            n = sum(1 for c in cs if c.t_start <= mid < c.t_end)
            if n > n_beams:
                v.append(f"C2 {gid}: {n} concurrent links at t={mid:.1f} "
                         f"but only {n_beams} beam(s)")
                break

    # C3/C4/C5 physics, sampled finely across the INTERIOR of each booked window.
    # The endpoints are exactly where the link starts and stops being usable, so
    # elevation sits on the mask and scan sits on the envelope: sampling them
    # makes float noise decide the verdict, and they carry no data anyway.
    _trapz = getattr(np, "trapezoid", None) or np.trapz
    C4_REL_TOL = 0.005              # capacity curves are interpolated, not exact
    # `forecast.contact_windows` bisects each edge to tol_s=0.05 s, so a window
    # may legitimately overhang the true crossing by that much. Step inside by
    # twice that before judging usability or pointing. Those 0.05 s carry no
    # data — the rate is ~0 there — so nothing is being excused.
    EDGE_S = 0.10
    worst_margin = 1.0
    for c in led:
        sat, g = sats[c.satellite_id], stations[c.station]
        dur = c.t_end - c.t_start
        rain = scn.weather.fade_db(g.id, c.t_start)

        # C3/C5 judge the interior: edge uncertainty must not decide the verdict.
        eps = min(EDGE_S, 0.25 * dur)
        t_in = np.linspace(c.t_start + eps, c.t_end - eps, 401)
        elev, rng = fc.elevation_series(sat, g, t_in)
        rate_in = fc.rate_series(sat, g, elev, rng, rain_zenith_db=rain)
        usable = (elev >= g.elevation_mask_deg) & (rate_in > 0)
        if not usable.all():
            v.append(f"C3 {c.request_id} {c.satellite_id}@{c.station}: "
                     f"{(~usable).sum()}/{len(t_in)} samples unusable in the booked window")

        # C4 integrates the FULL booked interval. Trimming the edges here would
        # shrink the reference below what was actually promised and manufacture
        # a violation — on a 30 s window, 0.2 s is 0.7%, which is the size of
        # the discrepancy this very check is looking for.
        t_full = np.linspace(c.t_start, c.t_end, 401)
        e_f, r_f = fc.elevation_series(sat, g, t_full)
        rate = fc.rate_series(sat, g, e_f, r_f, rain_zenith_db=rain)
        capacity_gbit = float(_trapz(rate, t_full)) / 1e9
        worst_margin = min(worst_margin, (capacity_gbit - c.gbit) / max(c.gbit, 1e-9))
        if c.gbit > capacity_gbit * (1.0 + C4_REL_TOL) + 1e-6:
            v.append(f"C4 {c.request_id} {c.satellite_id}@{c.station}: promised "
                     f"{c.gbit:.3f} Gbit, link budget allows {capacity_gbit:.3f} "
                     f"({(c.gbit / capacity_gbit - 1) * 100:+.2f}%)")
        scan = 90.0 - elev
        if getattr(g, "phased_array", False) and (scan > g.max_scan_deg + 1e-6).any():
            v.append(f"C5 {c.request_id} {c.satellite_id}@{c.station}: scan reaches "
                     f"{scan.max():.4f} deg, envelope is {g.max_scan_deg:.0f} deg")
    if led:
        print(f"  tightest C4 margin: {worst_margin * 100:+.3f}% "
              f"(negative = over-promise; tolerance {C4_REL_TOL * 100:.1f}%)")
    return v


# --------------------------------------------------------------------------- #
# execution conformance
# --------------------------------------------------------------------------- #
def check_execution(planner, cfg, promised: dict) -> tuple:
    """Run the booked ledger through the engine. Returns (delivered, results)."""
    from xnios.execution import execution_duration_s
    sim_cfg = sim_config_from_config(cfg)
    sim_cfg.duration_s = execution_duration_s(planner.commitments, sim_cfg.dt_s)
    scn = execution_scenario(scenario_from_config(cfg), planner.commitments)
    booked = set(promised)

    res = Simulator(scn, PlanScheduler(planner.commitments), sim_cfg).run()
    delivered = {sid: res.per_sat[sid]["delivered_gbit"] for sid in booked}
    return delivered, res


def main() -> None:
    profile = [p for p in SCENARIO_PROFILES if p["name"] == "baseline"][0]
    cfg, _ = build_config("india8", profile, 0)
    scn = scenario_from_config(cfg)

    planner = Planner(scn, t0=0.0, horizon_s=86400.0)
    planner.register_customer(Customer("ACME", tier="commercial"))

    # book a spread of requests: several satellites, several volumes, both intents
    order = sorted(((planner.look.next_contact(s.id, 0.0) or {}).get("wait_s", 1e18), s.id)
                   for s in scn.satellites)
    targets = [sid for _w, sid in order[:6]]
    promised = {}
    print("booking requests")
    print(f"  {'request':10s} {'satellite':10s} {'ask':>7s} {'decision':13s} "
          f"{'booked':>8s} {'windows':>8s}")
    print("  " + "-" * 62)
    for i, sid in enumerate(targets):
        want = [8.0, 15.0, 25.0, 40.0, 60.0, 12.0][i]
        intent = TimingIntent.ASAP if i % 2 == 0 else TimingIntent.FLEXIBLE
        p = planner.plan(CommRequest(satellite_id=sid, data_volume_gbit=want,
                                     customer_id="ACME", timing=intent), t_now=0.0)
        if planner.accept(p):
            promised[sid] = p.scheduled_gbit
        print(f"  {p.request_id:10s} {sid:10s} {want:6.1f}G {p.decision.value:13s} "
              f"{p.scheduled_gbit:7.1f}G {len(p.schedule):8d}")

    print(f"\n  {len(planner.commitments)} commitments, "
          f"{sum(promised.values()):.1f} Gbit promised")

    print("\nSTRUCTURAL CONFORMANCE (re-derived from xnios.forecast)")
    print("-" * 62)
    violations = check_structure(planner, scn)
    if violations:
        for x in violations:
            print(f"  FAIL {x}")
    else:
        print("  C1 one link per satellite .................. PASS")
        print("  C2 station beam capacity .................. PASS")
        print("  C3 windows inside usable contacts ......... PASS")
        print("  C4 promised volume within link budget ..... PASS")
        print("  C5 pointing inside scan envelope .......... PASS")

    print("\nEXECUTION CONFORMANCE (ledger executed by the engine)")
    print("-" * 62)
    delivered, res = check_execution(planner, cfg, promised)
    print(f"  {'satellite':10s} {'promised':>10s} {'delivered':>10s} {'ratio':>8s}")
    print("  " + "-" * 42)
    over = []
    for sid in sorted(promised):
        pr, dl = promised[sid], delivered[sid]
        ratio = dl / pr if pr > 0 else float("nan")
        flag = ""
        if dl > pr + 1e-3:
            over.append(sid)
            flag = "  <- EXCEEDS PROMISE"
        print(f"  {sid:10s} {pr:9.2f}G {dl:9.2f}G {ratio:7.1%}{flag}")
    tp, td = sum(promised.values()), sum(delivered.values())
    print("  " + "-" * 42)
    print(f"  {'TOTAL':10s} {tp:9.2f}G {td:9.2f}G {td / tp:7.1%}")

    print()
    if over:
        print(f"  FAIL delivered more than promised for {over} — the planner's "
              f"quote is not an upper bound")
    else:
        print("  PASS delivered <= promised for every request (the quote bounds "
              "the outcome)")
    print(f"  engine: {res.summary['handovers']} handovers, "
          f"{res.summary['reacquisitions']} reacquisitions, "
          f"{res.summary['sessions_interrupted']} interruptions")

    ok = not violations and not over
    print("\n" + ("CONFORMANCE: PASS" if ok else "CONFORMANCE: FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
