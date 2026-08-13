"""Latent station health — degradation with observable precursors.

V2 workstream B. The existing failure process (`dynamics.failure_events`) is
memoryless Poisson, which is precisely why §15.10 could not learn it: a constant
hazard has nothing preceding it, so no telemetry can anticipate an outage. That
is a property of the process, not a shortcoming of any model.

This module replaces the constant hazard with a causal chain:

    latent health h(t) in [0,1]        <- HIDDEN, never written to telemetry
        |
        +--> G/T penalty (dB)          -- PA efficiency loss, calibration drift
        +--> SNR jitter (dB)           -- an unstable front end is a noisy one
        |
        v
    failure hazard = f(h)              -- outage becomes likely as health falls

Only the middle layer reaches the operator. A station whose measured SNR sits a
decibel below what the link budget says it should, and wobbles more than it used
to, is degrading — and that is inferable from telemetry alone.

---------------------------------------------------------------------------
Why this is observable at all: Stage 1 is the instrument
---------------------------------------------------------------------------
`forecast.snr_db_at` reproduces the simulator's link budget exactly (§15.10
measured the residual at **0.00 dB** once the power allocator was accounted for).
The forecaster does **not** know about degradation. So

    residual = measured SNR - analytical SNR

is zero for a healthy station and grows as one degrades. The precursor is a
*residual against physics*, which is why Stage 1 had to exist first, and why this
transfers to real hardware: the same residual is computable from a real G/T and a
real ephemeris.

---------------------------------------------------------------------------
Discipline
---------------------------------------------------------------------------
* **The latent state is hidden.** `health()` exists for diagnostics and for
  validating the process — never for the feature layer. Exposing it would let a
  model read the answer instead of inferring it, which is the same mistake as
  labelling buffer exhaustion "link loss".
* **Opt-in.** No `degradation` block means no degradation, a zero penalty, and a
  bit-identical V1 run.
* **Precomputed.** The whole trajectory is drawn at construction from a seed, so
  runs are reproducible and the simulator only ever looks values up — the same
  contract as `DynamicWeatherModel` and `NetworkDynamics`.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .dynamics import Event

__all__ = ["StationDegradation", "make_degradation", "HOUSEKEEPING"]

#: station-local instrument channels, as recorded on `telemetry.StationRecord`
HOUSEKEEPING = ("pa_current_a", "temp_c", "vswr", "cal_residual_db", "noise_figure_db")


@dataclass
class StationDegradation:
    """Per-station latent health, its symptoms, and the outages it causes.

    Health follows a slow downward drift with occasional step shocks (a bias
    supply drooping, a calibration cycle missed), and recovers to full after a
    repair. An outage is triggered when health falls through `fail_below`, so an
    outage is always *preceded* by an observable decline — the opposite of the
    Poisson process it replaces.
    """

    station_ids: list
    duration_s: float
    dt_s: float = 10.0
    seed: int = 0

    drift_per_hour: float = 0.55          # mean health lost per hour of operation
    shock_mtbf_s: float = 1200.0          # step degradations (bias droop, missed cal)
    shock_size: float = 0.18
    fail_below: float = 0.25              # health at which the station drops out
    mttr_s: float = 240.0                 # repair time; health returns to 1.0

    gt_penalty_max_db: float = 3.0        # G/T loss at health 0
    jitter_max_db: float = 1.2            # SNR standard deviation at health 0

    def __post_init__(self):
        self._n = max(2, int(math.ceil(self.duration_s / self.dt_s)) + 2)
        self._h = {}                       # station -> [health per step]
        self._events: list[Event] = []
        for gid in self.station_ids:
            self._h[gid] = self._trajectory(gid)
        self._jrng = {gid: random.Random(f"{self.seed}-jit-{gid}") for gid in self.station_ids}
        # jitter is drawn per (station, step) up front so a lookup is pure
        self._j = {gid: [self._jrng[gid].gauss(0.0, 1.0) for _ in range(self._n)]
                   for gid in self.station_ids}

    # ------------------------------------------------------------------ latent

    def _trajectory(self, gid: str) -> list:
        rng = random.Random(f"{self.seed}-deg-{gid}")
        drift = self.drift_per_hour * self.dt_s / 3600.0
        p_shock = self.dt_s / max(self.dt_s, self.shock_mtbf_s)
        h = 1.0
        repair_until = -1.0
        out = []
        for i in range(self._n):
            t = i * self.dt_s
            if t < repair_until:
                out.append(0.0)            # out of service, being repaired
                continue
            if repair_until > 0 and t >= repair_until:
                h = 1.0                    # returned to service, freshly serviced
                repair_until = -1.0
            h -= drift * rng.uniform(0.5, 1.5)
            if rng.random() < p_shock:
                h -= self.shock_size * rng.uniform(0.5, 1.5)
            h = max(0.0, min(1.0, h))
            if h <= self.fail_below:
                self._events.append(Event(t, gid, "station_fail"))
                self._events.append(Event(min(self.duration_s, t + self.mttr_s),
                                          gid, "station_recover"))
                repair_until = t + self.mttr_s
                h = 0.0
            out.append(h)
        return out

    def _idx(self, t: float) -> int:
        return max(0, min(self._n - 1, int(t / self.dt_s)))

    def health(self, gid: str, t: float) -> float:
        """The latent state. **Diagnostics and validation only** — never a feature."""
        return self._h[gid][self._idx(t)]

    # -------------------------------------------------------------- observable

    def penalty_db(self, gid: str, t: float) -> float:
        """G/T loss visible as a shortfall against the analytical link budget."""
        h = self.health(gid, t)
        return self.gt_penalty_max_db * (1.0 - h) ** 2

    def jitter_db(self, gid: str, t: float) -> float:
        """Extra SNR noise. A degrading front end is an unstable one, so the
        *variance* of the residual carries information as well as its mean."""
        h = self.health(gid, t)
        return self.jitter_max_db * (1.0 - h) * self._j[gid][self._idx(t)]

    def loss_db(self, gid: str, t: float) -> float:
        """Total degradation the link budget should see at this instant."""
        return self.penalty_db(gid, t) + self.jitter_db(gid, t)

    # --------------------------------------------- station-local instrumentation

    def housekeeping(self, gid: str, t: float) -> dict:
        """Continuously-measurable station telemetry, derived from latent health.

        §15.12 found the binding constraint: health reached the operator *only*
        through the link budget, so it was observable 2.3 % of the time — a LEO
        pass is ~5 minutes out of ~96 — and outages arrived with hour-old data.
        That is a modelling gap, not an ML one. A real ground station reports its
        own condition every second whether or not a satellite is overhead.

        Each channel is a *noisy, partial* view of the same latent state, with its
        own scale and its own noise, so the model must combine several imperfect
        instruments rather than read one clean proxy:

            pa_current_a      draw rises as PA efficiency falls
            temp_c            dissipation rises with inefficiency
            vswr              match degrades non-linearly with wear
            cal_residual_db   calibration drifts between service cycles
            noise_figure_db   receiver front end gets noisier

        The latent health scalar itself is never returned here — inferring it is
        the model's job, and handing it over would be the same mistake as
        labelling buffer exhaustion "link loss".
        """
        h = self.health(gid, t)
        d = 1.0 - h                                    # 0 healthy .. 1 failed
        rng = random.Random(f"{self.seed}-hk-{gid}-{self._idx(t)}")
        return {
            "pa_current_a": 4.0 * (1.0 + 0.55 * d) + rng.gauss(0.0, 0.10),
            "temp_c": 32.0 + 26.0 * d + rng.gauss(0.0, 1.2),
            "vswr": 1.10 + 0.85 * d * d + rng.gauss(0.0, 0.030),
            "cal_residual_db": 0.05 + 1.60 * d + rng.gauss(0.0, 0.12),
            "noise_figure_db": 1.40 + 1.10 * d + rng.gauss(0.0, 0.09),
        }

    # ------------------------------------------------------------------ events

    def failure_events(self) -> list:
        """Outages, in the same `Event` form `NetworkDynamics` already consumes —
        so degradation-driven failures flow through the existing plumbing."""
        return list(self._events)


def make_degradation(cfg: dict | None, stations, duration_s: float, dt_s: float,
                     seed: int = 0):
    """Build from a scenario's `degradation` block. None -> no degradation."""
    if not cfg:
        return None
    ids = [g.id for g in stations]
    return StationDegradation(
        station_ids=ids, duration_s=duration_s, dt_s=dt_s,
        seed=int(cfg.get("seed", seed)),
        drift_per_hour=float(cfg.get("drift_per_hour", 0.55)),
        shock_mtbf_s=float(cfg.get("shock_mtbf_s", 1200.0)),
        shock_size=float(cfg.get("shock_size", 0.18)),
        fail_below=float(cfg.get("fail_below", 0.25)),
        mttr_s=float(cfg.get("mttr_s", 240.0)),
        gt_penalty_max_db=float(cfg.get("gt_penalty_max_db", 3.0)),
        jitter_max_db=float(cfg.get("jitter_max_db", 1.2)))
