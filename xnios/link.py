"""RF link budget -> achievable data rate (the *value* of a contact).

Simplified but physically-grounded: free-space path loss (Friis), a G/T receive
figure of merit, gaseous + rain attenuation scaled by elevation, then C/N0 -> SNR
-> spectral efficiency (Shannon, capped like a real modcod table). Below a minimum
SNR the link is unusable (rate = 0).

This is the single knob that makes elevation, weather, bandwidth and power *matter*
to the scheduler. Higher elevation -> shorter range + less atmosphere -> more rate.
"""

from __future__ import annotations

import math

C_LIGHT = 2.99792458e8       # m/s
K_BOLTZ_DBW = -228.6         # dBW/(K*Hz)  Boltzmann constant

# tunables (roughly X-band LEO downlink)
ZENITH_GAS_DB = 0.3          # clear-air gaseous loss at zenith
MIN_SNR_DB = -2.0            # below this, no lock -> rate 0
MAX_SPECTRAL_EFF = 5.5       # bps/Hz cap (~DVB-S2X high modcod)

# The projected aperture shrinks as cos(scan), so both the gain loss and the
# beam width run off that same cosine. Neither is extrapolated past this floor
# (scan ~87.1 deg): a first-order aperture model has nothing useful to say
# beyond it, and 1/cos is singular at 90. One constant so the two terms cannot
# drift apart.
COS_SCAN_FLOOR = 0.05


def beam_reachable(elev_deg: float, station) -> bool:
    """A phased array can only form a beam within max_scan_deg of boresight (zenith);
    below that elevation the beam cannot be steered there at all. Dishes track
    mechanically and are limited only by the elevation mask."""
    if not getattr(station, "phased_array", False):
        return True
    return (90.0 - elev_deg) <= station.max_scan_deg + 1e-9


def _scan_loss_db(elev_deg: float, station) -> float:
    """Phased-array gain rolloff as the beam steers off the local zenith (boresight).
    Scan angle = 90 - elevation; gain ~ cos(scan)^exp. Dishes (phased_array=False)
    mechanically point, so no scan loss."""
    if not getattr(station, "phased_array", False):
        return 0.0
    scan = math.radians(90.0 - max(elev_deg, 0.5))
    return -10.0 * station.scan_loss_exp * math.log10(max(math.cos(scan), COS_SCAN_FLOOR))


def scan_beamwidth_deg(elev_deg: float, station) -> float:
    """Beam width at this scan angle — first-order projected-aperture broadening.

    A planar array steered `scan` degrees off boresight presents an aperture
    foreshortened by cos(scan), and beamwidth goes inversely with aperture:

        beamwidth(scan) = beamwidth_0 / cos(scan),    scan = 90 - elevation

    So a beam at 30 deg elevation (60 deg scan) is twice as wide as at zenith,
    and at 10 deg elevation (80 deg scan) it is ~5.8x wider. Wider beams overlap
    more, which is what couples the steering envelope to co-channel interference.

    This is *only* the projected-aperture term. It is not a radiation-pattern
    model: grating lobes, element patterns and cross-pol are all absent, because
    element spacing is not represented in the station configuration. Results at
    large scan angles are therefore conditional on this simplification and say
    nothing about hardware feasibility past the grating-lobe limit.

    Opt-in via `station.beam_broadening`. Left False, this returns the fixed
    configured width and every earlier result reproduces bit-identically.
    """
    w0 = float(station.beamwidth_deg)
    if not (getattr(station, "phased_array", False)
            and getattr(station, "beam_broadening", False)):
        return w0
    scan = math.radians(90.0 - max(elev_deg, 0.5))
    return w0 / max(math.cos(scan), COS_SCAN_FLOOR)


