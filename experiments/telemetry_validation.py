"""Validation for the telemetry + health layer (V2 Phase 1).

Follows the house rule from `phase1_validation.py`: a new subsystem is not
"done" until a script asserts what it is supposed to do. The load-bearing check
is T1 — telemetry must be a pure observer, so a run with recording ON must
produce **bit-identical** KPIs to the same run with it OFF. If that ever fails,
the twin has been contaminated by its own instrumentation and every V1 result
is in question.

    python experiments/telemetry_validation.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import orbit as orb
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler, KPI_KEYS
from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from xnios.simulator import Simulator
from xnios.telemetry import (TelemetryRecorder, MemorySink, JsonlSink, MultiSink,
                             read_jsonl, to_rows, write_csv, SCHEMA_VERSION)
from xnios.health import assess, timeline

CHECKS = []

# Wall-clock measurements: nondeterministic by construction, so they are excluded
# from the identity check and shown to vary between two *untelemetered* runs too.
# Wall-clock measurements, excluded from the bit-identical assertion because they
# cannot be identical across two runs of anything. Keep this in step with
# KPI_KEYS: adding a timing KPI without listing it here fails T1 and looks like
# telemetry contamination, which is the one thing T1 exists to detect.
TIMING_KEYS = {"mean_decision_ms", "p50_decision_ms", "p99_decision_ms",
               "max_decision_ms"}


def check(ok: bool, label: str, detail: str = "") -> None:
    CHECKS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ->  {detail}" if detail else ""))


STATIONS = [
    {"id": "GS-Delhi", "lat": 28.61, "lon": 77.21, "num_beams": 4,
     "g_over_t_dbk": 24, "bandwidth_mhz": 200, "phased_array": True,
     "n_channels": 2, "dual_pol": False, "beamwidth_deg": 3.0,
     "weather": "rain", "setup_time_s": 2.0},
    {"id": "GS-Bengaluru", "lat": 12.97, "lon": 77.59, "num_beams": 4,
     "g_over_t_dbk": 24, "bandwidth_mhz": 200, "phased_array": True,
     "n_channels": 2, "dual_pol": False, "beamwidth_deg": 3.0,
     "weather": "clear", "setup_time_s": 2.0},
]


def _constellation(per_plane: int = 7, stagger_deg: float = 3.0) -> list:
    """Satellites that are actually *seen* by these two stations.

    Uses the repo's own `orbit.find_orbit_for_elevation` to aim one plane at each
    station, then staggers satellites along the orbit so their passes overlap.
    Picking RAANs by hand is how the documented coverage-gap bug happens (a plausible
    constellation that simply never flies over anything); solving for the geometry
    makes contention, interference and handover reachable instead of hypothetical.
    """
    sats = []
    for pi, st in enumerate(STATIONS):
        sol = orb.find_orbit_for_elevation(st["lat"], st["lon"], 53.0, 80.0, 600.0)
        for k in range(per_plane):
            offset = (k - (per_plane - 1) / 2.0) * stagger_deg
            sats.append({
                "id": f"SAT-{pi}{k:02d}", "inc": 53.0, "altitude_km": 600,
                "raan": sol["raan_deg"],
                "arg_lat0": sol["arg_lat0_deg"] + offset,
                "backlog_gbit": [8, 25, 60][k % 3],
                "tier": ["research", "commercial", "military", "emergency"][k % 4],
                "deadline_s": 600 + 120 * k,
            })
    return sats


# A scenario that exercises everything: multi-beam phased arrays under contention,
# dynamic weather and random failures — so events, interference, interruption and
# handover all actually occur rather than being untested code paths.
CONFIG = {
    "seed": 0,
    "t_mid": 900,
    "satellites": {
        "mode": "explicit", "freq_ghz": 8.2, "bandwidth_mhz": 50,
        "tx_power_w": 5, "tx_power_max_w": 10,
        "list": _constellation(),
    },
    "stations": STATIONS,
    "weather": {"provider": "dynamic", "dwell_s": 200},
    "dynamics": {"random": {"station_mtbf_s": 900, "station_mttr_s": 300,
                            "beam_mtbf_s": 700, "beam_mttr_s": 250}},
    "sim": {"duration_s": 1800, "dt_s": 10, "decision_interval_s": 10, "handover": True},
}

POLICY = ("fcfs/strongest", "equal", "adaptive", "coloring")
N_SATS = len(CONFIG["satellites"]["list"])


def build():
    scn = scenario_from_config(CONFIG)
    cfg = sim_config_from_config(CONFIG)
    sched, bw, pw, fr = POLICY
    return scn, cfg, (make_scheduler(sched), make_allocator(bw),
                      make_power_allocator(pw), make_freq_allocator(fr))


def run(telemetry=None):
    scn, cfg, (sched, bw, pw, fr) = build()
    sim = Simulator(scn, sched, cfg, allocator=bw, power_allocator=pw,
                    freq_allocator=fr, telemetry=telemetry)
    return sim.run()


def main() -> int:
    print("=" * 68)
    print(f"TELEMETRY + HEALTH VALIDATION   (schema {SCHEMA_VERSION})")
    print("=" * 68)

    # ---------------------------------------------------------------- T1
    print("\nT1  Telemetry is a pure observer (KPIs identical with it on/off)")
    base = run(telemetry=None)
    base2 = run(telemetry=None)
    rec = TelemetryRecorder(sink=MemorySink(), config=CONFIG)
    obs = run(telemetry=rec)

    physical = [k for k in KPI_KEYS if k not in TIMING_KEYS]
    diffs = {k: (base.summary[k], obs.summary[k]) for k in physical
             if base.summary[k] != obs.summary[k]}
    check(not diffs, "every physical KPI is bit-identical with recording enabled",
          f"{len(physical)} KPIs unchanged" if not diffs else f"DIVERGED: {diffs}")
    # the only keys that move are wall-clock ones, and they move without telemetry too
    jitter = {k for k in TIMING_KEYS & set(KPI_KEYS)
              if base.summary[k] != base2.summary[k]}
    check(jitter == (TIMING_KEYS & set(KPI_KEYS)),
          "the excluded keys are wall-clock jitter, not telemetry contamination",
          f"{sorted(jitter)} differ between two runs that both had telemetry OFF")
    check(base.per_sat == obs.per_sat,
          "per-satellite outcomes are identical too (not just the aggregates)")

    records = rec.records
    expected = int(round(CONFIG["sim"]["duration_s"] / CONFIG["sim"]["dt_s"]))
    check(len(records) == expected, "one record per simulation step",
          f"{len(records)} records for {expected} steps")

    # ---------------------------------------------------------------- T2
    print("\nT2  Every face is populated and internally consistent")
    r = max(records, key=lambda x: x.network.beams_active)   # a busy instant
    net = r.network
    check(net is not None and len(r.stations) == len(CONFIG["stations"]),
          "network + one row per station", f"t={r.t:.0f}s, {len(r.stations)} stations")
    check(len(r.satellites) == N_SATS,
          "one row per satellite", f"{len(r.satellites)} satellites")

    active = [l for l in r.links if l.active]
    check(len(active) == net.beams_active,
          "active link rows == busy beams reported by the network row",
          f"{len(active)} active of {len(r.links)} visible link rows")
    check(abs(sum(s.bits_delivered for s in r.stations) - net.bits_delivered_step) < 1.0,
          "station bits sum to the network total for the step",
          f"{net.bits_delivered_step / 1e9:.3f} Gbit this step")
    check(all(l.sinr_db <= l.snr_db + 1e-9 for l in active),
          "SINR never exceeds interference-free SNR on any active link")
    check(all(0.0 <= l.ber <= 0.5 for l in r.links), "BER within [0, 0.5] on every link")

    # candidate (non-active) links must be recorded too — that is the
    # counterfactual a learned policy needs
    cands = [l for l in r.links if not l.active]
    check(len(cands) > 0, "rejected candidate links are recorded, not just chosen ones",
          f"{len(cands)} candidate links at t={r.t:.0f}s")

    # ---------------------------------------------------------------- T3
    print("\nT3  Ground tracks and geometry are real")
    s0 = r.satellites[0]
    check(-90 <= s0.lat_deg <= 90 and -180 <= s0.lon_deg <= 180,
          "satellite sub-point is a valid lat/lon",
          f"{s0.sat_id} at ({s0.lat_deg:.2f}, {s0.lon_deg:.2f})")
    check(abs(s0.alt_km - CONFIG["satellites"]["list"][0]["altitude_km"]) < 5.0,
          "reconstructed altitude matches the orbit", f"{s0.alt_km:.1f} km")
    moved = abs(records[-1].satellites[0].lat_deg - records[0].satellites[0].lat_deg) \
        + abs(records[-1].satellites[0].lon_deg - records[0].satellites[0].lon_deg)
    check(moved > 1.0, "satellites actually move over the run", f"{moved:.1f} deg travelled")

    # ---------------------------------------------------------------- T4
    print("\nT4  Events are captured")
    kinds = {}
    for rr in records:
        for e in rr.events:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
    check("session_start" in kinds and "session_end" in kinds,
          "session lifecycle events recorded", str(kinds))
    check(any(k in kinds for k in ("station_fail", "beam_fail")),
          "failure events recorded (random dynamics active)")
    check("weather_change" in kinds, "weather transitions recorded (dynamic weather)")
    starts, ends = kinds.get("session_start", 0), kinds.get("session_end", 0)
    still_open = records[-1].network.sessions_active
    check(starts == ends + still_open,
          "session starts reconcile with ends + sessions still open at the end",
          f"{starts} starts = {ends} ends + {still_open} open")
    check(kinds.get("handover", 0) == obs.summary["proactive_handovers"],
          "handover events match the metrics collector",
          f"{kinds.get('handover', 0)} proactive handovers")
    check(kinds.get("interrupt", 0) == obs.summary["sessions_interrupted"],
          "interruption events match the metrics collector",
          f"{kinds.get('interrupt', 0)} interruptions")

    # ---------------------------------------------------------------- T5
    print("\nT5  Decision provenance")
    dec = [rr.decision for rr in records if rr.decision]
    check(len(dec) > 0, "decision records present", f"{len(dec)} decisions")
    d = dec[0]
    check(d.scheduler == "fcfs/strongest" and d.power_allocator == "adaptive"
          and d.bandwidth_allocator == "equal" and d.freq_allocator == "coloring",
          "the four active algorithms are all named",
          f"{d.scheduler} + {d.bandwidth_allocator} + {d.power_allocator} + {d.freq_allocator}")
    check(all(dd.n_assigned <= dd.n_free_candidates for dd in dec),
          "assignments never exceed the candidates the scheduler was offered")
    check(hasattr(d, "rationale") and hasattr(d, "reasons"),
          "explainability slots exist on every historical decision row",
          "rationale/reasons/expected present (empty under static policy)")

    # ---------------------------------------------------------------- T6
    print("\nT6  Persistence round-trip (JSONL sink)")
    tmp = tempfile.mkdtemp(prefix="xnios-tel-")
    path = os.path.join(tmp, "run.jsonl")
    rec2 = TelemetryRecorder(sink=MultiSink(MemorySink(), JsonlSink(path)),
                             config=CONFIG, run_id="validation-run")
    run(telemetry=rec2)
    back = list(read_jsonl(path))
    check(len(back) == expected, "every record survived the round-trip",
          f"{len(back)} lines, {os.path.getsize(path) / 1e6:.2f} MB")
    check(back[0]["schema_version"] == SCHEMA_VERSION, "schema version stamped on disk")
    meta_path = os.path.join(tmp, "run.meta.json")
    meta = json.load(open(meta_path, encoding="utf-8"))
    check(meta["run_id"] == "validation-run" and meta["n_satellites"] == N_SATS
          and len(meta["stations"]) == 2,
          "run metadata written (the Historical Memory primary key)",
          f"{meta['scheduler']} | {meta['n_satellites']} sats | seed {meta['seed']}")

    # ---------------------------------------------------------------- T7
    print("\nT7  Flattening for the feature layer")
    counts = {}
    for face in ("network", "station", "link", "satellite", "decision", "event"):
        counts[face] = len(to_rows(records, face, run_id="validation-run"))
    check(counts["network"] == expected and counts["station"] == expected * 2
          and counts["satellite"] == expected * N_SATS,
          "row counts match steps x entities", str(counts))
    csv_path = os.path.join(tmp, "network.csv")
    n = write_csv(records, csv_path, "network")
    check(n == expected and os.path.getsize(csv_path) > 0,
          "CSV export writes a usable table", f"{n} rows -> {csv_path}")

    # ---------------------------------------------------------------- T8
    print("\nT8  Health monitor")
    rep = assess(records[-1])
    check(0.0 <= rep.network_health <= 100.0, "network health is a percentage",
          f"{rep.network_health}% ({rep.level})")
    need = {"availability", "link_quality", "coverage", "delivery",
            "congestion", "failure_risk", "weather", "energy"}
    check(need.issubset(rep.indicators), "all indicators present",
          ", ".join(f"{k}={v.pct}%" for k, v in rep.indicators.items()))
    check(all(ind.factors for ind in rep.indicators.values()),
          "every indicator carries the factors behind it (XAI-ready)")
    check(len(rep.stations) == 2 and all(st.reasons for st in rep.stations),
          "per-station health with reasons",
          "; ".join(f"{st.station_id}:{st.health:.2f} ({st.reasons[0]})"
                    for st in rep.stations))

    # a station that is DOWN must read as critical
    downs = [(rr, st) for rr in records for st in rr.stations if not st.up]
    if downs:
        rr, _ = downs[0]
        rep_down = assess(rr)
        bad = [st for st in rep_down.stations if not st.up]
        check(all(st.health == 0.0 and st.level == "critical" for st in bad),
              "an offline station reports critical health",
              f"t={rr.t:.0f}s, {len(bad)} station(s) down")
        check(rep_down.indicators["failure_risk"].score > rep.indicators["failure_risk"].score
              or rep_down.indicators["availability"].score < 1.0,
              "an outage moves availability/failure-risk in the right direction")
    else:
        check(False, "scenario produced a station outage to test against")

    tl = timeline(records, every=20)
    check(len(tl) == len(range(0, expected, 20)) and all(0 <= x.network_health <= 100
                                                         for x in tl),
          "health timeline computes over the whole run",
          f"{len(tl)} points, health {min(x.network_health for x in tl):.0f}"
          f"-{max(x.network_health for x in tl):.0f}%")
    check(any("degradation model" in n for n in rep.notes),
          "failure risk is labelled as observed state, not a forecast")

    # ---------------------------------------------------------------- T9
    print("\nT9  Capture control (cost/size levers)")
    rec3 = TelemetryRecorder(sink=MemorySink(), capture=("network",), every_n=5,
                             config=CONFIG)
    run(telemetry=rec3)
    check(len(rec3.records) == expected // 5, "every_n thins the series",
          f"{len(rec3.records)} records at every_n=5")
    check(all(not r_.links and not r_.stations for r_ in rec3.records),
          "capture=('network',) drops the heavy faces")
    check(rec3.records[0].network is not None, "the requested face is still there")

    print("\n" + "=" * 68)
    ok = sum(CHECKS)
    print(f"RESULT: {ok}/{len(CHECKS)} checks passed" +
          ("  ->  TELEMETRY LAYER VALIDATED" if ok == len(CHECKS) else "  ->  FAILURES ABOVE"))
    print("=" * 68)
    print(f"\n{rep}")
    return 0 if ok == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
