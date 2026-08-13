"""Experiment driver — run a set of scheduling policies over a config and collect
their mean KPI vectors. Shared by the CLI (`run_experiment.py`) and the Streamlit
UI (`app.py`) so both compute results identically. Pandas-free on purpose.
"""

from __future__ import annotations

from .config import scenario_from_config, sim_config_from_config
from .simulator import Simulator
from .schedulers import (GreedyScheduler, RandomScheduler,
                         HungarianScheduler, HorizonScheduler, MIPScheduler)
from .allocators import (make_allocator, make_power_allocator, make_freq_allocator,
                         ALLOCATORS, POWER_ALLOCATORS, FREQ_ALLOCATORS)

ALLOCATOR_CHOICES = list(ALLOCATORS.keys())              # equal / priority / demand / maxrate
POWER_ALLOCATOR_CHOICES = list(POWER_ALLOCATORS.keys())  # fixed / adaptive / minenergy
FREQ_ALLOCATOR_CHOICES = list(FREQ_ALLOCATORS.keys())    # same / coloring (phased arrays)

# KPI summary keys reported for every policy (a Pareto vector — never one score)
KPI_KEYS = [
    "delivered_gbit", "completion_rate", "sla_compliance", "drop_rate",
    "mean_wait_s", "beam_utilization", "fairness",
    "mean_decision_ms", "p99_decision_ms", "max_decision_ms",
    "energy_kj", "gb_per_kj",
    "sessions_interrupted", "mean_recovery_s", "proactive_handovers",
]

# policy strings understood by make_scheduler: greedy "<ordering>/<station>", the
# optimisation schedulers "hungarian[/throughput|priority]" and "mip", and the
# forecast-aware "horizon[/throughput|urgency|sla]"
POLICY_CHOICES = [
    "random",
    "fcfs/strongest", "priority/strongest", "edf/strongest", "sjf/strongest",
    "ljf/strongest", "priority/nearest", "priority/least_loaded", "edf/highest_elev",
    "hungarian/throughput", "hungarian/priority", "mip",
    "horizon/throughput", "horizon/urgency", "horizon/sla",
]


def make_scheduler(name: str):
    if name == "random":
        return RandomScheduler()
    if name == "mip":
        return MIPScheduler()
    if name.startswith("hungarian"):
        obj = name.split("/", 1)[1] if "/" in name else "throughput"
        return HungarianScheduler(obj)
    if name.startswith("horizon"):
        obj = name.split("/", 1)[1] if "/" in name else "urgency"
        return HorizonScheduler(obj)
    order, station = name.split("/")
    return GreedyScheduler(order, station)


def run_with_oracle(config: dict, policies, n_seeds: int = 1,
                    oracle_slot_s: float = 20.0, progress_cb=None,
                    allocator: str = "equal", power_allocator: str = "fixed",
                    freq_allocator: str = "coloring"):
    """Like run_policies, but also computes the offline optimal-throughput ceiling
    (per seed, shared across policies) and adds `pct_optimal` to each policy row.

    Returns (rows, oracle_ceiling_gbit, per_sat). rows[i]['pct_optimal'] is the
    mean of (policy delivered / oracle delivered) over seeds.
    """
    from .oracle import optimal_throughput

    sim_cfg = sim_config_from_config(config)
    alloc = make_allocator(allocator)
    palloc = make_power_allocator(power_allocator)
    falloc = make_freq_allocator(freq_allocator)
    acc = {name: {k: 0.0 for k in KPI_KEYS} for name in policies}
    pct = {name: 0.0 for name in policies}
    per_sat = {}
    oracle_total = 0.0
    total = max(1, n_seeds * (len(policies) + 1))
    step = 0

    for seed in range(n_seeds):
        scn = scenario_from_config({**config, "seed": seed})
        oracle = optimal_throughput(scn, sim_cfg.duration_s, slot_s=oracle_slot_s)
        oracle_total += oracle.delivered_gbit
        step += 1
        if progress_cb:
            progress_cb(step / total, "optimal (oracle)", seed)
        for name in policies:
            res = Simulator(scn, make_scheduler(name), sim_cfg, allocator=alloc,
                            power_allocator=palloc, freq_allocator=falloc).run()
            for k in KPI_KEYS:
                acc[name][k] += res.summary[k]
            if oracle.delivered_gbit > 0:
                pct[name] += res.summary["delivered_gbit"] / oracle.delivered_gbit
            if seed == 0:
                per_sat[name] = res.per_sat
            step += 1
            if progress_cb:
                progress_cb(step / total, name, seed)

    rows = []
    for name in policies:
        row = {"policy": name, **{k: acc[name][k] / n_seeds for k in KPI_KEYS}}
        row["pct_optimal"] = pct[name] / n_seeds
        rows.append(row)
    return rows, oracle_total / n_seeds, per_sat


def run_policies(config: dict, policies, n_seeds: int = 1, progress_cb=None,
                 allocator: str = "equal", power_allocator: str = "fixed",
                 freq_allocator: str = "coloring"):
    """Run each policy over seeds 0..n_seeds-1 and average its KPIs.

    Returns (rows, per_sat):
      rows    = list of {"policy": name, <KPI_KEYS...>} dicts (means)
      per_sat = {policy: seed-0 per-satellite detail}
    progress_cb(fraction, policy_name, seed) is called after each run if given.
    `allocator` selects the bandwidth allocator applied to every policy.
    """
    sim_cfg = sim_config_from_config(config)
    alloc = make_allocator(allocator)
    palloc = make_power_allocator(power_allocator)
    falloc = make_freq_allocator(freq_allocator)
    rows, per_sat = [], {}
    total = max(1, len(policies) * n_seeds)
    step = 0
    for name in policies:
        acc = {k: 0.0 for k in KPI_KEYS}
        for seed in range(n_seeds):
            scn = scenario_from_config({**config, "seed": seed})
            res = Simulator(scn, make_scheduler(name), sim_cfg, allocator=alloc,
                            power_allocator=palloc, freq_allocator=falloc).run()
            for k in KPI_KEYS:
                acc[k] += res.summary[k]
            if seed == 0:
                per_sat[name] = res.per_sat
            step += 1
            if progress_cb:
                progress_cb(step / total, name, seed)
        rows.append({"policy": name, **{k: acc[k] / n_seeds for k in KPI_KEYS}})
    return rows, per_sat
