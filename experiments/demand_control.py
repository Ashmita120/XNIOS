"""V2 Stage 6 — does predicted demand change a scheduling decision for the better?

    python experiments/demand_control.py --runs 12

Stage 2 asks "is it predictable?". This asks the only question that decides
whether a model ships: **does acting on the prediction improve the network?**

Per-satellite bursty demand is the sole survivor of the feasibility gate
(§15.11, R² ~0.53 @ +60 s). An R² is not a result. Four schedulers, identical in
every other respect, on the same held-out worlds:

    fcfs/strongest    the V1 baseline
    ljf/strongest     longest-queue-first — the strongest NON-PREDICTIVE policy,
                      using backlog the ground segment already telemeters
    demand (model)    orders by backlog + predicted arrivals over the horizon
    demand (ORACLE)   the same, given the TRUE future arrivals

The oracle arm is the one that decides the workstream. It is the Stage-6 analogue
of the predictability ceiling: if perfect knowledge of future demand does not beat
`ljf`, then **no predictor can**, however good its R², and demand should be
dropped regardless. Only if the oracle wins is there a prize, and only then does
the model's share of it mean anything.

`ljf` is the criterion-2 discipline applied to control: beating FCFS is easy, and
if longest-queue-first captures the same gain then the benefit came from using
backlog, not from prediction.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios.allocators import make_allocator, make_freq_allocator, make_power_allocator
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler
from xnios.schedulers import GreedyScheduler, _order, _station_score
from xnios.simulator import Simulator
from xnios.state import Assignment
from xnios.telemetry import TelemetryRecorder, MemorySink

from api.presets import all_presets

try:
    from sklearn.ensemble import HistGradientBoostingRegressor
    HAVE_SK = True
except ImportError:                                       # pragma: no cover
    HAVE_SK = False

HORIZON_S = 60.0                      # the horizon the model is best at (R^2 0.53)
LAGS = [1, 2, 3, 6, 12]
TRAFFIC = {"model": "bursty", "mean_gbit_per_hour": 20.0, "chunk_gbit": 0.04,
           "burst_ratio": 6.0, "on_dwell_s": 240.0, "off_dwell_s": 600.0}
KPIS = ["delivered_gbit", "completion_rate", "sla_compliance", "fairness",
        "mean_wait_s", "beam_utilization"]


# --------------------------------------------------------------- the scheduler

class DemandAwareScheduler(GreedyScheduler):
    """Longest-queue-first over *anticipated* backlog rather than current backlog.

    A satellite's cost of being deferred is not what it holds now but what it will
    hold when a beam next frees up — and in this constellation the next contact is
    ~96 minutes away. So the ordering key is

        backlog(t) + demand_fn(sat, t)

    where `demand_fn` returns predicted arrivals over the horizon. With
    `demand_fn` returning zero this is exactly `ljf`, which is what makes the
    comparison clean: the *only* difference between the arms is the term added.
    """

    def __init__(self, demand_fn, station_key="strongest", weight=1.0):
        super().__init__(order_key="ljf", station_key=station_key)
        self.demand_fn = demand_fn
        self.weight = weight
        self.name = "demand-aware"

    def decide(self, state):
        free = {sid: st.free_beams for sid, st in state.stations.items()}
        out = []
        ranked = sorted(
            state.free_sats(),
            key=lambda s: (-(s.backlog_bits
                             + self.weight * self.demand_fn(s.sat_id, state.t)), s.sat_id))
        for sat in ranked:
            cands = [v for v in state.visible_for(sat.sat_id) if free.get(v.station_id, 0) > 0]
            if not cands:
                continue
            best = min(cands, key=lambda v: _station_score(v, self.station_key, free, self._rng))
            out.append(Assignment(sat.sat_id, best.station_id))
            free[best.station_id] -= 1
        return out


# --------------------------------------------------------------------- worlds

def build_cfg(preset, seed, beams=None):
    """`beams` caps beams per station. Without it the scheduler is never
    constrained: with 4 stations x 4 beams there are always enough beams for every
    simultaneously-visible satellite, ordering can never change an outcome, and
    every policy ties. A control experiment has to be able to show a difference
    before its null means anything."""
    cfg = copy.deepcopy(all_presets()[preset])
    cfg["seed"] = seed
    cfg["traffic"] = {**TRAFFIC, "seed": seed}
    import random as _r
    rng = _r.Random(f"world-{seed}")
    for s in cfg.get("satellites", {}).get("list", []):
        s["arg_lat0"] = s.get("arg_lat0", 0.0) + rng.uniform(-25.0, 25.0)
        s["backlog_gbit"] = s.get("backlog_gbit", 20.0) * rng.uniform(0.6, 1.6)
    if beams is not None:
        for g in cfg.get("stations", []):
            g["num_beams"] = int(beams)
    return cfg


def run(cfg, scheduler, telemetry=True):
    scn = scenario_from_config(cfg)
    simcfg = sim_config_from_config(cfg)
    rec = TelemetryRecorder(sink=MemorySink()) if telemetry else None
    res = Simulator(scn, scheduler, simcfg,
                    allocator=make_allocator("equal"),
                    power_allocator=make_power_allocator("adaptive"),
                    freq_allocator=make_freq_allocator("coloring"),
                    telemetry=rec).run()
    return scn, simcfg, (rec.sink.records if rec else []), res


# ------------------------------------------------------ arrivals: truth + model

def arrival_series(recs):
    """Per-satellite arrivals per step, from telemetry the ground segment has.

    arrivals = Δbacklog + Δdelivered — both are downlinked housekeeping, so this
    is reconstructible operationally, not just in the simulator.
    """
    ids = [s.sat_id for s in recs[0].satellites]
    bk = {sid: [] for sid in ids}
    dl = {sid: [] for sid in ids}
    for r in recs:
        by = {s.sat_id: s for s in r.satellites}
        for sid in ids:
            s = by.get(sid)
            bk[sid].append(s.backlog_bits if s else 0.0)
            dl[sid].append(s.delivered_bits if s else 0.0)
    arr = {}
    for sid in ids:
        b, d = np.array(bk[sid], float), np.array(dl[sid], float)
        arr[sid] = np.concatenate([[0.0], np.diff(b) + np.diff(d)])
    return arr, [r.t for r in recs]


def feature_rows(arr, ts, dt, horizon):
    """(features, target) for the demand model; features use history only."""
    X, y, meta = [], [], []
    k = int(horizon / dt)
    for sid, a in arr.items():
        for i in range(len(a)):
            row = [a[i]]
            for L in LAGS:
                w = a[max(0, i - L + 1):i + 1]
                row += [float(w.mean()), float(w.max())]
            gap = 0
            for j in range(i, -1, -1):
                if a[j] > 0:
                    break
                gap += 1
            row.append(float(gap))
            X.append(row)
            y.append(float(a[i + 1:i + 1 + k].sum()) if i + k < len(a) else np.nan)
            meta.append((sid, ts[i]))
    return np.array(X, float), np.array(y, float), meta


def train_demand_model(train_cfgs, horizon):
    """Fit on TRAINING worlds only, under the baseline policy."""
    X, y = [], []
    for cfg in train_cfgs:
        _, simcfg, recs, _ = run(cfg, make_scheduler("fcfs/strongest"))
        arr, ts = arrival_series(recs)
        Xi, yi, _ = feature_rows(arr, ts, simcfg.dt_s, horizon)
        ok = np.isfinite(yi)
        X.append(Xi[ok])
        y.append(yi[ok])
    X, y = np.vstack(X), np.concatenate(y)
    m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.08, random_state=0)
    m.fit(X, y)
    return m


class LiveDemand:
    """Streams arrivals as the run proceeds and answers `demand_fn(sat, t)`.

    Only history up to `t` is ever used. `truth=True` swaps in the actual future
    arrivals — the oracle arm.
    """

    def __init__(self, arr, ts, dt, horizon, model=None, truth=False):
        self.arr, self.dt, self.k = arr, dt, int(horizon / dt)
        self.idx = {round(t, 6): i for i, t in enumerate(ts)}
        self.model, self.truth = model, truth
        self._cache = {}

    def __call__(self, sid, t):
        i = self.idx.get(round(t, 6))
        if i is None or sid not in self.arr:
            return 0.0
        a = self.arr[sid]
        if self.truth:
            return float(a[i + 1:i + 1 + self.k].sum()) if i + self.k < len(a) else 0.0
        key = (sid, i)
        if key in self._cache:
            return self._cache[key]
        row = [a[i]]
        for L in LAGS:
            w = a[max(0, i - L + 1):i + 1]
            row += [float(w.mean()), float(w.max())]
        gap = 0
        for j in range(i, -1, -1):
            if a[j] > 0:
                break
            gap += 1
        row.append(float(gap))
        v = max(0.0, float(self.model.predict(np.array([row], float))[0]))
        self._cache[key] = v
        return v


# ------------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preset", default="india4-nominal")
    ap.add_argument("--runs", type=int, default=12)
    ap.add_argument("--beams", type=int, default=1,
                    help="beams per station; low values create real contention")
    args = ap.parse_args()
    if not HAVE_SK:
        print("scikit-learn required")
        return 2

    seeds = list(range(args.runs))
    test = seeds[::3]
    train = [s for s in seeds if s not in test]
    print(f"\n  {args.preset} · train worlds {train} · held-out worlds {test}\n")

    print("  training the demand model on training worlds only ...")
    model = train_demand_model([build_cfg(args.preset, s, args.beams) for s in train], HORIZON_S)

    arms = {k: [] for k in ("fcfs", "ljf", "model", "oracle")}
    for seed in test:
        cfg = build_cfg(args.preset, seed, args.beams)
        # one baseline pass supplies the arrival series both demand arms consult
        _, simcfg, recs, res_f = run(cfg, make_scheduler("fcfs/strongest"))
        arr, ts = arrival_series(recs)
        dt = simcfg.dt_s

        _, _, _, res_l = run(cfg, make_scheduler("ljf/strongest"), telemetry=False)
        _, _, _, res_m = run(cfg, DemandAwareScheduler(
            LiveDemand(arr, ts, dt, HORIZON_S, model=model)), telemetry=False)
        _, _, _, res_o = run(cfg, DemandAwareScheduler(
            LiveDemand(arr, ts, dt, HORIZON_S, truth=True)), telemetry=False)

        for k, r in (("fcfs", res_f), ("ljf", res_l), ("model", res_m), ("oracle", res_o)):
            arms[k].append(r.summary)
        print(f"    world {seed}: fcfs {res_f.summary['delivered_gbit']:.1f} · "
              f"ljf {res_l.summary['delivered_gbit']:.1f} · "
              f"model {res_m.summary['delivered_gbit']:.1f} · "
              f"oracle {res_o.summary['delivered_gbit']:.1f} Gb")

    print(f"\n  mean over {len(test)} held-out worlds\n")
    print(f"  {'KPI':<20} {'fcfs':>10} {'ljf':>10} {'model':>10} {'oracle':>10}"
          f"   {'oracle-ljf':>11} {'model-ljf':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10}   {'-'*11} {'-'*10}")
    for k in KPIS:
        v = {a: float(np.mean([s[k] for s in arms[a]])) for a in arms}
        print(f"  {k:<20} {v['fcfs']:>10.3f} {v['ljf']:>10.3f} {v['model']:>10.3f} "
              f"{v['oracle']:>10.3f}   {v['oracle']-v['ljf']:>+11.3f} "
              f"{v['model']-v['ljf']:>+10.3f}")

    d = {a: np.array([s["delivered_gbit"] for s in arms[a]]) for a in arms}
    gain = float(np.mean(d["oracle"] - d["ljf"]))
    sd = float(np.std(d["oracle"] - d["ljf"], ddof=1)) if len(test) > 1 else 0.0
    print(f"\n  ORACLE CEILING  delivered_gbit: {gain:+.3f} Gb over ljf "
          f"(sd {sd:.3f} across worlds)")
    if gain <= max(0.0, 2 * sd / max(1, len(test)) ** 0.5):
        print("  -> perfect demand knowledge does NOT beat longest-queue-first.")
        print("     No predictor can help here; demand should be dropped as a")
        print("     control target regardless of its R^2.")
    else:
        got = float(np.mean(d["model"] - d["ljf"]))
        print(f"  -> a prize exists. The model captures {100*got/gain:.0f}% of it.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
