"""X-NioS digital-twin API — the dashboard's data plane.

    uvicorn api.main:app --reload --port 8000

Everything served here is derived from `xnios.telemetry` records; no simulation
logic lives in this layer. That is the point of the architecture: telemetry is
the single feed, and the dashboard, the health monitor, the future feature layer
and the future controller are all *readers* of it.

The operator console in `web/` is served by this same app (see the StaticFiles
mount at the bottom), so `python run_api.py` is the whole stack: no Node, no
build step, no second origin.

Routes
    GET  /                             the operator console (web/index.html)
    GET  /api/policies                 available schedulers/allocators
    GET  /api/presets                  scenario presets
    POST /api/runs                     start a run (returns run_id)
    GET  /api/runs                     list runs
    GET  /api/runs/{id}                status + metadata + final KPI summary
    DELETE /api/runs/{id}              drop a run
    GET  /api/runs/{id}/frame          one telemetry record + its health report
    GET  /api/runs/{id}/series         network time series for the charts
    GET  /api/runs/{id}/health         health report (latest or at a step)
    GET  /api/runs/{id}/timeline       health over the whole run
    GET  /api/runs/{id}/export/{face}  CSV of one telemetry face
    WS   /api/ws/runs/{id}             live frames as the simulation produces them
"""

from __future__ import annotations

import asyncio
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from xnios.experiment import (POLICY_CHOICES, ALLOCATOR_CHOICES,
                              POWER_ALLOCATOR_CHOICES, FREQ_ALLOCATOR_CHOICES, KPI_KEYS)
from xnios.health import assess, timeline, DEFAULT_WEIGHTS
from xnios.telemetry import to_rows, SCHEMA_VERSION

from . import presets as presets_mod
from .planning import router as planning_router
from .store import STORE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

app = FastAPI(title="X-NioS Digital Twin API", version=SCHEMA_VERSION)
# The console is same-origin now, so CORS is not needed for it. This stays open
# for the cases that are still cross-origin: index.html opened straight off the
# filesystem with `window.XNIOS_API` set, and any external client of the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)


class StartRun(BaseModel):
    preset: str = "india4-nominal"
    config: dict | None = None            # inline config overrides the preset entirely
    scheduler: str = "fcfs/strongest"
    bandwidth_allocator: str = "equal"
    power_allocator: str = "adaptive"
    freq_allocator: str = "coloring"
    pace_ms: float = Field(0.0, ge=0.0, le=1000.0)   # >0 = stream at a watchable rate
    duration_s: float | None = None
    dt_s: float | None = None


# The operational surface: request in, plan out. Registered before the static
# mount so /api/plan/* wins over the console's catch-all.
app.include_router(planning_router)


@app.get("/api/policies")
def policies() -> dict:
    """Everything the operator (and, later, the AI decision engine) may choose."""
    return {
        "schedulers": POLICY_CHOICES,
        "bandwidth_allocators": ALLOCATOR_CHOICES,
        "power_allocators": POWER_ALLOCATOR_CHOICES,
        "freq_allocators": FREQ_ALLOCATOR_CHOICES,
        "kpi_keys": KPI_KEYS,
        "health_weights": DEFAULT_WEIGHTS,
        "schema_version": SCHEMA_VERSION,
    }


@app.get("/api/presets")
def list_presets() -> list:
    return presets_mod.summary()


@app.post("/api/runs")
def start_run(req: StartRun) -> dict:
    if req.config is not None:
        config = dict(req.config)
        preset = "custom"
    else:
        all_p = presets_mod.all_presets()
        if req.preset not in all_p:
            raise HTTPException(404, f"unknown preset '{req.preset}'")
        config = dict(all_p[req.preset])
        preset = req.preset

    sim = dict(config.get("sim", {}))
    if req.duration_s is not None:
        sim["duration_s"] = req.duration_s
    if req.dt_s is not None:
        sim["dt_s"] = req.dt_s
        sim.setdefault("decision_interval_s", req.dt_s)
    config["sim"] = sim

    policy = {
        "scheduler": req.scheduler,
        "bandwidth_allocator": req.bandwidth_allocator,
        "power_allocator": req.power_allocator,
        "freq_allocator": req.freq_allocator,
    }
    run = STORE.start(preset, config, policy, pace_ms=req.pace_ms)
    return run.info()


@app.get("/api/runs")
def list_runs() -> list:
    return STORE.list()


def _run_or_404(run_id: str):
    run = STORE.get(run_id)
    if run is None:
        raise HTTPException(404, f"unknown run '{run_id}'")
    return run


@app.get("/api/runs/{run_id}")
def run_info(run_id: str) -> dict:
    return _run_or_404(run_id).info()


@app.delete("/api/runs/{run_id}")
def drop_run(run_id: str) -> dict:
    return {"deleted": STORE.delete(run_id)}


