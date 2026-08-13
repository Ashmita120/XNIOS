"""Per-step telemetry — the raw time series everything above the twin is built on.

V1 answered "what did this run deliver?" (a KPI vector at the end). Nothing above
it can be built from that: an AI cannot learn from `throughput = 810 Gb`, a
dashboard cannot draw it, and a controller cannot act on it. What is needed is
*exactly what happened at every step* — and that is what this module records.

    Simulator step  ->  TelemetryRecord(t)  ->  sink
                             |
              +--------------+--------------+-------------+
              |              |              |             |
        Health monitor   Feature layer  Historical    Dashboard
                              |          memory
                         Forecast / models / controller

One `TelemetryRecord` per captured step, with five faces of the same instant:

    network      one row: the whole constellation at time t
    stations     one row per ground station
    links        one row per link (active session *and* merely-visible candidate)
    satellites   one row per satellite
    decision     which algorithms were in force, what they chose, and why
    events       discrete things that happened this step (start/end/fail/handover)

Design rules this module obeys:

* **Opt-in, zero behaviour change.** `Simulator(..., telemetry=None)` is the
  default and costs nothing (one `is not None` check per step). Recording never
  feeds back into the simulation, so V1 results and the 13/13 validation are
  bit-identical with telemetry on or off.
* **Observation, not judgement.** Records hold measured quantities only. No
  composite "health score" lives here — scalarisation belongs to the monitor
  layer (`xnios/health.py`), never to the twin. This is the same rule
  `metrics.py` follows by keeping KPIs a vector.
* **Streaming-capable.** A long run is written through a `TelemetrySink` as it
  goes (JSONL, optionally gzipped) so a multi-hour campaign never has to fit in
  RAM, and an interrupted run still leaves usable data on disk.
* **Self-describing.** Every record carries `SCHEMA_VERSION` and every run
  carries its full config in `RunMeta`, because this data will be regenerated
  many times as the twin evolves and old datasets must stay interpretable.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict

from . import orbit as orb
from .link import ber_from_sinr, snr_linear

# Bump when a record's fields change meaning. Datasets record it, so a stale
# feature table can always be detected instead of silently mis-read.
SCHEMA_VERSION = "1.0"

# what a recorder may capture; trim for cheaper/smaller runs
ALL_FACES = ("network", "stations", "links", "satellites", "decision", "events")


def _db(x: float) -> float:
    """Linear ratio -> dB, with a floor so a zero-power link logs as -300 dB
    instead of raising. Telemetry must never be able to crash a run."""
    return 10.0 * math.log10(x) if x > 1e-30 else -300.0


# --------------------------------------------------------------------------- #
# Records                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class LinkRecord:
    """One satellite<->station RF link at time t.

    Recorded for every *visible* pair, not just active sessions: the candidates
    a scheduler saw and rejected are exactly what a learned policy needs in
    order to learn a different choice. `active` separates the two.
    """

    sat_id: str
    station_id: str
    active: bool                  # currently carrying a session
    beam: int | None = None       # beam index serving it (active links only)
    channel: int | None = None    # frequency/pol slot from the freq allocator

    # geometry
    elev_deg: float = 0.0
    az_deg: float = 0.0
    range_km: float = 0.0
    scan_deg: float = 0.0         # off-boresight angle for a phased array (90 - elev)

    # RF
    snr_db: float = 0.0           # interference-free
    sinr_db: float = 0.0          # after co-channel interference C/(N+I)
    inr_db: float = -300.0        # interference-to-noise actually suffered
    ber: float = 0.5              # derived indicator (see link.ber_from_sinr)
    rain_fade_db: float = 0.0

    # allocation
    alloc_bw_hz: float = 0.0
    alloc_power_w: float = 0.0

    # throughput
    rate_bps: float = 0.0         # achieved this step (post-interference, post-alloc)
    clean_rate_bps: float = 0.0   # interference-free reference rate
    bits_delivered: float = 0.0   # actually moved this step

    # session
    slewing: bool = False         # beam still acquiring (setup_time_s not elapsed)
    session_age_s: float = 0.0

    #: seconds until this specific pair stops being usable (-1 = not usable now).
    #: Exact, and what proactive handover should trigger on.
    time_to_los_s: float = -1.0


@dataclass
class StationRecord:
    """One ground station at time t."""

    station_id: str
    lat_deg: float
    lon_deg: float

    up: bool = True                   # operational (False during an outage)
    beams_total: int = 1              # nameplate beams
    beams_available: int = 1          # usable now (beam failures reduce this)
    beams_active: int = 0             # carrying a session
    beam_utilization: float = 0.0     # beams_active / beams_available

    bandwidth_base_hz: float = 0.0    # nameplate pool (for degradation ratios)
    bandwidth_pool_hz: float = 0.0    # usable pool now (dynamic capacity)
    bandwidth_alloc_hz: float = 0.0   # handed out to links this step
    bandwidth_utilization: float = 0.0

    link_power_w: float = 0.0         # total radiated power on links into this station
    rate_bps: float = 0.0             # aggregate achieved rate
    bits_delivered: float = 0.0       # this step

    weather: str = "clear"
    rain_fade_db: float = 0.0

    connected_sats: list = field(default_factory=list)
    visible_sats: int = 0             # sats above the mask with a usable link
    mean_sinr_db: float = 0.0         # over active links (0 if idle)

    phased_array: bool = False
    n_channels: int = 1
    channels_in_use: int = 0

    # Availability/quality state, NOT a prediction. `health.py` turns this and
    # the rest of the record into an operator-facing score.
    degraded: bool = False        # up, but short of nameplate capacity

    # --- station-local housekeeping (V2 workstream B) --------------------------
    # Measured continuously by the station itself, independent of whether any
    # satellite is visible. Zero unless a `degradation` block is configured.
    # These are noisy *instruments*, not the latent health state: see
    # `degradation.StationDegradation.housekeeping`.
    pa_current_a: float = 0.0
    temp_c: float = 0.0
    vswr: float = 0.0
    cal_residual_db: float = 0.0
    noise_figure_db: float = 0.0


@dataclass
class SatelliteRecord:
    """One satellite at time t (including its ground track, for the map)."""

    sat_id: str
    lat_deg: float = 0.0
    lon_deg: float = 0.0
    alt_km: float = 0.0

    state: str = "idle"               # idle|waiting|slewing|transmitting|done
    backlog_bits: float = 0.0
    backlog0_bits: float = 0.0        # initial demand (for completion fraction)
    delivered_bits: float = 0.0       # cumulative
    bits_delivered_step: float = 0.0

    wait_s: float = 0.0               # cumulative ready-but-unserved time
    ready_since: float | None = None
    priority: int = 2
    tier: str = ""
    deadline_s: float | None = None
    time_to_deadline_s: float | None = None

    visible_stations: list = field(default_factory=list)
    n_visible: int = 0
    current_station: str | None = None
    current_beam: int | None = None

    rate_bps: float = 0.0             # achieved now
    best_visible_rate_bps: float = 0.0  # best link it *could* have had

    # --- analytical forecast (V2 Stage 1) --------------------------------------
    # Closed-form orbital mechanics from `xnios.forecast`, not a prediction: exact
    # to float precision and validated against the simulator (8/8, 0 disagreements
    # in 28k visibility samples). -1 means "no contact inside the search horizon".
    next_contact_s: float = -1.0        # seconds until the next usable contact
    next_contact_station: str = ""      # which station it will be
    contact_window_s: float = -1.0      # how long that contact lasts
    time_to_los_s: float = -1.0         # if in contact now, seconds until it ends


@dataclass
class NetworkRecord:
    """The whole network at time t — one row, the dashboard's headline feed."""

    t: float = 0.0

    # throughput
    bits_delivered_step: float = 0.0
    bits_delivered_total: float = 0.0
    throughput_bps: float = 0.0       # instantaneous (step bits / dt)

    # demand & progress
    queue_bits: float = 0.0           # total remaining backlog
    demand_bits: float = 0.0          # total initial demand
    completion_rate: float = 0.0      # sats fully drained / sats with demand
    delivery_fraction: float = 0.0    # bits delivered / bits demanded
    n_sats: int = 0
    n_completed: int = 0
    n_backlogged: int = 0
    n_waiting: int = 0                # ready, has data, not being served

    # capacity
    stations_total: int = 0
    stations_up: int = 0
    beams_total: int = 0
    beams_available: int = 0
    beams_active: int = 0
    beam_utilization: float = 0.0
    bandwidth_pool_hz: float = 0.0
    bandwidth_alloc_hz: float = 0.0
    bandwidth_utilization: float = 0.0

    # contention: how much demand is chasing how much capacity right now
    contention_ratio: float = 0.0     # sats wanting a beam / beams available

    # coverage & link quality
    n_visible_pairs: int = 0
    n_sats_with_link: int = 0
    coverage: float = 0.0             # backlogged sats with >=1 usable link
    mean_elev_deg: float = 0.0
    mean_sinr_db: float = 0.0
    min_sinr_db: float = 0.0

    # energy
    energy_j_step: float = 0.0
    energy_j_total: float = 0.0
    power_w: float = 0.0

    # weather
    weather_counts: dict = field(default_factory=dict)   # {state: n_stations}
    mean_rain_fade_db: float = 0.0
    max_rain_fade_db: float = 0.0

    # sessions (cumulative counters, so deltas are derivable)
    sessions_active: int = 0
    sessions_started_total: int = 0
    interruptions_total: int = 0
    handovers_total: int = 0
    proactive_handovers_total: int = 0

    mean_wait_s: float = 0.0
    decision_ms: float = 0.0          # last scheduler call


