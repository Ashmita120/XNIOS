"""Analytical forecasting — the future the physics already knows.

V2 Stage 1. This module answers every question about *future geometry* exactly,
so no model is ever trained to rediscover `orbit.py`:

    when does this pass start and end?      -> contact_windows()
    how long until this link drops?         -> time_to_los()
    where will the satellite be at t+90s?   -> elevation_at()
    when is the next chance to downlink?    -> next_contact()

It is deliberately *not* a predictor of anything uncertain. Weather realization,
failure timing and contention are the ML layer's job; everything here is closed
-form orbital mechanics plus the same link budget the simulator uses.

Three roles at once:
  1. the BASELINE every learned model must beat,
  2. a FEATURE source for those models (future elevation is by far the strongest
     predictor of future SNR),
  3. an operator feature on its own ("next contact in 04:20, 6-minute window").

Pure numpy + stdlib — no ML dependencies, same discipline as `telemetry.py`, so
the twin still runs anywhere. Nothing in the simulator imports this module; it is
a reader of the same world description, never a participant in the run.

---------------------------------------------------------------------------
Matching the simulator exactly
---------------------------------------------------------------------------
A forecast that disagrees with the twin is a bug, not "model error". The
visibility predicate is therefore copied from `Simulator._visibility` verbatim:

    elev >= station.elevation_mask_deg          AND
    link.beam_reachable(elev, station)          AND       (phased-array scan limit)
    achievable_rate_bps(...) > 0                          (SNR >= MIN_SNR_DB)

The middle term is easy to miss and usually dominates: a phased array with
`max_scan_deg=60` cannot steer below 30 deg elevation at all, so a station
configured with a 10 deg mask actually has a 30 deg *effective* mask. See
`effective_mask_deg`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np

from . import orbit as orb
from .link import (C_LIGHT, K_BOLTZ_DBW, MAX_SPECTRAL_EFF, MIN_SNR_DB,
                   ZENITH_GAS_DB, achievable_rate_bps, beam_reachable)

__all__ = [
    "ContactWindow",
    "effective_mask_deg",
    "elevation_series",
    "elevation_at",
    "rate_series",
    "contact_windows",
    "time_to_los",
    "next_contact",
    "contact_schedule",
]


# ---------------------------------------------------------------------------
# geometry, vectorised
# ---------------------------------------------------------------------------

def _sat_ecef_series(orbit, t: np.ndarray) -> np.ndarray:
    """(N,3) ECEF positions (km) for a circular orbit at times `t` (s).

    The vectorised twin of `orbit.sat_position_ecef`, which is scalar-per-call.
    Same algebra, evaluated for the whole grid at once — that is what makes
    scanning a 30-minute horizon for 20x4 pairs cost milliseconds instead of
    seconds, and it is the reason a controller can afford to call this inside a
    decision loop.

    `Rz(-gmst)` is applied as an explicit 2-D rotation per sample rather than a
    matrix product, since gmst varies along the grid.
    """
    t = np.asarray(t, dtype=float)
    a = orb.R_EARTH + orbit.alt_km
    n = orb.mean_motion(orbit.alt_km)
    u = math.radians(orbit.arg_lat0_deg) + n * t                     # (N,)

    # position in the orbital plane
    r_orbit = np.stack([a * np.cos(u), a * np.sin(u), np.zeros_like(u)], axis=1)

    # into the inertial frame: Rz(raan) @ Rx(inc)
    m = orb._rot_z(math.radians(orbit.raan_deg)) @ orb._rot_x(math.radians(orbit.inc_deg))
    r_eci = r_orbit @ m.T                                            # (N,3)

    # inertial -> Earth-fixed, one rotation angle per sample
    gmst = orb.OMEGA_EARTH * t
    cg, sg = np.cos(gmst), np.sin(gmst)
    x, y, z = r_eci[:, 0], r_eci[:, 1], r_eci[:, 2]
    return np.stack([cg * x + sg * y, -sg * x + cg * y, z], axis=1)


def elevation_series(sat, station, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(elevation deg, slant range km) of `sat` seen from `station` at times `t`."""
    gs = orb.gs_position_ecef(station.lat_deg, station.lon_deg, station.alt_km)
    up = gs / np.linalg.norm(gs)
    p = _sat_ecef_series(sat.orbit, t)
    d = p - gs
    rng = np.linalg.norm(d, axis=1)
    sin_el = np.clip((d @ up) / rng, -1.0, 1.0)
    return np.degrees(np.arcsin(sin_el)), rng


