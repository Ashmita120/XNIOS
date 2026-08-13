"""Weather model -> rain fade (dB) per station over time.

v0 keeps it deliberately simple and controllable: each station has a weather state
that maps to a zenith rain-attenuation value. Default is CLEAR everywhere (0 dB) so
validation is deterministic. Phase 9 will replace `fade_db` with a stochastic
(Markov/ERA5-driven) time series without touching the simulator.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# zenith rain attenuation (dB) by weather state. light_rain/heavy_rain/extreme extend the
# original 4-state table with finer severity granularity for weather-sensitivity sweeps;
# the original 4 keys/values are unchanged so existing scenarios/configs are unaffected.
FADE_DB = {
    "clear": 0.0,
    "cloudy": 0.5,
    "light_rain": 2.0,
    "rain": 3.0,
    "heavy_rain": 6.0,
    "storm": 8.0,
    "extreme": 15.0,
}


@dataclass
class WeatherModel:
    """Maps (station_id) -> weather state. Static (constant over the run)."""

    state_by_station: dict[str, str] = field(default_factory=dict)
    default_state: str = "clear"

    def state(self, station_id: str, t: float = 0.0) -> str:
        return self.state_by_station.get(station_id, self.default_state)

    def fade_db(self, station_id: str, t: float = 0.0) -> float:
        return FADE_DB.get(self.state(station_id, t), 0.0)


# weather evolves as a Markov chain over these states (rows = from, cols = to)
_STATES = ["clear", "cloudy", "rain", "storm"]
_TRANSITION = {
    "clear":  [0.80, 0.18, 0.02, 0.00],
    "cloudy": [0.30, 0.55, 0.13, 0.02],
    "rain":   [0.05, 0.35, 0.50, 0.10],
    "storm":  [0.00, 0.20, 0.40, 0.40],
}


class DynamicWeatherModel:
    """Weather that CHANGES during the run: each station walks a Markov chain
    (clear <-> cloudy <-> rain <-> storm) so link quality varies mid-pass. A per-
    station timeline is precomputed for reproducibility; `state(id, t)` looks it up."""

    def __init__(self, stations, seed: int = 0, dwell_s: float = 300.0,
                 horizon_s: float = 21600.0, init_state: str = "clear"):
        self.dwell_s = dwell_s
        n_blocks = max(1, int(horizon_s / dwell_s) + 1)
        self.walk = {}
        for g in stations:
            rng = random.Random(f"{seed}-{g.id}")
            cur = getattr(g, "_init_weather", init_state)
            seq = [cur]
            for _ in range(n_blocks):
                cur = rng.choices(_STATES, weights=_TRANSITION[cur])[0]
                seq.append(cur)
            self.walk[g.id] = seq

    def state(self, station_id: str, t: float = 0.0) -> str:
        seq = self.walk.get(station_id)
        if not seq:
            return "clear"
        idx = min(int(max(t, 0.0) / self.dwell_s), len(seq) - 1)
        return seq[idx]

    def fade_db(self, station_id: str, t: float = 0.0) -> float:
        return FADE_DB.get(self.state(station_id, t), 0.0)