@dataclass
class DecisionRecord:
    """Which algorithms were in force at time t, what they chose, and why.

    The `rationale` / `reasons` / `source` fields are empty under V1's static
    configuration. They exist now so the AI decision engine writes into a slot
    that already exists in every historical row — the explainability panel then
    needs no schema migration, and old runs stay comparable to new ones.
    """

    scheduler: str = ""
    bandwidth_allocator: str = ""
    power_allocator: str = ""
    freq_allocator: str = ""

    decision_ms: float = 0.0
    assignments: list = field(default_factory=list)   # [{sat_id, station_id, beam}]
    n_assigned: int = 0
    n_free_candidates: int = 0        # free sats with data the scheduler could place
    n_unserved: int = 0               # candidates it left unplaced

    source: str = "static"            # static | ai | manual
    rationale: str | None = None      # why THIS configuration (set by a controller)
    reasons: dict = field(default_factory=dict)       # sat_id -> why this assignment
    expected: dict = field(default_factory=dict)      # predicted effect, for XAI


@dataclass
class EventRecord:
    """A discrete thing that happened. Cheap to store, and the label source for
    anything later trained to anticipate them."""

    t: float
    kind: str                          # session_start|session_end|complete|interrupt|
                                       # recover|handover|station_fail|station_recover|
                                       # beam_fail|beam_recover|weather_change|deadline_miss
    sat_id: str | None = None
    station_id: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class TelemetryRecord:
    """Everything observable at one instant."""

    t: float
    step: int
    schema_version: str = SCHEMA_VERSION
    network: NetworkRecord | None = None
    stations: list = field(default_factory=list)      # list[StationRecord]
    links: list = field(default_factory=list)         # list[LinkRecord]
    satellites: list = field(default_factory=list)    # list[SatelliteRecord]
    decision: DecisionRecord | None = None
    events: list = field(default_factory=list)        # list[EventRecord]

    def to_dict(self) -> dict:
        return {
            "t": self.t,
            "step": self.step,
            "schema_version": self.schema_version,
            "network": asdict(self.network) if self.network else None,
            "stations": [asdict(s) for s in self.stations],
            "links": [asdict(l) for l in self.links],
            "satellites": [asdict(s) for s in self.satellites],
            "decision": asdict(self.decision) if self.decision else None,
            "events": [asdict(e) for e in self.events],
        }


