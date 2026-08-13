"""Pluggable decision makers.

Everything the research plan calls a "scheduling algorithm" or "station-selection
algorithm" is a Scheduler here. A scheduler is *stateless* w.r.t. the world: it
reads a NetworkState and returns conflict-free Assignments for currently-free
satellites onto free beams. Active sessions are sticky (the simulator keeps them
running until the buffer drains or the pass ends), so a scheduler only ever fills
spare capacity — no thrashing.

One configurable `GreedyScheduler` spans the whole classical grid:
  order_key   in {fcfs, priority, edf, sjf, ljf, random}   # Phase 2 (which sat)
  station_key in {nearest, highest_elev, strongest,         # Phase 3 (which station)
                  least_loaded, random}
Named subclasses (FCFS, EDF, ...) are just presets for readable experiment code.

Above them sit three joint solvers, in increasing order of what they know:
  HungarianScheduler  optimal matching of the CURRENT instant (myopic)
  MIPScheduler        the same optimum as a MILP, extensible with real constraints
  HorizonScheduler    optimal matching of the current *pass* — it prices each link
                      by the data it will carry before LOS, from xnios.lookahead
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod

import numpy as np
from scipy.optimize import linear_sum_assignment, milp, LinearConstraint, Bounds

from .state import NetworkState, Assignment


class Scheduler(ABC):
    name: str = "scheduler"

    def bind(self, scenario, sim_cfg):
        """Optional hook, called once before a run. Optimisation schedulers use it to
        get scenario access for look-ahead. Default: no-op (state is enough)."""
        pass

    @abstractmethod
    def decide(self, state: NetworkState) -> list[Assignment]:
        """Return assignments of free satellites onto free station beams."""
        raise NotImplementedError


# --- ordering keys: sort *free* satellites (first served first) ----------------
def _order(sats, key: str, rng: random.Random):
    inf = float("inf")
    if key == "fcfs":       # earliest ready first
        return sorted(sats, key=lambda s: (s.ready_since if s.ready_since is not None else inf, s.sat_id))
    if key == "priority":   # highest priority first
        return sorted(sats, key=lambda s: (-s.priority, s.sat_id))
    if key == "edf":        # earliest deadline first
        return sorted(sats, key=lambda s: (s.deadline_s if s.deadline_s is not None else inf, s.sat_id))
    if key == "sjf":        # least data remaining first
        return sorted(sats, key=lambda s: (s.backlog_bits, s.sat_id))
    if key == "ljf":        # most data remaining first
        return sorted(sats, key=lambda s: (-s.backlog_bits, s.sat_id))
    if key == "random":
        out = list(sats)
        rng.shuffle(out)
        return out
    raise ValueError(f"unknown order_key: {key}")


# --- station keys: pick the best station for a satellite -----------------------
def _station_score(vis, key: str, station_free: dict, rng: random.Random):
    """Lower score = better (so we can always min())."""
    if key == "nearest":
        return vis.range_km
    if key == "highest_elev":
        return -vis.elev_deg
    if key == "strongest":
        return -vis.rate_bps
    if key == "least_loaded":
        # fewer remaining free beams = more loaded -> prefer more free beams
        return -station_free.get(vis.station_id, 0)
    if key == "random":
        return rng.random()
    raise ValueError(f"unknown station_key: {key}")


class GreedyScheduler(Scheduler):
    def __init__(self, order_key: str = "fcfs", station_key: str = "strongest", seed: int = 0):
        self.order_key = order_key
        self.station_key = station_key
        self.name = f"greedy[{order_key}/{station_key}]"
        self._rng = random.Random(seed)

    def decide(self, state: NetworkState) -> list[Assignment]:
        # remaining free beams per station (mutated as we assign)
        free = {sid: st.free_beams for sid, st in state.stations.items()}
        assignments: list[Assignment] = []

        for sat in _order(state.free_sats(), self.order_key, self._rng):
            # candidate stations that can see this sat AND still have a free beam
            cands = [v for v in state.visible_for(sat.sat_id) if free.get(v.station_id, 0) > 0]
            if not cands:
                continue  # nobody free can serve it -> it waits
            best = min(cands, key=lambda v: _station_score(v, self.station_key, free, self._rng))
            assignments.append(Assignment(sat.sat_id, best.station_id))
            free[best.station_id] -= 1

        return assignments


# --- readable presets ----------------------------------------------------------
class RandomScheduler(GreedyScheduler):
    def __init__(self, seed: int = 0):
        super().__init__(order_key="random", station_key="random", seed=seed)
        self.name = "random"


class FCFS(GreedyScheduler):
    def __init__(self, station_key: str = "strongest"):
        super().__init__(order_key="fcfs", station_key=station_key)
        self.name = "fcfs"


class PriorityScheduler(GreedyScheduler):
    def __init__(self, station_key: str = "strongest"):
        super().__init__(order_key="priority", station_key=station_key)
        self.name = "priority"


class EDF(GreedyScheduler):
    def __init__(self, station_key: str = "strongest"):
        super().__init__(order_key="edf", station_key=station_key)
        self.name = "edf"


class SJF(GreedyScheduler):
    def __init__(self, station_key: str = "strongest"):
        super().__init__(order_key="sjf", station_key=station_key)
        self.name = "sjf"


# --------------------------------------------------------------------------- #
# Optimisation schedulers (scipy, no OR-Tools). They fill the gap between the
# greedy heuristics and the offline oracle: an executable *optimal* assignment.
# --------------------------------------------------------------------------- #
def _free_beam_slots(state):
    """Expand each station's free beams into a flat list of slot -> station_id."""
    slots = []
    for gid, stv in state.stations.items():
        slots.extend([gid] * stv.free_beams)
    return slots


