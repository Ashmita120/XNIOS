"""Static configuration objects for the digital twin.

These are *config* only (immutable-ish description of the world). All mutable
runtime state (backlogs draining, beams busy, sessions active) lives in the
Simulator, keyed by entity id. Keeping config and runtime separate is what lets
us reset scenarios and run many schedulers against an identical world.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --- priority tiers (higher number = more important) ---------------------------
TIERS = {
    "research": 1,
    "commercial": 2,
    "military": 3,
    "emergency": 4,
}


@dataclass
class OrbitElements:
    """Circular LEO orbit, parameterised for reproducible synthetic scenarios.

    A circular orbit is fully described by altitude, inclination, RAAN (right
    ascension of ascending node) and the argument of latitude at t=0. This is
    enough for correct pass geometry (elevation/azimuth/range) without pulling in
    SGP4/TLEs yet — swap `orbit.sat_position_ecef` for Skyfield later and every
    downstream module is unchanged.
    """

    alt_km: float = 600.0
    inc_deg: float = 53.0
    raan_deg: float = 0.0
    arg_lat0_deg: float = 0.0   # argument of latitude at t=0 (position along orbit)


@dataclass
class Satellite:
    """A satellite = a data-bearing 'pod' asking to downlink."""

    id: str
    orbit: OrbitElements
    backlog_bits: float = 5.0e9          # data waiting in the onboard buffer
    fill_rate_bps: float = 0.0           # buffer growth (EO imaging accrues data)
    priority: int = 2                    # 1..4, see TIERS
    tier: str = "commercial"
    deadline_s: float | None = None      # SLA deadline (absolute sim time); None = no SLA

    # --- link/tx parameters (X-band downlink defaults) ---
    tx_power_w: float = 5.0              # nominal transmit power (used by 'fixed' power alloc)
    tx_power_max_w: float = 10.0         # cap a power allocator may boost a link to
    tx_gain_dbi: float = 6.0
    freq_hz: float = 8.2e9
    bandwidth_hz: float = 50.0e6

    def __post_init__(self):
        if self.tier in TIERS and self.priority == 2:
            self.priority = TIERS[self.tier]


@dataclass
class GroundStation:
    """A ground station = a 'node' offering RF capacity via one or more beams.

    v0 is single-beam MVP: num_beams=1 means one satellite at a time. Raising
    num_beams already works in the engine (each beam serves one sat) — the
    interference/frequency coupling between beams is what's deferred to v2.
    """

    id: str
    lat_deg: float
    lon_deg: float
    alt_km: float = 0.0
    num_beams: int = 1
    elevation_mask_deg: float = 10.0     # below this, no usable link
    g_over_t_dbk: float = 20.0           # station figure of merit (G/T)
    setup_time_s: float = 0.0            # slew/acquisition before a session starts (v0: 0)
    bandwidth_hz: float = 500.0e6        # total bandwidth POOL shared across active beams.
                                         # Large default => uncontended (each link gets its
                                         # satellite's full bandwidth). Lower it (or add beams)
                                         # to make bandwidth allocation actually bite.

    # --- phased-array parameters (opt-in; default False = traditional dish) ---
    phased_array: bool = False           # True: one aperture forms num_beams electronic beams
    beamwidth_deg: float = 3.0           # angular width of a beam; drives scan loss + interference
    n_channels: int = 1                  # frequency channels for reuse (beams on different
                                         # channels don't interfere)
    scan_loss_exp: float = 1.3           # gain rolloff ~ cos(scan_angle)^exp as beams steer
                                         # away from the local zenith (boresight)
    max_scan_deg: float = 60.0           # electronic steering limit off boresight (zenith).
                                         # 60 deg -> reachable elevation >= 30 deg (FOV 120 deg)
    dual_pol: bool = False               # dual polarisation: doubles the reuse slots
                                         # (channel x pol), so more beams share a frequency
    beam_broadening: bool = False        # Model B: beam width grows as 1/cos(scan) off
                                         # boresight, so steering widens the beam and
                                         # raises co-channel interference. False (Model A)
                                         # holds the width fixed at beamwidth_deg.
