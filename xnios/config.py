"""Config-driven experiments.

Define an entire experiment — how many satellites, how many stations, how many
beams per station, frequency, bandwidth, data volumes, power, weather, sim length —
in a plain JSON (or YAML) file, with no code editing. Everything the hardcoded
scenarios express is expressible here. See `configs/example.json` for a fully
documented template.

Human-friendly units in configs: frequency in GHz, bandwidth in MHz, data in Gbit,
power in Watts, angles in degrees, times in seconds. They are converted to SI here.
"""

from __future__ import annotations

import json
import random

from .entities import OrbitElements, Satellite, GroundStation
from .simulator import Scenario, SimConfig
from .weather import WeatherModel
from . import orbit as orb


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        if path.lower().endswith((".yaml", ".yml")):
            import yaml                      # optional; only needed for YAML configs
            return yaml.safe_load(f)
        return json.load(f)


def sim_config_from_config(cfg: dict) -> SimConfig:
    s = cfg.get("sim", {})
    dt = s.get("dt_s", 5.0)
    return SimConfig(
        duration_s=s.get("duration_s", 1200.0),
        dt_s=dt,
        decision_interval_s=s.get("decision_interval_s", dt),
        trace=s.get("trace", False),
        handover=s.get("handover", False),
        handover_lead_s=s.get("handover_lead_s", 30.0),
        handover_mode=s.get("handover_mode", "elevation"),
    )


def scenario_from_config(cfg) -> Scenario:
    """Build a Scenario from a config dict (or a path to a JSON/YAML file)."""
    if isinstance(cfg, str):
        cfg = load_config(cfg)
    rng = random.Random(cfg.get("seed", 0))
    stations, weather = _build_stations(cfg)

    # optional live weather: fetch current conditions per station (falls back to the
    # static per-station 'weather' fields if the key/network is unavailable)
    wcfg = cfg.get("weather", {})
    provider = wcfg.get("provider")
    if provider in ("dynamic", "markov"):                     # weather changes over the run
        from .weather import DynamicWeatherModel
        weather = DynamicWeatherModel(stations, seed=cfg.get("seed", 0),
                                      dwell_s=wcfg.get("dwell_s", 300.0))
    else:
        try:
            if provider in ("openmeteo", "open-meteo"):
                from .weather_live import fetch_openmeteo_weather
                weather = fetch_openmeteo_weather(stations)   # free, no key
            elif provider in ("openweathermap", "owm"):
                from .weather_live import fetch_owm_weather
                weather = fetch_owm_weather(stations, api_key=wcfg.get("api_key"))
        except Exception:
            pass                                              # keep the static weather

    dynamics = _build_dynamics(cfg, stations)
    sats = _build_satellites(cfg, rng)

    # optional latent station health (V2 workstream B). Its outages are ordinary
    # dynamics Events, so degradation-driven failures reuse the existing plumbing
    # instead of forking it. Absent -> no degradation, and V1 is unchanged.
    from .degradation import make_degradation
    scfg = cfg.get("sim", {})
    degradation = make_degradation(cfg.get("degradation"), stations,
                                   duration_s=scfg.get("duration_s", 1200.0),
                                   dt_s=scfg.get("dt_s", 5.0),
                                   seed=cfg.get("seed", 0))
    if degradation is not None:
        from .dynamics import NetworkDynamics
        events = (list(dynamics.events) if dynamics else []) + degradation.failure_events()
        dynamics = NetworkDynamics(stations, events)

    # optional arrival process (V2 workstream A). Absent -> NoArrivals, i.e. V1:
    # buffers are filled once at t=0 and only drain.
    from .traffic import make_traffic
    tcfg = dict(cfg.get("traffic") or {})
    if tcfg:
        tcfg.setdefault("seed", cfg.get("seed", 0))
    traffic = make_traffic(tcfg, sats)

    return Scenario(sats, stations, weather=weather, dynamics=dynamics,
                    traffic=traffic, degradation=degradation,
                    name=cfg.get("name", "config scenario"))


def _build_dynamics(cfg, stations):
    """Station/beam failures + dynamic capacity from config['dynamics'] (explicit
    'events' list, or 'random' Poisson failure params). None -> static network."""
    dcfg = cfg.get("dynamics")
    if not dcfg:
        return None
    from .dynamics import NetworkDynamics, Event, failure_events
    events = []
    for e in dcfg.get("events", []):
        val = e.get("value", 0.0)
        if e["action"] == "bandwidth":
            val = e.get("value_mhz", val / 1e6) * 1e6         # MHz -> Hz
        events.append(Event(e["t"], e["station"], e["action"], val))
    r = dcfg.get("random")
    if r:
        events += failure_events(
            stations, seed=cfg.get("seed", 0),
            duration_s=cfg.get("sim", {}).get("duration_s", 1200.0),
            station_mtbf_s=r.get("station_mtbf_s", 0.0), station_mttr_s=r.get("station_mttr_s", 600.0),
            beam_mtbf_s=r.get("beam_mtbf_s", 0.0), beam_mttr_s=r.get("beam_mttr_s", 300.0))
    return NetworkDynamics(stations, events)