class HungarianScheduler(Scheduler):
    """Optimal one-to-one assignment of free satellites to free beams at THIS instant
    (maximise total value), via the Hungarian algorithm. Unlike greedy (which commits
    satellite-by-satellite), it solves the whole matching jointly. Myopic: it uses the
    instantaneous link rate, with no look-ahead. objective 'throughput' = rate,
    'priority' = rate x tier."""

    def __init__(self, objective: str = "throughput"):
        self.objective = objective
        self.name = f"hungarian/{objective}"

    def _value(self, sat, v):
        return v.rate_bps * (sat.priority if self.objective == "priority" else 1.0)

    def decide(self, state):
        free = state.free_sats()
        slots = _free_beam_slots(state)
        if not free or not slots:
            return []
        vis = {(v.sat_id, v.station_id): v for v in state.visibilities}
        NEG = -1e18
        val = np.full((len(free), len(slots)), NEG)
        for i, s in enumerate(free):
            for j, gid in enumerate(slots):
                v = vis.get((s.sat_id, gid))
                if v is not None and v.rate_bps > 0:
                    val[i, j] = self._value(s, v)
        rows, cols = linear_sum_assignment(-val)          # maximise -> minimise -value
        return [Assignment(free[i].sat_id, slots[j])
                for i, j in zip(rows, cols) if val[i, j] > NEG / 2]


