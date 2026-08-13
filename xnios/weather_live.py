"""Live weather -> per-station weather state.

Fetches current conditions at each ground station's lat/lon and maps them to the
twin's weather states (clear / cloudy / rain / storm), which drive rain fade in the
link budget. Fetched once per run (cached briefly) — the sim runs far faster than
weather changes, so a snapshot of *current* conditions at run start is the sensible
"real-time" model.

Two providers:
  * Open-Meteo  (recommended) — free, NO API KEY. `fetch_openmeteo_weather(stations)`.
  * OpenWeatherMap — needs a key (api_key= or OWM_API_KEY env var).

Any failure (no network, timeout, no key) falls back to 'clear' per station so a run
never crashes on weather.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from .weather import WeatherModel

OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"
_CACHE: dict = {}                 # key -> (timestamp, WeatherModel)
_CACHE_TTL_S = 600.0             # re-fetch at most every 10 minutes


def _map_openmeteo(cur: dict) -> str:
    """Map an Open-Meteo 'current' block to our four states."""
    rain = cur.get("rain", 0.0) or 0.0
    precip = cur.get("precipitation", rain) or 0.0
    p = max(rain, precip)                        # mm in the last hour
    cloud = cur.get("cloud_cover", 0) or 0       # %
    wind = cur.get("wind_speed_10m", 0) or 0     # km/h
    if p >= 7.5 or wind >= 55:                   # heavy rain or gale -> storm
        return "storm"
    if p >= 0.2:
        return "rain"
    if cloud >= 60:
        return "cloudy"
    return "clear"


def fetch_openmeteo_weather(stations, timeout: float = 8.0) -> WeatherModel:
    """Per-station current conditions from Open-Meteo (no API key). Cached; errors
    fall back to 'clear'."""
    key = ("om", tuple((g.id, round(g.lat_deg, 3), round(g.lon_deg, 3)) for g in stations))
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL_S:
        return hit[1]

    states = {}
    for g in stations:
        try:
            params = urllib.parse.urlencode({
                "latitude": g.lat_deg, "longitude": g.lon_deg,
                "current": "precipitation,rain,cloud_cover,wind_speed_10m"})
            with urllib.request.urlopen(f"{OPENMETEO_URL}?{params}", timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            states[g.id] = _map_openmeteo(data.get("current", {}))
        except Exception:
            states[g.id] = "clear"
    model = WeatherModel(state_by_station=states)
    _CACHE[key] = (time.time(), model)
    return model


def _map_condition(main: str, rain_mm_h: float) -> str:
    """Map an OWM 'weather.main' + rain rate to our four states."""
    m = (main or "").lower()
    if "thunder" in m or "squall" in m or "tornado" in m:
        return "storm"
    if "rain" in m or "drizzle" in m:
        return "storm" if (rain_mm_h or 0.0) > 7.5 else "rain"   # heavy rain -> storm
    if "cloud" in m:
        return "cloudy"
    return "clear"               # Clear / mist / haze / etc.


def _fetch_one(lat: float, lon: float, api_key: str, timeout: float) -> str:
    params = urllib.parse.urlencode({"lat": lat, "lon": lon, "appid": api_key})
    with urllib.request.urlopen(f"{OWM_URL}?{params}", timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    main = (data.get("weather") or [{}])[0].get("main", "")
    rain = (data.get("rain") or {}).get("1h", 0.0)
    return _map_condition(main, rain)


def fetch_owm_weather(stations, api_key: str | None = None,
                      timeout: float = 8.0) -> WeatherModel:
    """Return a WeatherModel with each station's current condition. Cached per
    (station set, key) for _CACHE_TTL_S seconds. Missing key / errors -> 'clear'."""
    api_key = api_key or os.environ.get("OWM_API_KEY")
    if not api_key:
        raise ValueError("OpenWeatherMap API key required (api_key= or OWM_API_KEY env var)")

    key = (tuple((g.id, round(g.lat_deg, 3), round(g.lon_deg, 3)) for g in stations), api_key)
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL_S:
        return hit[1]

    states = {}
    for g in stations:
        try:
            states[g.id] = _fetch_one(g.lat_deg, g.lon_deg, api_key, timeout)
        except Exception:
            states[g.id] = "clear"           # graceful fallback per station
    model = WeatherModel(state_by_station=states)
    _CACHE[key] = (time.time(), model)
    return model