# --------------------------------------------------------------------------- #
def _reference_orbit(planes, idx) -> OrbitElements:
    p = planes[idx]
    return OrbitElements(alt_km=p.get("altitude_km", 600.0), inc_deg=p["inc"],
                         raan_deg=p.get("raan", 0.0), arg_lat0_deg=0.0)


def _build_stations(cfg):
    """Stations may give explicit lat/lon, OR `place_under` a satellite plane so a
    novice can drop a station beneath a pass without doing orbital math."""
    planes = cfg.get("satellites", {}).get("planes", [{"inc": 53.0, "raan": 0.0}])
    t_mid = cfg.get("t_mid", 600.0)
    stations, wx = [], {}
    for st in cfg["stations"]:
        if "place_under" in st:
            pu = st["place_under"]
            lat0, lon0 = orb.place_station_under(
                _reference_orbit(planes, pu.get("plane", 0)), pu.get("t", t_mid))
            lat, lon = lat0 + pu.get("dlat", 0.0), lon0 + pu.get("dlon", 0.0)
        else:
            lat, lon = st["lat"], st["lon"]
        stations.append(GroundStation(
            id=st["id"], lat_deg=lat, lon_deg=lon, alt_km=st.get("alt_km", 0.0),
            num_beams=st.get("num_beams", 1),
            elevation_mask_deg=st.get("elevation_mask_deg", 10.0),
            g_over_t_dbk=st.get("g_over_t_dbk", 20.0),
            setup_time_s=st.get("setup_time_s", 0.0),
            bandwidth_hz=st.get("bandwidth_mhz", 500.0) * 1e6,   # shared bandwidth pool
            phased_array=st.get("phased_array", False),
            beamwidth_deg=st.get("beamwidth_deg", 3.0),
            n_channels=st.get("n_channels", 1),
            scan_loss_exp=st.get("scan_loss_exp", 1.3),
            max_scan_deg=st.get("max_scan_deg", 60.0),
            dual_pol=st.get("dual_pol", False),
        ))
        if "weather" in st:
            wx[st["id"]] = st["weather"]
    return stations, WeatherModel(state_by_station=wx)


def _build_satellites(cfg, rng):
    spec = cfg["satellites"]
    t_mid = cfg.get("t_mid", 600.0)
    freq = spec.get("freq_ghz", 8.2) * 1e9
    bw = spec.get("bandwidth_mhz", 50.0) * 1e6
    power = spec.get("tx_power_w", 5.0)
    power_max = spec.get("tx_power_max_w", power * 2.0)     # headroom a power allocator may use
    gain = spec.get("tx_gain_dbi", 6.0)

    # --- explicit: user lists every satellite ---
    if spec.get("mode", "generate") == "explicit":
        sats = []
        for s in spec["list"]:
            o = OrbitElements(alt_km=s.get("altitude_km", 600.0), inc_deg=s["inc"],
                              raan_deg=s.get("raan", 0.0), arg_lat0_deg=s.get("arg_lat0", 0.0))
            sats.append(Satellite(
                id=s["id"], orbit=o, backlog_bits=s["backlog_gbit"] * 1e9,
                tier=s.get("tier", "commercial"), deadline_s=s.get("deadline_s"),
                tx_power_w=s.get("tx_power_w", power),
                tx_power_max_w=s.get("tx_power_max_w", power_max),
                tx_gain_dbi=s.get("tx_gain_dbi", gain),
                freq_hz=s.get("freq_ghz", spec.get("freq_ghz", 8.2)) * 1e9,
                bandwidth_hz=s.get("bandwidth_mhz", spec.get("bandwidth_mhz", 50.0)) * 1e6,
            ))
        return sats

    # --- generate: sample `count` satellites from distributions ---
    count = spec.get("count", 20)
    planes = spec.get("planes", [{"inc": 53.0, "raan": 0.0}])
    spread = spec.get("arg_lat_spread_deg", 10.0)
    bl = spec.get("backlog_gbit", {"classes": [2, 20, 80], "weights": [0.35, 0.4, 0.25]})
    classes = [c * 1e9 for c in bl["classes"]]
    weights = bl.get("weights")
    tiers = spec.get("tiers", ["research", "commercial", "commercial", "military", "emergency"])
    tier_dl = spec.get("tier_deadline_s",
                       {"emergency": 90, "military": 180, "commercial": 300, "research": 550})
    use_dl = spec.get("use_deadlines", True)

    sats = []
    for i in range(count):
        plane = planes[i % len(planes)]
        o = OrbitElements(alt_km=plane.get("altitude_km", 600.0), inc_deg=plane["inc"],
                          raan_deg=plane.get("raan", 0.0),
                          arg_lat0_deg=rng.uniform(-spread, spread))
        tier = rng.choice(tiers)
        backlog = rng.choices(classes, weights=weights)[0] * rng.uniform(0.7, 1.3)
        deadline = (t_mid + tier_dl.get(tier, 300) * rng.uniform(0.8, 1.2)) if use_dl else None
        sats.append(Satellite(
            id=f"SAT-{i:03d}", orbit=o, backlog_bits=backlog, tier=tier,
            deadline_s=deadline, tx_power_w=power, tx_power_max_w=power_max,
            tx_gain_dbi=gain, freq_hz=freq, bandwidth_hz=bw))
    return sats
