"""Network dynamics — station/beam failures and time-varying capacity.

The simulator queries `NetworkDynamics.snapshot(t)` each step to get, per station:
  * up            — is the station operational? (False during an outage / maintenance)
  * beams         — how many beams are currently usable (a beam failure drops this by 1)
  * bandwidth_hz  — the current bandwidth pool (dynamic capacity)

Everything is expressed as timestamped EVENTS applied in order, so it is fully
reproducible. Events can be scripted in a config or generated randomly (Poisson
failures + repair times). With no dynamics, stations are always up at full capacity,
so existing behaviour is unchanged.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Event:
    t: float
    station: str
    action: str          # station_fail / station_recover / beam_fail / beam_recover / bandwidth
    value: float = 0.0    # for 'bandwidth' (Hz); for beam_fail/recover, how many (default 1)


class NetworkDynamics:
    def __init__(self, stations, events=None):
        self.base_beams = {g.id: g.num_beams for g in stations}
        self.base_bw = {g.id: g.bandwidth_hz for g in stations}
        self.events = sorted(events or [], key=lambda e: e.t)

    def snapshot(self, t: float) -> dict:
        """{station_id: {'up': bool, 'beams': int, 'bandwidth_hz': float}} at time t."""
        up = {sid: True for sid in self.base_beams}
        beams = dict(self.base_beams)
        bw = dict(self.base_bw)
        for e in self.events:
            if e.t > t:
                break
            if e.action == "station_fail":
                up[e.station] = False
            elif e.action == "station_recover":
                up[e.station] = True
            elif e.action == "beam_fail":
                beams[e.station] = max(0, beams[e.station] - int(e.value or 1))
            elif e.action == "beam_recover":
                beams[e.station] = min(self.base_beams[e.station],
                                       beams[e.station] + int(e.value or 1))
            elif e.action == "bandwidth":
                bw[e.station] = e.value
        return {sid: {"up": up[sid], "beams": beams[sid], "bandwidth_hz": bw[sid]}
                for sid in self.base_beams}


def failure_events(stations, seed=0, duration_s=3600.0,
                   station_mtbf_s=0.0, station_mttr_s=600.0,
                   beam_mtbf_s=0.0, beam_mttr_s=300.0):
    """Generate random station/beam failure+recovery events (Poisson). An MTBF of 0
    disables that failure type. Repair time = MTTR."""
    rng = random.Random(seed)
    events = []
    for g in stations:
        if station_mtbf_s > 0:
            t = rng.expovariate(1.0 / station_mtbf_s)
            while t < duration_s:
                events.append(Event(t, g.id, "station_fail"))
                events.append(Event(min(duration_s, t + station_mttr_s), g.id, "station_recover"))
                t += station_mttr_s + rng.expovariate(1.0 / station_mtbf_s)
        if beam_mtbf_s > 0 and g.num_beams > 1:
            t = rng.expovariate(1.0 / beam_mtbf_s)
            while t < duration_s:
                events.append(Event(t, g.id, "beam_fail", 1))
                events.append(Event(min(duration_s, t + beam_mttr_s), g.id, "beam_recover", 1))
                t += beam_mttr_s + rng.expovariate(1.0 / beam_mtbf_s)
    return events
