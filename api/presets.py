"""Scenario presets the dashboard offers in its run dialog.

Two sources: every JSON in `configs/` (so a config the user edits on disk shows
up in the UI automatically), plus built-ins that are generated with
`orbit.find_orbit_for_elevation` so their geometry is guaranteed rather than
hoped for — the coverage-gap failure mode documented in `testing.md` is a
plausible-looking constellation that never actually flies over anything.
"""

from __future__ import annotations

import json
import os

from xnios import orbit as orb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "configs")


def _aimed_constellation(stations, per_plane: int, stagger_deg: float,
                         backlogs, inc_deg: float = 53.0, alt_km: float = 600.0) -> list:
    """One orbital plane aimed at each station, satellites staggered along it so
    their passes overlap and the network is genuinely contended."""
    sats = []
    for pi, st in enumerate(stations):
        sol = orb.find_orbit_for_elevation(st["lat"], st["lon"], inc_deg, 80.0, alt_km)
        for k in range(per_plane):
            offset = (k - (per_plane - 1) / 2.0) * stagger_deg
            sats.append({
                "id": f"SAT-{pi}{k:02d}", "inc": inc_deg, "altitude_km": alt_km,
                "raan": sol["raan_deg"], "arg_lat0": sol["arg_lat0_deg"] + offset,
                "backlog_gbit": backlogs[k % len(backlogs)],
                "tier": ["research", "commercial", "military", "emergency"][k % 4],
                "deadline_s": 600 + 120 * k,
            })
    return sats


_INDIA_4 = [
    {"id": "Delhi", "lat": 28.61, "lon": 77.21, "g_over_t_dbk": 24, "num_beams": 4,
     "phased_array": True, "beamwidth_deg": 3.0, "n_channels": 4, "dual_pol": True,
     "max_scan_deg": 60, "bandwidth_mhz": 300, "weather": "clear", "setup_time_s": 2.0},
    {"id": "Bengaluru-ISTRAC", "lat": 13.03, "lon": 77.51, "g_over_t_dbk": 27, "num_beams": 4,
     "phased_array": True, "beamwidth_deg": 3.0, "n_channels": 4, "dual_pol": True,
     "max_scan_deg": 60, "bandwidth_mhz": 300, "weather": "clear", "setup_time_s": 2.0},
    {"id": "Ahmedabad-SAC", "lat": 23.03, "lon": 72.58, "g_over_t_dbk": 24, "num_beams": 4,
     "phased_array": True, "beamwidth_deg": 3.0, "n_channels": 4, "dual_pol": True,
     "max_scan_deg": 60, "bandwidth_mhz": 300, "weather": "rain", "setup_time_s": 2.0},
    {"id": "Guwahati", "lat": 26.14, "lon": 91.74, "g_over_t_dbk": 22, "num_beams": 4,
     "phased_array": True, "beamwidth_deg": 3.0, "n_channels": 4, "dual_pol": True,
     "max_scan_deg": 60, "bandwidth_mhz": 300, "weather": "cloudy", "setup_time_s": 2.0},
]


def _builtin() -> dict:
    nominal = {
        "name": "India 4 — nominal operations",
        "description": "Four real Indian phased-array sites, clear-to-cloudy weather, "
                       "no failures. The baseline every other preset is compared against.",
        "seed": 0, "t_mid": 900,
        "stations": _INDIA_4,
        "satellites": {"mode": "explicit", "freq_ghz": 8.2, "bandwidth_mhz": 50,
                       "tx_power_w": 5, "tx_power_max_w": 10,
                       "list": _aimed_constellation(_INDIA_4, 5, 4.0, [8, 25, 60])},
        "weather": {"provider": "static"},
        "sim": {"duration_s": 1800, "dt_s": 10, "decision_interval_s": 10},
    }

    congested = json.loads(json.dumps(nominal))
    congested["name"] = "India 4 — congestion"
    congested["description"] = ("Twice the satellites against the same four stations: "
                                "beams saturate and the throughput/fairness trade-off "
                                "between schedulers becomes visible.")
    congested["satellites"]["list"] = _aimed_constellation(_INDIA_4, 10, 2.0, [8, 25, 60])

    storm = json.loads(json.dumps(nominal))
    storm["name"] = "India 4 — storm + failures"
    storm["description"] = ("Markov weather plus Poisson station/beam outages. Drives "
                            "interruptions, recovery and self-healing — and is where "
                            "adaptive power earns its keep.")
    storm["weather"] = {"provider": "dynamic", "dwell_s": 240}
    storm["dynamics"] = {"random": {"station_mtbf_s": 900, "station_mttr_s": 300,
                                    "beam_mtbf_s": 700, "beam_mttr_s": 250}}
    storm["sim"]["handover"] = True
    storm["sim"]["handover_lead_s"] = 40

    return {"india4-nominal": nominal, "india4-congested": congested,
            "india4-storm": storm}


def _from_configs() -> dict:
    out = {}
    if not os.path.isdir(CONFIG_DIR):
        return out
    for fn in sorted(os.listdir(CONFIG_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CONFIG_DIR, fn), encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue
        key = "file:" + fn[:-5]
        cfg.setdefault("name", fn)
        cfg.setdefault("description", f"Loaded from configs/{fn}")
        out[key] = cfg
    return out


def all_presets() -> dict:
    """{key: config}. Built-ins first, then whatever is in `configs/`."""
    return {**_builtin(), **_from_configs()}


def summary() -> list:
    """Light listing for the run dialog — no satellite lists sent to the browser."""
    out = []
    for key, cfg in all_presets().items():
        sats = cfg.get("satellites", {})
        n = len(sats.get("list", [])) if sats.get("mode") == "explicit" else sats.get("count", 0)
        out.append({
            "key": key,
            "name": cfg.get("name", key),
            "description": cfg.get("description", ""),
            "n_satellites": n,
            "n_stations": len(cfg.get("stations", [])),
            "duration_s": cfg.get("sim", {}).get("duration_s", 1200),
            "dt_s": cfg.get("sim", {}).get("dt_s", 5),
            "weather": cfg.get("weather", {}).get("provider", "static"),
            "failures": bool(cfg.get("dynamics")),
            "handover": bool(cfg.get("sim", {}).get("handover")),
        })
    return out
