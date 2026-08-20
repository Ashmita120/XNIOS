"""Run registry — executes simulations off the request thread and holds their
telemetry so the API and the WebSocket can both read it.

A run is started, the simulator executes in a worker thread writing into a
`MemorySink`, and every produced record is also handed to any subscribed
WebSocket. `pace_ms` deliberately slows the loop so the dashboard sees a live
feed: the twin runs a 30-minute scenario in well under a second, which is
correct for research and useless for watching.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler
from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from xnios.simulator import Simulator
from xnios.telemetry import (TelemetryRecorder, MemorySink, MultiSink, CallbackSink,
                             TelemetrySink, to_rows)
from xnios.health import assess


class PaceSink(TelemetrySink):
    """Sleep between records so a fast simulation streams at a watchable rate."""

    def __init__(self, ms: float):
        self.s = max(0.0, ms / 1000.0)

    def write(self, record) -> None:
        if self.s:
            time.sleep(self.s)


@dataclass
class Run:
    run_id: str
    preset: str
    config: dict
    policy: dict
    kind: str = "scenario"            # scenario (engineering) | plan (an executed booking)
    status: str = "queued"            # queued | running | done | error
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    recorder: TelemetryRecorder | None = None
    summary: dict | None = None
    total_steps: int = 0
    subscribers: list = field(default_factory=list)   # list[queue-like]
    notes: dict = field(default_factory=dict)   # anything the caller wants surfaced
    # injected world/decision-maker for a plan run; None = build from config
    _scenario_fn: object = None
    _scheduler_fn: object = None
    _on_done: object = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- read side ---------------------------------------------------------
    @property
    def records(self) -> list:
        return self.recorder.records if self.recorder else []

    @property
    def progress(self) -> float:
        return (len(self.records) / self.total_steps) if self.total_steps else 0.0

    def info(self) -> dict:
        meta = self.recorder.meta if self.recorder else None
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "preset": self.preset,
            "name": self.config.get("name", self.preset),
            "status": self.status,
            "error": self.error,
            "created": self.created,
            "finished": self.finished,
            "steps": len(self.records),
            "total_steps": self.total_steps,
            "progress": round(self.progress, 4),
            "policy": self.policy,
            "summary": self.summary,
            "notes": self.notes,
            "meta": _meta_dict(meta),
        }

    def frame(self, step: int | None = None) -> dict | None:
        """One record plus its health assessment — the dashboard's unit of update."""
        recs = self.records
        if not recs:
            return None
        idx = len(recs) - 1 if step is None else max(0, min(step, len(recs) - 1))
        r = recs[idx]
        return {"record": r.to_dict(), "health": assess(r).to_dict(),
                "index": idx, "steps": len(recs), "total_steps": self.total_steps}

    def series(self, fields: list, every: int = 1) -> dict:
        rows = to_rows(self.records, "network")
        rows = rows[::max(1, every)]
        keys = fields or ["t", "throughput_bps", "beam_utilization", "queue_bits"]
        return {k: [row.get(k) for row in rows] for k in keys}

    # -- write side --------------------------------------------------------
    def _publish(self, record) -> None:
        with self._lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(record)
            except Exception:
                pass                       # slow/closed consumer: drop, never block the sim

    def subscribe(self, q) -> None:
        with self._lock:
            self.subscribers.append(q)

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)


def _meta_dict(meta) -> dict | None:
    if meta is None:
        return None
    from dataclasses import asdict
    d = asdict(meta)
    d.pop("config", None)                  # too big for a status poll
    return d


class RunStore:
    """In-process registry of runs. Deliberately not a database: Historical
    Memory (V2 Phase 4) is a separate, durable layer — this is just the live
    working set the dashboard is looking at."""

    def __init__(self, max_runs: int = 12):
        self.runs: dict = {}
        self.max_runs = max_runs
        self._lock = threading.Lock()

    def list(self) -> list:
        return [r.info() for r in sorted(self.runs.values(),
                                         key=lambda r: r.created, reverse=True)]

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def delete(self, run_id: str) -> bool:
        return self.runs.pop(run_id, None) is not None

    def start(self, preset: str, config: dict, policy: dict,
              pace_ms: float = 0.0, capture=None,
              scenario_fn=None, scheduler_fn=None, kind: str = "scenario",
              on_done=None) -> Run:
        """Start a run.

        `scenario_fn` / `scheduler_fn` let a caller inject the world and the
        decision maker instead of building them from `config` and a policy
        name. That is how a *booked plan* becomes a run: the planner supplies a
        scenario carrying only the promised demand and a `PlanScheduler` that
        follows the ledger, and everything downstream — telemetry, the frame
        endpoint, the WebSocket — works unchanged because a plan run is just a
        run.

        `kind` marks which it is, so the console can tell an operator's own
        execution apart from an engineering scenario. `on_done(run)` fires once
        the worker finishes, which is how a plan run reconciles its ledger.
        """
        run_id = uuid.uuid4().hex[:12]
        sim_cfg = config.get("sim", {})
        run = Run(run_id=run_id, preset=preset, config=config, policy=policy,
                  kind=kind,
                  total_steps=int(round(sim_cfg.get("duration_s", 1200)
                                        / sim_cfg.get("dt_s", 5))))
        run._scenario_fn = scenario_fn
        run._scheduler_fn = scheduler_fn
        run._on_done = on_done
        with self._lock:
            self.runs[run_id] = run
            if len(self.runs) > self.max_runs:      # evict the oldest finished run
                old = sorted((r for r in self.runs.values() if r.status in ("done", "error")),
                             key=lambda r: r.created)
                if old:
                    self.runs.pop(old[0].run_id, None)

        sinks = [MemorySink(), CallbackSink(run._publish)]
        if pace_ms:
            sinks.append(PaceSink(pace_ms))
        run.recorder = TelemetryRecorder(sink=MultiSink(*sinks), run_id=run_id,
                                         config=config,
                                         capture=capture or ("network", "stations", "links",
                                                             "satellites", "decision", "events"))
        threading.Thread(target=self._execute, args=(run,), daemon=True).start()
        return run

    @staticmethod
    def _execute(run: Run) -> None:
        run.status = "running"
        try:
            cfg = sim_config_from_config(run.config)
            scn = (run._scenario_fn() if run._scenario_fn
                   else scenario_from_config(run.config))
            p = run.policy
            sched = (run._scheduler_fn() if run._scheduler_fn
                     else make_scheduler(p.get("scheduler", "fcfs/strongest")))
            sim = Simulator(
                scn, sched, cfg,
                allocator=make_allocator(p.get("bandwidth_allocator", "equal")),
                power_allocator=make_power_allocator(p.get("power_allocator", "fixed")),
                freq_allocator=make_freq_allocator(p.get("freq_allocator", "coloring")),
                telemetry=run.recorder,
            )
            res = sim.run()
            run.summary = dict(res.summary)
            run.status = "done"
        except Exception as exc:                   # surface it, never kill the server
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            run.finished = time.time()
            if run._on_done:
                try:
                    run._on_done(run)
                except Exception:                  # a hook must never fail a run
                    traceback.print_exc()
            run._publish(None)                     # sentinel: stream complete


STORE = RunStore()
