"""Planning API — submit a communication request, get a plan back.

    POST   /api/plan/network            bind the planner to a network preset
    GET    /api/plan/network            what it is bound to
    POST   /api/plan/customers          configure an account (tier, SLA, quota)
    GET    /api/plan/customers
    POST   /api/plan/objects            register a named payload
    POST   /api/plan                    QUOTE a request (books nothing)
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
from xnios.planner import (Planner, Customer, DataObject, CommRequest,
                           TimingIntent, CommPlan)

from . import presets as presets_mod

router = APIRouter(prefix="/api/plan", tags=["planning"])

DEFAULT_PRESET = "india4-nominal"
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
    _STATE["planner"] = Planner(scn, t0=0.0, horizon_s=horizon_s)
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

    satellite_id: str
    data_volume_gbit: float | None = Field(None, gt=0)
    data_object_id: str | None = None
    timing: TimingIntent = TimingIntent.ASAP
    deadline_s: float | None = None
    priority: str | None = None
    customer_id: str | None = None
    t_now: float = 0.0

    def to_request(self) -> CommRequest:
        try:
            return CommRequest(
                satellite_id=self.satellite_id,
                customer_id=self.customer_id,
                data_volume_gbit=self.data_volume_gbit,
                data_object_id=self.data_object_id,
                timing=self.timing,
                deadline_s=self.deadline_s,
                priority=self.priority,
            )
        except ValueError as e:
            raise HTTPException(422, str(e))


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
