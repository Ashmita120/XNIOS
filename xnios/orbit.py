"""Orbital dynamics + observation geometry (the physics ground truth).

Synthetic circular-orbit propagation in an Earth-centred inertial (ECI) frame,
rotated into an Earth-centred Earth-fixed (ECEF) frame, from which we compute the
topocentric elevation / azimuth / range of a satellite as seen by a ground station.

Everything downstream (link budget, visibility, scheduling) only needs
`elevation_azimuth_range`, so this whole module is the single place to later swap
in Skyfield/SGP4 with real TLEs — the interface stays the same.
"""

from __future__ import annotations

import math
import numpy as np

# physical constants
MU_EARTH = 398_600.4418      # km^3 / s^2  (gravitational parameter)
R_EARTH = 6371.0             # km          (mean spherical Earth radius)
OMEGA_EARTH = 7.2921159e-5   # rad / s     (Earth rotation rate)


def mean_motion(alt_km: float) -> float:
    """Angular rate (rad/s) of a circular orbit at the given altitude."""
    a = R_EARTH + alt_km
    return math.sqrt(MU_EARTH / a**3)


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def _rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def sat_position_ecef(orbit, t: float) -> np.ndarray:
    """Satellite position in ECEF (km) at time t (s) for a circular orbit."""
    a = R_EARTH + orbit.alt_km
    n = mean_motion(orbit.alt_km)
    u = math.radians(orbit.arg_lat0_deg) + n * t          # argument of latitude
    inc = math.radians(orbit.inc_deg)
    raan = math.radians(orbit.raan_deg)

    r_orbit = np.array([a * math.cos(u), a * math.sin(u), 0.0])
    r_eci = _rot_z(raan) @ _rot_x(inc) @ r_orbit          # into inertial frame

    gmst = OMEGA_EARTH * t                                # Earth rotation angle
    r_ecef = _rot_z(-gmst) @ r_eci                        # inertial -> Earth-fixed
    return r_ecef


