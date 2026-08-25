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
    """One booked slice of one contact.

    `t_start`/`t_end` bound the SLICE — when the transfer starts and when it has
    moved what this window owes it. That is usually shorter than the contact
    itself, because a request that needs 67 s of a 257 s pass books 67 s and
    leaves the rest for someone else. `pass_t_rise`/`pass_t_set` carry the
    containing contact, so the two are never confused for one another.
    """

    station: str
    t_start: float
    t_end: float
    deliverable_gbit: float
    peak_elev_deg: float
    contended: bool = False          # capacity was reduced by existing commitments
    pass_t_rise: float = 0.0
    pass_t_set: float = 0.0

    @property
    def duration_s(self) -> float:
        """How long the transfer occupies this contact."""
        return self.t_end - self.t_start

    @property
    def contact_s(self) -> float:
        """How long the contact itself lasts, booked or not."""
        return self.pass_t_set - self.pass_t_rise

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration_s"] = self.duration_s
        d["contact_s"] = self.contact_s
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
    quota_remaining_gbit: float | None = None    # None = unmetered account
    quota_limited: bool = False                  # shortfall is the quota, not the network
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
    customer_id: str | None = None


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

    def _next_free_contact(self, sat_id: str, t_now: float) -> dict | None:
        """The soonest contact this satellite could actually still use.

        `Lookahead.next_contact` answers a geometric question — when does this
        satellite next rise over a station — and knows nothing about the ledger.
        On the reject path that produced a straight contradiction: a request
        turned down for "no usable contact before the deadline" was offered
        "Ahmedabad-SAC, in 00:00, capacity 34.6 Gbit", which is the contact it
        had just been refused, every second of it already promised to an earlier
        request.

        So this asks the question the operator meant: the first contact with
        capacity nobody has booked, and how much of it is really free.
        """
        sat_busy = [(c.t_start, c.t_end) for c in self.commitments
                    if c.satellite_id == sat_id]
        horizon_end = t_now + self.horizon_s
        for p in self.look.by_sat.get(sat_id, ()):
            if p.t_set <= t_now or p.t_rise >= horizon_end:
                continue
            _g, free_station, _c = self._available(p, t_now, horizon_end)
            free = self._subtract(free_station, sat_busy) if free_station else []
            if not free:
                continue
            gbit = sum(p.bits_until(a, b) for a, b in free) / 1e9
            if gbit <= 1e-9:
                continue
            return {"station": p.station_id,
                    "in_s": max(0.0, free[0][0] - t_now),
                    "deliverable_gbit": gbit}
        return None

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

        # shortfall starts at the WHOLE request and is reduced by what gets
        # scheduled. It must not default to 0: every early return below (unknown
        # satellite, exhausted quota, no contact) exits with nothing scheduled,
        # and a caller asking "was this covered?" via shortfall would read a
        # rejection as a complete success.
        plan = CommPlan(request_id=req.request_id, satellite_id=req.satellite_id,
                        data_volume_gbit=need_gbit, decision=Decision.REJECT,
                        admitted=False, reason_code="", shortfall_gbit=need_gbit,
                        customer_id=req.customer_id, tier=tier, priority=priority)

        if req.satellite_id not in self.sats:
            plan.reason_code = "unknown_satellite"
            plan.explanation.append(f"'{req.satellite_id}' is not in the network")
            return plan

        # Quota caps how much of this request may be ADMITTED. It is charged
        # against bookings, so a quote never consumes it.
        quota_left = self.quota_remaining(req.customer_id)
        plan.quota_remaining_gbit = None if quota_left == float("inf") else quota_left
        if quota_left <= 0:
            plan.decision = Decision.REJECT
            plan.reason_code = "quota_exhausted"
            plan.explanation.append(
                f"Account {req.customer_id} has consumed its "
                f"{self.customers[req.customer_id].quota_gbit:.1f} Gbit quota")
            return plan
        allowance = min(need_gbit, quota_left)

        t_limit = (req.deadline_s if req.timing is TimingIntent.BY_DEADLINE
                   and req.deadline_s is not None else t_now + self.horizon_s)

        # A satellite is on one link at a time, so contacts already booked for it
        # by other requests block every station, not just the one they use. That
        # has to be applied HERE, while candidates are built: ordering and
        # capacity both read from this list, and station-level values are not
        # what this request can actually have. Leaving it to the fill loop made
        # Ahmedabad look like it started at T+0 and Delhi at T+7 when both were
        # blocked until T+67 — so a 16 Gbit window outranked a 52 Gbit one and
        # split a request across two contacts that one could have carried.
        sat_busy0 = [(c.t_start, c.t_end) for c in self.commitments
                     if c.satellite_id == req.satellite_id]

        # every contact this satellite has left, with the capacity actually free
        cands = []
        for p in self.look.by_sat.get(req.satellite_id, ()):
            if p.t_set <= t_now or p.t_rise >= t_limit:
                continue
            _gbit_station, free_station, contended = self._available(p, t_now, t_limit)
            if not free_station:
                continue
            free = self._subtract(free_station, sat_busy0)
            if not free:
                continue
            gbit = sum(p.bits_until(a, b) for a, b in free) / 1e9
            if gbit <= 0:
                continue
            cands.append((p, gbit, free[0][0], free,
                          contended or len(free) != len(free_station)))

        if not cands:
            plan.reason_code = "no_contact_in_horizon"
            plan.explanation.append(
                f"No usable contact for {req.satellite_id} "
                + (f"before the deadline (t+{t_limit:.0f} s)"
                   if req.timing is TimingIntent.BY_DEADLINE
                   else f"within {self.horizon_s / 3600:.0f} h"))
            plan.next_opportunity = self._next_free_contact(req.satellite_id, t_now)
            if plan.next_opportunity is not None:
                n = plan.next_opportunity
                plan.explanation.append(
                    f"The soonest contact with capacity still free is "
                    f"{n['station']} in {n['in_s']:.0f} s ({n['deliverable_gbit']:.1f} Gbit)")
            return plan

        order = self._order(cands, req)
        remaining = allowance
        # `free` already excludes other requests' bookings; this tracks what
        # THIS plan books as it goes, so its own windows cannot overlap either
        sat_busy = list(sat_busy0)
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
                contended=contended or len(usable) != len(free),
                pass_t_rise=p.t_rise, pass_t_set=p.t_set))
            sat_busy.append((t_start, t_done))
            remaining -= took

        plan.schedule.sort(key=lambda w: w.t_start)
        plan.scheduled_gbit = sum(w.deliverable_gbit for w in plan.schedule)
        plan.shortfall_gbit = max(0.0, need_gbit - plan.scheduled_gbit)
        # was the shortfall the network's fault, or the account's allowance?
        plan.quota_limited = (allowance < need_gbit - 1e-9
                              and plan.scheduled_gbit >= allowance - 1e-6)
        self._decide(plan, req, cands, t_now, t_limit)
        return plan

    def _order(self, cands, req: CommRequest) -> list:
        """Which windows to use first.

        ASAP / BY_DEADLINE take contacts in time order — the soonest completion.
        FLEXIBLE takes the fattest windows first, which is what "sometime today,
        optimise for network efficiency" actually means: fewer, better passes,
        leaving the scarce short ones for requests that have no choice.

        Ties on start time are DELIBERATELY not broken by capacity, and the
        reason is measured rather than aesthetic.

        A satellite is often visible from two stations at once, so candidates
        routinely come free at the same instant. Preferring the fatter window
        there does help one request — an 18.4 Gbit job that would otherwise
        split across a 16 Gbit contact and a 2 Gbit tail finishes in a single
        contact, 21 s sooner. But it also makes every request grab the best
        window it can see, which starves the request that had no alternative.
        Measured over 4 regimes x 5 paired worlds, that cost the 0-trivial
        control 2.8 pp of weighted completion (95.8 -> 93.0) and broke the tie
        that control exists to demonstrate; capping the preference at the
        request's own need did not recover it.

        So the greedy version is left out. Choosing between equally-early
        windows is exactly the opportunity-cost decision `plan_batch(policy=
        "oppcost")` exists to make with the whole request set in view; FCFS is
        the baseline it is measured against and should not be quietly clever.
        A surplus window costs nothing anyway — execution releases it.
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
        # Contacts this plan did NOT take. Excluding the ones it did matters:
        # reporting a window as an "alternative" while the schedule is already
        # using it is how Delhi ended up listed both as contact 2 and as the
        # next opportunity 8 hours later.
        used = {(w.station, round(w.pass_t_rise, 3)) for w in plan.schedule}
        spare = [c for c in cands if (c[0].station_id, round(c[0].t_rise, 3)) not in used]

        # Two different questions, and they had been conflated under one label.
        # `next_opportunity` is the EARLIEST unused contact — what an operator
        # who dislikes this plan wants. `best_later` is the FATTEST one, which is
        # what the flexible-timing deferral decision needs; it is not reported.
        nxt = min(spare, key=lambda c: c[2]) if spare else None
        best_later = max(spare, key=lambda c: c[1]) if spare else None
        if nxt is not None:
            plan.next_opportunity = {
                "station": nxt[0].station_id,
                "in_s": max(0.0, nxt[2] - t_now),
                "deliverable_gbit": nxt[1],
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
            if plan.quota_limited:
                plan.reason_code = "quota_exceeded"
                plan.explanation.append(
                    f"Account quota allows {plan.quota_remaining_gbit:.1f} Gbit more; "
                    f"{plan.data_volume_gbit:.1f} Gbit was requested")
            else:
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
            wait = first.t_start - t_now
            # Two different reasons a transfer cannot start yet, and saying the
            # wrong one is worse than saying nothing. Compare when the CONTACT
            # opens against when this request may USE it: if the contact is
            # already open by then, the delay is other bookings, not geometry.
            if first.t_start > first.pass_t_rise + 1.0:
                plan.explanation.append(
                    f"{first.station} is in contact from T+{first.pass_t_rise:.0f} s, "
                    f"but {req.satellite_id} is committed to earlier requests until "
                    f"T+{first.t_start:.0f} s")
            else:
                plan.explanation.append(
                    f"Next usable contact is {first.station} in {wait:.0f} s")

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
            if len(plan.schedule) > 1:
                # The quote assumes nominal transmit power; execution runs an
                # adaptive allocator that can beat it, so later contacts are
                # reserved rather than predicted. Unused ones are released.
                plan.explanation.append(
                    f"The last {len(plan.schedule) - 1} contact(s) are reserved as "
                    f"headroom — the quote assumes nominal transmit power, and any "
                    f"that go unused are released back automatically")
        b = plan.beam_requirement
        if b and not b["within_scan_envelope"]:
            plan.explanation.append(
                f"WARNING scan angle {b['scan_angle_deg']:.0f} deg exceeds the "
                f"array's {self.stations[first.station].max_scan_deg:.0f} deg envelope")
        if any(w.contended for w in plan.schedule):
            plan.explanation.append("Some capacity is shared with existing commitments")

    # -------------------------------------------------------------- booking
    def accept(self, plan: CommPlan, allow_partial: bool = False) -> bool:
        """Book a quoted plan against the ledger. Later requests see it.

        `allow_partial` books what the network *can* do for a request it cannot
        fully satisfy. Off by default: silently half-delivering a job nobody
        agreed to half-deliver is worse than refusing it. Under contention it is
        the honest choice, which is why the multi-request study turns it on.
        """
        if not (plan.admitted or (allow_partial and plan.schedule)):
            return False
        for w in plan.schedule:
            self.commitments.append(Commitment(
                request_id=plan.request_id, satellite_id=plan.satellite_id,
                station=w.station, t_start=w.t_start, t_end=w.t_end,
                gbit=w.deliverable_gbit, customer_id=plan.customer_id))
        return True

    # ------------------------------------------------------- multi-request
    BATCH_POLICIES = ("oppcost", "fcfs")

    def _probe_available(self, req: CommRequest, t_now: float) -> float:
        """Gbit the network would give this request if it asked for everything.

        The capacity its satellite can still reach before its own deadline,
        after existing bookings — quoted, so it costs nothing and sees the live
        ledger.
        """
        probe = CommRequest(
            satellite_id=req.satellite_id, request_id=f"{req.request_id}~probe",
            customer_id=req.customer_id, data_volume_gbit=1.0e9,
            timing=req.timing, deadline_s=req.deadline_s, priority=req.priority)
        return self.plan(probe, t_now).scheduled_gbit

    def plan_batch(self, requests, t_now: float = 0.0, policy: str = "oppcost",
                   allow_partial: bool = False) -> list:
        """Book a set of competing requests together.

        `oppcost` scores each unbooked request by

            weight x min(1, volume / capacity-still-available-before-its-deadline)

        and books the highest, then **recomputes every remaining score against
        the updated ledger**. That recomputation is the whole point: measured
        over 4 regimes x 5 paired worlds it closes 78% of the FCFS-to-optimal
        gap at slack load and 100% under real and severe contention, matching
        the MILP in 14 of 15 contended worlds. Every *static* ordering tried —
        earliest-deadline, tier-first, deadline-first, weight/volume density —
        plateaued near 64%. Collapsing this into a sort key would throw away the
        entire benefit.

        Cost is O(n^2) quotes, which is why it is a batch call and not the
        default path for a single request. Measured 18.3 ms for 14 requests,
        against 50.2 ms for the equivalent MILP.

        `fcfs` books in submission order and quotes once per request. Kept as an
        explicit policy: it is the baseline every comparison is against, and the
        thing to fall back to when a booking looks wrong.

        Returns the plans in the order they were booked.
        """
        if policy not in self.BATCH_POLICIES:
            raise ValueError(f"unknown batch policy: {policy}")
        reqs = list(requests)

        if policy == "fcfs":
            out = []
            for r in reqs:
                plan = self.plan(r, t_now)
                self.accept(plan, allow_partial=allow_partial)
                out.append(plan)
            return out

        out, remaining = [], list(reqs)
        order = {id(r): i for i, r in enumerate(reqs)}      # stable tie-break
        while remaining:
            scored = []
            for r in remaining:
                q = self.plan(r, t_now)
                if q.shortfall_gbit > 1e-6:
                    # Cannot complete: booked after everything that can, because
                    # weighted completion counts whole requests and a partial
                    # scores nothing either way.
                    #
                    # But WITHIN that group the objective is indifferent, and it
                    # used to fall through to submission order — so the leftover
                    # capacity went to whoever queued first. A research request
                    # took 75 Gbit while a military one was rejected, which is
                    # FCFS wearing the policy's name. Once every remaining
                    # request is in this group none of them can complete no
                    # matter how they are ordered, so ranking by tier here costs
                    # the measured weighted-completion figure nothing and puts
                    # the scraps where the mission says they belong.
                    key = (1, -float(q.priority))
                else:
                    avail = self._probe_available(r, t_now)
                    ratio = q.data_volume_gbit / max(avail, 1e-9)
                    key = (0, -(q.priority * min(ratio, 1.0)))
                scored.append((key, order[id(r)], r, q))
            scored.sort(key=lambda x: (x[0], x[1]))
            _key, _i, req, plan = scored[0]
            self.accept(plan, allow_partial=allow_partial)
            out.append(plan)
            remaining.remove(req)
        return out

    def committed_gbit(self, customer_id: str | None) -> float:
        """Volume this account has actually booked. Quotas are charged here, not
        at quote time — asking what a transfer would cost must be free."""
        if customer_id is None:
            return 0.0
        return sum(c.gbit for c in self.commitments if c.customer_id == customer_id)

    def quota_remaining(self, customer_id: str | None) -> float:
        cust = self.customers.get(customer_id) if customer_id else None
        if cust is None or cust.quota_gbit is None:
            return float("inf")
        return max(0.0, cust.quota_gbit - self.committed_gbit(customer_id))

    def release(self, request_id: str) -> int:
        n = len(self.commitments)
        self.commitments = [c for c in self.commitments if c.request_id != request_id]
        return n - len(self.commitments)

    def ledger(self) -> list:
        return [asdict(c) for c in sorted(self.commitments, key=lambda c: c.t_start)]
