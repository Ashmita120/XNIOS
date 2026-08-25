"""KPI collection — the definition of 'performance'.

Because the objective is multi-objective (Pareto), we record a *vector* of KPIs and
never collapse them to one number inside the sim. Scalarisation (weights) is a
property of a scheduler/reward, applied later at analysis time.

Tracked outputs (per the research plan): throughput, latency, waiting time, beam
utilisation, station utilisation, plus completion rate, SLA compliance and Jain
fairness for the multi-objective view.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .link import MAX_SPECTRAL_EFF


@dataclass
class Results:
    summary: dict = field(default_factory=dict)
    per_sat: dict = field(default_factory=dict)              # sat_id -> {...}
    delivered_by_station: dict = field(default_factory=dict)  # station_id -> bits
    served_station_of: dict = field(default_factory=dict)     # sat_id -> station_id (first served)

    def __str__(self) -> str:
        s = self.summary
        lines = [
            f"  throughput            : {s.get('throughput_mbps', 0):.2f} Mbps (aggregate delivered/time)",
            f"  data delivered        : {s.get('delivered_gbit', 0):.2f} Gbit",
            f"  completion rate       : {s.get('completion_rate', 0) * 100:.1f} %",
            f"  mean wait time        : {s.get('mean_wait_s', 0):.1f} s",
            f"  mean latency (in-sys) : {s.get('mean_latency_s', 0):.1f} s",
            f"  beam utilisation      : {s.get('beam_utilization', 0) * 100:.1f} % "
            f"(peak {s.get('peak_beam_utilization', 0) * 100:.0f}%)",
            f"  station utilisation   : {s.get('station_utilization', 0) * 100:.1f} %",
            f"  SLA compliance        : {s.get('sla_compliance', 0) * 100:.1f} %",
            f"  fairness (Jain)       : {s.get('fairness', 0):.3f}",
            f"  data dropped          : {s.get('dropped_gbit', 0):.2f} Gbit "
            f"({s.get('drop_rate', 0) * 100:.1f}% of demand)",
            f"  handovers / reacq     : {s.get('handovers', 0)} / {s.get('reacquisitions', 0)}",
            f"  decision time         : {s.get('mean_decision_ms', 0):.3f} ms mean, "
            f"{s.get('p99_decision_ms', 0):.3f} ms p99, "
            f"{s.get('max_decision_ms', 0):.3f} ms max",
            f"  transmit energy       : {s.get('energy_kj', 0):.1f} kJ "
            f"({s.get('gb_per_kj', 0):.2f} Gb/kJ)",
            f"  failures: interrupted : {s.get('sessions_interrupted', 0)} sessions, "
            f"recovery {s.get('mean_recovery_s', 0):.0f} s, "
            f"proactive handovers {s.get('proactive_handovers', 0)}",
        ]
        return "\n".join(lines)


class MetricsCollector:
    """Accumulates events during a run, then computes the summary vector."""

    def __init__(self, sat_ids, station_ids, num_beams: dict):
        self.sat_ids = list(sat_ids)
        self.station_ids = list(station_ids)
        self.num_beams = dict(num_beams)

        self.delivered = {s: 0.0 for s in sat_ids}            # bits per satellite
        self.wait_time = {s: 0.0 for s in sat_ids}            # ready-but-not-served time
        self.ready_since = {s: None for s in sat_ids}
        self.completed_t = {s: None for s in sat_ids}
        self.backlog0 = {s: 0.0 for s in sat_ids}             # initial demand (for completion)
        self.deadline = {s: None for s in sat_ids}

        self.delivered_by_station = {st: 0.0 for st in station_ids}
        self.served_station_of = {}                           # sat_id -> first station served on
        self.beam_busy_time = {st: 0.0 for st in station_ids}  # beam-seconds busy
        self.station_active_time = {st: 0.0 for st in station_ids}  # >=1 beam busy
        self.tx_beam_s = 0.0                                  # beam-seconds CARRYING data
        self.tx_wall_s = 0.0                                  # wall time >=1 link carrying

        # --- resource / dynamics metrics (added after review) ---
        self.total_beams = sum(num_beams.values())
        self.sessions_started = {s: 0 for s in sat_ids}       # incl. re-acquisitions
        self.handovers = {s: 0 for s in sat_ids}              # station *changes*
        self.last_station = {s: None for s in sat_ids}
        self.decision_times = []                              # wall seconds per decide()
        self.peak_busy_beams = 0                              # max concurrent beams in use
        self.energy_j = 0.0                                   # total transmit energy (Joules)
        self.interruptions = 0                                # sessions killed by a failure
        self.recovery_times = []                              # s from interruption to re-service
        self.proactive_handovers = 0                          # seamless pre-LOS switches

        # --- served-link quality (one sample per active link per step) ---
        # Needed to explain *why* a configuration performs as it does: at the
        # modcod cap extra SNR buys nothing, so a change that trades SNR for
        # contact time can win. `inr` is the interference-to-noise ratio the
        # phased array inflicts on itself.
        self.link_samples = 0
        self.sinr_db_sum = 0.0
        self.inr_sum = 0.0
        self.capped_samples = 0                               # at MAX_SPECTRAL_EFF
        self.outage_samples = 0                               # SINR below lock -> rate 0

        self.sim_time = 0.0

    # --- event hooks called by the simulator ---
    def note_ready(self, sat_id: str, t: float):
        if self.ready_since[sat_id] is None:
            self.ready_since[sat_id] = t

    def note_wait(self, sat_id: str, dt: float):
        self.wait_time[sat_id] += dt

    def note_transfer(self, sat_id: str, station_id: str, bits: float):
        self.delivered[sat_id] += bits
        self.delivered_by_station[station_id] += bits
        self.served_station_of.setdefault(sat_id, station_id)

    def note_complete(self, sat_id: str, t: float):
        if self.completed_t[sat_id] is None:
            self.completed_t[sat_id] = t

    def note_beam_busy(self, station_id: str, busy_beams: int, dt: float):
        self.beam_busy_time[station_id] += busy_beams * dt
        if busy_beams > 0:
            self.station_active_time[station_id] += dt

    def note_tx_step(self, spans) -> None:
        """Seconds each link actually CARRIED DATA this step.

        Deliberately not `note_beam_busy`. That one counts beam occupancy from
        the busy map at the end of the step, which is the right quantity for
        utilisation but the wrong one for a rate:

          * the step on which a transfer completes has already had its session
            freed by then, so its beam time is never counted at all — and
          * a step spent slewing onto a newly acquired satellite is charged in
            full even though nothing moved.

        Divide delivered bits by that and you get a mean rate ABOVE the link's
        peak rate, which is impossible on its face. An 18.4 Gbit transfer that
        ran 66.9 s at the 275 Mbps modcod ceiling was reported as 60 s and
        306.7 Mbps. So the rate metrics get their own denominator, measured
        where the bits are actually moved.
        """
        if not spans:
            return
        self.tx_beam_s += sum(spans)      # beam-seconds carrying
        self.tx_wall_s += max(spans)      # wall time with >=1 link carrying

    def note_session_start(self, sat_id: str, station_id: str):
        """A (re)acquisition. Counts a handover only when the station changes."""
        self.sessions_started[sat_id] += 1
        prev = self.last_station[sat_id]
        if prev is not None and prev != station_id:
            self.handovers[sat_id] += 1
        self.last_station[sat_id] = station_id

    def note_decision(self, wall_seconds: float):
        self.decision_times.append(wall_seconds)

    def note_peak(self, total_busy_beams: int):
        self.peak_busy_beams = max(self.peak_busy_beams, total_busy_beams)

    def note_energy(self, power_w: float, dt: float):
        self.energy_j += power_w * dt

    def note_interruption(self):
        self.interruptions += 1

    def note_recovery(self, seconds: float):
        self.recovery_times.append(seconds)

    def note_proactive_handover(self):
        self.proactive_handovers += 1

    def note_link(self, sinr_lin: float, inr: float, rate_bps: float):
        """One served link, one step. Called from the rate computation."""
        self.link_samples += 1
        self.inr_sum += inr
        if sinr_lin > 0:
            self.sinr_db_sum += 10.0 * math.log10(sinr_lin)
            if math.log2(1.0 + sinr_lin) >= MAX_SPECTRAL_EFF:
                self.capped_samples += 1
        if rate_bps <= 0:
            self.outage_samples += 1

    # --- finalise ---
    def finalize(self, sim_time: float) -> Results:
        self.sim_time = sim_time
        total_delivered = sum(self.delivered.values())

        # completion: delivered essentially all of the initial demand
        completed = sum(
            1 for s in self.sat_ids
            if self.backlog0[s] > 0 and self.delivered[s] >= self.backlog0[s] - 1.0
        )
        n_demand = sum(1 for s in self.sat_ids if self.backlog0[s] > 0)
        completion_rate = completed / n_demand if n_demand else 1.0

        waits = [self.wait_time[s] for s in self.sat_ids if self.backlog0[s] > 0]
        mean_wait = sum(waits) / len(waits) if waits else 0.0

        latencies = [
            self.completed_t[s] - (self.ready_since[s] or 0.0)
            for s in self.sat_ids if self.completed_t[s] is not None
        ]
        mean_latency = sum(latencies) / len(latencies) if latencies else 0.0

        total_beam_seconds = sum(self.num_beams[st] for st in self.station_ids) * sim_time
        beam_util = (sum(self.beam_busy_time.values()) / total_beam_seconds
                     if total_beam_seconds else 0.0)
        station_util = (sum(self.station_active_time.values())
                        / (len(self.station_ids) * sim_time)) if sim_time and self.station_ids else 0.0
        # Occupancy (above) and carrying time (here) are different questions; see
        # note_tx_step. Utilisation keeps the occupancy numbers so every published
        # result reproduces; the rate metrics use the carrying ones.
        active_tx_s, beam_busy_s = self.tx_wall_s, self.tx_beam_s

        # SLA: completed on or before deadline (sats without a deadline are trivially met)
        sla_ok, sla_n = 0, 0
        for s in self.sat_ids:
            if self.backlog0[s] <= 0:
                continue
            sla_n += 1
            dl = self.deadline[s]
            done_t = self.completed_t[s]
            if dl is None:
                sla_ok += 1
            elif done_t is not None and done_t <= dl:
                sla_ok += 1
        sla_compliance = sla_ok / sla_n if sla_n else 1.0

        fairness = self._jain([self.delivered[s] for s in self.sat_ids if self.backlog0[s] > 0])

        # data that never got downlinked within the sim (buffer overflow / missed
        # pass). Session-level analog of packet loss; true BER-based loss comes with
        # the detailed RF layer later.
        total_demand = sum(self.backlog0[s] for s in self.sat_ids if self.backlog0[s] > 0)
        dropped = sum(max(0.0, self.backlog0[s] - self.delivered[s])
                      for s in self.sat_ids if self.backlog0[s] > 0)
        drop_rate = dropped / total_demand if total_demand else 0.0

        # Decision latency is a real-time constraint, not a curiosity: the
        # controller has `decision_interval_s` to answer, every interval. A mean
        # hides the one solve that blew the budget, so report the tail too.
        dts = self.decision_times
        mean_decision_ms = (sum(dts) / len(dts) * 1e3) if dts else 0.0
        max_decision_ms = (max(dts) * 1e3) if dts else 0.0
        p50_decision_ms = self._pct(dts, 0.50) * 1e3
        p99_decision_ms = self._pct(dts, 0.99) * 1e3

        total_handovers = sum(self.handovers.values())
        total_reacquisitions = sum(max(0, n - 1) for n in self.sessions_started.values())
        peak_beam_util = self.peak_busy_beams / self.total_beams if self.total_beams else 0.0

        energy_kj = self.energy_j / 1e3
        # energy efficiency: gigabits delivered per kilojoule of transmit energy
        gb_per_kj = (total_delivered / 1e9) / energy_kj if energy_kj > 0 else 0.0

        summary = {
            # Three different rates, and conflating them is how a 67-second
            # 18.4 Gbit transfer got reported at 141 Mbps.
            #
            #   throughput_mbps  delivered / the WHOLE run, dead time included.
            #                    A network-level figure: it answers "what did
            #                    this network carry per second of operation",
            #                    which is the right question for a scenario
            #                    comparison and the wrong one for one request.
            #   mean_rate_mbps   delivered / seconds a link was actually up.
            #                    What an operator means by "how fast did my
            #                    transfer go".
            #   link_rate_mbps   delivered / beam-seconds. Per-beam rate, so it
            #                    does not inflate when several beams run at once.
            "throughput_mbps": (total_delivered / sim_time) / 1e6 if sim_time else 0.0,
            "mean_rate_mbps": (total_delivered / active_tx_s) / 1e6 if active_tx_s else 0.0,
            "link_rate_mbps": (total_delivered / beam_busy_s) / 1e6 if beam_busy_s else 0.0,
            "active_tx_s": active_tx_s,
            "beam_busy_s": beam_busy_s,
            "delivered_gbit": total_delivered / 1e9,
            "completion_rate": completion_rate,
            "mean_wait_s": mean_wait,
            "mean_latency_s": mean_latency,
            "beam_utilization": beam_util,
            "peak_beam_utilization": peak_beam_util,
            "station_utilization": station_util,
            "sla_compliance": sla_compliance,
            "fairness": fairness,
            "drop_rate": drop_rate,
            "dropped_gbit": dropped / 1e9,
            "handovers": total_handovers,
            "reacquisitions": total_reacquisitions,
            "mean_decision_ms": mean_decision_ms,
            "p50_decision_ms": p50_decision_ms,
            "p99_decision_ms": p99_decision_ms,
            "max_decision_ms": max_decision_ms,
            "energy_kj": energy_kj,
            "gb_per_kj": gb_per_kj,
            "sessions_interrupted": self.interruptions,
            "mean_recovery_s": (sum(self.recovery_times) / len(self.recovery_times)
                                if self.recovery_times else 0.0),
            "proactive_handovers": self.proactive_handovers,
            "mean_sinr_db": (self.sinr_db_sum / self.link_samples
                             if self.link_samples else 0.0),
            "mean_inr": self.inr_sum / self.link_samples if self.link_samples else 0.0,
            "modcod_capped_frac": (self.capped_samples / self.link_samples
                                   if self.link_samples else 0.0),
            "link_outage_frac": (self.outage_samples / self.link_samples
                                 if self.link_samples else 0.0),
            "link_samples": self.link_samples,
        }
        per_sat = {
            s: {
                "delivered_gbit": self.delivered[s] / 1e9,
                "demand_gbit": self.backlog0[s] / 1e9,
                "wait_s": self.wait_time[s],
                "completed_t": self.completed_t[s],
                "served_on": self.served_station_of.get(s),
            }
            for s in self.sat_ids
        }
        return Results(summary, per_sat, dict(self.delivered_by_station), dict(self.served_station_of))

    @staticmethod
    def _pct(values, q: float) -> float:
        """Nearest-rank percentile. Stdlib only, and correct for tiny samples."""
        if not values:
            return 0.0
        xs = sorted(values)
        i = min(len(xs) - 1, max(0, int(round(q * len(xs) + 0.5)) - 1))
        return xs[i]

    @staticmethod
    def _jain(x):
        x = [v for v in x]
        if not x:
            return 1.0
        s = sum(x)
        s2 = sum(v * v for v in x)
        return (s * s) / (len(x) * s2) if s2 > 0 else 1.0