def carrier_to_noise_density_dbhz(range_km, elev_deg, sat, station,
                                  rain_zenith_db=0.0, tx_power_w=None,
                                  gt_penalty_db=0.0) -> float:
    """C/N0 (dB-Hz) for one link, incl. path loss, atmosphere/rain and scan loss.

    `gt_penalty_db` is receive-chain degradation (V2 workstream B): PA efficiency
    loss and calibration drift show up as a shortfall in the station's effective
    G/T. Unlike rain it is *not* elevation-scaled — a degraded front end is
    degraded at every elevation. The default of 0.0 leaves the budget
    bit-identical to V1.
    """
    power = tx_power_w if tx_power_w is not None else sat.tx_power_w
    range_m = range_km * 1000.0
    wavelength = C_LIGHT / sat.freq_hz
    fspl_db = 20.0 * math.log10(4.0 * math.pi * range_m / wavelength)
    eirp_dbw = 10.0 * math.log10(power) + sat.tx_gain_dbi
    sin_el = math.sin(math.radians(max(elev_deg, 0.5)))
    atmos_db = (ZENITH_GAS_DB + rain_zenith_db) / sin_el
    return (eirp_dbw - fspl_db - atmos_db + station.g_over_t_dbk - gt_penalty_db
            - K_BOLTZ_DBW - _scan_loss_db(elev_deg, station))


def snr_linear(range_km, elev_deg, sat, station, rain_zenith_db=0.0,
               bandwidth_hz=None, tx_power_w=None, gt_penalty_db=0.0) -> float:
    """Signal-to-noise ratio (linear) for the link over its bandwidth."""
    if elev_deg <= 0 or not beam_reachable(elev_deg, station):
        return 0.0
    bw = bandwidth_hz if bandwidth_hz is not None else sat.bandwidth_hz
    power = tx_power_w if tx_power_w is not None else sat.tx_power_w
    if bw <= 0 or power <= 0:
        return 0.0
    cn0 = carrier_to_noise_density_dbhz(range_km, elev_deg, sat, station,
                                        rain_zenith_db, power, gt_penalty_db)
    snr_db = cn0 - 10.0 * math.log10(bw)
    return 10.0 ** (snr_db / 10.0)


def rate_from_sinr(bandwidth_hz: float, sinr_lin: float) -> float:
    """Shannon rate (capped at a modcod ceiling) from a linear SINR."""
    if sinr_lin <= 0:
        return 0.0
    snr_db = 10.0 * math.log10(sinr_lin)
    if snr_db < MIN_SNR_DB:
        return 0.0
    return bandwidth_hz * min(math.log2(1.0 + sinr_lin), MAX_SPECTRAL_EFF)


def ber_from_sinr(sinr_lin: float) -> float:
    """Bit error rate for a link at this SINR — an *indicator*, not a modelled
    quantity: nothing in the simulator consumes it, and no rate depends on it.

    Uses the closed-form uncoded-QPSK curve, BER = Q(sqrt(Es/N0)) with
    Q(x) = 0.5*erfc(x/sqrt(2)). A real DVB-S2X link runs LDPC/BCH coding and so
    sits many orders of magnitude lower at the same SINR; this is deliberately
    the *uncoded* reference so it stays a monotone, interpretable proxy for link
    quality on the operator dashboard rather than a fake claim of the coded
    performance. Below the lock threshold the link carries nothing, so BER is
    reported as 0.5 (no information).
    """
    if sinr_lin <= 0:
        return 0.5
    if 10.0 * math.log10(sinr_lin) < MIN_SNR_DB:
        return 0.5
    return 0.5 * math.erfc(math.sqrt(sinr_lin / 2.0))


def achievable_rate_bps(range_km: float, elev_deg: float, sat, station,
                        rain_zenith_db: float = 0.0,
                        bandwidth_hz: float | None = None,
                        tx_power_w: float | None = None,
                        gt_penalty_db: float = 0.0) -> float:
    """Achievable downlink rate (bits/s) for one interference-free link.

    bandwidth_hz / tx_power_w override the satellite's defaults — this is how a
    resource ALLOCATOR feeds in the bandwidth/power it granted this link. Includes
    phased-array scan loss. Interference (multi-beam) is applied separately by the
    simulator via snr_linear + rate_from_sinr.
    """
    if elev_deg <= 0 or not beam_reachable(elev_deg, station):
        return 0.0
    bw = bandwidth_hz if bandwidth_hz is not None else sat.bandwidth_hz
    if bw <= 0:
        return 0.0
    return rate_from_sinr(bw, snr_linear(range_km, elev_deg, sat, station,
                                         rain_zenith_db, bw, tx_power_w,
                                         gt_penalty_db))
