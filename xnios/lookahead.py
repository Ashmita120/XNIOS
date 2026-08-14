"""Real-time lookahead — the forecast, shaped for a decision loop.

`forecast.py` answers questions exactly but not cheaply: one `contact_windows`
call costs ~0.5 ms, and a controller that asked it per satellite per station per
step would spend more time forecasting than deciding. This module turns that
into a service a scheduler can call inside `decide()` without noticing:

    look = Lookahead(sats, stations, weather, span_s=11400)   # ~0.5 s, once
    look.remaining_bits("SAT-007", "Delhi", t)                # ~2 us, per call

Two ideas do the work.

**Precompute the passes, not the answers.** Contact windows are closed-form
orbital mechanics — they do not change while the run proceeds — so every window
in the horizon is found once at construction. Queries are then list lookups
behind a per-pair cursor that only ever moves forward, which is exactly how a
real-time loop walks time. Cost per query is O(1) amortised.

**Carry capacity, not just duration.** A scheduler does not want "the pass ends
in 412 s"; it wants "you can move 18.4 Gbit through it". Each pass therefore
stores a cumulative-delivered-bits curve sampled across its own span, so
`remaining_bits(t)` integrates the link budget from now to LOS by interpolation
instead of by recomputing physics. This is the single number that makes a
forecast-aware scheduler possible: it prices a beam-second correctly, and it
collapses "high elevation now but setting", "low but rising" and "long but weak"
onto one comparable scale.

What this module deliberately does NOT do: predict weather, failures or
contention. Rain is held at its value when the horizon was built (the same
honest assumption `forecast.py` makes), and nothing here is fitted to anything.
It is analytical lookahead, available in real time — not a learned model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from . import forecast as fc

__all__ = ["Pass", "Lookahead"]


@dataclass
class Pass:
    """One usable contact, with the data it can carry.

    `_grid`/`_cum` are the cumulative-bits curve: `_cum[k]` is the total that
    could be delivered between `t_rise` and `_grid[k]` at the nominal link
    budget (full satellite bandwidth, nominal power, no interference, weather
    frozen). Allocators and co-channel interference only ever take away from
    that, so it is an upper bound on the pass — the right sign for a scheduler
    comparing opportunities.
    """

    sat_id: str
    station_id: str
    t_rise: float
    t_set: float
    peak_elev_deg: float
    total_bits: float
    _grid: np.ndarray = field(repr=False, default=None)
    _cum: np.ndarray = field(repr=False, default=None)

    @property
    def duration_s(self) -> float:
        return self.t_set - self.t_rise

    def contains(self, t: float) -> bool:
        return self.t_rise <= t <= self.t_set

    def remaining_bits(self, t: float) -> float:
        """Data still deliverable between `t` and loss of signal."""
        if t <= self.t_rise:
            return self.total_bits
        if t >= self.t_set:
            return 0.0
        return max(0.0, self.total_bits - float(np.interp(t, self._grid, self._cum)))

    def bits_until(self, t: float, t_end: float) -> float:
        """Data deliverable in [t, t_end], clipped to the pass."""
        if t_end <= self.t_rise or t >= self.t_set:
            return 0.0
        lo = float(np.interp(max(t, self.t_rise), self._grid, self._cum))
        hi = float(np.interp(min(t_end, self.t_set), self._grid, self._cum))
        return max(0.0, hi - lo)

    def time_for_bits(self, t_from: float, bits: float) -> float | None:
        """When `bits` have been delivered, starting at `t_from`. None if never.

        The inverse of the cumulative curve. This is what makes a plan able to
        say "completes at t+143 s" instead of quoting the end of the window —
        a transfer that only needs part of a pass finishes inside it, and
        deadline compliance depends on the difference.
        """
        start = max(t_from, self.t_rise)
        if bits <= 0:
            return start
        target = float(np.interp(start, self._grid, self._cum)) + bits
        if target > self._cum[-1] + 1e-6:
            return None                      # the pass cannot carry that much
        return float(np.interp(target, self._cum, self._grid))


class Lookahead:
    """Every contact in the horizon, indexed for O(1) real-time queries.

    Build cost is one vectorised sweep per (satellite, station) pair and scales
    with the horizon, not with how often it is asked. `stats()` reports it, so
    the startup cost is visible rather than hidden inside a decision latency.

    A run whose horizon outlives the span rebuilds in place (see `ensure`). That
    is a latency spike by construction, so the default span is sized to cover a
    whole run plus its lookahead and never fire.
    """

    def __init__(self, sats, stations, weather=None, t0: float = 0.0,
                 span_s: float = 11400.0, step_s: float = 10.0,
                 grid_s: float = 15.0, max_samples: int = 96,
                 refresh_margin_s: float = 300.0):
        self._sats = list(sats)
        self._stations = list(stations)
        self._weather = weather
        self.span_s = float(span_s)
        self.step_s = float(step_s)
        self.grid_s = float(grid_s)
        self.max_samples = int(max_samples)
        self.refresh_margin_s = float(refresh_margin_s)

        self.build_ms = 0.0
        self.builds = 0
        self.queries = 0
        self._last_t = -1e18
        self.build(t0, t0 + self.span_s)

    # ---------------------------------------------------------------- build
    def _rain(self, station_id: str, t: float) -> float:
        return self._weather.fade_db(station_id, t) if self._weather is not None else 0.0

    def build(self, t0: float, t1: float) -> None:
        """(Re)compute every pass in [t0, t1]. Called once at construction."""
        wall = time.perf_counter()
        self.passes: dict[tuple[str, str], list[Pass]] = {}
        self.by_sat: dict[str, list[Pass]] = {}

        for s in self._sats:
            merged: list[Pass] = []
            for g in self._stations:
                rain = self._rain(g.id, t0)
                # refine_peak=False: the peak elevation is a display quantity,
                # and refining it costs about as much as finding the window.
                windows = fc.contact_windows(s, g, t0, t1, step_s=self.step_s,
                                             rain_zenith_db=rain, refine_peak=False)
                ps = [self._with_capacity(s, g, w, rain) for w in windows]
                self.passes[(s.id, g.id)] = ps
                merged.extend(ps)
            merged.sort(key=lambda p: (p.t_rise, p.station_id))
            self.by_sat[s.id] = merged

        self._cur = {k: 0 for k in self.passes}
        self._cur_sat = {sid: 0 for sid in self.by_sat}
        self.span = (float(t0), float(t1))
        self._last_t = -1e18
        self.build_ms = (time.perf_counter() - wall) * 1e3
        self.builds += 1

    def _with_capacity(self, sat, station, w, rain: float) -> Pass:
        """Attach the cumulative-bits curve to a geometric window."""
        n = int(np.clip(round(w.duration_s / self.grid_s) + 1, 3, self.max_samples))
        grid = np.linspace(w.t_rise, w.t_set, n)
        elev, rng = fc.elevation_series(sat, station, grid)
        rate = fc.rate_series(sat, station, elev, rng, rain_zenith_db=rain)
        # trapezoid: bits between consecutive samples, accumulated
        cum = np.concatenate(
            ([0.0], np.cumsum(0.5 * (rate[1:] + rate[:-1]) * np.diff(grid))))
        return Pass(sat_id=w.sat_id, station_id=w.station_id,
                    t_rise=w.t_rise, t_set=w.t_set,
                    peak_elev_deg=w.peak_elev_deg,
                    total_bits=float(cum[-1]), _grid=grid, _cum=cum)

    def ensure(self, t: float) -> None:
        """Keep the horizon ahead of `t`, rebuilding only when it runs out."""
        if t > self.span[1] - self.refresh_margin_s:
            self.build(t, t + self.span_s)

    # ---------------------------------------------------------------- query
    def _seek(self, t: float) -> None:
        """Cursors only move forward. If time goes backwards (a replay, or a
        `Simulator.snapshot` probe at an arbitrary instant), reset them."""
        if t < self._last_t:
            for k in self._cur:
                self._cur[k] = 0
            for k in self._cur_sat:
                self._cur_sat[k] = 0
        self._last_t = t

    def pass_at(self, sat_id: str, station_id: str, t: float) -> Pass | None:
        """The contact in progress on this link at `t`, or None."""
        self.queries += 1
        ps = self.passes.get((sat_id, station_id))
        if not ps:
            return None
        self._seek(t)
        key = (sat_id, station_id)
        i = self._cur[key]
        while i < len(ps) and ps[i].t_set < t:
            i += 1
        self._cur[key] = i
        if i < len(ps) and ps[i].t_rise <= t:
            return ps[i]
        return None

    def time_to_los(self, sat_id: str, station_id: str, t: float) -> float:
        """Seconds until this link drops, or -1 if it is not up now."""
        p = self.pass_at(sat_id, station_id, t)
        return (p.t_set - t) if p is not None else -1.0

    def remaining_bits(self, sat_id: str, station_id: str, t: float) -> float:
        """Data still deliverable on the contact in progress. 0 if none is."""
        p = self.pass_at(sat_id, station_id, t)
        return p.remaining_bits(t) if p is not None else 0.0

    def _sat_passes_from(self, sat_id: str, t: float):
        """Passes of `sat_id` that have not finished by `t`, in time order."""
        ps = self.by_sat.get(sat_id)
        if not ps:
            return ()
        self._seek(t)
        i = self._cur_sat[sat_id]
        while i < len(ps) and ps[i].t_set < t:
            i += 1
        self._cur_sat[sat_id] = i
        return ps[i:]

    def future_bits(self, sat_id: str, t: float, horizon_s: float,
                    exclude_station: str | None = None) -> float:
        """Total data this satellite could still move before `t + horizon_s`.

        The opportunity-cost term: a satellite with hours of contact ahead can
        afford to yield a beam now; one whose next chance is 80 minutes away
        cannot. `exclude_station` drops the link being scored, so the answer is
        "what else do I have".
        """
        t_end = t + horizon_s
        total = 0.0
        for p in self._sat_passes_from(sat_id, t):
            if p.t_rise >= t_end:
                break
            if exclude_station is not None and p.station_id == exclude_station \
                    and p.contains(t):
                continue
            total += p.bits_until(t, t_end)
        return total

    def next_contact(self, sat_id: str, t: float) -> dict | None:
        """The soonest contact at or after `t`: station, wait, span, capacity."""
        for p in self._sat_passes_from(sat_id, t):
            return {"station": p.station_id,
                    "wait_s": max(0.0, p.t_rise - t),
                    "t_aos": p.t_rise, "t_los": p.t_set,
                    "window_s": p.duration_s,
                    "capacity_bits": p.remaining_bits(t),
                    "peak_elev_deg": p.peak_elev_deg}
        return None

    # ---------------------------------------------------------------- report
    def stats(self) -> dict:
        n = sum(len(v) for v in self.passes.values())
        return {"passes": n, "pairs": len(self.passes), "builds": self.builds,
                "build_ms": self.build_ms, "queries": self.queries,
                "span_s": self.span[1] - self.span[0],
                "capacity_gbit": sum(p.total_bits for ps in self.passes.values()
                                     for p in ps) / 1e9}
