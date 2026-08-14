"""Request -> Plan. The surface that turns the twin into a planning system.

Everything else in X-NioS is a closed loop: you describe a whole world, press
go, and watch a simulation decide for every satellite at once. That is the wrong
shape for an operator, who arrives with one job:

    "SAT-202 has 18.4 Gbit to get down before 14:00. Can you take it, and how?"

This module answers exactly that. It is a *reader* of the same machinery the
simulator runs on — `lookahead` for contact windows and their capacity, `link`
for the budget, `entities` for the hardware — plus one piece of state nothing
else has: a ledger of what the network has already promised to other requests.
That ledger is what makes admission control possible.

Two layers of input, deliberately separated:

    Customer      configured once: tier, SLA, quota. The operator does not
                  retype "I am Gold" on every job.
    CommRequest   per job: which satellite, how much data (or which data
                  object), and when it is needed.

Everything else — station, beam geometry, frequency, bandwidth, power, exact
timing — is what X-NioS is for. None of it appears in a request.

What this module does NOT do: decide the channel, or optimise jointly across
requests. Channel assignment stays where it already works, at execution time in
`allocators.GraphColorFreq`, which colours live beams against live geometry
every step; a plan reports `assigned_at_execution` rather than inventing a
number it cannot honour. And the plan is built greedily over contact windows,
not optimised. Both are deliberate: this is the interface, and the decision
engine underneath it is expected to get smarter without the interface changing.

Beams are described as a *requirement* (how many, pointed where, at what scan
angle), never as an index. A phased array synthesises a beam by phasing its
elements toward a direction; it does not pick one from a pool. `Assignment.beam`
in the simulator is a capacity counter, and a plan that said "Beam-3" would be
reporting something that does not exist.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, asdict
from enum import Enum

from . import orbit as orb
from .entities import TIERS
from .link import scan_beamwidth_deg
from .lookahead import Lookahead

__all__ = [
    "Customer", "DataObject", "TimingIntent", "CommRequest",
    "PlanWindow", "CommPlan", "Decision", "Planner",
]

# what a user may call a priority, mapped onto the engine's 1..4 tiers
PRIORITY_NAMES = {"low": 1, "normal": 2, "standard": 2, "high": 3,
                  "urgent": 4, "critical": 4, "emergency": 4}


# --------------------------------------------------------------------------- #
# configured once, per account
# --------------------------------------------------------------------------- #
@dataclass
class Customer:
    """An account. Tier and SLA live here, not in every request."""

    customer_id: str
    name: str = ""
    tier: str = "commercial"                  # research|commercial|military|emergency
    sla_availability: float = 0.99
    quota_gbit: float | None = None           # None = unmetered

    @property
    def priority(self) -> int:
        return TIERS.get(self.tier, 2)


@dataclass
class DataObject:
    """A named payload, so a request can say "the imagery" instead of a number."""

    object_id: str
    satellite_id: str
    size_gbit: float
    description: str = ""


# --------------------------------------------------------------------------- #
# per job
# --------------------------------------------------------------------------- #
class TimingIntent(str, Enum):
    """What the requester actually needs, which is not always a deadline.

    ASAP         start at the earliest usable contact
    BY_DEADLINE  must complete before `deadline_s`; fail loudly if it cannot
    FLEXIBLE     no hard time bound — prefer the windows that carry the most,
                 which is where planning-time deferral earns its keep
    """

    ASAP = "asap"
    BY_DEADLINE = "by_deadline"
    FLEXIBLE = "flexible"


_ids = itertools.count(1)


@dataclass
class CommRequest:
    satellite_id: str
    request_id: str = field(default_factory=lambda: f"REQ-{next(_ids):04d}")
    customer_id: str | None = None
    data_volume_gbit: float | None = None     # either this...
    data_object_id: str | None = None         # ...or this
    timing: TimingIntent = TimingIntent.ASAP
    deadline_s: float | None = None           # required for BY_DEADLINE
    priority: str | None = None               # overrides the customer's tier

    def __post_init__(self):
        if isinstance(self.timing, str):
            self.timing = TimingIntent(self.timing)
        if (self.data_volume_gbit is None) == (self.data_object_id is None):
            raise ValueError("give exactly one of data_volume_gbit / data_object_id")
        if self.timing is TimingIntent.BY_DEADLINE and self.deadline_s is None:
            raise ValueError("timing=by_deadline requires deadline_s")
        if self.data_volume_gbit is not None and self.data_volume_gbit <= 0:
            raise ValueError("data_volume_gbit must be positive")


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
class Decision(str, Enum):
    TRANSMIT_NOW = "TRANSMIT_NOW"    # a usable contact is open and it is the right one
    SCHEDULE = "SCHEDULE"            # wait: a later window serves this better
    PARTIAL = "PARTIAL"              # some of it fits, not all
    REJECT = "REJECT"                # nothing feasible in the horizon


@dataclass
class PlanWindow:
    """One booked slice of one contact."""

    station: str
    t_start: float
    t_end: float
    deliverable_gbit: float
    peak_elev_deg: float
    contended: bool = False          # capacity was reduced by existing commitments

    @property
    def duration_s(self) -> float:
        return self.t_end - self.t_start

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration_s"] = self.duration_s
        return d


@dataclass
class CommPlan:
    request_id: str
    satellite_id: str
    data_volume_gbit: float
    decision: Decision
    admitted: bool
    reason_code: str
    customer_id: str | None = None
    tier: str | None = None
    priority: int = 2
    recommendation: PlanWindow | None = None
    schedule: list = field(default_factory=list)          # list[PlanWindow]
    scheduled_gbit: float = 0.0
    shortfall_gbit: float = 0.0
    completes_at_s: float | None = None
    meets_deadline: bool | None = None
    next_opportunity: dict | None = None
    frequency: dict = field(default_factory=dict)
    beam_requirement: dict | None = None
    explanation: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decision"] = self.decision.value
        d["recommendation"] = self.recommendation.to_dict() if self.recommendation else None
        d["schedule"] = [w.to_dict() for w in self.schedule]
        return d

    def card(self) -> str:
        """The plan as an operator would read it."""
        w = self.recommendation
        lines = [
            "+" + "-" * 52 + "+",
            "|" + "  X-NioS COMMUNICATION PLAN".ljust(52) + "|",
            "+" + "-" * 52 + "+",
            f"| Request        {self.request_id}".ljust(53) + "|",
            f"| Satellite      {self.satellite_id}".ljust(53) + "|",
        ]
        if self.customer_id:
            lines.append(f"| Customer       {self.customer_id} ({self.tier})".ljust(53) + "|")
        lines += [
            f"| Data           {self.data_volume_gbit:.1f} Gbit".ljust(53) + "|",
            f"| Priority       {self.priority}".ljust(53) + "|",
            "|" + " " * 52 + "|",
        ]
        if w is not None:
            lines += [
                f"| Station        {w.station}".ljust(53) + "|",
                f"| Start          t+{w.t_start:.0f} s".ljust(53) + "|",
                f"| Usable window  {w.duration_s:.0f} s".ljust(53) + "|",
                f"| Deliverable    {w.deliverable_gbit:.1f} Gbit".ljust(53) + "|",
            ]
        if self.beam_requirement:
            b = self.beam_requirement
            lines.append(f"| Beam           {b['count']} x az {b['az_deg']:.0f} / "
                         f"el {b['elev_deg']:.0f} (scan {b['scan_angle_deg']:.0f})"
                         .ljust(53) + "|")
        if self.frequency:
            lines.append(f"| Frequency      {self.frequency['band']}, "
                         f"{self.frequency['channel']}".ljust(53) + "|")
        if self.next_opportunity:
            n = self.next_opportunity
            lines += [
                "|" + " " * 52 + "|",
                f"| Next chance    {n['station']} in {n['in_s']:.0f} s".ljust(53) + "|",
                f"|                capacity {n['deliverable_gbit']:.1f} Gbit".ljust(53) + "|",
            ]
        lines += [
            "|" + " " * 52 + "|",
            f"| DECISION       {self.decision.value}".ljust(53) + "|",
            "+" + "-" * 52 + "+",
        ]
        for e in self.explanation:
            lines.append(f"  - {e}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the planner
# --------------------------------------------------------------------------- #
@dataclass
class Commitment:
    """Capacity the network has already promised. The basis of admission control."""

    request_id: str
    satellite_id: str
    station: str
    t_start: float
    t_end: float
    gbit: float


class Planner:
    """Answers "can you take this, and how" against the live network.

    Holds the contact horizon (via `Lookahead`) and the commitment ledger.
    `plan()` is pure — it books nothing — so a caller can quote before
    committing. `accept()` books a quoted plan; `release()` cancels it.
    """

    BAND_NAMES = [(4e9, "C-band"), (8e9, "X-band"), (12e9, "Ku-band"),
                  (18e9, "K-band"), (40e9, "Ka-band")]

    def __init__(self, scenario, t0: float = 0.0, horizon_s: float = 86400.0):
        self.scn = scenario
        self.sats = {s.id: s for s in scenario.satellites}
        self.stations = {g.id: g for g in scenario.stations}
        self.horizon_s = float(horizon_s)
        self.look = Lookahead(scenario.satellites, scenario.stations,
                              weather=scenario.weather, t0=t0, span_s=horizon_s)
        self.customers: dict[str, Customer] = {}
        self.objects: dict[str, DataObject] = {}
        self.commitments: list[Commitment] = []

    # ------------------------------------------------------------- registries
    def register_customer(self, c: Customer) -> Customer:
        self.customers[c.customer_id] = c
        return c

    def register_object(self, o: DataObject) -> DataObject:
        self.objects[o.object_id] = o
        return o

    # ------------------------------------------------------------ admission
    def _free_intervals(self, station_id: str, lo: float, hi: float) -> list:
        """Sub-intervals of [lo, hi] where this station still has a spare beam.

        Sweeps the commitment ledger: the station is full wherever the number of
        overlapping commitments reaches its beam count. This is what stops the
        planner from promising the same capacity twice.
        """
        if hi <= lo:
            return []
        n_beams = self.stations[station_id].num_beams
        busy = [(c.t_start, c.t_end) for c in self.commitments
                if c.station == station_id and c.t_end > lo and c.t_start < hi]
        if not busy:
            return [(lo, hi)]

        edges = sorted({lo, hi} | {t for iv in busy for t in iv if lo < t < hi})
        out = []
        for a, b in zip(edges, edges[1:]):
            mid = 0.5 * (a + b)
            if sum(1 for s, e in busy if s <= mid < e) < n_beams:
                if out and abs(out[-1][1] - a) < 1e-9:
                    out[-1] = (out[-1][0], b)        # merge touching intervals
                else:
                    out.append((a, b))
        return out

    def _available(self, p, t_from: float, t_to: float) -> tuple:
        """(gbit deliverable, free sub-intervals, contended)."""
        lo, hi = max(t_from, p.t_rise), min(t_to, p.t_set)
        free = self._free_intervals(p.station_id, lo, hi)
        if not free:
            return 0.0, [], True
        bits = sum(p.bits_until(a, b) for a, b in free)
        contended = not (len(free) == 1 and abs(free[0][0] - lo) < 1e-6
                         and abs(free[0][1] - hi) < 1e-6)
        return bits / 1e9, free, contended

    @staticmethod
    def _subtract(free: list, busy: list) -> list:
        """`free` minus `busy`. Used to enforce one link per satellite at a time.

        A satellite is often visible from several stations at once, and without
        this the planner happily books it on two of them concurrently — a plan
        the engine cannot execute, since a session is a single (station, beam)
        per satellite and the oracle constrains sum_s x[i,s,t] <= 1.
        """
        out = []
        for a, b in free:
            segs = [(a, b)]
            for s, e in busy:
                nxt = []
                for x, y in segs:
                    if e <= x or s >= y:
                        nxt.append((x, y))
                        continue
                    if s > x:
                        nxt.append((x, min(s, y)))
                    if e < y:
                        nxt.append((max(e, x), y))
                segs = nxt
            out.extend([(x, y) for x, y in segs if y - x > 1e-9])
        return out

    @staticmethod
    def _fill(p, free: list, want_gbit: float) -> tuple:
        """Book `want_gbit` across this pass's free sub-intervals.

        Returns (t_start, t_complete, gbit taken). `t_complete` is when the
        transfer actually finishes, which is inside the window whenever the
        window has spare capacity — not the end of the window.
        """
        want = want_gbit * 1e9
        taken = 0.0
        for a, b in free:
            seg = p.bits_until(a, b)
            if taken + seg >= want - 1e-3:
                done = p.time_for_bits(a, want - taken)
                return free[0][0], (b if done is None else min(done, b)), want_gbit
            taken += seg
        return free[0][0], free[-1][1], taken / 1e9

    # ---------------------------------------------------------------- planning
    def _resolve(self, req: CommRequest) -> tuple:
        """(bits needed, customer, tier, priority) from the request + registries."""
        if req.data_object_id is not None:
            obj = self.objects.get(req.data_object_id)
            if obj is None:
                raise KeyError(f"unknown data object '{req.data_object_id}'")
            need_gbit = obj.size_gbit
        else:
            need_gbit = float(req.data_volume_gbit)

        cust = self.customers.get(req.customer_id) if req.customer_id else None
        tier = cust.tier if cust else None
        if req.priority is not None:
            key = req.priority.strip().lower()
            priority = PRIORITY_NAMES.get(key, TIERS.get(key, 2))
        else:
            priority = cust.priority if cust else 2
        return need_gbit, cust, tier, priority

    def _band(self, sat) -> str:
        for hz, name in self.BAND_NAMES:
            if sat.freq_hz < hz * 1.5:
                return name
        return f"{sat.freq_hz / 1e9:.1f} GHz"

    def _beam_requirement(self, sat_id: str, station_id: str, t: float) -> dict:
        """What the array is being asked to form — direction and scan angle.

        Not a beam index. A phased array synthesises a beam toward a direction;
        the operationally meaningful quantities are how many simultaneous beams
        are needed, where they point, and how far off boresight that is (which
        sets both the gain loss and, under Model B, the beam width).
        """
        sat, g = self.sats[sat_id], self.stations[station_id]
        # Evaluate just inside the window, not at the rise instant. At t_rise the
        # elevation sits exactly on the effective mask by construction, so the
        # scan angle sits exactly on the envelope and float noise decides whether
        # the check passes. One second in, the question is well posed.
        pos = orb.sat_position_ecef(sat.orbit, t + 1.0)
        gs = orb.gs_position_ecef(g.lat_deg, g.lon_deg, g.alt_km)
        elev, az, rng = orb.elevation_azimuth_range(gs, g.lat_deg, g.lon_deg, pos)
        scan = 90.0 - elev
        return {"count": 1, "az_deg": az, "elev_deg": elev,
                "scan_angle_deg": scan, "range_km": rng,
                "within_scan_envelope": scan <= float(getattr(g, "max_scan_deg", 90.0)) + 1e-6,
                "beamwidth_deg": scan_beamwidth_deg(elev, g)}

    def plan(self, req: CommRequest, t_now: float = 0.0) -> CommPlan:
        """Quote a plan. Books nothing — call `accept()` to hold the capacity."""
        need_gbit, cust, tier, priority = self._resolve(req)
        self.look.ensure(t_now)

        plan = CommPlan(request_id=req.request_id, satellite_id=req.satellite_id,
                        data_volume_gbit=need_gbit, decision=Decision.REJECT,
                        admitted=False, reason_code="",
                        customer_id=req.customer_id, tier=tier, priority=priority)

        if req.satellite_id not in self.sats:
            plan.reason_code = "unknown_satellite"
            plan.explanation.append(f"'{req.satellite_id}' is not in the network")
            return plan

        t_limit = (req.deadline_s if req.timing is TimingIntent.BY_DEADLINE
                   and req.deadline_s is not None else t_now + self.horizon_s)

        # every contact this satellite has left, with the capacity actually free
        cands = []
        for p in self.look.by_sat.get(req.satellite_id, ()):
            if p.t_set <= t_now or p.t_rise >= t_limit:
                continue
            gbit, free, contended = self._available(p, t_now, t_limit)
            if gbit <= 0 or not free:
                continue
            cands.append((p, gbit, free[0][0], free, contended))

        if not cands:
            plan.reason_code = "no_contact_in_horizon"
            plan.explanation.append(
                f"No usable contact for {req.satellite_id} "
                + (f"before the deadline (t+{t_limit:.0f} s)"
                   if req.timing is TimingIntent.BY_DEADLINE
                   else f"within {self.horizon_s / 3600:.0f} h"))
            nxt = self.look.next_contact(req.satellite_id, t_now)
            if nxt:
                plan.next_opportunity = {"station": nxt["station"], "in_s": nxt["wait_s"],
                                         "deliverable_gbit": nxt["capacity_bits"] / 1e9}
            return plan

        order = self._order(cands, req)
        remaining = need_gbit
        # the satellite can only be on one link at a time, including against
        # contacts already booked for it by earlier requests
        sat_busy = [(c.t_start, c.t_end) for c in self.commitments
                    if c.satellite_id == req.satellite_id]
        for p, _gbit, _t_a, free, contended in order:
            if remaining <= 1e-9:
                break
            usable = self._subtract(free, sat_busy)
            if not usable:
                continue
            avail = sum(p.bits_until(a, b) for a, b in usable) / 1e9
            if avail <= 0:
                continue
            t_start, t_done, took = self._fill(p, usable, min(avail, remaining))
            if took <= 0:
                continue
            plan.schedule.append(PlanWindow(
                station=p.station_id, t_start=t_start, t_end=t_done,
                deliverable_gbit=took, peak_elev_deg=p.peak_elev_deg,
                contended=contended or len(usable) != len(free)))
            sat_busy.append((t_start, t_done))
            remaining -= took

        plan.schedule.sort(key=lambda w: w.t_start)
        plan.scheduled_gbit = sum(w.deliverable_gbit for w in plan.schedule)
        plan.shortfall_gbit = max(0.0, need_gbit - plan.scheduled_gbit)
        self._decide(plan, req, cands, t_now, t_limit)
        return plan

    def _order(self, cands, req: CommRequest) -> list:
        """Which windows to use first.

        ASAP / BY_DEADLINE take contacts in time order — the soonest completion.
        FLEXIBLE takes the fattest windows first, which is what "sometime today,
        optimise for network efficiency" actually means: fewer, better passes,
        leaving the scarce short ones for requests that have no choice.
        """
        if req.timing is TimingIntent.FLEXIBLE:
            return sorted(cands, key=lambda c: (-c[1], c[2]))
        return sorted(cands, key=lambda c: c[2])

    def _decide(self, plan: CommPlan, req: CommRequest, cands, t_now: float,
                t_limit: float) -> None:
        """Set the decision, the admission verdict and the reasons."""
        sat = self.sats[req.satellite_id]
        first = plan.schedule[0] if plan.schedule else None

        # the best window that is NOT the one we would start with — used both for
        # "next opportunity" and for the planning-time defer comparison
        later = [c for c in cands if first is None or c[2] > first.t_start + 1e-6]
        best_later = max(later, key=lambda c: c[1]) if later else None
        if best_later is not None:
            plan.next_opportunity = {
                "station": best_later[0].station_id,
                "in_s": max(0.0, best_later[2] - t_now),
                "deliverable_gbit": best_later[1],
            }

        if first is None:
            plan.decision = Decision.REJECT
            plan.reason_code = "no_capacity"
            plan.explanation.append("Every contact in range is already committed")
            return

        plan.completes_at_s = max(w.t_end for w in plan.schedule)
        plan.recommendation = first
        plan.beam_requirement = self._beam_requirement(
            req.satellite_id, first.station, max(t_now, first.t_start))
        plan.frequency = {"band": self._band(sat),
                          "channel": "assigned_at_execution",
                          "note": "GraphColorFreq colours live beams each step"}

        in_contact_now = first.t_start <= t_now + 1e-6

        # planning-time deferral: only for FLEXIBLE, and only when it is a real
        # improvement. Runtime deferral does not exist and is not needed; this is
        # advice about *when to schedule*, not a scheduler withholding a beam.
        deferring = False
        if (req.timing is TimingIntent.FLEXIBLE and in_contact_now
                and best_later is not None and best_later[1] > 2.0 * first.deliverable_gbit):
            deferring = True

        if plan.shortfall_gbit > 1e-6:
            plan.decision = Decision.PARTIAL
            plan.admitted = False
            plan.reason_code = ("insufficient_capacity_before_deadline"
                                if req.timing is TimingIntent.BY_DEADLINE
                                else "insufficient_capacity_in_horizon")
            plan.explanation.append(
                f"Only {plan.scheduled_gbit:.1f} of {plan.data_volume_gbit:.1f} Gbit "
                f"can be delivered ({plan.shortfall_gbit:.1f} Gbit short)")
        elif deferring:
            plan.decision = Decision.SCHEDULE
            plan.admitted = True
            plan.reason_code = "better_window_later"
            plan.explanation.append(
                f"Transmitting now yields {first.deliverable_gbit:.1f} Gbit; "
                f"{best_later[0].station_id} in {best_later[2] - t_now:.0f} s yields "
                f"{best_later[1]:.1f} Gbit")
        elif in_contact_now:
            plan.decision = Decision.TRANSMIT_NOW
            plan.admitted = True
            plan.reason_code = "contact_open"
        else:
            plan.decision = Decision.SCHEDULE
            plan.admitted = True
            plan.reason_code = "awaiting_next_contact"
            plan.explanation.append(
                f"Next usable contact is {first.station} in {first.t_start - t_now:.0f} s")

        if req.timing is TimingIntent.BY_DEADLINE and req.deadline_s is not None:
            plan.meets_deadline = (plan.completes_at_s <= req.deadline_s
                                   and plan.shortfall_gbit <= 1e-6)
            if not plan.meets_deadline and plan.decision is not Decision.PARTIAL:
                plan.decision = Decision.PARTIAL
                plan.admitted = False
                plan.reason_code = "misses_deadline"
                plan.explanation.append(
                    f"Completes at t+{plan.completes_at_s:.0f} s, deadline t+{req.deadline_s:.0f} s")

        if plan.admitted:
            plan.explanation.insert(0, (
                f"{plan.scheduled_gbit:.1f} Gbit deliverable across "
                f"{len(plan.schedule)} contact(s), "
                f"completing at t+{plan.completes_at_s:.0f} s"))
        b = plan.beam_requirement
        if b and not b["within_scan_envelope"]:
            plan.explanation.append(
                f"WARNING scan angle {b['scan_angle_deg']:.0f} deg exceeds the "
                f"array's {self.stations[first.station].max_scan_deg:.0f} deg envelope")
        if any(w.contended for w in plan.schedule):
            plan.explanation.append("Some capacity is shared with existing commitments")

    # -------------------------------------------------------------- booking
    def accept(self, plan: CommPlan) -> bool:
        """Book a quoted plan against the ledger. Later requests see it."""
        if not plan.admitted:
            return False
        for w in plan.schedule:
            self.commitments.append(Commitment(
                request_id=plan.request_id, satellite_id=plan.satellite_id,
                station=w.station, t_start=w.t_start, t_end=w.t_end,
                gbit=w.deliverable_gbit))
        return True

    def release(self, request_id: str) -> int:
        n = len(self.commitments)
        self.commitments = [c for c in self.commitments if c.request_id != request_id]
        return n - len(self.commitments)

    def ledger(self) -> list:
        return [asdict(c) for c in sorted(self.commitments, key=lambda c: c.t_start)]