class HorizonScheduler(Scheduler):
    """Forecast-aware assignment: rank links by the data they will actually carry.

    Every other scheduler here ranks a link by its rate *at this instant*. That
    is the wrong quantity, and it is wrong in a way that costs real throughput:
    a satellite at 70 deg and setting has a magnificent rate and 20 seconds to
    use it, most of which the beam spends slewing; one at 35 deg and rising has
    a worse rate and eight minutes. Myopic policies take the first, deliver
    almost nothing, and hand the beam back.

    This scheduler asks `xnios.lookahead` for the integral instead — the bits
    deliverable between now and loss of signal, setup time already subtracted —
    and solves the whole free-satellites-to-free-beams matching jointly with the
    Hungarian algorithm. No physics is re-derived and nothing is learned: the
    contact windows are the same closed-form orbital mechanics the simulator
    runs on, precomputed once and looked up in microseconds.

    Objectives:
      throughput  value = bits deliverable on this pass
      urgency     + opportunity cost: how much of the backlog can ONLY be moved
                  now, i.e. what no future contact inside the horizon can cover.
                  Parameter-free — it is a difference of two forecasts.
      sla         urgency, scaled by tier and by how close the deadline is.
    """

    OBJECTIVES = {"throughput": 0.0, "urgency": 1.0, "sla": 1.0}

    def __init__(self, objective: str = "urgency", horizon_s: float = 5400.0,
                 span_pad_s: float = 5400.0):
        if objective not in self.OBJECTIVES:
            raise ValueError(f"unknown objective: {objective}")
        self.objective = objective
        self.urgency_w = self.OBJECTIVES[objective]
        self.horizon_s = float(horizon_s)      # how far ahead "later" counts
        self.span_pad_s = float(span_pad_s)    # forecast past the end of the run
        self.name = f"horizon/{objective}"
        self.look = None
        self._setup = {}
        self._fallback_s = 5.0

    def bind(self, scenario, sim_cfg):
        from .lookahead import Lookahead
        self.look = Lookahead(scenario.satellites, scenario.stations,
                              weather=scenario.weather, t0=0.0,
                              span_s=float(sim_cfg.duration_s) + self.span_pad_s)
        self._setup = {g.id: g.setup_time_s for g in scenario.stations}
        self._fallback_s = float(getattr(sim_cfg, "decision_interval_s", 5.0))

    def _deadline_factor(self, sat, t: float) -> float:
        """1.0 with time to spare, rising to 2.0 at the deadline. Past it the
        SLA is already lost, so stop paying for it."""
        dl = sat.deadline_s
        if dl is None:
            return 1.0
        slack = dl - t
        if slack <= 0:
            return 0.25
        return 1.0 + max(0.0, (self.horizon_s - slack) / self.horizon_s)

    def decide(self, state):
        free = state.free_sats()
        slots = _free_beam_slots(state)
        if not free or not slots or self.look is None:
            return []
        self.look.ensure(state.t)

        stations = sorted(set(slots))
        col_of = {gid: j for j, gid in enumerate(stations)}
        vis = {(v.sat_id, v.station_id): v for v in state.visibilities}
        t = state.t
        NEG = -1e18
        val = np.full((len(free), len(stations)), NEG)

        for i, s in enumerate(free):
            # everything this satellite could still move inside the horizon,
            # across every station — the yardstick the opportunity cost uses
            total_future = (self.look.future_bits(s.sat_id, t, self.horizon_s)
                            if self.urgency_w else 0.0)
            for gid in stations:
                v = vis.get((s.sat_id, gid))
                if v is None or v.rate_bps <= 0:
                    continue                       # not usable now -> not assignable
                setup = self._setup.get(gid, 0.0)
                p = self.look.pass_at(s.sat_id, gid, t + setup)
                if p is not None:
                    bits = min(s.backlog_bits, p.remaining_bits(t + setup))
                else:
                    # The twin sees a link the forecast does not: weather has
                    # moved since the horizon was built, or the pass ends inside
                    # the setup time. Keep it assignable on its instantaneous
                    # rate so we never do worse than the myopic policy, but rank
                    # it below anything with real forecast mass behind it.
                    bits = min(s.backlog_bits, v.rate_bps * self._fallback_s)
                if bits <= 0:
                    continue

                value = bits
                if self.urgency_w:
                    later = total_future
                    if p is not None:
                        later -= p.bits_until(t, t + self.horizon_s)
                    later = max(0.0, later)
                    # bits of backlog that no future contact can cover: the part
                    # that is genuinely now-or-never
                    marginal = (min(s.backlog_bits, bits + later)
                                - min(s.backlog_bits, later))
                    value += self.urgency_w * max(0.0, marginal)
                if self.objective == "sla":
                    value *= s.priority * self._deadline_factor(s, t)
                val[i, col_of[gid]] = value

        if not (val > NEG / 2).any():
            return []

        # one column per free beam, so a station with 4 spare beams can take 4
        cols = [col_of[gid] for gid in slots]
        rows, picks = linear_sum_assignment(-val[:, cols])
        return [Assignment(free[i].sat_id, slots[j])
                for i, j in zip(rows, picks) if val[i, cols[j]] > NEG / 2]


class MIPScheduler(Scheduler):
    """Throughput-optimal assignment of free satellites to free beams via a MILP
    (scipy/HiGHS). For plain assignment this finds the SAME optimum as Hungarian, but
    as a general MILP it is the framework you extend with constraints Hungarian can't
    express (min-rate SLA guarantees, interference coupling between phased-array beams).
    It is slower than Hungarian for the same result — the quality-vs-runtime trade-off
    the Dec(ms) column makes visible."""

    def __init__(self):
        self.name = "mip"

    def decide(self, state):
        free = state.free_sats()
        slots = _free_beam_slots(state)
        if not free or not slots:
            return []
        rate = {(v.sat_id, v.station_id): v.rate_bps for v in state.visibilities}
        n, m = len(free), len(slots)

        c = np.zeros(n * m)                                # minimise -throughput
        for i, s in enumerate(free):
            for j, gid in enumerate(slots):
                c[i * m + j] = -rate.get((s.sat_id, gid), 0.0) / 1e9
        if not c.any():
            return []

        A = np.zeros((n + m, n * m))
        for i in range(n):
            for j in range(m):
                A[i, i * m + j] = 1.0                      # each satellite at most once
                A[n + j, i * m + j] = 1.0                  # each beam slot at most once
        res = milp(c, constraints=LinearConstraint(A, -np.inf, np.ones(n + m)),
                   integrality=np.ones(n * m), bounds=Bounds(0, 1),
                   options={"time_limit": 10})
        if res.x is None:
            return []
        x = res.x.reshape(n, m)
        return [Assignment(free[i].sat_id, slots[j])
                for i in range(n) for j in range(m)
                if x[i, j] > 0.5 and rate.get((free[i].sat_id, slots[j]), 0) > 0]