def elevation_at(sat, station, t: float) -> float:
    """Elevation (deg) of `sat` from `station` at a single time."""
    elev, _rng = elevation_series(sat, station, np.array([float(t)]))
    return float(elev[0])


def rate_series(sat, station, elev_deg, range_km, rain_zenith_db: float = 0.0,
                bandwidth_hz: float | None = None,
                tx_power_w: float | None = None) -> np.ndarray:
    """Vectorised twin of `link.achievable_rate_bps` — bits/s at every sample.

    Same link budget, evaluated for a whole grid at once. Zero wherever the link
    carries nothing: below the horizon, outside a phased array's steering limit,
    or under the SNR floor.

    This is what turns a contact window from "you have 412 seconds" into "you can
    move 18.4 Gbit", which is the quantity a scheduler actually needs — a pass
    that is long but low is worth less than one that is short and overhead.
    `_usable_mask` re-derives the threshold form of the same budget; it is left
    alone deliberately, so the validated window-finding path stays bit-identical.
    """
    elev = np.atleast_1d(np.asarray(elev_deg, dtype=float))
    rng = np.atleast_1d(np.asarray(range_km, dtype=float))
    out = np.zeros_like(elev)

    bw = float(sat.bandwidth_hz if bandwidth_hz is None else bandwidth_hz)
    power = float(sat.tx_power_w if tx_power_w is None else tx_power_w)
    if bw <= 0 or power <= 0:
        return out

    ok = elev > 0.0
    if getattr(station, "phased_array", False):
        ok &= (90.0 - elev) <= float(station.max_scan_deg) + 1e-9
    if not ok.any():
        return out

    e = np.maximum(elev, 0.5)
    wavelength = C_LIGHT / sat.freq_hz
    fspl_db = 20.0 * np.log10(4.0 * np.pi * rng * 1000.0 / wavelength)
    eirp_dbw = 10.0 * math.log10(power) + sat.tx_gain_dbi
    atmos_db = (ZENITH_GAS_DB + rain_zenith_db) / np.sin(np.radians(e))
    if getattr(station, "phased_array", False):
        scan_loss_db = -10.0 * station.scan_loss_exp * np.log10(
            np.maximum(np.cos(np.radians(90.0 - e)), 0.05))
    else:
        scan_loss_db = 0.0

    cn0 = (eirp_dbw - fspl_db - atmos_db + station.g_over_t_dbk
           - K_BOLTZ_DBW - scan_loss_db)
    snr_db = cn0 - 10.0 * np.log10(bw)
    ok &= snr_db >= MIN_SNR_DB
    if not ok.any():
        return out
    snr = np.power(10.0, snr_db[ok] / 10.0)
    out[ok] = bw * np.minimum(np.log2(1.0 + snr), MAX_SPECTRAL_EFF)
    return out


# ---------------------------------------------------------------------------
# the visibility predicate
# ---------------------------------------------------------------------------

def effective_mask_deg(station) -> float:
    """The elevation a link actually needs, not the one that is configured.

    A phased array steers electronically within `max_scan_deg` of zenith, so it
    physically cannot form a beam below `90 - max_scan_deg` — regardless of the
    elevation mask. For the India presets (`max_scan_deg=60`, mask 10) the real
    threshold is 30 deg. Dishes track mechanically, so the mask stands.
    """
    mask = float(getattr(station, "elevation_mask_deg", 0.0))
    if getattr(station, "phased_array", False):
        mask = max(mask, 90.0 - float(getattr(station, "max_scan_deg", 90.0)))
    return mask


def link_usable(sat, station, elev_deg: float, range_km: float,
                rain_zenith_db: float = 0.0) -> bool:
    """The simulator's own test: above the mask, steerable, and carrying data."""
    if elev_deg < float(getattr(station, "elevation_mask_deg", 0.0)):
        return False
    if not beam_reachable(elev_deg, station):
        return False
    return achievable_rate_bps(range_km, elev_deg, sat, station,
                               rain_zenith_db=rain_zenith_db) > 0.0


# ---------------------------------------------------------------------------
# contact windows
# ---------------------------------------------------------------------------

