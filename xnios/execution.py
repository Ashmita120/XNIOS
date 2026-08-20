"""Executing a booked plan — the join between planning and the twin.

Until now the two halves shared nothing. The planner booked capacity into a
ledger; the simulator ran an unrelated scenario preset. Telemetry therefore
described a network the requester had never asked about, which is exactly why
an operator console built on it could only ever show someone else's run.

This module closes the loop:

    request -> quote -> accept -> ledger -> EXECUTE -> telemetry

`PlanScheduler` follows a booked ledger literally — it has no policy of its own,
so whatever the engine delivers is attributable to the plan and nothing else.
`execution_scenario` builds the world that plan runs in: the same stations and
orbits, but demand set to exactly what was promised, and *only* on the
satellites that were booked. Nothing else competes, so the run answers "did the
network do what it said" rather than "what happens in this scenario".

This is deliberately the same code `experiments/plan_conformance.py` tests. That
test asserts promised == delivered end to end; if it imported its own copy of
the scheduler it would be gating a fiction.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict

from .schedulers import Scheduler
from .state import Assignment

__all__ = ["PlanScheduler", "execution_scenario", "execution_duration_s",
           "promised_by_satellite", "surplus_commitments"]


class PlanScheduler(Scheduler):
    """Executes a booked ledger literally — no policy of its own.

    At each decision it offers every satellite whose committed window covers
    `now` to the station that window names, and nothing else. It never invents
    an assignment, so the delivered total is attributable to the plan.

    A commitment is anything with `satellite_id`, `station`, `t_start` and
    `t_end` — the planner's `Commitment`, or a dict from the API.
    """

    name = "plan-follower"

    def __init__(self, commitments):
        self.by_sat = defaultdict(list)
        for c in commitments:
            self.by_sat[_get(c, "satellite_id")].append(
                (_get(c, "station"), float(_get(c, "t_start")), float(_get(c, "t_end"))))
        for v in self.by_sat.values():
            v.sort(key=lambda w: w[1])

    def decide(self, state):
        out = []
        for sat in state.free_sats():
            for station, t0, t1 in self.by_sat.get(sat.sat_id, ()):
                if t0 - 1e-6 <= state.t <= t1 + 1e-6:
                    # only if the link is genuinely usable this instant; the
                    # engine is the authority on that, not the plan
                    if any(v.station_id == station
                           for v in state.visible_for(sat.sat_id)):
                        out.append(Assignment(sat.sat_id, station))
                    break
        return out


def _get(c, key):
    return c[key] if isinstance(c, dict) else getattr(c, key)


def promised_by_satellite(commitments) -> dict:
    """Gbit the ledger promised each satellite, summed across its windows."""
    out: dict = defaultdict(float)
    for c in commitments:
        out[_get(c, "satellite_id")] += float(_get(c, "gbit"))
    return dict(out)


def execution_duration_s(commitments, dt_s: float, tail_s: float = 60.0) -> float:
    """Long enough to cover every booked window, rounded onto the step grid."""
    if not commitments:
        return dt_s
    end = max(float(_get(c, "t_end")) for c in commitments)
    return float(math.ceil((end + tail_s) / dt_s) * dt_s)


def surplus_commitments(commitments, records, tol_bits: float = 1.0) -> list:
    """Booked windows through which no data actually moved.

    The planner quotes at nominal transmit power while the engine runs an
    adaptive allocator that can beat it, so a transfer often completes before
    its later reservations open. Those windows are capacity nobody used and
    nobody else could book — real waste, not a rounding artifact.

    Attribution is per satellite: a satellite carries one link at a time, and a
    window belongs to exactly one satellite, so "did this window carry
    anything" is "did that satellite's delivered total move across its span".
    """
    if not records:
        return []
    timeline = [(r.t, {s.sat_id: s.delivered_bits for s in r.satellites})
                for r in records]

    def delivered(sat_id: str, t: float) -> float:
        v = 0.0
        for tt, by_sat in timeline:
            if tt > t:
                break
            v = by_sat.get(sat_id, v)
        return v

    out = []
    for c in commitments:
        sid = _get(c, "satellite_id")
        moved = delivered(sid, _get(c, "t_end")) - delivered(sid, _get(c, "t_start"))
        if moved <= tol_bits:
            out.append(c)
    return out


def execution_scenario(scenario, commitments):
    """A copy of `scenario` carrying only the demand the ledger promised.

    Booked satellites get exactly their promised volume as backlog; every other
    satellite is zeroed so it neither competes for beams nor pollutes the KPIs.
    The stations, orbits, weather and hardware are untouched — this is the same
    network, asked a different question.
    """
    scn = copy.deepcopy(scenario)
    promised = promised_by_satellite(commitments)
    for s in scn.satellites:
        s.backlog_bits = promised.get(s.id, 0.0) * 1e9
    return scn
