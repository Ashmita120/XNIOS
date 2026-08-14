"""Optimal allocation of competing communication requests (the Phase 2 reference).

`oracle.py` maximises delivered bits and knows nothing about who asked, when
they need it, or what they are owed — its objective is `max sum d[i]`, with no
term for a deadline, a tier or a completed request. It cannot score a policy
that trades throughput for SLA, so it cannot be the reference for a study about
priority. This is that reference.

    x[r,g,t]  1 if request r is served by station g in slot t     (binary)
    d[r]      data delivered to request r                          (continuous)
    met[r]    1 only if r was delivered IN FULL                    (binary)

    s.t.  d[r]      <= sum_{g,t} x[r,g,t] * bits[r,g,t]     can't deliver unsent
          d[r]      <= volume[r]                            no credit for overshoot
          met[r]*volume[r] <= d[r]                          partial is not "met"
          sum_r x[r,g,t] <= beams[g]        for each g,t     station capacity
          sum_{r in sat s} sum_g x[r,g,t] <= 1  for s,t      one link per satellite

Only slots inside a request's deadline become variables, so "delivered" here
always means "delivered in time" and no separate lateness term is needed.

Two objectives, because the interesting question is the tension between them:

    throughput   max sum d[r]                  the old ceiling, restated
    priority     max sum w[r] * met[r]         weighted COMPLETION, not volume

`priority` is deliberately about whole requests. Half a 40 Gbit transfer usually
has no operational value, and an objective summing bits would happily shave 20%
off four jobs rather than finish three — which is exactly the behaviour a tier
system exists to prevent. A small epsilon on delivered volume breaks ties
between equally-weighted solutions.

Solving both is the point: if they disagree, there is a real decision to make
and a policy can be judged on which side of it lands. If they agree, the
priority machinery has nothing to do.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
from scipy.sparse import lil_matrix

__all__ = ["RequestOracleResult", "optimal_allocation"]


@dataclass
class RequestOracleResult:
    delivered_gbit: float = 0.0
    met: dict = field(default_factory=dict)          # request_id -> fully delivered?
    delivered: dict = field(default_factory=dict)    # request_id -> gbit
    weighted_met: float = 0.0                        # sum w*met / sum w
    n_met: int = 0
    objective: str = ""
    optimal: bool = False
    status: str = ""
    solve_ms: float = 0.0
    n_vars: int = 0
    n_slots: int = 0


def _slot_bits(look, sat_id: str, station_id: str, t0: float, t1: float) -> float:
    """Data deliverable on this link during [t0, t1], across whatever passes overlap."""
    total = 0.0
    for p in look.passes.get((sat_id, station_id), ()):
        if p.t_set <= t0:
            continue
        if p.t_rise >= t1:
            break
        total += p.bits_until(t0, t1)
    return total


def optimal_allocation(look, stations, requests, t_now: float = 0.0,
                       slot_s: float = 30.0, objective: str = "priority",
                       time_limit_s: float = 60.0) -> RequestOracleResult:
    """Best achievable allocation of `requests` over the network.

    `requests` is a list of dicts with keys: request_id, satellite_id,
    volume_gbit, deadline_s, weight. `look` is a built `Lookahead`; `stations`
    maps station_id -> GroundStation.
    """
    if not requests:
        return RequestOracleResult(objective=objective, optimal=True)

    horizon = max(r["deadline_s"] for r in requests)
    n_slots = max(1, int(np.ceil((horizon - t_now) / slot_s)))
    beams = {gid: g.num_beams for gid, g in stations.items()}

    # ---- enumerate the (request, station, slot) triples that can carry data ----
    triples = []                       # (r_idx, station_id, slot, gbit)
    gt_rows: dict = {}                 # (station, slot) -> row
    st_rows: dict = {}                 # (satellite, slot) -> row
    for ri, r in enumerate(requests):
        last = min(n_slots, int(np.ceil((r["deadline_s"] - t_now) / slot_s)))
        for s in range(last):
            a = t_now + s * slot_s
            b = min(a + slot_s, r["deadline_s"])
            if b <= a:
                continue
            for gid in stations:
                bits = _slot_bits(look, r["satellite_id"], gid, a, b)
                if bits <= 0:
                    continue
                triples.append((ri, gid, s, bits / 1e9))
                gt_rows.setdefault((gid, s), len(gt_rows))
                st_rows.setdefault((r["satellite_id"], s), len(st_rows))

    K, R = len(triples), len(requests)
    if K == 0:
        return RequestOracleResult(objective=objective, optimal=True,
                                   status="no usable contact", n_slots=n_slots,
                                   met={r["request_id"]: False for r in requests},
                                   delivered={r["request_id"]: 0.0 for r in requests})

    # layout: [x_0..x_{K-1}] [d_0..d_{R-1}] [met_0..met_{R-1}]
    #
    # `met` only exists when the objective needs it. Under `throughput` it is
    # inert — zero objective coefficient, constrained but unread — and carrying
    # R extra binaries plus a badly scaled row made HiGHS fail outright on the
    # severe-contention instances. Completion is derived from `d` afterwards
    # either way, so nothing is lost by dropping the variables.
    use_met = objective == "priority"
    n_var = K + R + (R if use_met else 0)
    i_d, i_met = K, K + R

    n_rows = len(gt_rows) + len(st_rows) + R + (R if use_met else 0)
    A = lil_matrix((n_rows, n_var))
    lb = np.full(n_rows, -np.inf)
    ub = np.zeros(n_rows)
    b_gt, b_st, b_del, b_met = 0, len(gt_rows), len(gt_rows) + len(st_rows), \
        len(gt_rows) + len(st_rows) + R

    for k, (ri, gid, s, gbit) in enumerate(triples):
        A[b_gt + gt_rows[(gid, s)], k] = 1.0                       # station capacity
        A[b_st + st_rows[(requests[ri]["satellite_id"], s)], k] = 1.0   # one link/sat
        A[b_del + ri, k] = -gbit                                   # d_r - sum bits*x <= 0
    for (gid, _s), row in gt_rows.items():
        ub[b_gt + row] = beams[gid]
    for _key, row in st_rows.items():
        ub[b_st + row] = 1.0
    for ri, r in enumerate(requests):
        A[b_del + ri, i_d + ri] = 1.0
        ub[b_del + ri] = 0.0
        if use_met:
            # met_r - d_r/volume_r <= 0: met can only be 1 on a full delivery.
            # Normalised by volume so the row's coefficients stay near unity —
            # the unnormalised form put hundreds of Gbit against -1 and HiGHS
            # returned failure on the large instances.
            A[b_met + ri, i_met + ri] = 1.0
            A[b_met + ri, i_d + ri] = -1.0 / float(r["volume_gbit"])
            ub[b_met + ri] = 0.0

    c = np.zeros(n_var)
    if objective == "throughput":
        c[i_d:i_d + R] = -1.0
    elif objective == "priority":
        for ri, r in enumerate(requests):
            c[i_met + ri] = -float(r["weight"])
        c[i_d:i_d + R] = -1e-6                  # tie-break toward moving more data
    else:
        raise ValueError(f"unknown objective: {objective}")

    integrality = np.zeros(n_var)
    integrality[:K] = 1
    if use_met:
        integrality[i_met:] = 1

    var_lb = np.zeros(n_var)
    var_ub = np.ones(n_var)
    for ri, r in enumerate(requests):
        var_ub[i_d + ri] = float(r["volume_gbit"])

    t0 = time.perf_counter()
    res = milp(c, constraints=LinearConstraint(A.tocsr(), lb, ub),
               integrality=integrality, bounds=Bounds(var_lb, var_ub),
               options={"time_limit": time_limit_s})
    solve_ms = (time.perf_counter() - t0) * 1e3

    if res.x is None:
        return RequestOracleResult(objective=objective, optimal=False,
                                   status=getattr(res, "message", "no solution"),
                                   solve_ms=solve_ms, n_vars=n_var, n_slots=n_slots,
                                   met={r["request_id"]: False for r in requests},
                                   delivered={r["request_id"]: 0.0 for r in requests})

    d = res.x[i_d:i_d + R]
    # Completion is read off the delivered volume, not the binary, so both
    # objectives are scored the same way and `met` never disagrees with `d`.
    met = {r["request_id"]: bool(d[ri] >= float(r["volume_gbit"]) - 1e-6)
           for ri, r in enumerate(requests)}
    delivered = {r["request_id"]: float(max(0.0, d[ri])) for ri, r in enumerate(requests)}
    w_total = sum(float(r["weight"]) for r in requests) or 1.0
    return RequestOracleResult(
        delivered_gbit=float(sum(delivered.values())),
        met=met, delivered=delivered,
        weighted_met=sum(float(r["weight"]) for r in requests if met[r["request_id"]]) / w_total,
        n_met=sum(met.values()),
        objective=objective, optimal=(res.status == 0),
        status=getattr(res, "message", ""), solve_ms=solve_ms,
        n_vars=n_var, n_slots=n_slots)
