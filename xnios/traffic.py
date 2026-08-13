"""Traffic arrival processes — where new data comes from.

V2 workstream A. Until now the twin had no arrival process at all: every
satellite was handed `backlog_bits` at t=0 and drained it, so future demand was
not uncertain, it was *known and monotonically decreasing*. The Stage 2
feasibility study (§15.10) could therefore find no headroom in demand, queue or
congestion prediction — there was nothing there to predict.

---------------------------------------------------------------------------
The lesson Stage 2 taught, applied here
---------------------------------------------------------------------------
Station failures are a memoryless Poisson process, which is exactly why failure
prediction is unlearnable: nothing observable precedes an event whose hazard is
constant. **Variance without memory is noise, not signal.**

A Poisson arrival process has the same defect. It would make the queue jump
around — creating variance, and the illusion of a harder problem — while leaving
the future just as unpredictable as before. Re-running the gate would kill demand
prediction a second time, for the same reason.

So the models here are graded by *memory*, not by variance:

    PoissonArrivals       memoryless        the CONTROL — expected to show no headroom
    BurstyArrivals        Markov on/off     state persists, so recent history informs
                                            the near future
    DiurnalArrivals       time-of-day       a deterministic envelope over either

`BurstyArrivals` is the one that should make demand learnable. Keeping the
memoryless control in the same experiment is what turns "we added traffic and got
headroom" into "we added *structured* traffic and got headroom, while unstructured
traffic gave none" — which is a claim about the process, not about the model.

---------------------------------------------------------------------------
Opting in
---------------------------------------------------------------------------
Default is `NoArrivals`, which returns 0.0 bits every step, so a scenario without
a `traffic` block behaves **bit-identically to V1**. Enable per scenario:

    "traffic": {
        "model": "bursty",              # none | poisson | bursty
        "mean_gbit_per_hour": 40,       # long-run average per satellite
        "burst_ratio": 6.0,             # ON-state rate vs the long-run mean
        "on_dwell_s": 240, "off_dwell_s": 600,
        "diurnal_amplitude": 0.0,       # 0 = off; 0.4 = +/-40% over a day
        "seed": 0
    }

The burst state is deliberately **latent**: it is never written to telemetry, so
a model must infer it from the arrival history rather than read the answer — the
same discipline §15.5B requires of station health.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

__all__ = ["NoArrivals", "PoissonArrivals", "BurstyArrivals", "make_traffic"]


class NoArrivals:
    """V1 behaviour: buffers are filled once at t=0 and only ever drain."""

    kind = "none"
    stateful = False

    def arrivals(self, sat_id: str, t: float, dt_s: float) -> float:
        return 0.0

    def state(self, sat_id: str) -> str:
        return "n/a"


def _diurnal(t: float, amplitude: float, period_s: float = 86400.0,
             phase_s: float = 0.0) -> float:
    """Smooth time-of-day multiplier in [1-amplitude, 1+amplitude]."""
    if amplitude <= 0.0:
        return 1.0
    return 1.0 + amplitude * math.sin(2.0 * math.pi * (t + phase_s) / period_s)


@dataclass
class PoissonArrivals:
    """Memoryless arrivals — the experimental control.

    Data lands as a Poisson process of fixed-size chunks. The count in each step
    is independent of every other step, so the *only* thing that predicts future
    demand is the long-run rate. A model should not beat that, and the Stage 2
    gate should say so.
    """

    sat_ids: list
    mean_bps: dict                              # sat_id -> long-run bits/s
    chunk_bits: float = 0.5e9
    diurnal_amplitude: float = 0.0
    seed: int = 0

    kind = "poisson"
    stateful = False

    def __post_init__(self):
        self._rng = {sid: random.Random(f"{self.seed}-poisson-{sid}") for sid in self.sat_ids}

    def arrivals(self, sat_id: str, t: float, dt_s: float) -> float:
        rate = self.mean_bps.get(sat_id, 0.0) * _diurnal(t, self.diurnal_amplitude)
        if rate <= 0:
            return 0.0
        lam = rate * dt_s / self.chunk_bits     # expected chunks this step
        rng = self._rng[sat_id]
        # Knuth's method; lam is small here (well under 1 for realistic rates)
        k, p, target = 0, 1.0, math.exp(-lam)
        while p > target and k < 1000:
            p *= rng.random()
            k += 1
        return max(0, k - 1) * self.chunk_bits

    def state(self, sat_id: str) -> str:
        return "memoryless"


@dataclass
class BurstyArrivals:
    """Markov-modulated arrivals — ON/OFF bursts with dwell.

    Each satellite walks a two-state chain. In ON it accrues data at
    `burst_ratio` times its long-run mean; in OFF it accrues almost nothing.
    Because the state *persists* for a dwell time, the recent arrival history
    carries information about the near future — which is precisely what makes
    demand a legitimate prediction target rather than noise.

    This is a realistic shape as well as a convenient one: an imaging satellite
    accrues data in concentrated passes over targets, not uniformly around the
    orbit.
    """

    sat_ids: list
    mean_bps: dict
    burst_ratio: float = 6.0
    on_dwell_s: float = 240.0
    off_dwell_s: float = 600.0
    chunk_bits: float = 0.25e9
    diurnal_amplitude: float = 0.0
    seed: int = 0

    kind = "bursty"
    stateful = True

    _on: dict = field(default_factory=dict, init=False)
    _rng: dict = field(default_factory=dict, init=False)

    def __post_init__(self):
        duty = self.on_dwell_s / max(1e-9, self.on_dwell_s + self.off_dwell_s)
        # `burst_ratio` is the ON:OFF *ratio*; normalise both scales so the
        # long-run mean is exactly `mean_bps` for any ratio >= 1. Solving for an
        # OFF rate of zero instead (on = burst_ratio, off = 0) only has a
        # solution when burst_ratio <= 1/duty, and silently clamping the negative
        # result to zero is what made the measured rate 129% too high.
        raw_on = max(1.0, float(self.burst_ratio))
        raw_off = 1.0
        mean_mult = duty * raw_on + (1.0 - duty) * raw_off
        self._on_scale = raw_on / mean_mult
        self._off_scale = raw_off / mean_mult
        for sid in self.sat_ids:
            rng = random.Random(f"{self.seed}-bursty-{sid}")
            self._rng[sid] = rng
            self._on[sid] = rng.random() < duty        # start in steady state

    def _step_state(self, sat_id: str, dt_s: float) -> bool:
        rng = self._rng[sat_id]
        on = self._on[sat_id]
        dwell = self.on_dwell_s if on else self.off_dwell_s
        if rng.random() < dt_s / max(dt_s, dwell):     # exponential dwell, discretised
            on = not on
            self._on[sat_id] = on
        return on

    def arrivals(self, sat_id: str, t: float, dt_s: float) -> float:
        on = self._step_state(sat_id, dt_s)
        base = self.mean_bps.get(sat_id, 0.0) * _diurnal(t, self.diurnal_amplitude)
        rate = base * (self._on_scale if on else self._off_scale)
        if rate <= 0:
            return 0.0
        lam = rate * dt_s / self.chunk_bits
        rng = self._rng[sat_id]
        k, p, target = 0, 1.0, math.exp(-lam)
        while p > target and k < 1000:
            p *= rng.random()
            k += 1
        return max(0, k - 1) * self.chunk_bits

    def state(self, sat_id: str) -> str:
        """The latent burst state. Diagnostics only — never written to telemetry,
        or a model would read the answer instead of inferring it."""
        return "on" if self._on.get(sat_id) else "off"


def make_traffic(cfg: dict | None, satellites):
    """Build a traffic model from a scenario's `traffic` block. None -> NoArrivals."""
    if not cfg:
        return NoArrivals()
    kind = str(cfg.get("model", "none")).lower()
    if kind in ("none", "off", ""):
        return NoArrivals()

    ids = [s.id for s in satellites]
    per_hour = float(cfg.get("mean_gbit_per_hour", 0.0)) * 1e9
    mean_bps = {s.id: per_hour / 3600.0 for s in satellites}

    # optional per-tier scaling, so priority classes differ in load as well as
    # in scheduling weight
    tier_scale = cfg.get("tier_scale") or {}
    for s in satellites:
        mean_bps[s.id] *= float(tier_scale.get(getattr(s, "tier", ""), 1.0))

    common = dict(sat_ids=ids, mean_bps=mean_bps,
                  diurnal_amplitude=float(cfg.get("diurnal_amplitude", 0.0)),
                  seed=int(cfg.get("seed", 0)))
    if kind == "poisson":
        return PoissonArrivals(chunk_bits=float(cfg.get("chunk_gbit", 0.5)) * 1e9, **common)
    if kind in ("bursty", "markov", "onoff"):
        return BurstyArrivals(
            burst_ratio=float(cfg.get("burst_ratio", 6.0)),
            on_dwell_s=float(cfg.get("on_dwell_s", 240.0)),
            off_dwell_s=float(cfg.get("off_dwell_s", 600.0)),
            chunk_bits=float(cfg.get("chunk_gbit", 0.25)) * 1e9, **common)
    raise ValueError(f"unknown traffic model {kind!r}")