@dataclass
class RunMeta:
    """Everything needed to reproduce and index a run. Written once per run —
    this is the primary key of the Historical Memory layer."""

    run_id: str
    schema_version: str = SCHEMA_VERSION
    scenario: str = ""
    seed: int | None = None
    started_unix: float = 0.0
    duration_s: float = 0.0
    dt_s: float = 0.0
    decision_interval_s: float = 0.0
    n_satellites: int = 0
    n_stations: int = 0
    n_beams_total: int = 0
    scheduler: str = ""
    bandwidth_allocator: str = ""
    power_allocator: str = ""
    freq_allocator: str = ""
    handover: bool = False
    weather_model: str = ""
    dynamics: bool = False
    config: dict = field(default_factory=dict)        # the source config dict, verbatim
    stations: list = field(default_factory=list)      # [{id, lat, lon, beams, ...}] for maps


# --------------------------------------------------------------------------- #
# Sinks                                                                        #
# --------------------------------------------------------------------------- #

class TelemetrySink(ABC):
    """Where records go. Keeping this abstract is what lets the same recorder
    feed RAM (a UI run), disk (a training campaign), or both."""

    def begin(self, meta: RunMeta) -> None:
        pass

    @abstractmethod
    def write(self, record: TelemetryRecord) -> None:
        ...

    def end(self, summary: dict | None = None) -> None:
        pass


class MemorySink(TelemetrySink):
    """Keep records in a list. Default: fine for a single run, and what the UI
    and the health monitor read. `max_records` guards a runaway long run."""

    def __init__(self, max_records: int | None = None):
        self.meta: RunMeta | None = None
        self.records: list = []
        self.summary: dict | None = None
        self.max_records = max_records

    def begin(self, meta: RunMeta) -> None:
        self.meta = meta
        self.records = []

    def write(self, record: TelemetryRecord) -> None:
        self.records.append(record)
        if self.max_records is not None and len(self.records) > self.max_records:
            del self.records[0]

    def end(self, summary: dict | None = None) -> None:
        self.summary = summary


class JsonlSink(TelemetrySink):
    """Stream one JSON object per line to disk (`.jsonl`, or `.jsonl.gz` when
    `compress`). Flushed per record, so an interrupted campaign still leaves a
    readable, partial dataset — the same lesson `bench_common.CsvWriter` applies
    to the benchmark sweep.

    Layout: `<dir>/<run_id>.meta.json` + `<dir>/<run_id>.jsonl[.gz]`.
    """

    def __init__(self, path: str, compress: bool = False):
        self.path = path + (".gz" if compress and not path.endswith(".gz") else "")
        self.compress = compress or self.path.endswith(".gz")
        self._f = None

    def begin(self, meta: RunMeta) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        self._f = (gzip.open(self.path, "wt", encoding="utf-8") if self.compress
                   else open(self.path, "w", encoding="utf-8"))
        base = self.path[:-3] if self.path.endswith(".gz") else self.path
        meta_path = base.rsplit(".jsonl", 1)[0] + ".meta.json"
        with open(meta_path, "w", encoding="utf-8") as mf:
            json.dump(asdict(meta), mf, indent=2, default=str)

    def write(self, record: TelemetryRecord) -> None:
        if self._f is None:
            return
        self._f.write(json.dumps(record.to_dict(), separators=(",", ":"), default=float))
        self._f.write("\n")
        self._f.flush()

    def end(self, summary: dict | None = None) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


