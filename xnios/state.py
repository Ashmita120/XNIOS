"""The observable network state passed to a Scheduler, and the Assignment it
returns. This is the *contract* between the world and any decision maker.

A scheduler receives a NetworkState (what is visible, who is free, what each link
is worth right now) and returns a list of Assignments (which free satellite to
serve on which station). The simulator validates and applies them. Nothing else
about the internal world is exposed — so a rule-based, MIP, or RL policy all see
exactly the same information.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SatView:
    sat_id: str
    backlog_bits: float
    priority: int
    deadline_s: float | None
    ready_since: float | None    # first time it became ready (visible + has data)
    is_free: bool                # not currently in an active session


@dataclass
class StationView:
    station_id: str
    num_beams: int
    free_beams: int
    weather: str

    @property
    def load(self) -> float:
        return 1.0 - (self.free_beams / self.num_beams) if self.num_beams else 1.0


@dataclass
class VisibilityView:
    sat_id: str
    station_id: str
    elev_deg: float
    range_km: float
    rate_bps: float              # achievable (interference-free) rate on this link right now
    az_deg: float = 0.0          # azimuth to the satellite (for phased-array beam separation)


@dataclass
class Assignment:
    sat_id: str
    station_id: str
    # beam index is chosen by the simulator (lowest free beam); schedulers in v0
    # only decide sat<->station. Kept optional for future explicit beam control.
    beam: int | None = None


@dataclass
class NetworkState:
    t: float
    sats: dict                   # sat_id -> SatView
    stations: dict               # station_id -> StationView
    visibilities: list           # list[VisibilityView] with rate_bps > 0

    def visible_for(self, sat_id: str) -> list:
        """Usable links for a satellite, best (highest rate) first."""
        vs = [v for v in self.visibilities if v.sat_id == sat_id]
        return sorted(vs, key=lambda v: v.rate_bps, reverse=True)

    def free_sats(self) -> list:
        return [s for s in self.sats.values() if s.is_free and s.backlog_bits > 0]