@app.get("/api/runs/{run_id}/frame")
def frame(run_id: str, step: int | None = None) -> dict:
    f = _run_or_404(run_id).frame(step)
    if f is None:
        raise HTTPException(409, "no telemetry yet")
    return f


@app.get("/api/runs/{run_id}/health")
def health(run_id: str, step: int | None = None, window: int = 1) -> dict:
    run = _run_or_404(run_id)
    recs = run.records
    if not recs:
        raise HTTPException(409, "no telemetry yet")
    idx = len(recs) - 1 if step is None else max(0, min(step, len(recs) - 1))
    lo = max(0, idx - window + 1)
    return assess(recs[lo:idx + 1]).to_dict()


@app.get("/api/runs/{run_id}/timeline")
def health_timeline(run_id: str, every: int = 5) -> list:
    run = _run_or_404(run_id)
    return [{"t": r.t, "network_health": r.network_health, "level": r.level,
             "congestion": r.indicators["congestion"].score,
             "failure_risk": r.indicators["failure_risk"].score,
             "coverage": r.indicators["coverage"].score,
             "link_quality": r.indicators["link_quality"].score,
             "availability": r.indicators["availability"].score}
            for r in timeline(run.records, every=every)]


@app.get("/api/runs/{run_id}/series")
def series(run_id: str,
           fields: str = "t,throughput_bps,beam_utilization,queue_bits,"
                         "bits_delivered_total,energy_j_total,mean_sinr_db,"
                         "contention_ratio,n_waiting,beams_active,coverage",
           every: int = 1) -> dict:
    run = _run_or_404(run_id)
    return run.series([f.strip() for f in fields.split(",") if f.strip()], every)


@app.get("/api/runs/{run_id}/export/{face}.csv")
def export(run_id: str, face: str) -> StreamingResponse:
    """Download one telemetry face as CSV — the same table the feature layer will
    consume, so the dataset is inspectable before any model is trained on it."""
    import csv

    run = _run_or_404(run_id)
    rows = to_rows(run.records, face, run_id=run_id)
    if not rows:
        raise HTTPException(404, f"no rows for face '{face}'")
    keys = list({k: None for row in rows for k in row}.keys())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    for row in rows:
        w.writerow({k: (v if not isinstance(v, (list, dict)) else str(v))
                    for k, v in row.items()})
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-{face}.csv"'})


@app.websocket("/api/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str, from_step: int = Query(0)):
    """Live frames. Sends the run's metadata, replays anything already produced
    from `from_step`, then follows the simulation to completion."""
    await ws.accept()
    run = STORE.get(run_id)
    if run is None:
        await ws.send_json({"type": "error", "message": f"unknown run '{run_id}'"})
        await ws.close()
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)

    class _Bridge:
        """Thread-safe hand-off from the simulator worker to the event loop."""

        def put_nowait(self, item):
            loop.call_soon_threadsafe(_safe_put, queue, item)

    def _safe_put(q, item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass                                  # UI is behind; drop, never stall the sim

    bridge = _Bridge()
    run.subscribe(bridge)
    try:
        await ws.send_json({"type": "meta", "run": run.info()})

        sent = from_step
        while sent < len(run.records):            # catch up on what already exists
            f = run.frame(sent)
            await ws.send_json({"type": "frame", **f})
            sent += 1

        while True:
            if run.status in ("done", "error") and sent >= len(run.records):
                break
            try:
                record = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if record is None:                    # sentinel from the worker
                continue
            while sent < len(run.records):
                await ws.send_json({"type": "frame", **run.frame(sent)})
                sent += 1

        await ws.send_json({"type": "end", "run": run.info()})
    except WebSocketDisconnect:
        pass
    finally:
        run.unsubscribe(bridge)


@app.get("/api/healthz")
def healthz() -> dict:
    return {"ok": True, "schema_version": SCHEMA_VERSION, "runs": len(STORE.runs)}


# --------------------------------------------------------------------------
# The console.
#
# Mounted LAST on purpose: Starlette matches routes in registration order, so
# every /api/* route above wins and this only catches what is left. `html=True`
# serves index.html for "/".
#
# web/ is plain HTML/CSS/ES modules — nothing to build, nothing to install — so
# there is no dist/ step between editing a file and reloading the page.
# --------------------------------------------------------------------------
class _NoCacheStatic(StaticFiles):
    """Serve the console with `no-cache`.

    There is no bundler here, so no content-hashed filenames either — the browser
    sees `/js/map.js` forever and will happily keep an old copy of an ES module
    across a normal reload, which reads as "my change did nothing". `no-cache`
    still allows a 304 (the ETag does the work), so nothing is re-downloaded
    unless it actually changed; it only forbids using a cached copy *without*
    asking. Edit a file, reload, see the change.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        response_headers.setdefault("cache-control", "no-cache")
        return super().is_not_modified(response_headers, request_headers)

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = "no-cache"
        return response


if os.path.isdir(WEB):
    app.mount("/", _NoCacheStatic(directory=WEB, html=True), name="console")