class MultiSink(TelemetrySink):
    """Fan out to several sinks (e.g. keep in RAM for the UI *and* stream to disk)."""

    def __init__(self, *sinks):
        self.sinks = list(sinks)

    @property
    def records(self) -> list:
        """Delegate to the first sink that retains records, so a recorder wrapped
        in a MultiSink still answers `recorder.records`."""
        for s in self.sinks:
            r = getattr(s, "records", None)
            if r is not None:
                return r
        return []

    def begin(self, meta: RunMeta) -> None:
        for s in self.sinks:
            s.begin(meta)

    def write(self, record: TelemetryRecord) -> None:
        for s in self.sinks:
            s.write(record)

    def end(self, summary: dict | None = None) -> None:
        for s in self.sinks:
            s.end(summary)


class CallbackSink(TelemetrySink):
    """Push each record to a function — the hook a live dashboard/WebSocket uses."""

    def __init__(self, fn):
        self.fn = fn

    def write(self, record: TelemetryRecord) -> None:
        self.fn(record)


def read_jsonl(path: str):
    """Iterate raw record dicts back out of a JsonlSink file (plain or .gz)."""
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


# --------------------------------------------------------------------------- #
# Recorder                                                                     #
# --------------------------------------------------------------------------- #

class _ForecastCache:
    """Analytical contact windows, computed once per run and looked up per step.

    `forecast.contact_windows` costs ~0.5 ms per pair; recomputing it every step
    for every pair would dominate the run. The windows do not change — they are
    closed-form orbital mechanics — so one pass at `begin_run` is enough, and
    every later question is a list scan.

    The horizon deliberately runs past the end of the simulation so "next
    contact" can point at an opportunity the run itself never reaches: an
    operator needs to know the next pass is 82 minutes away even when the
    scenario stops in 10.

    24 hours, because LEO ground tracks precess ~24 deg west per orbit and do
    *not* revisit the same station next orbit. Measured on `india4-nominal`:
    SAT-002 gets 8 windows in 24 h with gaps of up to 728 minutes, and its second
    contact is 8.5 hours after its first. A 2-hour lookahead answers "no contact"
    almost always, which is worse than useless. Costs ~430 ms once per run.
    """

    LOOKAHEAD_S = 86400.0

    def __init__(self, sim):
        from . import forecast as fc
        self.win = {}
        horizon = sim.cfg.duration_s + self.LOOKAHEAD_S
        for s in sim.scn.satellites:
            for g in sim.scn.stations:
                rain = sim.scn.weather.fade_db(g.id, 0.0)
                self.win[(s.id, g.id)] = fc.contact_windows(
                    s, g, 0.0, horizon, step_s=10.0, rain_zenith_db=rain)

    def time_to_los(self, sid, gid, t) -> float:
        for w in self.win.get((sid, gid), ()):
            if w.t_rise <= t <= w.t_set:
                return w.t_set - t
        return -1.0

    def next_contact(self, sid, t):
        """(seconds until the next contact, station, its duration). In contact
        now -> 0.0 and the current window."""
        best = None
        for (s, gid), ws in self.win.items():
            if s != sid:
                continue
            for w in ws:
                if w.t_set < t:
                    continue
                wait = max(0.0, w.t_rise - t)
                if best is None or wait < best[0]:
                    best = (wait, gid, w.duration_s)
                break
        return best or (-1.0, "", -1.0)



