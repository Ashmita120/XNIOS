"""Scenario builders.

Validation scenarios (E1-E4) are constructed with *deterministic* pass geometry by
placing stations under a known sub-satellite point, so the expected behaviour is
unambiguous. `random_scenario` is the parametrised generator used later to sweep
the plan's variables (satellite count, station count, ...).
"""

from __future__ import annotations

import random

from .entities import OrbitElements, Satellite, GroundStation
from .simulator import Scenario
from .weather import WeatherModel
from . import orbit as orb

# a common LEO reference orbit
def _orbit(arg_lat0=0.0, inc=53.0, raan=0.0, alt=600.0):
    return OrbitElements(alt_km=alt, inc_deg=inc, raan_deg=raan, arg_lat0_deg=arg_lat0)


def e1_one_sat_one_station(t_mid=600.0) -> Scenario:
    """E1: one satellite, one station. A single pass overhead the station.
    Expected: the satellite fully downlinks -> 100% completion."""
    o = _orbit(arg_lat0=0.0)
    lat, lon = orb.place_station_under(o, t_mid)            # station under the pass
    sat = Satellite("SAT-1", o, backlog_bits=5.0e9, tier="commercial")
    gs = GroundStation("GS-1", lat, lon, num_beams=1)
    return Scenario([sat], [gs], name="E1 one-sat/one-station")


def e2_one_station_two_sats(t_mid=600.0) -> Scenario:
    """E2: one single-beam station, two satellites on the same plane, phased so
    both are visible together. Expected: one is served, the other waits."""
    o1 = _orbit(arg_lat0=0.0)
    o2 = _orbit(arg_lat0=-2.0)                              # trailing by ~2 deg of arc
    lat, lon = orb.place_station_under(o1, t_mid)
    s1 = Satellite("SAT-1", o1, backlog_bits=6.0e9, tier="commercial")
    s2 = Satellite("SAT-2", o2, backlog_bits=6.0e9, tier="commercial")
    gs = GroundStation("GS-1", lat, lon, num_beams=1)       # single beam -> contention
    return Scenario([s1, s2], [gs], name="E2 one-station/two-sats")


def e3_five_stations_one_sat(t_mid=600.0) -> Scenario:
    """E3: one satellite, five stations. GS-0 sits exactly under the pass; the
    others are offset. Expected: a 'nearest'/'highest-elev' scheduler picks GS-0."""
    o = _orbit(arg_lat0=0.0)
    lat, lon = orb.place_station_under(o, t_mid)
    stations = [GroundStation("GS-0", lat, lon, num_beams=1)]   # directly overhead
    offsets = [(+4, +4), (-4, +4), (+4, -4), (-4, -4)]          # deg lat/lon
    for i, (dlat, dlon) in enumerate(offsets, start=1):
        stations.append(GroundStation(f"GS-{i}", lat + dlat, lon + dlon, num_beams=1))
    sat = Satellite("SAT-1", o, backlog_bits=5.0e9, tier="commercial")
    return Scenario([sat], stations, name="E3 five-stations/one-sat")


def e4_visibility_expiry(t_mid=400.0, duration_hint=1200.0) -> Scenario:
    """E4: one satellite with effectively infinite data, one station. The pass
    ends before the buffer drains. Expected: transfer stops at LOS (no completion,
    delivered stops growing after the satellite sets)."""
    o = _orbit(arg_lat0=0.0)
    lat, lon = orb.place_station_under(o, t_mid)
    sat = Satellite("SAT-1", o, backlog_bits=1.0e14)        # too big to finish in one pass
    gs = GroundStation("GS-1", lat, lon, num_beams=1)
    return Scenario([sat], [gs], name="E4 visibility-expiry")


def congested_scenario(n_sats=16, n_stations=2, seed=0, t_mid=600.0) -> Scenario:
    """A deliberately contended world: a tight cluster of satellites in one plane
    all pass over a few stations at once, so many satellites compete for few beams.
    This is where scheduling *policy* actually matters (FCFS vs priority vs EDF vs
    SJF diverge on wait/SLA/completion). Satellites get varied priority, demand and
    SLA deadlines so the ordering keys produce genuinely different outcomes."""
    rng = random.Random(seed)
    center = _orbit(arg_lat0=0.0)
    lat, lon = orb.place_station_under(center, t_mid)

    # stations clustered under the pass (single beam each -> scarce capacity)
    stations = []
    for j in range(n_stations):
        stations.append(GroundStation(f"GS-{j}", lat + rng.uniform(-2, 2),
                                       lon + rng.uniform(-2, 2), num_beams=1))

    # satellites tightly phased in argument of latitude -> visible together
    sats = []
    tiers = ["research", "commercial", "commercial", "military", "emergency"]
    for i in range(n_sats):
        o = _orbit(arg_lat0=rng.uniform(-6, 6))            # ±6° arc around overhead
        sats.append(Satellite(
            f"SAT-{i}", o,
            backlog_bits=rng.uniform(8e9, 16e9),           # demand ~ capacity -> some miss
            tier=rng.choice(tiers),
            deadline_s=t_mid + rng.uniform(150, 450),      # SLA deadline
        ))
    return Scenario(sats, stations, name=f"congested n_sat={n_sats} n_gs={n_stations} seed={seed}")