@dataclass
class ContactWindow:
    """One geometric pass of a satellite over a station.

    `t_rise`/`t_set` are the instants the link becomes / stops being usable, not
    merely the horizon crossings — the phased-array scan limit and the SNR floor
    are both folded in. `open_start`/`open_end` mark a window clipped by the
    search horizon rather than by geometry, so a caller can tell "the pass began
    before I looked" from "the pass began here".
    """

    sat_id: str
    station_id: str
    t_rise: float
    t_set: float
    t_peak: float
    peak_elev_deg: float
    open_start: bool = False
    open_end: bool = False

    @property
    def duration_s(self) -> float:
        return self.t_set - self.t_rise

    def contains(self, t: float) -> bool:
        return self.t_rise <= t <= self.t_set

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration_s"] = self.duration_s
        return d


def _usable_mask(sat, station, elev: np.ndarray, rng: np.ndarray,
                 rain_zenith_db: float) -> np.ndarray:
    """Boolean array: is the link usable at each sampled instant?

    Vectorised where the physics allows. The SNR floor is a closed-form
    threshold, so it is applied analytically rather than by calling the scalar
    rate function per sample.
    """
    mask = elev >= effective_mask_deg(station)
    if not mask.any():
        return mask

    # SNR >= MIN_SNR_DB, using the same link budget as link.snr_linear
    bw = float(sat.bandwidth_hz)
    power = float(sat.tx_power_w)
    if bw <= 0 or power <= 0:
        return np.zeros_like(mask)

    e = np.maximum(elev, 0.5)
    range_m = rng * 1000.0
    wavelength = C_LIGHT / sat.freq_hz
    fspl_db = 20.0 * np.log10(4.0 * np.pi * range_m / wavelength)
    eirp_dbw = 10.0 * math.log10(power) + sat.tx_gain_dbi
    atmos_db = (ZENITH_GAS_DB + rain_zenith_db) / np.sin(np.radians(e))

    if getattr(station, "phased_array", False):
        scan = np.radians(90.0 - e)
        scan_loss_db = -10.0 * station.scan_loss_exp * np.log10(
            np.maximum(np.cos(scan), 0.05))
    else:
        scan_loss_db = 0.0

    cn0 = (eirp_dbw - fspl_db - atmos_db + station.g_over_t_dbk
           - K_BOLTZ_DBW - scan_loss_db)
    snr_db = cn0 - 10.0 * np.log10(bw)
    return mask & (snr_db >= MIN_SNR_DB)


def _bisect_edge(sat, station, lo: float, hi: float, rain_zenith_db: float,
                 want_usable_at_hi: bool, tol_s: float) -> float:
    """Refine a rise (or set) instant bracketed by [lo, hi] to `tol_s`.

    The coarse scan only tells us the transition happened somewhere inside one
    step; bisection turns that into a time accurate to a fraction of a second,
    which is what makes `time_to_los` usable as a handover trigger.
    """
    for _ in range(60):
        if hi - lo <= tol_s:
            break
        mid = 0.5 * (lo + hi)
        elev, rng = elevation_series(sat, station, np.array([mid]))
        ok = bool(_usable_mask(sat, station, elev, rng, rain_zenith_db)[0])
        if ok == want_usable_at_hi:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def contact_windows(sat, station, t0: float, t1: float, step_s: float = 10.0,
                    rain_zenith_db: float = 0.0, tol_s: float = 0.05,
                    refine_peak: bool = True) -> list[ContactWindow]:
    """Every usable pass of `sat` over `station` in [t0, t1].

    Coarse-scans the horizon on a `step_s` grid, then bisects each transition to
    `tol_s`. `step_s` must be shorter than the shortest pass you care about — a
    LEO pass is minutes long, so 10 s is comfortable and costs one vectorised
    evaluation for the whole horizon.

    `rain_zenith_db` holds weather fixed across the horizon. That is the honest
    analytical assumption: how weather will actually evolve is exactly the part
    this module refuses to guess.
    """
    if t1 <= t0:
        return []
    n = max(2, int(math.ceil((t1 - t0) / step_s)) + 1)
    grid = np.linspace(t0, t1, n)
    elev, rng = elevation_series(sat, station, grid)
    ok = _usable_mask(sat, station, elev, rng, rain_zenith_db)
    if not ok.any():
        return []

    out: list[ContactWindow] = []
    edges = np.flatnonzero(np.diff(ok.astype(np.int8)))       # index i: change between i and i+1
    starts = [0] if ok[0] else []
    starts += [int(i) + 1 for i in edges if not ok[i]]        # False -> True
    ends = [int(i) for i in edges if ok[i]]                   # True -> False
    if ok[-1]:
        ends.append(len(grid) - 1)

    for i_start, i_end in zip(starts, ends):
        open_start = i_start == 0
        open_end = i_end == len(grid) - 1

        t_rise = grid[0] if open_start else _bisect_edge(
            sat, station, grid[i_start - 1], grid[i_start], rain_zenith_db, True, tol_s)
        t_set = grid[-1] if open_end else _bisect_edge(
            sat, station, grid[i_end], grid[i_end + 1], rain_zenith_db, False, tol_s)

        seg = slice(i_start, i_end + 1)
        j = int(np.argmax(elev[seg])) + i_start
        t_peak, peak = float(grid[j]), float(elev[j])
        if refine_peak:
            t_peak, peak = _refine_peak(sat, station, grid, j, t_rise, t_set)

        out.append(ContactWindow(
            sat_id=sat.id, station_id=station.id,
            t_rise=float(t_rise), t_set=float(t_set),
            t_peak=float(t_peak), peak_elev_deg=float(peak),
            open_start=open_start, open_end=open_end))
    return out


