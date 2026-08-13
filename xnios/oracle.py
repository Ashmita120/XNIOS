"""Offline optimal-throughput oracle (the upper bound).

Computes the maximum total data that ANY scheduler could possibly downlink for a
scenario, given perfect foresight of every contact window and link rate. This is
the ceiling: every online policy's delivered / oracle gives its "% of optimal".

Formulation (time-indexed MILP, solved with scipy/HiGHS — no external solver):
  slots      : the horizon is cut into `slot_s`-second slots
  x[i,s,t]   : 1 if satellite i uses station s in slot t   (binary)
  d[i]       : data delivered to satellite i               (<= its backlog)
  maximise   sum_i d[i]
  s.t.       d[i] <= sum_{s,t} x[i,s,t] * bits[i,s,t]       (can't deliver more than sent)
             sum_i x[i,s,t] <= beams[s]     for each s,t    (station beam capacity)
             sum_s x[i,s,t] <= 1            for each i,t     (a sat uses one link at a time)

Only visible (bits>0) triples become variables, so the model stays small. The
oracle has strictly MORE freedom than the sticky online engine (perfect foresight,
free re-assignment, no setup cost), so its value is a valid upper bound.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
from scipy.sparse import lil_matrix

from . import orbit as orb
from .link import achievable_rate_bps


@dataclass
class OracleResult:
    delivered_gbit: float = 0.0
    per_sat_gbit: dict = field(default_factory=dict)
    status: str = ""
    optimal: bool = False
    solve_ms: float = 0.0
    slot_s: float = 0.0
    n_vars: int = 0


def optimal_throughput(scenario, duration_s: float, slot_s: float = 15.0,
                       time_limit_s: float = 20.0, integer: bool = False) -> OracleResult:
    """integer=False (default) solves the LP relaxation: a fast, rigorous UPPER
    bound on the achievable throughput. integer=True solves the exact MILP (slower,
    may hit the time limit on large scenarios)."""
    import time

    sats = scenario.satellites
    stations = scenario.stations
    weather = scenario.weather
    n_slots = max(1, int(round(duration_s / slot_s)))

    gs_ecef = {g.id: orb.gs_position_ecef(g.lat_deg, g.lon_deg, g.alt_km) for g in stations}
    backlog_g = {s.id: s.backlog_bits / 1e9 for s in sats}

    # --- enumerate visible (sat, station, slot) triples -> one binary var each ---
    triples = []                 # list of (i_idx, station_id, t_idx, bits_gbit)
    st_rows = {}                 # (station_id, t) -> row index (capacity)
    it_rows = {}                 # (i_idx, t) -> row index (single link)
    sat_index = {s.id: i for i, s in enumerate(sats)}

    for t in range(n_slots):
        t_mid = (t + 0.5) * slot_s
        pos = {s.id: orb.sat_position_ecef(s.orbit, t_mid) for s in sats}
        for s in sats:
            i = sat_index[s.id]
            for g in stations:
                elev, _az, rng = orb.elevation_azimuth_range(
                    gs_ecef[g.id], g.lat_deg, g.lon_deg, pos[s.id])
                if elev < g.elevation_mask_deg:
                    continue
                # ceiling assumes the best any allocator could do: max transmit power
                # (bandwidth is already capped at the satellite's own bandwidth)
                rate = achievable_rate_bps(rng, elev, s, g,
                                           rain_zenith_db=weather.fade_db(g.id, t_mid),
                                           tx_power_w=s.tx_power_max_w)
                if rate <= 0:
                    continue
                bits_g = rate * slot_s / 1e9
                k = len(triples)
                triples.append((i, g.id, t, bits_g))
                st_rows.setdefault((g.id, t), len(st_rows))
                it_rows.setdefault((i, t), len(it_rows))

    K = len(triples)
    n_sat = len(sats)
    if K == 0:
        return OracleResult(0.0, {s.id: 0.0 for s in sats}, "no visibility", True, 0.0, slot_s, 0)

    # variable layout: [x_0..x_{K-1}] (binary) then [d_0..d_{n_sat-1}] (continuous)
    n_var = K + n_sat
    beams = {g.id: g.num_beams for g in stations}

    n_rows = len(st_rows) + len(it_rows) + n_sat
    A = lil_matrix((n_rows, n_var))
    lb = np.full(n_rows, -np.inf)
    ub = np.empty(n_rows)

    base_st = 0
    base_it = len(st_rows)
    base_del = len(st_rows) + len(it_rows)

    # station capacity + single-link rows
    for k, (i, gid, t, bits_g) in enumerate(triples):
        A[base_st + st_rows[(gid, t)], k] = 1.0
        A[base_it + it_rows[(i, t)], k] = 1.0
        A[base_del + i, k] = -bits_g            # d_i - sum bits*x <= 0
    for (gid, t), r in st_rows.items():
        ub[base_st + r] = beams[gid]
    for _key, r in it_rows.items():
        ub[base_it + r] = 1.0
    for i in range(n_sat):
        A[base_del + i, K + i] = 1.0
        ub[base_del + i] = 0.0

    # objective: maximise sum d  ->  minimise -sum d
    c = np.zeros(n_var)
    c[K:] = -1.0

    integrality = np.zeros(n_var)
    if integer:
        integrality[:K] = 1                      # x binary (exact MILP); else LP relaxation

    var_lb = np.zeros(n_var)
    var_ub = np.ones(n_var)
    for i, s in enumerate(sats):
        var_ub[K + i] = backlog_g[s.id]          # d_i <= backlog

    t0 = time.perf_counter()
    res = milp(c, constraints=LinearConstraint(A.tocsr(), lb, ub),
               integrality=integrality, bounds=Bounds(var_lb, var_ub),
               options={"time_limit": time_limit_s})
    solve_ms = (time.perf_counter() - t0) * 1e3

    if res.x is None:
        return OracleResult(0.0, {s.id: 0.0 for s in sats},
                            getattr(res, "message", "no solution"), False, solve_ms, slot_s, n_var)

    d = res.x[K:]
    per_sat = {s.id: float(max(0.0, d[i])) for i, s in enumerate(sats)}
    return OracleResult(
        delivered_gbit=float(sum(per_sat.values())),
        per_sat_gbit=per_sat,
        status=getattr(res, "message", ""),
        optimal=(res.status == 0),
        solve_ms=solve_ms,
        slot_s=slot_s,
        n_vars=n_var,
    )