def heterogeneous_scenario(seed=0, t_mid=600.0, n_per_plane=15) -> Scenario:
    """A contended AND heterogeneous world — the scenario that makes policy choice
    actually matter (addresses the 'too symmetric' critique):

      * demand classes  : small (2 Gb) / medium (15 Gb) / huge (60 Gb)  -> SJF matters
      * deadlines by tier: emergency 90s .. research 550s               -> EDF matters
      * heterogeneous stations: differing beams, antenna G/T, weather   -> station key matters
      * two orbital planes with spread phasing: varied pass geometry & handover chances

    Expect a genuine Pareto split: EDF/priority win SLA (often at some throughput
    cost), SJF wins completion count, 'strongest' beats 'nearest' by dodging the
    rainy/weak stations.
    """
    rng = random.Random(seed)
    plane_a = dict(inc=53.0, raan=0.0)
    plane_b = dict(inc=53.0, raan=6.0)                      # shifted node -> different track
    lat, lon = orb.place_station_under(_orbit(arg_lat0=0.0, **plane_a), t_mid)

    # heterogeneous stations near the tracks: (dlat, dlon, beams, G/T, weather).
    # Axes are deliberately in CONFLICT so 'nearest' != 'strongest' != 'least_loaded':
    #   the closest, highest-capacity station is rain-degraded (weak link), while the
    #   strongest links are farther out with only one beam each.
    specs = [
        ("GS-0", 0.0, 0.0, 2, 19.0, "rain"),               # nearest + 2 beams, but weak (rain)
        ("GS-1", 3.0, -3.0, 1, 25.0, "clear"),             # strong link, farther, 1 beam
        ("GS-2", -3.0, 3.0, 1, 24.0, "clear"),             # strong link, farther, 1 beam
        ("GS-3", 2.0, 5.0, 1, 18.0, "cloudy"),             # weak & far
    ]
    stations, wx = [], {}
    for sid, dlat, dlon, beams, gt, weather_state in specs:
        stations.append(GroundStation(sid, lat + dlat, lon + dlon,
                                       num_beams=beams, g_over_t_dbk=gt))
        wx[sid] = weather_state

    # heterogeneous satellites across the two planes. Total demand is set well above
    # beam capacity (~1.8x) so NOT everyone completes -> the who-finishes trade-off
    # (SJF completion vs EDF/priority SLA vs FCFS throughput) becomes visible.
    backlog_classes = [2e9, 20e9, 80e9]                    # small / medium / huge
    backlog_weights = [0.35, 0.4, 0.25]
    tier_deadline = {"emergency": 90, "military": 180, "commercial": 300, "research": 550}
    tiers = ["research", "commercial", "commercial", "military", "emergency"]

    sats = []
    for p, plane in enumerate([plane_a, plane_b]):
        for i in range(n_per_plane):
            o = _orbit(arg_lat0=rng.uniform(-10, 10), **plane)
            tier = rng.choice(tiers)
            backlog = rng.choices(backlog_classes, weights=backlog_weights)[0]
            sats.append(Satellite(
                f"SAT-{p}{i:02d}", o,
                backlog_bits=backlog * rng.uniform(0.7, 1.3),
                tier=tier,
                deadline_s=t_mid + tier_deadline[tier] * rng.uniform(0.8, 1.2),
            ))
    return Scenario(sats, stations, weather=WeatherModel(state_by_station=wx),
                    name=f"heterogeneous seed={seed}")


def random_scenario(n_sats=10, n_stations=5, seed=0,
                    backlog_range=(2e9, 8e9), weather=None) -> Scenario:
    """Parametrised scenario for scaling/stress sweeps (plan's Variables 1-8)."""
    rng = random.Random(seed)
    sats = []
    for i in range(n_sats):
        o = _orbit(
            arg_lat0=rng.uniform(0, 360),
            inc=rng.choice([53.0, 70.0, 97.6]),
            raan=rng.uniform(0, 360),
            alt=rng.choice([550.0, 600.0, 700.0]),
        )
        tier = rng.choice(["research", "commercial", "commercial", "military", "emergency"])
        sats.append(Satellite(
            f"SAT-{i}", o,
            backlog_bits=rng.uniform(*backlog_range),
            tier=tier,
        ))
    # spread stations over a range of latitudes/longitudes
    stations = []
    for j in range(n_stations):
        lat = rng.uniform(-55, 55)
        lon = rng.uniform(-180, 180)
        stations.append(GroundStation(f"GS-{j}", lat, lon, num_beams=1))
    return Scenario(sats, stations, weather=weather or WeatherModel(),
                    name=f"random n_sat={n_sats} n_gs={n_stations} seed={seed}")