def _refine_peak(sat, station, grid: np.ndarray, j: int,
                 t_rise: float, t_set: float) -> tuple[float, float]:
    """Golden-section the maximum elevation between the samples bracketing `j`."""
    lo = max(t_rise, float(grid[j - 1]) if j > 0 else t_rise)
    hi = min(t_set, float(grid[j + 1]) if j + 1 < len(grid) else t_set)
    if hi <= lo:
        return float(grid[j]), float(elevation_at(sat, station, float(grid[j])))
    phi = 0.5 * (3.0 - math.sqrt(5.0))
    for _ in range(40):
        if hi - lo < 1e-3:
            break
        a = lo + phi * (hi - lo)
        b = hi - phi * (hi - lo)
        ea, eb = elevation_series(sat, station, np.array([a, b]))[0]
        if ea < eb:
            lo = a
        else:
            hi = b
    t = 0.5 * (lo + hi)
    return t, elevation_at(sat, station, t)


# ---------------------------------------------------------------------------
# the two questions a controller actually asks
# ---------------------------------------------------------------------------

def time_to_los(sat, station, t: float, horizon_s: float = 1800.0,
                step_s: float = 10.0, rain_zenith_db: float = 0.0,
                tol_s: float = 0.05) -> float | None:
    """Seconds until this link stops being usable, or None if it is not usable now.

    This is the quantity proactive handover is built on: V1 triggers a handover
    when the *current* elevation would be below the mask `handover_lead_s` from
    now, which answers the question one lead-time at a time. This answers it
    directly and exactly.
    """
    for w in contact_windows(sat, station, t, t + horizon_s, step_s=step_s,
                             rain_zenith_db=rain_zenith_db, tol_s=tol_s,
                             refine_peak=False):
        if w.t_rise <= t + tol_s:
            return max(0.0, w.t_set - t)
        break                       # first window starts later -> not usable now
    return None


def next_contact(sat, stations, t: float, horizon_s: float = 7200.0,
                 step_s: float = 10.0, rain_zenith_db: float = 0.0):
    """The soonest usable contact for `sat` across `stations` after `t`.

    Returns the earliest-rising window (ties broken by peak elevation), or None
    if the satellite sees nobody inside the horizon. The exact version of
    `orbit.next_contact`, which scans on a 120 s grid and reports only the
    instant it happens to land on.
    """
    best = None
    for g in stations:
        for w in contact_windows(sat, g, t, t + horizon_s, step_s=step_s,
                                 rain_zenith_db=rain_zenith_db):
            if w.t_set <= t:
                continue
            key = (max(w.t_rise, t), -w.peak_elev_deg)
            if best is None or key < best[0]:
                best = (key, w)
            break                                   # windows are time-ordered
    if best is None:
        return None
    w = best[1]
    return {"station": w.station_id, "t_aos": w.t_rise,
            "wait_s": max(0.0, w.t_rise - t), "window": w}


def contact_schedule(sats, stations, t0: float, t1: float, step_s: float = 10.0,
                     rain_zenith_db: float = 0.0) -> list[ContactWindow]:
    """Every window for every pair in [t0, t1], sorted by rise time.

    The full analytical picture of what the network *could* do — the input a
    congestion or SLA-risk model needs, and the thing no model should ever be
    trained to reproduce.
    """
    out: list[ContactWindow] = []
    for s in sats:
        for g in stations:
            out.extend(contact_windows(s, g, t0, t1, step_s=step_s,
                                       rain_zenith_db=rain_zenith_db))
    out.sort(key=lambda w: (w.t_rise, w.sat_id, w.station_id))
    return out
