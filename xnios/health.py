"""Network health monitor — telemetry turned into operator-facing indicators.

`telemetry.py` records what *is*. This module says what it *means*: the
"Network Health = 92%, Congestion = Medium, Failure Risk = Low" panel the
dashboard shows, plus the factor breakdown behind every number.

Two rules make this a monitor rather than a second metrics module:

* **It lives outside the twin.** The simulator's KPI vector is never collapsed
  to a single score (`metrics.py` keeps it a vector on purpose, because
  scalarisation weights are a *policy* choice, not a physical fact). A health
  score is exactly such a scalarisation — so it belongs here, downstream, where
  the weights are explicit, tunable, and reported alongside the result.
* **It reports state, not predictions.** `failure_risk` here is computed from
  what is already observable (outages in progress, lost beams, weather severity,
  redundancy left). It is *not* a forecast, because in the current twin failures
  are a memoryless Poisson process — nothing precedes them, so nothing can
  honestly anticipate them. Real failure prediction needs precursor signals
  (a degradation model) first; this module is where that will surface when it
  exists, and `HealthReport.notes` says so explicitly rather than implying a
  capability that isn't there.

Usage:

    from xnios.health import assess
    report = assess(recorder.latest())          # one instant
    report = assess(recorder.records[-12:])     # smoothed over a window
    report.network_health      -> 0..100
    report.congestion.level    -> "low" | "moderate" | "high" | "critical"
    report.to_dict()           -> JSON for the API/dashboard
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .weather import FADE_DB

# --- weights of the composite health score (explicit, so they can be argued
# with and changed). They sum to 1.0; `assess(weights=...)` overrides them.
DEFAULT_WEIGHTS = {
    "availability": 0.25,     # are the stations and beams actually there
    "link_quality": 0.25,     # is the RF good enough to carry data
    "coverage": 0.20,         # can backlogged satellites reach anyone at all
    "delivery": 0.20,         # is data actually moving vs. queueing up
    "congestion": 0.10,       # is capacity oversubscribed
}

# level thresholds, low -> critical
_LEVELS = ("low", "moderate", "high", "critical")


def _level(x: float, t1: float, t2: float, t3: float) -> str:
    """Bucket a 0..1 severity into the four operator levels."""
    if x < t1:
        return _LEVELS[0]
    if x < t2:
        return _LEVELS[1]
    if x < t3:
        return _LEVELS[2]
    return _LEVELS[3]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _mean(xs, default=0.0) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else default


@dataclass
class Indicator:
    """One named indicator: a 0..1 score, a level, and the factors behind it.

    `factors` is what the explainability panel renders — every number the
    operator sees can be opened up into the measurements that produced it.
    """

    name: str
    score: float                 # 0..1, always "higher = better" EXCEPT severity ones
    level: str
    value: float = 0.0           # the raw quantity this summarises
    unit: str = ""
    severity: bool = False       # True: higher score = worse (congestion, risk)
    factors: dict = field(default_factory=dict)
    note: str = ""

    @property
    def pct(self) -> float:
        return round(100.0 * self.score, 1)


@dataclass
class StationHealth:
    station_id: str
    health: float                # 0..1
    level: str
    up: bool
    beams_available: int
    beams_total: int
    beam_utilization: float
    bandwidth_utilization: float
    weather: str
    rain_fade_db: float
    mean_sinr_db: float
    connected: int
    degraded: bool
    reasons: list = field(default_factory=list)


@dataclass
class HealthReport:
    t: float
    network_health: float                  # 0..100, the headline number
    level: str
    indicators: dict = field(default_factory=dict)     # name -> Indicator
    stations: list = field(default_factory=list)       # list[StationHealth]
    headline: dict = field(default_factory=dict)       # raw KPIs for the tiles
    weights: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    window_steps: int = 1

    def to_dict(self) -> dict:
        return {
            "t": self.t,
            "network_health": self.network_health,
            "level": self.level,
            "indicators": {k: asdict(v) for k, v in self.indicators.items()},
            "stations": [asdict(s) for s in self.stations],
            "headline": self.headline,
            "weights": self.weights,
            "notes": self.notes,
            "window_steps": self.window_steps,
        }

    def __str__(self) -> str:
        lines = [f"Network Health  {self.network_health:.0f}%  ({self.level})"]
        for k, ind in self.indicators.items():
            arrow = "risk" if ind.severity else "good"
            lines.append(f"  {k:<14} {ind.pct:>5.1f}%  {ind.level:<9} ({arrow})")
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Assessment                                                                   #
# --------------------------------------------------------------------------- #

def assess(records, weights: dict | None = None,
           sinr_good_db: float = 12.0, sinr_floor_db: float = -2.0) -> HealthReport:
    """Assess one `TelemetryRecord` or a window of them (the last one is the
    reference instant; the rest smooth the noisy quantities).

    `sinr_floor_db` is the link's lock threshold (`link.MIN_SNR_DB`) and
    `sinr_good_db` the SINR at which a link is considered comfortable — link
    quality is scored as the margin between them.
    """
    if records is None:
        raise ValueError("assess() needs a TelemetryRecord or a list of them")
    window = records if isinstance(records, (list, tuple)) else [records]
    window = [r for r in window if r is not None]
    if not window:
        raise ValueError("assess() got an empty window")

    cur = window[-1]
    net = cur.network
    if net is None:
        raise ValueError("records must include the 'network' face")
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    total_w = sum(w.values()) or 1.0

    nets = [r.network for r in window if r.network is not None]
    notes: list = []

    # --- availability: how much of the nameplate capacity is actually usable
    beams_avail = _mean(n.beams_available for n in nets)
    beams_total = max(1.0, float(net.beams_total))
    stations_up_frac = (net.stations_up / net.stations_total) if net.stations_total else 1.0
    beam_avail_frac = _clamp(beams_avail / beams_total)
    availability = _clamp(0.5 * stations_up_frac + 0.5 * beam_avail_frac)
    avail_ind = Indicator(
        name="availability", score=availability,
        level=_level(1.0 - availability, 0.05, 0.2, 0.4),
        value=100.0 * availability, unit="%",
        factors={
            "stations_up": f"{net.stations_up}/{net.stations_total}",
            "beams_available": f"{beams_avail:.1f}/{beams_total:.0f}",
            "degraded_stations": sum(1 for s in cur.stations if s.degraded),
        },
    )

    # --- link quality: mean SINR margin over the links actually carrying data
    active = [l for l in cur.links if l.active]
    span = max(1e-6, sinr_good_db - sinr_floor_db)
    if active:
        margins = [_clamp((l.sinr_db - sinr_floor_db) / span) for l in active]
        lq = _mean(margins)
        mean_sinr = _mean(l.sinr_db for l in active)
        worst = min(active, key=lambda l: l.sinr_db)
        lq_factors = {
            "mean_sinr_db": round(mean_sinr, 2),
            "min_sinr_db": round(worst.sinr_db, 2),
            "worst_link": f"{worst.sat_id}->{worst.station_id}",
            "mean_ber": f"{_mean(l.ber for l in active):.2e}",
            "active_links": len(active),
            "mean_rain_fade_db": round(net.mean_rain_fade_db, 2),
        }
        lq_note = ""
    else:
        # nothing transmitting: judge the opportunity, not a non-existent link
        cands = [l for l in cur.links if not l.active]
        lq = _mean((_clamp((l.sinr_db - sinr_floor_db) / span) for l in cands), default=1.0)
        lq_factors = {"active_links": 0, "visible_links": len(cands),
                      "mean_rain_fade_db": round(net.mean_rain_fade_db, 2)}
        lq_note = "no active links — scored on visible candidates"
    lq_ind = Indicator(name="link_quality", score=_clamp(lq),
                       level=_level(1.0 - _clamp(lq), 0.25, 0.5, 0.75),
                       value=100.0 * _clamp(lq), unit="%",
                       factors=lq_factors, note=lq_note)

    # --- coverage: can the satellites that still owe data reach anyone?
    coverage = _clamp(_mean(n.coverage for n in nets))
    cov_ind = Indicator(
        name="coverage", score=coverage,
        level=_level(1.0 - coverage, 0.15, 0.35, 0.6),
        value=100.0 * coverage, unit="%",
        factors={"backlogged_sats": net.n_backlogged,
                 "sats_with_link": net.n_sats_with_link,
                 "visible_pairs": net.n_visible_pairs},
    )
    if net.n_backlogged and coverage < 0.5:
        notes.append("Low coverage: most backlogged satellites have no usable link — "
                     "a geometry/capacity limit, not a scheduling one.")

    # --- delivery: is the queue draining, or is data just piling up?
    served = net.beams_active
    demanders = net.n_waiting + net.beams_active
    serve_frac = (served / demanders) if demanders else 1.0
    drained = net.delivery_fraction
    delivery = _clamp(0.6 * serve_frac + 0.4 * _clamp(drained))
    del_ind = Indicator(
        name="delivery", score=delivery,
        level=_level(1.0 - delivery, 0.25, 0.5, 0.75),
        value=net.throughput_bps / 1e9, unit="Gbps",
        factors={"serving": served, "waiting": net.n_waiting,
                 "delivered_gbit": round(net.bits_delivered_total / 1e9, 2),
                 "queue_gbit": round(net.queue_bits / 1e9, 2),
                 "completion_rate": round(net.completion_rate, 3)},
    )

    # --- congestion: SEVERITY (higher = worse). Demand chasing capacity.
    contention = _mean(n.contention_ratio for n in nets)
    util = _mean(n.beam_utilization for n in nets)
    cong_sev = _clamp(0.6 * _clamp(contention / 2.0) + 0.4 * _clamp(util))
    cong_ind = Indicator(
        name="congestion", score=cong_sev, severity=True,
        level=_level(cong_sev, 0.35, 0.6, 0.85),
        value=contention, unit="demand/beam",
        factors={"contention_ratio": round(contention, 3),
                 "beam_utilization": round(util, 3),
                 "waiting": net.n_waiting, "beams_active": net.beams_active,
                 "beams_available": net.beams_available},
    )

    # --- failure risk: SEVERITY, and observational only (see module docstring)
    outage_frac = 1.0 - ((net.stations_up / net.stations_total)
                         if net.stations_total else 1.0)
    beam_loss = 1.0 - beam_avail_frac
    wx_sev = _clamp(net.max_rain_fade_db / max(FADE_DB.values()))
    redundancy = _clamp(1.0 - (net.stations_up - 1) / max(1.0, net.stations_total - 1)) \
        if net.stations_total > 1 else 1.0
    risk = _clamp(0.40 * outage_frac + 0.25 * beam_loss
                  + 0.20 * wx_sev + 0.15 * redundancy)
    risk_ind = Indicator(
        name="failure_risk", score=risk, severity=True,
        level=_level(risk, 0.2, 0.45, 0.7),
        value=100.0 * risk, unit="%",
        factors={"stations_down": net.stations_total - net.stations_up,
                 "beams_lost": int(round(beams_total - beams_avail)),
                 "max_rain_fade_db": round(net.max_rain_fade_db, 2),
                 "interruptions": net.interruptions_total,
                 "single_points_of_failure": redundancy > 0.9},
        note="observed state, not a forecast",
    )
    notes.append("failure_risk reflects outages and degradation already present. "
                 "Predictive failure risk requires a station degradation model "
                 "(precursor signals); the current failure process is memoryless.")

    # --- weather severity: SEVERITY
    wx = _clamp(net.mean_rain_fade_db / max(FADE_DB.values()))
    wx_ind = Indicator(
        name="weather", score=wx, severity=True,
        level=_level(wx, 0.15, 0.35, 0.6),
        value=net.mean_rain_fade_db, unit="dB",
        factors={"states": dict(net.weather_counts),
                 "mean_fade_db": round(net.mean_rain_fade_db, 2),
                 "max_fade_db": round(net.max_rain_fade_db, 2)},
    )

    # --- energy efficiency (informational, not in the composite)
    gb = net.bits_delivered_total / 1e9
    kj = net.energy_j_total / 1e3
    eff = (gb / kj) if kj > 0 else 0.0
    en_ind = Indicator(
        name="energy", score=_clamp(eff / 50.0), level="",
        value=eff, unit="Gb/kJ",
        factors={"energy_kj": round(kj, 2), "power_w": round(net.power_w, 1),
                 "delivered_gbit": round(gb, 2)},
    )

    # --- composite: goodness indicators directly, severities as (1 - severity)
    parts = {
        "availability": avail_ind.score,
        "link_quality": lq_ind.score,
        "coverage": cov_ind.score,
        "delivery": del_ind.score,
        "congestion": 1.0 - cong_ind.score,
    }
    score = sum(w[k] * parts[k] for k in w if k in parts) / total_w
    health = round(100.0 * _clamp(score), 1)

    stations = [_station_health(s, cur, sinr_floor_db, span) for s in cur.stations]

    return HealthReport(
        t=cur.t, network_health=health,
        level=_level(1.0 - score, 0.15, 0.3, 0.5),
        indicators={
            "availability": avail_ind, "link_quality": lq_ind, "coverage": cov_ind,
            "delivery": del_ind, "congestion": cong_ind, "failure_risk": risk_ind,
            "weather": wx_ind, "energy": en_ind,
        },
        stations=stations,
        headline={
            "throughput_gbps": round(net.throughput_bps / 1e9, 3),
            "delivered_gbit": round(net.bits_delivered_total / 1e9, 2),
            "queue_gbit": round(net.queue_bits / 1e9, 2),
            "completion_rate": round(net.completion_rate, 4),
            "beam_utilization": round(net.beam_utilization, 4),
            "sessions_active": net.sessions_active,
            "stations_up": net.stations_up, "stations_total": net.stations_total,
            "mean_sinr_db": round(net.mean_sinr_db, 2),
            "energy_kj": round(kj, 2),
            "interruptions": net.interruptions_total,
            "handovers": net.handovers_total,
            "scheduler": cur.decision.scheduler if cur.decision else "",
            "power_allocator": cur.decision.power_allocator if cur.decision else "",
            "bandwidth_allocator": cur.decision.bandwidth_allocator if cur.decision else "",
            "freq_allocator": cur.decision.freq_allocator if cur.decision else "",
        },
        weights=w, notes=notes, window_steps=len(window),
    )


def _station_health(s, record, sinr_floor_db: float, span: float) -> StationHealth:
    """Per-station health: availability of its own capacity, weighted with the
    quality of the links it is actually carrying. An idle, fully-available
    station is healthy (1.0) — nothing is wrong with it."""
    reasons: list = []
    if not s.up:
        return StationHealth(
            station_id=s.station_id, health=0.0, level="critical", up=False,
            beams_available=s.beams_available, beams_total=s.beams_total,
            beam_utilization=0.0, bandwidth_utilization=0.0, weather=s.weather,
            rain_fade_db=s.rain_fade_db, mean_sinr_db=0.0, connected=0,
            degraded=True, reasons=["station is down (outage)"],
        )

    beam_frac = (s.beams_available / s.beams_total) if s.beams_total else 1.0
    if beam_frac < 1.0:
        reasons.append(f"{s.beams_total - s.beams_available} of {s.beams_total} beams unavailable")

    links = [l for l in record.links if l.station_id == s.station_id and l.active]
    if links:
        rf = _mean(_clamp((l.sinr_db - sinr_floor_db) / span) for l in links)
        if rf < 0.5:
            reasons.append(f"weak links (mean SINR {s.mean_sinr_db:.1f} dB)")
    else:
        rf = 1.0

    if s.rain_fade_db >= FADE_DB["rain"]:
        reasons.append(f"{s.weather} ({s.rain_fade_db:.1f} dB fade)")
    if s.beam_utilization >= 0.95:
        reasons.append("all beams committed")

    bw_frac = _clamp(s.bandwidth_pool_hz / s.bandwidth_base_hz) if s.bandwidth_base_hz else 1.0
    if bw_frac < 1.0:
        reasons.append(f"bandwidth pool reduced to {100 * bw_frac:.0f}% of nameplate")
    health = _clamp(0.55 * beam_frac + 0.30 * rf + 0.15 * bw_frac)
    return StationHealth(
        station_id=s.station_id, health=health,
        level=_level(1.0 - health, 0.15, 0.35, 0.6), up=True,
        beams_available=s.beams_available, beams_total=s.beams_total,
        beam_utilization=s.beam_utilization,
        bandwidth_utilization=s.bandwidth_utilization,
        weather=s.weather, rain_fade_db=s.rain_fade_db,
        mean_sinr_db=s.mean_sinr_db, connected=len(s.connected_sats),
        degraded=s.degraded, reasons=reasons or ["nominal"],
    )


def timeline(records, weights: dict | None = None, every: int = 1) -> list:
    """Health over a whole run — one report per (every-th) record. This is the
    series the dashboard's health chart draws, and the label source for anything
    later trained to anticipate degradation."""
    return [assess(r, weights) for i, r in enumerate(records) if i % max(1, every) == 0]
