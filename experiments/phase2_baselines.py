"""Phase 2/3 - Baseline scheduling & station-selection comparison.

Research question: on an identical world, which classical policy wins on which KPI?
This is the template for every future experiment: fix the scenario(s), swap the
scheduler, tabulate the same multi-objective KPI vector. AI must beat this table.

Run:  python experiments/phase2_baselines.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xnios import scenarios
from xnios.simulator import Simulator, SimConfig
from xnios.schedulers import GreedyScheduler, RandomScheduler

# a CONTENDED, HETEROGENEOUS world: diverse demand/deadlines/stations, so ordering
# AND station-selection policies face genuinely different decisions.
N_WORLDS = 6
SCENARIOS = [scenarios.heterogeneous_scenario(seed=s) for s in range(N_WORLDS)]
CFG = SimConfig(duration_s=1200, dt_s=5)

KPIS = [
    ("delivered_gbit", "Deliv(Gb)", "{:.1f}"),
    ("completion_rate", "Compl%", "{:.0%}"),
    ("sla_compliance", "SLA%", "{:.0%}"),
    ("drop_rate", "Drop%", "{:.0%}"),
    ("mean_wait_s", "Wait(s)", "{:.0f}"),
    ("beam_utilization", "BeamU%", "{:.0%}"),
    ("fairness", "Fair", "{:.2f}"),
    ("mean_decision_ms", "Dec(ms)", "{:.3f}"),
]


def avg_over_scenarios(policy_factory):
    """Fresh scheduler per scenario (RNG state), average the KPI vector."""
    acc = {k: 0.0 for k, _, _ in KPIS}
    for scn in SCENARIOS:
        res = Simulator(scn, policy_factory(), CFG).run()
        for k, _, _ in KPIS:
            acc[k] += res.summary[k]
    return {k: v / len(SCENARIOS) for k, v in acc.items()}


FACTORIES = {
    "random":             lambda: RandomScheduler(),
    "fcfs/strongest":     lambda: GreedyScheduler("fcfs", "strongest"),
    "priority/strongest": lambda: GreedyScheduler("priority", "strongest"),
    "edf/strongest":      lambda: GreedyScheduler("edf", "strongest"),
    "sjf/strongest":      lambda: GreedyScheduler("sjf", "strongest"),
    "priority/nearest":   lambda: GreedyScheduler("priority", "nearest"),
    "priority/leastload": lambda: GreedyScheduler("priority", "least_loaded"),
}


if __name__ == "__main__":
    print("=" * 78)
    print("X-NioS - Phase 2/3 baseline comparison")
    s0 = SCENARIOS[0]
    n_beams = sum(g.num_beams for g in s0.stations)
    print(f"  {N_WORLDS} heterogeneous worlds x {len(s0.satellites)} sats / "
          f"{len(s0.stations)} stations ({n_beams} beams), "
          f"{CFG.duration_s/60:.0f} min each (mean KPIs)")
    print("=" * 78)

    header = f"{'policy':<20}" + "".join(f"{lbl:>10}" for _, lbl, _ in KPIS)
    print(header)
    print("-" * len(header))

    rows = {}
    for name, fac in FACTORIES.items():
        s = avg_over_scenarios(fac)
        rows[name] = s
        line = f"{name:<20}" + "".join(fmt.format(s[k]).rjust(10) for k, _, fmt in KPIS)
        print(line)

    print("-" * len(header))
    best_deliv = max(rows, key=lambda n: rows[n]["delivered_gbit"])
    best_sla = max(rows, key=lambda n: rows[n]["sla_compliance"])
    best_wait = min(rows, key=lambda n: rows[n]["mean_wait_s"])
    print(f"best throughput : {best_deliv}")
    print(f"best SLA        : {best_sla}")
    print(f"lowest wait     : {best_wait}")
    print("\nNote: policies differ materially once demand exceeds beam capacity. This")
    print("differentiated, multi-objective table is the baseline every optimiser")
    print("(Phase 13) and RL agent (Phase 14) must beat. (Under-subscribed worlds make")
    print("all policies tie -- scheduling only matters under contention.)")
