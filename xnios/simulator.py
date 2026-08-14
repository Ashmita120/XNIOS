"""The event/time-stepped simulation engine.

Per step: propagate orbits -> compute visibility & link value -> free ended
sessions (buffer drained or pass lost) -> let the scheduler fill spare capacity ->
transfer data -> record KPIs. The simulator owns ALL mutable state; the scheduler
only ever sees a NetworkState snapshot and returns Assignments.

Sessions are sticky: once a satellite is assigned to a beam it keeps that beam
until its buffer drains or the station can no longer see it. This models the fact
that you don't casually drop a live downlink, and it keeps schedulers from
thrashing when `decide` is called every step.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass

from . import orbit as orb
from .link import (achievable_rate_bps, snr_linear, rate_from_sinr,
                   scan_beamwidth_deg)
from .weather import WeatherModel
from .state import NetworkState, SatView, StationView, VisibilityView
from .metrics import MetricsCollector, Results
from .allocators import (EqualAllocator, LinkDemand, FixedPower, PowerDemand,
                         GraphColorFreq, BeamNode)


@dataclass
class SimConfig:
    duration_s: float = 1200.0        # total simulated time
    dt_s: float = 5.0                 # integration / decision step
    decision_interval_s: float = 5.0  # how often the scheduler is consulted
    verbose: bool = False
    trace: bool = False               # record per-step delivered totals (validation)
    handover: bool = False            # proactive handover: switch station before LOS
    handover_lead_s: float = 30.0     # trigger a handover if the pass ends within this
    # "elevation" = V1: compare elevation at t+lead against the *configured* mask.
    # "forecast"  = V2: exact seconds-to-LOS from xnios.forecast, which also honours
    #               the phased-array steering limit the elevation test ignores.
    # "capacity"  = V3: same exact trigger, but the DESTINATION is chosen by the data
    #               the alternative can actually carry (xnios.lookahead) rather than
    #               by its instantaneous rate. "forecast" answers only *when* to leave;
    #               this also answers *where* to go.
    handover_mode: str = "elevation"


@dataclass
class Scenario:
    satellites: list       # list[Satellite]
    stations: list         # list[GroundStation]
    weather: WeatherModel = None
    name: str = "scenario"
    dynamics: object = None    # NetworkDynamics (failures / dynamic capacity); None = static
    traffic: object = None     # arrival process; None/NoArrivals = V1 (fill once at t=0)
    degradation: object = None  # StationDegradation; None = no receive-chain decay

    def __post_init__(self):
        if self.weather is None:
            self.weather = WeatherModel()
        if self.traffic is None:
            from .traffic import NoArrivals
            self.traffic = NoArrivals()


class Simulator:
    def __init__(self, scenario: Scenario, scheduler, config: SimConfig = None,
                 allocator=None, power_allocator=None, freq_allocator=None,
                 telemetry=None):
        self.scn = scenario
        self.scheduler = scheduler
        self.cfg = config or SimConfig()
        self.allocator = allocator or EqualAllocator()        # divides each station's bw pool
        self.power_allocator = power_allocator or FixedPower()  # sets each link's tx power
        self.freq_allocator = freq_allocator or GraphColorFreq()  # channels for phased arrays
        # optional TelemetryRecorder: observes every step, never influences one.
        # None (default) = no recording and no cost.
        self.tel = telemetry

        self.sats = {s.id: s for s in scenario.satellites}
        self.stations = {g.id: g for g in scenario.stations}
        self._gs_ecef = {g.id: orb.gs_position_ecef(g.lat_deg, g.lon_deg, g.alt_km)
                         for g in scenario.stations}
        self._last_sat_ecef = {}                              # for telemetry ground tracks

    def run(self) -> Results:
        cfg = self.cfg
        scn = self.scn

        # --- runtime state ---
        backlog = {sid: s.backlog_bits for sid, s in self.sats.items()}
        arrived_total = {}                                 # sat_id -> bits that ARRIVED mid-run
        done = {sid: False for sid in self.sats}
        ready_since = {sid: None for sid in self.sats}
        # active sessions: sat_id -> (station_id, beam_index)
        session = {}
        session_ready = {}       # sat_id -> time a (re)acquired beam finishes slewing
        interrupted_at = {}      # sat_id -> time a failure killed its session (for recovery)
        # busy beams per station: station_id -> {beam_index: sat_id}
        busy = {gid: {} for gid in self.stations}

        metrics = MetricsCollector(
            self.sats.keys(), self.stations.keys(),
            {gid: g.num_beams for gid, g in self.stations.items()},
        )
        for sid, s in self.sats.items():
            metrics.backlog0[sid] = s.backlog_bits
            metrics.deadline[sid] = s.deadline_s

        if cfg.handover and cfg.handover_mode == "forecast":
            from .telemetry import _ForecastCache
            self._los_cache = _ForecastCache(self)         # windows once, lookups after
        elif cfg.handover and cfg.handover_mode == "capacity":
            from .lookahead import Lookahead                # windows + capacity curves
            self._look = Lookahead(self.scn.satellites, self.scn.stations,
                                   weather=self.scn.weather, t0=0.0,
                                   span_s=cfg.duration_s + 5400.0)

        self.scheduler.bind(self.scn, self.cfg)            # look-ahead schedulers use this
        if self.tel is not None:
            self.tel.begin_run(self)

        t = 0.0
        steps = int(round(cfg.duration_s / cfg.dt_s))
        last_decision = -1e18
        self.trace = []                                    # [(t, {sat_id: delivered_bits})]

        for _ in range(steps):
            delivered_step = {}                            # telemetry: bits moved this step
            energy_step = 0.0
            # 0) traffic arrivals (opt-in; the default NoArrivals returns 0.0 for
            #    every satellite, so a scenario with no `traffic` block behaves
            #    bit-identically to V1). New data reopens a drained satellite:
            #    `note_complete` is idempotent, so its first completion still stands.
            traffic = getattr(scn, "traffic", None)
            if traffic is not None and traffic.kind != "none":
                for sid in self.sats:
                    bits = traffic.arrivals(sid, t, cfg.dt_s)
                    if bits > 0.0:
                        backlog[sid] += bits
                        arrived_total[sid] = arrived_total.get(sid, 0.0) + bits
                        done[sid] = False

            # 0b) current network dynamics: which stations are up, beams & bandwidth now
            dyn = scn.dynamics.snapshot(t) if scn.dynamics else None
            avail = {gid: (dyn[gid]["beams"] if dyn else g.num_beams)
                     for gid, g in self.stations.items()}

            # 1) visibility (excludes failed/maintenance stations)
            vis = self._visibility(t, backlog, done, dyn)
            vis_by_sat = {}
            for v in vis:
                vis_by_sat.setdefault(v.sat_id, {})[v.station_id] = v

            # 2) failures kill in-progress sessions (station down, or beams lost)
            self._apply_failures(session, busy, dyn, avail, metrics, t, interrupted_at)

            # 2b) proactive handover: move sessions about to lose their pass to a
            #     still-visible station BEFORE LOS, so there is no interruption
            if cfg.handover:
                self._proactive_handover(session, busy, vis_by_sat, avail, t, metrics,
                                         backlog)

            # 3) update readiness & drop sessions whose pass ended (LOS)
            for sid in self.sats:
                if done[sid] or backlog[sid] <= 0:
                    continue
                if sid in vis_by_sat:                       # visible with usable link
                    if ready_since[sid] is None:
                        ready_since[sid] = t
                        metrics.note_ready(sid, t)
            self._release_lost_sessions(session, busy, vis_by_sat, t)

            # 4) build the state snapshot and consult the scheduler (spare capacity)
            if t - last_decision >= cfg.decision_interval_s - 1e-9:
                state = self._make_state(t, backlog, done, ready_since, session, busy, vis, avail)
                t0 = time.perf_counter()
                new = self.scheduler.decide(state)
                decision_s = time.perf_counter() - t0
                metrics.note_decision(decision_s)                 # real-time feasibility
                before = set(session.keys())
                accepted = self._apply(new, session, busy, vis_by_sat, avail)
                if self.tel is not None:
                    self.tel.note_decision(self, t, accepted, decision_s,
                                           len(state.free_sats()))
                for sid in session.keys() - before:               # newly started sessions
                    gid = session[sid][0]
                    metrics.note_session_start(sid, gid)
                    session_ready[sid] = t + self.stations[gid].setup_time_s   # beam slew
                    if sid in interrupted_at:                     # recovered after a failure
                        metrics.note_recovery(t - interrupted_at.pop(sid))
                        if self.tel is not None:
                            self.tel.note_event(t, "recover", sid, gid)
                    if self.tel is not None:
                        self.tel.note_session_start(t, sid, gid, session[sid][1])
                last_decision = t

            # 5) allocate bandwidth + power, resolve phased-array interference into a
            #    per-link rate, then transfer and bill the transmit energy
            alloc_bw = self._allocate(session, vis_by_sat, backlog, t, dyn)
            alloc_pw = self._allocate_power(session, vis_by_sat, alloc_bw, t)
            diag = {} if self.tel is not None else None       # per-link RF detail for telemetry
            rates = self._compute_rates(session, vis_by_sat, alloc_bw, alloc_pw, t, diag,
                                        metrics=metrics)
            for sid, (gid, _beam) in list(session.items()):
                v = vis_by_sat.get(sid, {}).get(gid)
                if v is None:
                    continue
                # beam-switching delay: the beam transmits only after it finishes
                # slewing to the (re)acquired satellite
                xfer_dt = cfg.dt_s
                ready = session_ready.get(sid)
                if ready is not None:
                    xfer_dt = min(cfg.dt_s, (t + cfg.dt_s) - ready)
                if xfer_dt <= 0:
                    continue                                # still acquiring the satellite
                pw = alloc_pw.get(sid, self.sats[sid].tx_power_w)
                metrics.note_energy(pw, xfer_dt)            # power spent while transmitting
                energy_step += pw * xfer_dt
                rate = rates.get(sid, 0.0)
                if rate <= 0:
                    continue
                bits = min(rate * xfer_dt, backlog[sid])
                backlog[sid] -= bits
                delivered_step[sid] = delivered_step.get(sid, 0.0) + bits
                metrics.note_transfer(sid, gid, bits)
                if backlog[sid] <= 1.0:                     # buffer drained -> complete
                    backlog[sid] = 0.0
                    done[sid] = True
                    metrics.note_complete(sid, t + cfg.dt_s)
                    self._free_session(sid, session, busy, t, "complete")
                    if self.tel is not None:
                        self.tel.note_event(t, "complete", sid, gid)

            # 5) waiting time: ready, has data, but not being served this step
            for sid in self.sats:
                if done[sid] or backlog[sid] <= 0:
                    continue
                if ready_since[sid] is not None and sid not in session:
                    metrics.note_wait(sid, cfg.dt_s)

            # 6) utilisation bookkeeping
            for gid in self.stations:
                metrics.note_beam_busy(gid, len(busy[gid]), cfg.dt_s)
            total_busy = sum(len(busy[gid]) for gid in self.stations)
            metrics.note_peak(total_busy)

            if cfg.trace:
                self.trace.append((t, dict(metrics.delivered), total_busy))

            # 7) telemetry: observe the settled state of this step (never feeds back)
            if self.tel is not None:
                self.tel.capture_step(self, t, {
                    "backlog": backlog, "done": done, "ready_since": ready_since,
                    "session": session, "session_ready": session_ready, "busy": busy,
                    "avail": avail, "dyn": dyn, "vis": vis, "vis_by_sat": vis_by_sat,
                    "alloc_bw": alloc_bw, "alloc_pw": alloc_pw, "rates": rates,
                    "diag": diag or {}, "delivered_step": delivered_step,
                    "energy_step": energy_step, "metrics": metrics,
                })

            t += cfg.dt_s

        results = metrics.finalize(cfg.duration_s)
        if self.tel is not None:
            self.tel.end_run(results)
        return results

    def _allocate(self, session, vis_by_sat, backlog, t, dyn=None) -> dict:
        """Group active sessions by station and let the allocator divide each
        station's (possibly time-varying) bandwidth pool among the links."""
        by_station = defaultdict(list)
        for sid, (gid, _beam) in session.items():
            v = vis_by_sat.get(sid, {}).get(gid)
            if v is None or v.rate_bps <= 0:
                continue
            by_station[gid].append((sid, v))

        out = {}
        for gid, links in by_station.items():
            station = self.stations[gid]
            rain = self.scn.weather.fade_db(gid, t)
            pool = dyn[gid]["bandwidth_hz"] if dyn else station.bandwidth_hz
            demands = [
                LinkDemand(
                    sat_id=sid,
                    want_hz=self.sats[sid].bandwidth_hz,
                    priority=self.sats[sid].priority,
                    backlog_bits=backlog[sid],
                    rate_fn=self._rate_fn(v, self.sats[sid], station, rain,
                                          self._gt_penalty(gid, t)),
                )
                for sid, v in links
            ]
            out.update(self.allocator.allocate(pool, demands))
        return out

    def _apply_failures(self, session, busy, dyn, avail, metrics, t, interrupted_at):
        """Kill in-progress sessions on failed stations, and (for beam failures) on
        stations now serving more satellites than they have working beams."""
        if dyn is None:
            return
        for gid in self.stations:
            if not dyn[gid]["up"]:                          # whole station down
                for sid in [s for s, (g, _b) in session.items() if g == gid]:
                    self._free_session(sid, session, busy, t, "station_down")
                    metrics.note_interruption()
                    interrupted_at[sid] = t
                    if self.tel is not None:
                        self.tel.note_event(t, "interrupt", sid, gid, cause="station_down")
            else:                                           # beams may have been lost
                over = len(busy[gid]) - avail[gid]
                if over > 0:
                    victims = [s for s, (g, _b) in session.items() if g == gid][:over]
                    for sid in victims:
                        self._free_session(sid, session, busy, t, "beam_lost")
                        metrics.note_interruption()
                        interrupted_at[sid] = t
                        if self.tel is not None:
                            self.tel.note_event(t, "interrupt", sid, gid, cause="beam_lost")

    def _ending_soon(self, sat, gid, t) -> bool:
        """Is this pass about to end?

        V1 ("elevation") asks whether the elevation `lead` seconds from now is
        still above the station's *configured* mask. That misses the phased-array
        steering limit: with `max_scan_deg=60` a beam cannot be formed below 30
        deg at all, while the configured mask says 10, so the test reports the
        pass as continuing when the link is already unusable and the session is
        dropped as an interruption instead of being handed over.

        V2 ("forecast") asks `xnios.forecast` for the exact seconds to loss of
        signal, which folds in the mask, the steering limit and the SNR floor.
        """
        lead = self.cfg.handover_lead_s
        mode = self.cfg.handover_mode
        if mode == "capacity":
            ttl = self._look.time_to_los(sat.id, gid, t)   # -1 = not in contact
            return 0.0 <= ttl <= lead
        if mode != "forecast":
            return self._elev_at(sat, gid, t + lead) < self.stations[gid].elevation_mask_deg
        ttl = self._los_cache.time_to_los(sat.id, gid, t)
        return ttl is not None and ttl <= lead

    def _handover_value(self, sid, v, t, backlog):
        """How good is this handover destination?

        "elevation"/"forecast" rank by instantaneous rate — which answers *when*
        to leave a pass exactly, then picks *where* to go myopically. A station
        showing a great rate right now may be about to set itself.

        "capacity" ranks by the data the alternative can actually carry before
        its own LOS, capped by what the satellite still has to send, with the
        slew time already spent. Rate breaks ties: when two alternatives can
        both drain the buffer, the faster one frees the beam sooner.
        """
        if self.cfg.handover_mode != "capacity":
            return v.rate_bps
        setup = self.stations[v.station_id].setup_time_s
        bits = self._look.remaining_bits(sid, v.station_id, t + setup)
        return (min(backlog.get(sid, 0.0), bits), v.rate_bps)

    def _proactive_handover(self, session, busy, vis_by_sat, avail, t, metrics, backlog):
        """Before a satellite's current pass ends, move it to another already-visible
        station that has a free beam — a make-before-break switch, no interruption."""
        for sid, (gid, beam) in list(session.items()):
            sat = self.sats[sid]
            if not self._ending_soon(sat, gid, t):
                continue                                    # pass not ending yet
            # pick the best currently-visible alternative with spare capacity
            alts = [v for v in vis_by_sat.get(sid, {}).values()
                    if v.station_id != gid and (avail[v.station_id] - len(busy[v.station_id])) > 0]
            if not alts:
                continue
            best = max(alts, key=lambda v: self._handover_value(sid, v, t, backlog))
            new_beam = self._first_free_beam(best.station_id, busy, avail[best.station_id])
            if new_beam is None:
                continue
            busy[gid].pop(beam, None)                        # release old beam
            busy[best.station_id][new_beam] = sid            # acquire new beam
            session[sid] = (best.station_id, new_beam)
            metrics.note_proactive_handover()
            metrics.note_session_start(sid, best.station_id)  # counts as a station change
            if self.tel is not None:
                self.tel.note_event(t, "handover", sid, best.station_id,
                                    from_station=gid, proactive=True,
                                    rate_bps=best.rate_bps)

    def _gt_penalty(self, gid: str, t: float) -> float:
        """Receive-chain degradation for this station right now (V2 workstream B).

        0.0 unless a `degradation` block is configured, which keeps every V1 run
        bit-identical. Deliberately NOT visible to `xnios.forecast`: the residual
        between measured and forecast SNR is exactly the precursor a health model
        has to infer.
        """
        deg = getattr(self.scn, "degradation", None)
        return deg.loss_db(gid, t) if deg is not None else 0.0

    def _elev_at(self, sat, gid, t) -> float:
        p = orb.sat_position_ecef(sat.orbit, t)
        g = self.stations[gid]
        elev, _az, _r = orb.elevation_azimuth_range(self._gs_ecef[gid], g.lat_deg, g.lon_deg, p)
        return elev

    @staticmethod
    def _rate_fn(v, sat, station, rain, gt=0.0):
        return lambda bw: achievable_rate_bps(v.range_km, v.elev_deg, sat, station,
                                              rain_zenith_db=rain, bandwidth_hz=bw,
                                              gt_penalty_db=gt)

    def _allocate_power(self, session, vis_by_sat, alloc_bw, t) -> dict:
        """Let the power allocator set each active link's transmit power, given the
        bandwidth it was granted (rate_fn holds bandwidth fixed, varies power)."""
        demands = []
        for sid, (gid, _beam) in session.items():
            v = vis_by_sat.get(sid, {}).get(gid)
            if v is None or v.rate_bps <= 0:
                continue
            sat, station = self.sats[sid], self.stations[gid]
            rain = self.scn.weather.fade_db(gid, t)
            bw = alloc_bw.get(sid, sat.bandwidth_hz)
            demands.append(PowerDemand(
                sat_id=sid, nominal_w=sat.tx_power_w, max_w=sat.tx_power_max_w,
                rate_fn=self._power_rate_fn(v, sat, station, rain, bw,
                                            self._gt_penalty(gid, t))))
        return self.power_allocator.allocate(demands) if demands else {}

    @staticmethod
    def _power_rate_fn(v, sat, station, rain, bw, gt=0.0):
        return lambda pw: achievable_rate_bps(v.range_km, v.elev_deg, sat, station,
                                              rain_zenith_db=rain, bandwidth_hz=bw,
                                              tx_power_w=pw, gt_penalty_db=gt)

    def _compute_rates(self, session, vis_by_sat, alloc_bw, alloc_pw, t, diag=None,
                       metrics=None) -> dict:
        """Per-link data rate. For phased-array stations serving >1 beam, resolve
        co-channel interference (C/(N+I)) after a frequency allocation; otherwise
        the interference-free rate.

        `diag` (telemetry only): if a dict is passed, it is filled with the
        intermediate RF quantities per satellite — snr/sinr/bandwidth/power/
        channel/interference — which are otherwise local and unobservable."""
        by_station = defaultdict(list)
        for sid, (gid, _beam) in session.items():
            v = vis_by_sat.get(sid, {}).get(gid)
            if v is None or v.rate_bps <= 0:
                continue
            by_station[gid].append((sid, v))

        rates = {}
        for gid, links in by_station.items():
            station = self.stations[gid]
            rain = self.scn.weather.fade_db(gid, t)
            gt = self._gt_penalty(gid, t)                # receive-chain degradation
            info = {}                                    # sid -> (v, bw, snr_linear)
            for sid, v in links:
                sat = self.sats[sid]
                bw = alloc_bw.get(sid, sat.bandwidth_hz)
                pw = alloc_pw.get(sid, sat.tx_power_w)
                snr = snr_linear(v.range_km, v.elev_deg, sat, station,
                                 rain_zenith_db=rain, bandwidth_hz=bw, tx_power_w=pw,
                                 gt_penalty_db=gt)
                info[sid] = (v, bw, snr)

            # no interference: single beam, or a traditional (non-phased-array) station
            if not (getattr(station, "phased_array", False) and len(links) > 1):
                for sid, (v, bw, snr) in info.items():
                    rates[sid] = rate_from_sinr(bw, snr)
                    if metrics is not None:
                        metrics.note_link(snr, 0.0, rates[sid])
                    if diag is not None:
                        diag[sid] = {"snr": snr, "sinr": snr, "inr": 0.0, "bw": bw,
                                     "pw": alloc_pw.get(sid, self.sats[sid].tx_power_w),
                                     "channel": None}
                continue

            # phased array with multiple beams: assign channels, then C/(N+I).
            # dual polarisation doubles the orthogonal reuse slots (channel x pol),
            # so twice as many beams can share the spectrum before they interfere.
            beams = [BeamNode(sid, v.az_deg, v.elev_deg) for sid, (v, _b, _s) in info.items()]
            n_slots = station.n_channels * (2 if getattr(station, "dual_pol", False) else 1)
            # Model A: one fixed width for every beam. Model B: each beam's width
            # follows its own scan angle, so a low pass is a wide beam.
            width = {sid: scan_beamwidth_deg(v.elev_deg, station)
                     for sid, (v, _b, _s) in info.items()}
            # Two beams need separating by whichever of them is wider.
            sep_fn = lambda a, b: (self._angular_sep(info[a][0], info[b][0]),
                                   2.0 * max(width[a], width[b]))
            channels = self.freq_allocator.allocate(beams, n_slots, sep_fn)
            for sid, (v, bw, snr) in info.items():
                inr = 0.0
                for oid, (ov, _ob, osnr) in info.items():
                    if oid == sid or channels.get(oid) != channels.get(sid):
                        continue                         # different channel -> no interference
                    sep = self._angular_sep(v, ov)
                    # the victim's own pattern sets how much of the neighbour leaks in
                    inr += osnr * self._sidelobe(sep, width[sid])
                rates[sid] = rate_from_sinr(bw, snr / (1.0 + inr))
                if metrics is not None:
                    metrics.note_link(snr / (1.0 + inr), inr, rates[sid])
                if diag is not None:
                    diag[sid] = {"snr": snr, "sinr": snr / (1.0 + inr), "inr": inr,
                                 "bw": bw, "pw": alloc_pw.get(sid, self.sats[sid].tx_power_w),
                                 "channel": channels.get(sid)}
        return rates

    @staticmethod
    def _angular_sep(v1, v2) -> float:
        """Angle (deg) between two beam pointings given their (az, elev)."""
        e1, a1 = math.radians(v1.elev_deg), math.radians(v1.az_deg)
        e2, a2 = math.radians(v2.elev_deg), math.radians(v2.az_deg)
        c = math.sin(e1) * math.sin(e2) + math.cos(e1) * math.cos(e2) * math.cos(a1 - a2)
        return math.degrees(math.acos(max(-1.0, min(1.0, c))))

    @staticmethod
    def _sidelobe(sep_deg: float, beamwidth_deg: float) -> float:
        """Fraction of a neighbour's power leaking into a beam this far away:
        ~1 inside the beam, rolling off to a -30 dB sidelobe floor."""
        if sep_deg <= beamwidth_deg:
            return 1.0
        return max((beamwidth_deg / sep_deg) ** 2, 1e-3)

    def snapshot(self, t: float) -> NetworkState:
        """Observable state at time t assuming every satellite is free and every
        beam idle. Used to test a scheduler's *decision rule* in isolation (e.g.
        'given simultaneous visibility, which station?') without session history."""
        backlog = {sid: s.backlog_bits for sid, s in self.sats.items()}
        done = {sid: False for sid in self.sats}
        ready = {sid: t for sid in self.sats}
        vis = self._visibility(t, backlog, done)
        empty_busy = {gid: {} for gid in self.stations}
        avail = {gid: g.num_beams for gid, g in self.stations.items()}
        return self._make_state(t, backlog, done, ready, session={}, busy=empty_busy,
                                vis=vis, avail=avail)

    # ------------------------------------------------------------------ helpers
    def _visibility(self, t, backlog, done, dyn=None):
        out = []
        sat_ecef = {sid: orb.sat_position_ecef(s.orbit, t) for sid, s in self.sats.items()}
        self._last_sat_ecef = sat_ecef            # telemetry reads ground tracks from here
        for sid, s in self.sats.items():
            if done[sid] or backlog[sid] <= 0:
                continue
            for gid, g in self.stations.items():
                if dyn is not None and not dyn[gid]["up"]:      # station down -> no link
                    continue
                elev, az, rng = orb.elevation_azimuth_range(
                    self._gs_ecef[gid], g.lat_deg, g.lon_deg, sat_ecef[sid])
                if elev < g.elevation_mask_deg:
                    continue
                rain = self.scn.weather.fade_db(gid, t)
                rate = achievable_rate_bps(rng, elev, s, g, rain_zenith_db=rain,
                                           gt_penalty_db=self._gt_penalty(gid, t))
                if rate <= 0:
                    continue
                out.append(VisibilityView(sid, gid, elev, rng, rate, az_deg=az))
        return out

    def _make_state(self, t, backlog, done, ready_since, session, busy, vis, avail) -> NetworkState:
        sats = {
            sid: SatView(
                sat_id=sid,
                backlog_bits=backlog[sid],
                priority=s.priority,
                deadline_s=s.deadline_s,
                ready_since=ready_since[sid],
                is_free=(sid not in session),
            )
            for sid, s in self.sats.items() if not done[sid] and backlog[sid] > 0
        }
        stations = {
            gid: StationView(
                station_id=gid,
                num_beams=max(1, avail[gid]),
                free_beams=max(0, avail[gid] - len(busy[gid])),
                weather=self.scn.weather.state(gid, t),
            )
            for gid, g in self.stations.items()
        }
        return NetworkState(t=t, sats=sats, stations=stations, visibilities=vis)

    def _apply(self, assignments, session, busy, vis_by_sat, avail):
        """Validate and apply the scheduler's assignments. Returns the accepted
        ones as (sat_id, station_id, beam) — what the telemetry decision record
        reports, so it logs what actually happened, not what was proposed."""
        accepted = []
        for a in assignments:
            if a.sat_id in session:                         # already active, ignore
                continue
            if a.station_id not in self.stations:
                continue
            # validate: link must currently be usable
            if a.station_id not in vis_by_sat.get(a.sat_id, {}):
                continue
            free_beam = self._first_free_beam(a.station_id, busy, avail[a.station_id])
            if free_beam is None:                           # no capacity -> it waits
                continue
            busy[a.station_id][free_beam] = a.sat_id
            session[a.sat_id] = (a.station_id, free_beam)
            accepted.append((a.sat_id, a.station_id, free_beam))
        return accepted

    def _release_lost_sessions(self, session, busy, vis_by_sat, t=None):
        for sid, (gid, _beam) in list(session.items()):
            if gid not in vis_by_sat.get(sid, {}):          # pass ended / link lost
                self._free_session(sid, session, busy, t, "los")

    @staticmethod
    def _first_free_beam(station_id, busy, num_beams):
        used = busy[station_id]
        if len(used) >= num_beams:
            return None                                     # station at capacity
        idx = 0
        while idx in used:
            idx += 1
        return idx

    def _free_session(self, sid, session, busy, t=None, reason=""):
        if sid not in session:
            return
        gid, beam = session.pop(sid)
        busy[gid].pop(beam, None)
        if self.tel is not None and t is not None:
            self.tel.note_session_end(t, sid, gid, reason)