class TelemetryRecorder:
    """Turns the simulator's per-step state into `TelemetryRecord`s.

    All derivation lives here rather than in `simulator.py`, so the engine gains
    only a handful of lines and stays the single source of physics. Pass one to
    `Simulator(..., telemetry=recorder)`:

        rec = TelemetryRecorder(sink=MemorySink())
        res = Simulator(scn, sched, cfg, telemetry=rec).run()
        rec.records[-1].network.beam_utilization

    `stride_s` / `every_n` thin the series out for long or large runs (capture
    cost is dominated by the per-link rows). `capture` drops whole faces you do
    not need, e.g. `capture=("network",)` for a cheap KPI time series.
    """

    def __init__(self, sink: TelemetrySink | None = None, run_id: str | None = None,
                 capture=ALL_FACES, stride_s: float | None = None, every_n: int = 1,
                 include_idle_links: bool = True, config: dict | None = None):
        self.sink = sink if sink is not None else MemorySink()
        self.run_id = run_id or f"run-{int(time.time() * 1000):x}"
        self.capture = set(capture)
        self.stride_s = stride_s
        self.every_n = max(1, int(every_n))
        self.include_idle_links = include_idle_links
        self.config = config or {}

        self.meta: RunMeta | None = None
        self._last_t: float | None = None
        self._step = 0
        self._pending_events: list = []
        self._pending_decision: DecisionRecord | None = None
        self._session_started_at: dict = {}
        self._prev_weather: dict = {}
        self._prev_dyn: dict = {}

    # -- accessors -----------------------------------------------------------
    @property
    def records(self) -> list:
        return getattr(self.sink, "records", [])

    def latest(self) -> TelemetryRecord | None:
        recs = self.records
        return recs[-1] if recs else None

    # -- lifecycle (called by the Simulator) ---------------------------------
    def begin_run(self, sim) -> None:
        scn, cfg = sim.scn, sim.cfg
        # analytical forecast, built once: exact and free to query thereafter
        self._fc = _ForecastCache(sim)
        self.meta = RunMeta(
            run_id=self.run_id,
            scenario=getattr(scn, "name", "scenario"),
            seed=self.config.get("seed"),
            started_unix=time.time(),
            duration_s=cfg.duration_s,
            dt_s=cfg.dt_s,
            decision_interval_s=cfg.decision_interval_s,
            n_satellites=len(sim.sats),
            n_stations=len(sim.stations),
            n_beams_total=sum(g.num_beams for g in sim.stations.values()),
            scheduler=_name_of(sim.scheduler),
            bandwidth_allocator=_name_of(sim.allocator),
            power_allocator=_name_of(sim.power_allocator),
            freq_allocator=_name_of(sim.freq_allocator),
            handover=cfg.handover,
            weather_model=type(scn.weather).__name__,
            dynamics=scn.dynamics is not None,
            config=self.config,
            stations=[{
                "id": g.id, "lat": g.lat_deg, "lon": g.lon_deg,
                "num_beams": g.num_beams, "g_over_t_dbk": g.g_over_t_dbk,
                "bandwidth_hz": g.bandwidth_hz, "phased_array": g.phased_array,
                "n_channels": g.n_channels, "dual_pol": g.dual_pol,
                "max_scan_deg": g.max_scan_deg, "elevation_mask_deg": g.elevation_mask_deg,
            } for g in sim.stations.values()],
        )
        self.sink.begin(self.meta)

    def end_run(self, results) -> None:
        self.sink.end(dict(getattr(results, "summary", {}) or {}))

    # -- things the simulator reports as they happen -------------------------
    def note_event(self, t: float, kind: str, sat_id: str | None = None,
                   station_id: str | None = None, **detail) -> None:
        if "events" in self.capture:
            self._pending_events.append(EventRecord(t, kind, sat_id, station_id, detail))

    def note_decision(self, sim, t: float, assignments, decision_ms: float,
                      n_free_candidates: int) -> None:
        """Called right after `scheduler.decide()`. `assignments` is what was
        *accepted* by the simulator (post-validation), so the record reflects
        what actually happened, not what was merely proposed."""
        if "decision" not in self.capture:
            return
        rows = [{"sat_id": a[0], "station_id": a[1], "beam": a[2]} for a in assignments]
        explain = getattr(sim.scheduler, "explain", None)
        self._pending_decision = DecisionRecord(
            scheduler=_name_of(sim.scheduler),
            bandwidth_allocator=_name_of(sim.allocator),
            power_allocator=_name_of(sim.power_allocator),
            freq_allocator=_name_of(sim.freq_allocator),
            decision_ms=decision_ms * 1000.0,
            assignments=rows,
            n_assigned=len(rows),
            n_free_candidates=n_free_candidates,
            n_unserved=max(0, n_free_candidates - len(rows)),
            source=getattr(sim.scheduler, "source", "static"),
            rationale=getattr(sim.scheduler, "rationale", None),
            reasons=(explain() if callable(explain) else {}) or {},
        )

    # -- the per-step capture ------------------------------------------------
    def capture_step(self, sim, t: float, ctx: dict) -> None:
        """Build and emit one record. `ctx` is the simulator's step state; see
        `Simulator.run`. Called at the very end of a step, after transfer, so
        every number is the settled value for that instant."""
        step = self._step
        self._step += 1

        # 1) discrete events derived from state changes (failures, weather)
        self._diff_events(sim, t, ctx)

        # 2) thinning — events/decisions still accumulate, so nothing is lost,
        #    they just attach to the next captured record
        if step % self.every_n != 0:
            return
        if self.stride_s is not None and self._last_t is not None \
                and (t - self._last_t) < self.stride_s - 1e-9:
            return
        self._last_t = t

        links = self._links(sim, t, ctx) if "links" in self.capture else []
        stations = self._stations(sim, t, ctx, links) if "stations" in self.capture else []
        sats = self._satellites(sim, t, ctx) if "satellites" in self.capture else []
        network = (self._network(sim, t, ctx, stations, links, sats)
                   if "network" in self.capture else None)

        rec = TelemetryRecord(
            t=t, step=step, network=network, stations=stations, links=links,
            satellites=sats, decision=self._pending_decision,
            events=list(self._pending_events),
        )
        self._pending_events.clear()
        self.sink.write(rec)

    # -- face builders -------------------------------------------------------
    def _links(self, sim, t, ctx) -> list:
        session, diag, rates = ctx["session"], ctx["diag"], ctx["rates"]
        alloc_bw, alloc_pw = ctx["alloc_bw"], ctx["alloc_pw"]
        delivered_step, session_ready = ctx["delivered_step"], ctx["session_ready"]
        fcq = self._fc
        out = []
        for v in ctx["vis"]:
            sid, gid = v.sat_id, v.station_id
            active = session.get(sid, (None, None))[0] == gid
            if not active and not self.include_idle_links:
                continue
            sat, station = sim.sats[sid], sim.stations[gid]
            rain = sim.scn.weather.fade_db(gid, t)
            d = diag.get(sid) if active else None

            if d is not None:
                snr_lin, sinr_lin = d["snr"], d["sinr"]
                bw, pw, ch, inr = d["bw"], d["pw"], d["channel"], d["inr"]
            else:
                # candidate link: what it *would* get at nominal bandwidth/power
                snr_lin = snr_linear(v.range_km, v.elev_deg, sat, station,
                                     rain_zenith_db=rain)
                sinr_lin, bw, pw, ch, inr = snr_lin, sat.bandwidth_hz, sat.tx_power_w, None, 0.0

            started = self._session_started_at.get(sid) if active else None
            ready_at = session_ready.get(sid) if active else None
            out.append(LinkRecord(
                sat_id=sid, station_id=gid, active=active,
                beam=session[sid][1] if active else None, channel=ch,
                elev_deg=v.elev_deg, az_deg=v.az_deg, range_km=v.range_km,
                scan_deg=max(0.0, 90.0 - v.elev_deg),
                snr_db=_db(snr_lin), sinr_db=_db(sinr_lin), inr_db=_db(inr),
                ber=ber_from_sinr(sinr_lin), rain_fade_db=rain,
                alloc_bw_hz=bw, alloc_power_w=pw,
                rate_bps=rates.get(sid, 0.0) if active else 0.0,
                clean_rate_bps=v.rate_bps,
                time_to_los_s=fcq.time_to_los(sid, gid, t),
                bits_delivered=delivered_step.get(sid, 0.0) if active else 0.0,
                slewing=bool(active and ready_at is not None and ready_at > t),
                session_age_s=(t - started) if started is not None else 0.0,
            ))
        return out

    def _stations(self, sim, t, ctx, links) -> list:
        busy, avail, dyn = ctx["busy"], ctx["avail"], ctx["dyn"]
        hk = self._housekeeping(sim)
        by_station = {}
        for l in links:
            by_station.setdefault(l.station_id, []).append(l)

        out = []
        for gid, g in sim.stations.items():
            ls = by_station.get(gid, [])
            act = [l for l in ls if l.active]
            pool = dyn[gid]["bandwidth_hz"] if dyn else g.bandwidth_hz
            up = dyn[gid]["up"] if dyn else True
            beams_avail = avail[gid]
            alloc = sum(l.alloc_bw_hz for l in act)
            out.append(StationRecord(
                station_id=gid, lat_deg=g.lat_deg, lon_deg=g.lon_deg,
                up=up, beams_total=g.num_beams, beams_available=beams_avail,
                beams_active=len(busy[gid]),
                beam_utilization=(len(busy[gid]) / beams_avail) if beams_avail else 0.0,
                bandwidth_base_hz=g.bandwidth_hz,
                bandwidth_pool_hz=pool, bandwidth_alloc_hz=alloc,
                bandwidth_utilization=(alloc / pool) if pool else 0.0,
                link_power_w=sum(l.alloc_power_w for l in act),
                rate_bps=sum(l.rate_bps for l in act),
                bits_delivered=sum(l.bits_delivered for l in act),
                weather=sim.scn.weather.state(gid, t),
                rain_fade_db=sim.scn.weather.fade_db(gid, t),
                connected_sats=[l.sat_id for l in act],
                visible_sats=len(ls),
                mean_sinr_db=(sum(l.sinr_db for l in act) / len(act)) if act else 0.0,
                phased_array=g.phased_array, n_channels=g.n_channels,
                channels_in_use=len({l.channel for l in act if l.channel is not None}),
                degraded=bool(up and (beams_avail < g.num_beams or pool < g.bandwidth_hz)),
                **hk(gid, t),
            ))
        return out

    @staticmethod
    def _housekeeping(sim):
        """Station-local instrument readings, or zeros when no degradation model
        is configured. Telemetry stays a pure observer: it reads the model, never
        advances it."""
        deg = getattr(sim.scn, "degradation", None)
        if deg is None:
            return lambda gid, t: {}
        return lambda gid, t: deg.housekeeping(gid, t)

    def _satellites(self, sim, t, ctx) -> list:
        backlog, done, session = ctx["backlog"], ctx["done"], ctx["session"]
        vis_by_sat, rates = ctx["vis_by_sat"], ctx["rates"]
        metrics, session_ready = ctx["metrics"], ctx["session_ready"]
        ecef = getattr(sim, "_last_sat_ecef", {}) or {}

        fcq = self._fc
        out = []
        for sid, s in sim.sats.items():
            p = ecef.get(sid)
            if p is not None:
                lat, lon = orb.subsatellite_point(p)
                alt = float((p[0] ** 2 + p[1] ** 2 + p[2] ** 2) ** 0.5) - orb.R_EARTH
            else:
                lat = lon = alt = 0.0

            vis = vis_by_sat.get(sid, {})
            cur = session.get(sid)
            cur_gid = cur[0] if cur else None
            nc = fcq.next_contact(sid, t)
            if done[sid] or backlog[sid] <= 0:
                state = "done"
            elif cur is None:
                state = "waiting" if ctx["ready_since"][sid] is not None else "idle"
            else:
                ready_at = session_ready.get(sid)
                state = "slewing" if (ready_at is not None and ready_at > t) else "transmitting"

            out.append(SatelliteRecord(
                sat_id=sid, lat_deg=lat, lon_deg=lon, alt_km=alt, state=state,
                backlog_bits=backlog[sid], backlog0_bits=metrics.backlog0.get(sid, 0.0),
                delivered_bits=metrics.delivered.get(sid, 0.0),
                bits_delivered_step=ctx["delivered_step"].get(sid, 0.0),
                wait_s=metrics.wait_time.get(sid, 0.0),
                ready_since=ctx["ready_since"][sid],
                priority=s.priority, tier=s.tier, deadline_s=s.deadline_s,
                time_to_deadline_s=(s.deadline_s - t) if s.deadline_s is not None else None,
                visible_stations=sorted(vis.keys()), n_visible=len(vis),
                current_station=cur[0] if cur else None,
                current_beam=cur[1] if cur else None,
                rate_bps=rates.get(sid, 0.0),
                best_visible_rate_bps=max((v.rate_bps for v in vis.values()), default=0.0),
                next_contact_s=nc[0], next_contact_station=nc[1],
                contact_window_s=nc[2],
                time_to_los_s=(fcq.time_to_los(sid, cur_gid, t) if cur_gid else -1.0),
            ))
        return out

    def _network(self, sim, t, ctx, stations, links, sats) -> NetworkRecord:
        cfg, metrics = sim.cfg, ctx["metrics"]
        backlog, done, session = ctx["backlog"], ctx["done"], ctx["session"]
        step_bits = sum(ctx["delivered_step"].values())
        total_bits = sum(metrics.delivered.values())
        demand = sum(metrics.backlog0.values())
        with_demand = [sid for sid in sim.sats if metrics.backlog0.get(sid, 0.0) > 0]
        n_completed = sum(1 for sid in with_demand if done[sid])

        beams_avail = sum(s.beams_available for s in stations) if stations \
            else sum(ctx["avail"].values())
        beams_active = sum(len(b) for b in ctx["busy"].values())
        beams_total = sum(g.num_beams for g in sim.stations.values())
        pool = sum(s.bandwidth_pool_hz for s in stations)
        alloc = sum(s.bandwidth_alloc_hz for s in stations)

        act = [l for l in links if l.active]
        vis_pairs = len(links) if self.include_idle_links else len(ctx["vis"])
        sats_with_link = len({v.sat_id for v in ctx["vis"]})
        backlogged = [sid for sid in sim.sats if not done[sid] and backlog[sid] > 0]
        waiting = [sid for sid in backlogged
                   if ctx["ready_since"][sid] is not None and sid not in session]

        wcounts: dict = {}
        fades = []
        for s in stations:
            wcounts[s.weather] = wcounts.get(s.weather, 0) + 1
            fades.append(s.rain_fade_db)
        if not stations:                       # stations face disabled
            for gid in sim.stations:
                st = sim.scn.weather.state(gid, t)
                wcounts[st] = wcounts.get(st, 0) + 1
                fades.append(sim.scn.weather.fade_db(gid, t))

        return NetworkRecord(
            t=t,
            bits_delivered_step=step_bits, bits_delivered_total=total_bits,
            throughput_bps=step_bits / cfg.dt_s if cfg.dt_s else 0.0,
            queue_bits=sum(backlog.values()), demand_bits=demand,
            completion_rate=(n_completed / len(with_demand)) if with_demand else 0.0,
            delivery_fraction=(total_bits / demand) if demand else 0.0,
            n_sats=len(sim.sats), n_completed=n_completed,
            n_backlogged=len(backlogged), n_waiting=len(waiting),
            stations_total=len(sim.stations),
            stations_up=sum(1 for s in stations if s.up) if stations else len(sim.stations),
            beams_total=beams_total, beams_available=beams_avail, beams_active=beams_active,
            beam_utilization=(beams_active / beams_avail) if beams_avail else 0.0,
            bandwidth_pool_hz=pool, bandwidth_alloc_hz=alloc,
            bandwidth_utilization=(alloc / pool) if pool else 0.0,
            contention_ratio=(len(waiting) + beams_active) / beams_avail if beams_avail else 0.0,
            n_visible_pairs=vis_pairs, n_sats_with_link=sats_with_link,
            coverage=(sats_with_link / len(backlogged)) if backlogged else 1.0,
            mean_elev_deg=(sum(v.elev_deg for v in ctx["vis"]) / len(ctx["vis"]))
            if ctx["vis"] else 0.0,
            mean_sinr_db=(sum(l.sinr_db for l in act) / len(act)) if act else 0.0,
            min_sinr_db=min((l.sinr_db for l in act), default=0.0),
            energy_j_step=ctx["energy_step"], energy_j_total=metrics.energy_j,
            power_w=ctx["energy_step"] / cfg.dt_s if cfg.dt_s else 0.0,
            weather_counts=wcounts,
            mean_rain_fade_db=(sum(fades) / len(fades)) if fades else 0.0,
            max_rain_fade_db=max(fades) if fades else 0.0,
            sessions_active=len(session),
            sessions_started_total=sum(metrics.sessions_started.values()),
            interruptions_total=metrics.interruptions,
            handovers_total=sum(metrics.handovers.values()),
            proactive_handovers_total=metrics.proactive_handovers,
            mean_wait_s=(sum(metrics.wait_time.values()) / len(sim.sats)) if sim.sats else 0.0,
            decision_ms=(self._pending_decision.decision_ms
                         if self._pending_decision else 0.0),
        )

    # -- state-change detection ---------------------------------------------
    def _diff_events(self, sim, t, ctx) -> None:
        """Emit events for changes the simulator does not announce directly:
        station/beam availability transitions and weather changes."""
        if "events" not in self.capture:
            return
        dyn, avail = ctx["dyn"], ctx["avail"]
        for gid in sim.stations:
            up = dyn[gid]["up"] if dyn else True
            beams = avail[gid]
            prev = self._prev_dyn.get(gid)
            if prev is not None:
                if prev["up"] and not up:
                    self.note_event(t, "station_fail", station_id=gid)
                elif not prev["up"] and up:
                    self.note_event(t, "station_recover", station_id=gid)
                if beams < prev["beams"]:
                    self.note_event(t, "beam_fail", station_id=gid,
                                    lost=prev["beams"] - beams, beams=beams)
                elif beams > prev["beams"]:
                    self.note_event(t, "beam_recover", station_id=gid,
                                    gained=beams - prev["beams"], beams=beams)
            self._prev_dyn[gid] = {"up": up, "beams": beams}

            w = sim.scn.weather.state(gid, t)
            if self._prev_weather.get(gid) not in (None, w):
                self.note_event(t, "weather_change", station_id=gid,
                                to=w, was=self._prev_weather[gid],
                                fade_db=sim.scn.weather.fade_db(gid, t))
            self._prev_weather[gid] = w

    # -- session bookkeeping the simulator hands over ------------------------
    def note_session_start(self, t: float, sat_id: str, station_id: str, beam: int) -> None:
        self._session_started_at[sat_id] = t
        self.note_event(t, "session_start", sat_id, station_id, beam=beam)

    def note_session_end(self, t: float, sat_id: str, station_id: str, reason: str) -> None:
        started = self._session_started_at.pop(sat_id, None)
        self.note_event(t, "session_end", sat_id, station_id, reason=reason,
                        duration_s=(t - started) if started is not None else None)


