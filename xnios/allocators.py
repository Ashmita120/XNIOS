"""Pluggable resource allocators — the "how much" layer.

Where a Scheduler decides who/where (which satellite -> which station), an Allocator
decides how much: it divides a station's shared BANDWIDTH POOL among the links
currently active on that station. Same plug-in shape as schedulers, so it's a second
experiment axis you swap and compare.

Each active link presents a LinkDemand (how much it wants, its priority, its backlog,
and a rate_fn that returns the data rate it would get for a given bandwidth). An
allocator returns {sat_id: bandwidth_hz}, each <= its want and summing to <= the pool.

Allocation only *matters* when more than one link shares a station at once (i.e. a
multi-beam station, or a scarce pool). With one link, or a generous pool, every link
simply gets its full bandwidth and the choice of allocator is a no-op.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

_MAXRATE_CHUNKS = 16   # granularity of the greedy max-rate search


@dataclass
class LinkDemand:
    sat_id: str
    want_hz: float                 # most bandwidth this link can use (sat's own bandwidth)
    priority: int
    backlog_bits: float
    rate_fn: Callable[[float], float]   # bandwidth_hz -> achievable rate (bits/s)


class Allocator(ABC):
    name: str = "allocator"

    @abstractmethod
    def allocate(self, pool_hz: float, links: list) -> dict:
        """Return {sat_id: bandwidth_hz} for the links sharing this station pool."""
        raise NotImplementedError


def _weighted(pool_hz, links, weight):
    """Split the pool proportionally to a per-link weight, capped at each want."""
    total = sum(max(weight(l), 1e-9) for l in links)
    return {l.sat_id: min(pool_hz * max(weight(l), 1e-9) / total, l.want_hz) for l in links}


class EqualAllocator(Allocator):
    """Every active link gets an equal share of the pool (capped at its want)."""
    name = "equal"

    def allocate(self, pool_hz, links):
        if not links:
            return {}
        share = pool_hz / len(links)
        return {l.sat_id: min(share, l.want_hz) for l in links}


class PriorityAllocator(Allocator):
    """Bandwidth split in proportion to customer priority (emergency > ... > research)."""
    name = "priority"

    def allocate(self, pool_hz, links):
        return _weighted(pool_hz, links, lambda l: l.priority) if links else {}


class DemandAllocator(Allocator):
    """Bandwidth split in proportion to how much data each link still owes."""
    name = "demand"

    def allocate(self, pool_hz, links):
        return _weighted(pool_hz, links, lambda l: l.backlog_bits) if links else {}


class MaxRateAllocator(Allocator):
    """Greedily hand bandwidth to whichever link gains the most rate from it —
    maximises total throughput (favours strong links). The 'optimization' option."""
    name = "maxrate"

    def allocate(self, pool_hz, links):
        if not links:
            return {}
        got = {l.sat_id: 0.0 for l in links}
        chunk = pool_hz / _MAXRATE_CHUNKS
        remaining = pool_hz
        while remaining > 1e-6:
            c = min(chunk, remaining)
            best, best_gain = None, 0.0
            for l in links:
                if got[l.sat_id] + c > l.want_hz + 1e-9:
                    continue
                gain = l.rate_fn(got[l.sat_id] + c) - l.rate_fn(got[l.sat_id])
                if gain > best_gain:
                    best, best_gain = l, gain
            if best is None:               # everyone at their want, or no gain left
                break
            got[best.sat_id] += c
            remaining -= c
        return got


class LPAllocator(Allocator):
    """Provably-optimal bandwidth split for maximum total throughput, via a linear
    program over the concave rate(bandwidth) curves (each curve approximated by its
    tangent upper-envelope). Like `maxrate` but exact, and an LP framework that can
    later take extra constraints (e.g. per-link minimum-rate SLA)."""
    name = "lp"
    _SAMPLES = 6

    def allocate(self, pool_hz, links):
        import numpy as np
        from scipy.optimize import linprog
        if not links:
            return {}
        n = len(links)
        # variables: b_0..b_{n-1} (MHz), r_0..r_{n-1} (Mbps); maximise sum r.
        A_ub, b_ub = [], []
        for i, l in enumerate(links):
            want_mhz = l.want_hz / 1e6
            for s in range(1, self._SAMPLES + 1):
                b_hz = (want_mhz * s / self._SAMPLES) * 1e6      # tangent point
                eps = max(b_hz * 1e-3, 1e3)
                r0, r1 = l.rate_fn(b_hz) / 1e6, l.rate_fn(b_hz + eps) / 1e6
                slope = (r1 - r0) / (eps / 1e6)                  # Mbps per MHz
                intercept = r0 - slope * (b_hz / 1e6)
                row = [0.0] * (2 * n)
                row[i] = -slope                                  # r_i - slope*b_i <= intercept
                row[n + i] = 1.0
                A_ub.append(row); b_ub.append(intercept)
        A_ub.append([1.0] * n + [0.0] * n); b_ub.append(pool_hz / 1e6)   # sum b <= pool
        c = [0.0] * n + [-1.0] * n
        bounds = [(0, l.want_hz / 1e6) for l in links] + [(0, None) for _ in links]
        res = linprog(c, A_ub=np.array(A_ub), b_ub=np.array(b_ub), bounds=bounds, method="highs")
        if not res.success:
            share = pool_hz / n                                  # fallback: equal
            return {l.sat_id: min(share, l.want_hz) for l in links}
        return {l.sat_id: float(max(0.0, res.x[i])) * 1e6 for i, l in enumerate(links)}


ALLOCATORS = {a.name: a for a in [EqualAllocator, PriorityAllocator,
                                  DemandAllocator, MaxRateAllocator, LPAllocator]}


def make_allocator(name: str) -> Allocator:
    return ALLOCATORS[name]()


# --------------------------------------------------------------------------- #
# Power allocators — set each active link's TRANSMIT POWER (bounded by the
# satellite's max), trading throughput against energy. Unlike bandwidth there is
# no shared pool (a satellite has one link), so this is per-link power *control*.
# --------------------------------------------------------------------------- #
_PWR_MIN_W = 0.5           # floor a link may be dialed down to
_PWR_STEPS = 12            # search granularity for adaptive / min-energy


@dataclass
class PowerDemand:
    sat_id: str
    nominal_w: float               # the 'fixed' power (satellite's tx_power_w)
    max_w: float                   # cap (satellite's tx_power_max_w)
    rate_fn: Callable[[float], float]   # power_w -> achievable rate (at its allocated bandwidth)


class PowerAllocator(ABC):
    name: str = "power"

    @abstractmethod
    def allocate(self, links: list) -> dict:
        """Return {sat_id: power_w} for the active links."""
        raise NotImplementedError


def _levels(lo, hi):
    return [lo + (hi - lo) * i / (_PWR_STEPS - 1) for i in range(_PWR_STEPS)]


class FixedPower(PowerAllocator):
    """Every link transmits at its nominal power (the baseline / current behaviour)."""
    name = "fixed"

    def allocate(self, links):
        return {l.sat_id: l.nominal_w for l in links}


class AdaptivePower(PowerAllocator):
    """Boost power on links that still gain rate from it (weak / rain-faded); dial
    down links already saturated (extra power wasted) to save energy."""
    name = "adaptive"

    def allocate(self, links):
        out = {}
        for l in links:
            r_nom, r_max = l.rate_fn(l.nominal_w), l.rate_fn(l.max_w)
            if r_max > r_nom * 1.02:                 # more power meaningfully helps -> boost
                out[l.sat_id] = l.max_w
            else:                                    # saturated -> least power keeping ~r_nom
                out[l.sat_id] = _min_power_for(l, 0.99 * r_nom)
        return out


class MinEnergyPower(PowerAllocator):
    """Use the least power that still reaches ~95% of the best achievable rate —
    maximises energy efficiency (Gb/kJ), giving up a little throughput."""
    name = "minenergy"

    def allocate(self, links):
        return {l.sat_id: _min_power_for(l, 0.95 * l.rate_fn(l.max_w)) for l in links}


def _min_power_for(link, target_rate):
    """Smallest power (from the search grid) whose rate >= target_rate."""
    for p in _levels(_PWR_MIN_W, link.max_w):
        if link.rate_fn(p) >= target_rate:
            return p
    return link.max_w


POWER_ALLOCATORS = {a.name: a for a in [FixedPower, AdaptivePower, MinEnergyPower]}


def make_power_allocator(name: str) -> PowerAllocator:
    return POWER_ALLOCATORS[name]()


# --------------------------------------------------------------------------- #
# Frequency allocators — assign a channel to each beam of a phased-array station.
# Two beams on the SAME channel interfere if they are angularly close (< ~a couple
# of beamwidths); different channels never interfere. So this is graph colouring:
# nodes = beams, edges = angularly-close pairs, colours = channels.
# --------------------------------------------------------------------------- #
@dataclass
class BeamNode:
    sat_id: str
    az_deg: float
    elev_deg: float


class FreqAllocator(ABC):
    name: str = "freq"

    @abstractmethod
    def allocate(self, beams: list, n_channels: int, sep_fn) -> dict:
        """Return {sat_id: channel_index}. sep_fn(a, b) -> angular separation (deg)."""
        raise NotImplementedError


class SameChannel(FreqAllocator):
    """All beams on channel 0 — no frequency reuse (worst case: full interference)."""
    name = "same"

    def allocate(self, beams, n_channels, sep_fn):
        return {b.sat_id: 0 for b in beams}


class GraphColorFreq(FreqAllocator):
    """Greedy graph colouring: give each beam the lowest channel not used by an
    already-assigned, angularly-close beam. Falls back to the least-conflicting
    channel when all channels are taken."""
    name = "coloring"

    def __init__(self, near_factor: float = 2.0):
        self.near_factor = near_factor      # 'close' = within near_factor * beamwidth

    def allocate(self, beams, n_channels, sep_fn):
        assigned = {}
        order = sorted(beams, key=lambda b: b.elev_deg, reverse=True)  # strongest first
        for b in order:
            conflict = {}
            for other_id, ch in assigned.items():
                sep, thresh = sep_fn(b.sat_id, other_id)
                if sep < thresh:                       # angularly close -> would interfere
                    conflict[ch] = conflict.get(ch, 0) + 1
            free = [c for c in range(n_channels) if c not in conflict]
            assigned[b.sat_id] = free[0] if free else min(range(n_channels),
                                                          key=lambda c: conflict.get(c, 0))
        return assigned


FREQ_ALLOCATORS = {a.name: a for a in [SameChannel, GraphColorFreq]}


def make_freq_allocator(name: str) -> FreqAllocator:
    return FREQ_ALLOCATORS[name]()