def gs_position_ecef(lat_deg: float, lon_deg: float, alt_km: float = 0.0) -> np.ndarray:
    """Ground-station position in ECEF (km) on a spherical Earth."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    r = R_EARTH + alt_km
    return np.array([
        r * math.cos(lat) * math.cos(lon),
        r * math.cos(lat) * math.sin(lon),
        r * math.sin(lat),
    ])


def subsatellite_point(sat_ecef: np.ndarray) -> tuple[float, float]:
    """Latitude/longitude (deg) directly beneath a satellite. Used to place
    ground stations under a known pass when constructing validation scenarios."""
    x, y, z = sat_ecef
    r = math.sqrt(x * x + y * y + z * z)
    lat = math.degrees(math.asin(z / r))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon


def elevation_azimuth_range(gs_ecef: np.ndarray, lat_deg: float, lon_deg: float,
                            sat_ecef: np.ndarray) -> tuple[float, float, float]:
    """Topocentric elevation (deg), azimuth (deg), slant range (km).

    Uses the local up/east/north basis at the station (spherical Earth, so 'up'
    is the radial direction). Elevation is the angle of the satellite above the
    local horizon; range is the straight-line distance.
    """
    d = sat_ecef - gs_ecef                                # station -> satellite
    rng = float(np.linalg.norm(d))
    d_hat = d / rng

    up = gs_ecef / np.linalg.norm(gs_ecef)               # local vertical (radial)
    lon = math.radians(lon_deg)
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    north = np.cross(up, east)

    elev = math.degrees(math.asin(float(np.dot(d_hat, up))))
    az = math.degrees(math.atan2(float(np.dot(d, east)), float(np.dot(d, north)))) % 360.0
    return elev, az, rng


def place_station_under(orbit, t: float) -> tuple[float, float]:
    """Return (lat, lon) of the sub-satellite point at time t, i.e. a station
    placed here sees the satellite directly overhead (elevation ~90 deg) at t.
    Convenience for building deterministic validation passes."""
    return subsatellite_point(sat_position_ecef(orbit, t))


def find_orbit_for_elevation(target_lat: float, target_lon: float, inc_deg: float,
                             target_elev_deg: float, alt_km: float = 600.0,
                             raan_step_deg: float = 1.0, u_samples: int = 720) -> dict:
    """Search for a (RAAN, arg_lat0) pair whose satellite reaches a peak elevation of
    approximately `target_elev_deg` over (target_lat, target_lon), for a circular orbit
    of the given inclination/altitude.

    Formalizes the ad hoc RAAN search used to diagnose a coverage-gap bug (an arbitrary
    RAAN choice can miss every station entirely, capping peak elevation below any usable
    threshold). For a fixed RAAN, the peak elevation reachable over the target is a
    function of RAAN alone (~90 deg at the best-aligned RAAN, falling off as RAAN moves
    away). This scans RAAN to find that best case, then walks outward from it until the
    achievable peak crosses `target_elev_deg`, linearly interpolating between the
    bracketing grid points. Returns {"raan_deg", "arg_lat0_deg", "achieved_elev_deg"} —
    feed the first two into an OrbitElements/plane entry; `arg_lat0_deg` places a
    satellite at its peak pass over the target at t=0 (jitter it per-satellite for a
    spread of pass times).

    Evaluated at t=0 (GMST=0): a RAAN sweep at t=0 already explores every rotation a
    nonzero t would also produce (RAAN and GMST both just add a z-rotation), so this is
    equivalent to also searching over epoch.
    """
    gs_ecef = gs_position_ecef(target_lat, target_lon, 0.0)
    up = gs_ecef / np.linalg.norm(gs_ecef)
    a = R_EARTH + alt_km
    inc = math.radians(inc_deg)
    us = np.linspace(0.0, 2 * math.pi, u_samples, endpoint=False)
    r_orbit = np.stack([a * np.cos(us), a * np.sin(us), np.zeros_like(us)], axis=1)  # (N,3)
    rx_inc = _rot_x(inc)

    def peak_for_raan(raan_deg: float) -> tuple[float, float]:
        """(peak elevation deg, arg_lat0 deg at that peak) for this RAAN."""
        m = _rot_z(math.radians(raan_deg)) @ rx_inc            # (3,3)
        r_eci = r_orbit @ m.T                                  # (N,3); t=0 -> ecef == eci
        d = r_eci - gs_ecef
        rng = np.linalg.norm(d, axis=1)
        d_hat = d / rng[:, None]
        elev = np.degrees(np.arcsin(np.clip(d_hat @ up, -1.0, 1.0)))
        i = int(np.argmax(elev))
        return float(elev[i]), math.degrees(us[i])

    raan_grid = np.arange(0.0, 360.0, raan_step_deg)
    peaks = np.array([peak_for_raan(r)[0] for r in raan_grid])
    i_best = int(np.argmax(peaks))
    raan_best = float(raan_grid[i_best])

    if peaks[i_best] < target_elev_deg - 1e-6:
        # inclination can't reach this elevation at this target at all (e.g. target
        # latitude beyond inclination reach) -- return the best achievable instead.
        elev, u0 = peak_for_raan(raan_best)
        return {"raan_deg": raan_best, "arg_lat0_deg": u0, "achieved_elev_deg": elev}

    def walk(direction: int):
        prev_raan, prev_elev = raan_best, float(peaks[i_best])
        idx = i_best
        for _ in range(int(180 / raan_step_deg)):
            idx = (idx + direction) % len(raan_grid)
            raan, elev = float(raan_grid[idx]), float(peaks[idx])
            if elev <= target_elev_deg:
                span = prev_elev - elev
                frac = 0.0 if span <= 1e-9 else (prev_elev - target_elev_deg) / span
                delta = raan - prev_raan
                if direction > 0 and delta < 0:
                    delta += 360.0
                if direction < 0 and delta > 0:
                    delta -= 360.0
                return (prev_raan + frac * delta) % 360.0
            prev_raan, prev_elev = raan, elev
        return None

    candidates = [r for r in (walk(+1), walk(-1)) if r is not None]
    raan_hit = (min(candidates, key=lambda r: min(abs(r - raan_best), 360 - abs(r - raan_best)))
               if candidates else raan_best)

    elev, u0 = peak_for_raan(raan_hit)
    return {"raan_deg": raan_hit, "arg_lat0_deg": u0, "achieved_elev_deg": elev}


def next_contact(sat, stations, t_start: float,
                 horizon_s: float = 172800.0, step_s: float = 120.0):
    """Scan forward from t_start for the next moment ANY station sees `sat` above
    its elevation mask — i.e. the next chance to resume its downlink after it ran
    out of time. Returns {"station", "t_aos", "wait_s", "elev_deg"} for the first
    such opportunity (highest-elevation station at that instant), or None if there
    is no contact within `horizon_s`. Pure geometry — which station actually gets
    assigned is then up to the scheduler."""
    gs_ecef = {g.id: gs_position_ecef(g.lat_deg, g.lon_deg, g.alt_km) for g in stations}
    t = t_start
    end = t_start + horizon_s
    while t <= end:
        p = sat_position_ecef(sat.orbit, t)
        best = None
        for g in stations:
            elev, _az, _rng = elevation_azimuth_range(gs_ecef[g.id], g.lat_deg, g.lon_deg, p)
            if elev >= g.elevation_mask_deg and (best is None or elev > best["elev_deg"]):
                best = {"station": g.id, "t_aos": t, "wait_s": t - t_start, "elev_deg": elev}
        if best is not None:
            return best
        t += step_s
    return None