def _name_of(obj) -> str:
    """Readable identity of a scheduler/allocator for the decision record."""
    n = getattr(obj, "name", None)
    if isinstance(n, str) and n:
        order = getattr(obj, "order_key", None)
        station = getattr(obj, "station_key", None)
        if order and station:
            return f"{order}/{station}"
        return n
    order, station = getattr(obj, "order_key", None), getattr(obj, "station_key", None)
    if order and station:
        return f"{order}/{station}"
    return type(obj).__name__


# --------------------------------------------------------------------------- #
# Flattening — the bridge to the feature layer / CSV / pandas                  #
# --------------------------------------------------------------------------- #

def to_rows(records, face: str = "network", run_id: str | None = None) -> list:
    """Flatten a list of `TelemetryRecord` into plain dicts, one per entity per
    step — the tabular shape the feature layer, CSV export and pandas all want.

        rows = to_rows(rec.records, "link")

    `face` in {network, station, link, satellite, decision, event}.
    """
    out = []
    for r in records:
        base = {"t": r.t, "step": r.step}
        if run_id:
            base["run_id"] = run_id
        if face == "network":
            if r.network:
                d = asdict(r.network)
                d.pop("weather_counts", None)
                out.append({**base, **d})
        elif face == "station":
            out.extend({**base, **asdict(s)} for s in r.stations)
        elif face == "link":
            out.extend({**base, **asdict(l)} for l in r.links)
        elif face == "satellite":
            out.extend({**base, **asdict(s)} for s in r.satellites)
        elif face == "decision":
            if r.decision:
                d = asdict(r.decision)
                d.pop("assignments", None)
                d.pop("reasons", None)
                d.pop("expected", None)
                out.append({**base, **d})
        elif face == "event":
            out.extend({**base, "kind": e.kind, "sat_id": e.sat_id,
                        "station_id": e.station_id, **e.detail} for e in r.events)
        else:
            raise ValueError(f"unknown face: {face}")
    return out


def write_csv(records, path: str, face: str = "network", run_id: str | None = None) -> int:
    """Export one face to CSV. Returns the number of rows written."""
    import csv

    rows = to_rows(records, face, run_id)
    if not rows:
        return 0
    keys = list({k: None for row in rows for k in row}.keys())   # union, ordered
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: (v if not isinstance(v, (list, dict)) else json.dumps(v))
                        for k, v in row.items()})
    return len(rows)
