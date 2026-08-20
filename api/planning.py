"""Planning API — submit a communication request, get a plan back.

    POST   /api/plan/network            bind the planner to a network preset
    GET    /api/plan/network            what it is bound to
    POST   /api/plan/customers          configure an account (tier, SLA, quota)
    GET    /api/plan/customers
    POST   /api/plan/objects            register a named payload
    POST   /api/plan                    QUOTE a request (books nothing)
    POST   /api/plan/execute            RUN the booked ledger through the twin
    POST   /api/plan/{request_id}/accept   CONFIRM a quote (books capacity)
    DELETE /api/plan/{request_id}       release a booking
    GET    /api/plan/ledger             everything currently promised

Quote and confirm are separate on purpose. Admission control only means
anything if a caller can ask "could you take this?" without the asking itself
consuming the capacity — and if two callers race, the second confirm sees the
first one's booking, because both go through the same ledger.

The planner is bound to a *network*, not to a simulation run. Runs in
`api.main` are closed-loop what-if experiments; this is the operational
surface, and the two do not share state.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from xnios.config import scenario_from_config
from xnios.execution import (PlanScheduler, execution_duration_s,
                             execution_scenario, promised_by_satellite)
from xnios.planner import (Planner, Customer, DataObject, CommRequest,
                           TimingIntent, CommPlan)

from . import presets as presets_mod
from .store import STORE

router = APIRouter(prefix="/api/plan", tags=["planning"])

DEFAULT_PRESET = "india4-nominal"
WALL_BUDGET_S = 25.0        # longest a paced plan execution may take to stream
_STATE: dict = {"preset": None, "planner": None, "quotes": {}}


def _planner() -> Planner:
    """The bound planner, binding the default network on first use."""
    if _STATE["planner"] is None:
        _bind(DEFAULT_PRESET)
    return _STATE["planner"]


def _bind(preset: str, horizon_s: float = 86400.0) -> dict:
    all_p = presets_mod.all_presets()
    if preset not in all_p:
        raise HTTPException(404, f"unknown preset '{preset}'")
    scn = scenario_from_config(dict(all_p[preset]))
    planner = Planner(scn, t0=0.0, horizon_s=horizon_s)
    # One account per tier, so a freshly started server is immediately usable:
    # tier, SLA and quota render on the first request instead of "—", and the
    # batch comparison has something to arbitrate between. Replace or add your
    # own via POST /api/plan/customers.
    for tier, sla, quota in (("research", 0.95, 400.0),
                             ("commercial", 0.99, 800.0),
                             ("military", 0.999, None),
                             ("emergency", 0.9999, None)):
        planner.register_customer(Customer(
            customer_id=f"ACCT-{tier.upper()}", name=f"{tier.title()} account",
            tier=tier, sla_availability=sla, quota_gbit=quota))
    _STATE["planner"] = planner
    _STATE["preset"] = preset
    _STATE["quotes"] = {}
    return _network_info()


def _network_info() -> dict:
    p = _STATE["planner"]
    stats = p.look.stats()
    return {
        "preset": _STATE["preset"],
        "satellites": sorted(p.sats),
        "stations": [
            {"id": g.id, "lat": g.lat_deg, "lon": g.lon_deg, "beams": g.num_beams,
             "phased_array": g.phased_array, "max_scan_deg": g.max_scan_deg,
             "beamwidth_deg": g.beamwidth_deg, "n_channels": g.n_channels,
             "elevation_mask_deg": g.elevation_mask_deg}
            for g in p.stations.values()
        ],
        "horizon_s": p.horizon_s,
        "contacts_precomputed": stats["passes"],
        "horizon_build_ms": stats["build_ms"],
        "commitments": len(p.commitments),
    }


# ------------------------------------------------------------------ models
class BindNetwork(BaseModel):
    preset: str = DEFAULT_PRESET
    horizon_s: float = Field(86400.0, gt=0)


class CustomerIn(BaseModel):
    customer_id: str
    name: str = ""
    tier: str = "commercial"
    sla_availability: float = Field(0.99, ge=0.0, le=1.0)
    quota_gbit: float | None = None


class DataObjectIn(BaseModel):
    object_id: str
    satellite_id: str
    size_gbit: float = Field(..., gt=0)
    description: str = ""


class RequestIn(BaseModel):
    """What a user actually supplies. Nothing here describes the network."""

    request_id: str | None = None
    satellite_id: str
    data_volume_gbit: float | None = Field(None, gt=0)
    data_object_id: str | None = None
    timing: TimingIntent = TimingIntent.ASAP
    deadline_s: float | None = None
    priority: str | None = None
    customer_id: str | None = None
    t_now: float = 0.0

    def to_request(self) -> CommRequest:
        kw = dict(
            satellite_id=self.satellite_id,
            customer_id=self.customer_id,
            data_volume_gbit=self.data_volume_gbit,
            data_object_id=self.data_object_id,
            timing=self.timing,
            deadline_s=self.deadline_s,
            priority=self.priority,
        )
        if self.request_id:
            kw["request_id"] = self.request_id
        try:
            return CommRequest(**kw)
        except ValueError as e:
            raise HTTPException(422, str(e))


class BatchIn(BaseModel):
    """Several requests competing for the same network.

    `commit` defaults to False: the batch is planned against the live ledger and
    then rolled back, so a caller can compare policies — or simply see what
    would happen — without consuming anything. That keeps the quote/confirm
    separation that a single request already has, which matters more here, not
    less: a batch books several bookings at once.
    """

    requests: list[RequestIn] = Field(..., min_length=1, max_length=200)
    policy: str = "oppcost"
    allow_partial: bool = True
    commit: bool = False
    t_now: float = 0.0


# ------------------------------------------------------------------ routes
@router.post("/network")
def bind_network(req: BindNetwork) -> dict:
    return _bind(req.preset, req.horizon_s)


@router.get("/network")
def get_network() -> dict:
    _planner()
    return _network_info()


@router.post("/customers")
def add_customer(c: CustomerIn) -> dict:
    cust = _planner().register_customer(Customer(**c.model_dump()))
    return {"customer_id": cust.customer_id, "tier": cust.tier,
            "priority": cust.priority, "sla_availability": cust.sla_availability}


@router.get("/customers")
def list_customers() -> list:
    return [{"customer_id": c.customer_id, "name": c.name, "tier": c.tier,
             "priority": c.priority, "sla_availability": c.sla_availability,
             "quota_gbit": c.quota_gbit}
            for c in _planner().customers.values()]


@router.post("/objects")
def add_object(o: DataObjectIn) -> dict:
    obj = _planner().register_object(DataObject(**o.model_dump()))
    return {"object_id": obj.object_id, "satellite_id": obj.satellite_id,
            "size_gbit": obj.size_gbit}


@router.post("")
def quote(req: RequestIn) -> dict:
    """Plan a request without booking anything."""
    p = _planner()
    try:
        plan = p.plan(req.to_request(), t_now=req.t_now)
    except KeyError as e:
        raise HTTPException(404, str(e))
    _STATE["quotes"][plan.request_id] = plan
    return plan.to_dict()


@router.post("/batch")
def batch(req: BatchIn) -> dict:
    """Plan several competing requests together.

    This is where a tier actually arbitrates. `oppcost` scores every unbooked
    request by weight x (volume / capacity still available before its deadline)
    and re-scores after every booking, so a high-tier request whose opportunity
    is about to close outranks one that can still be served later. `fcfs` books
    in submission order, which is what a sequence of single requests does today.
    """
    p = _planner()
    if req.policy not in p.BATCH_POLICIES:
        raise HTTPException(422, f"policy must be one of {list(p.BATCH_POLICIES)}")

    saved = list(p.commitments)                  # rollback point for a dry run
    try:
        plans = p.plan_batch([r.to_request() for r in req.requests],
                             t_now=req.t_now, policy=req.policy,
                             allow_partial=req.allow_partial)
    except KeyError as e:
        p.commitments = saved
        raise HTTPException(404, str(e))

    w_total = sum(pl.priority for pl in plans) or 1
    met = [pl for pl in plans if pl.shortfall_gbit <= 1e-6 and pl.schedule]
    summary = {
        "policy": req.policy,
        "requests": len(plans),
        "fully_met": len(met),
        "partial": sum(1 for pl in plans
                       if pl.schedule and pl.shortfall_gbit > 1e-6),
        "rejected": sum(1 for pl in plans if not pl.schedule),
        "requested_gbit": sum(pl.data_volume_gbit for pl in plans),
        "scheduled_gbit": sum(pl.scheduled_gbit for pl in plans),
        # the objective the policy is actually optimising
        "weighted_completion": sum(pl.priority for pl in met) / w_total,
        "committed": req.commit,
    }

    if req.commit:
        for pl in plans:
            _STATE["quotes"][pl.request_id] = pl
    else:
        p.commitments = saved

    return {"summary": summary,
            "booked_order": [pl.request_id for pl in plans],
            "plans": [pl.to_dict() for pl in plans]}


class ExecuteIn(BaseModel):
    pace_ms: float = Field(120.0, ge=0.0, le=1000.0)   # >0 = stream at a watchable rate


@router.post("/execute")
def execute(req: ExecuteIn) -> dict:
    """Run the booked ledger through the twin.

    This is the join the system was missing. Until now the planner booked
    capacity and the simulator ran unrelated scenario presets, so an operator
    console could only ever show someone else's run. Here the accepted plan
    *becomes* the run: the world carries exactly the promised demand, and
    `PlanScheduler` follows the ledger rather than applying a policy, so the
    delivered total is attributable to the plan and nothing else.

    Returns a normal run — telemetry, frames and the WebSocket all work
    unchanged, because a plan run is just a run with `kind: "plan"`.
    """
    p = _planner()
    if not p.commitments:
        raise HTTPException(409, "nothing booked — accept a plan first")

    commitments = list(p.commitments)
    config = dict(presets_mod.all_presets()[_STATE["preset"]])
    sim = dict(config.get("sim", {}))
    dt = float(sim.get("dt_s", 5.0))
    sim["duration_s"] = execution_duration_s(commitments, dt)
    sim.setdefault("decision_interval_s", dt)
    config["sim"] = sim
    config["name"] = f"executed plan · {len({c.request_id for c in commitments})} request(s)"

    # Pacing exists so the console reads as live, but a plan whose second pass is
    # eight hours out is thousands of steps of dead time — at 120 ms each that is
    # six minutes of watching nothing happen. Cap the total wall time instead of
    # the per-step delay, so a short plan still streams and a long one does not
    # hold the operator hostage.
    steps = max(1, int(round(sim["duration_s"] / dt)))
    pace_ms = min(req.pace_ms, (WALL_BUDGET_S * 1000.0) / steps)

    base = p.scn
    run = STORE.start(
        preset=_STATE["preset"], config=config,
        policy={"scheduler": "plan-follower", "bandwidth_allocator": "equal",
                "power_allocator": "adaptive", "freq_allocator": "coloring"},
        pace_ms=pace_ms, kind="plan",
        scenario_fn=lambda: execution_scenario(base, commitments),
        scheduler_fn=lambda: PlanScheduler(commitments),
    )
    promised = promised_by_satellite(commitments)
    return {**run.info(),
            "pace_ms": round(pace_ms, 2),
            "promised_gbit": sum(promised.values()),
            "promised_by_satellite": promised,
            "requests": sorted({c.request_id for c in commitments}),
            "windows": len(commitments)}


@router.post("/{request_id}/accept")
def accept(request_id: str) -> dict:
    """Confirm a quote. Fails if it was never admissible, or already booked."""
    plan: CommPlan | None = _STATE["quotes"].get(request_id)
    if plan is None:
        raise HTTPException(404, f"no quote '{request_id}' — POST /api/plan first")
    p = _planner()
    if any(c.request_id == request_id for c in p.commitments):
        raise HTTPException(409, f"'{request_id}' is already booked")
    if not p.accept(plan):
        raise HTTPException(409, {"error": "not admissible",
                                  "decision": plan.decision.value,
                                  "reason_code": plan.reason_code})
    return {"booked": True, "request_id": request_id,
            "windows": len(plan.schedule), "gbit": plan.scheduled_gbit,
            "commitments": len(p.commitments)}


@router.delete("/{request_id}")
def release(request_id: str) -> dict:
    return {"released": _planner().release(request_id)}


@router.get("/ledger")
def ledger() -> dict:
    p = _planner()
    rows = p.ledger()
    return {"commitments": rows,
            "total_gbit": sum(r["gbit"] for r in rows),
            "by_station": {g: sum(r["gbit"] for r in rows if r["station"] == g)
                           for g in sorted({r["station"] for r in rows})}}
