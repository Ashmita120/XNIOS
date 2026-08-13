"""Run a config-defined experiment with one or more schedulers.

    python run_experiment.py --config configs/example.json
    python run_experiment.py --config configs/example.json --seeds 5
    python run_experiment.py --config configs/example.json --scheduler edf/strongest

Edit configs/example.json (or copy it) to set YOUR experiment: satellite count,
stations, beams, frequency, bandwidth, data volumes, weather, sim length.
"""

from __future__ import annotations

import argparse

from xnios.config import load_config, sim_config_from_config, scenario_from_config
from xnios.experiment import POLICY_CHOICES, run_policies, run_with_oracle

# display columns: (KPI key, label, format)
COLS = [
    ("delivered_gbit", "Deliv(Gb)", "{:.1f}"),
    ("completion_rate", "Compl%", "{:.0%}"),
    ("sla_compliance", "SLA%", "{:.0%}"),
    ("drop_rate", "Drop%", "{:.0%}"),
    ("mean_wait_s", "Wait(s)", "{:.0f}"),
    ("beam_utilization", "BeamU%", "{:.0%}"),
    ("fairness", "Fair", "{:.2f}"),
    ("energy_kj", "En(kJ)", "{:.0f}"),
    ("gb_per_kj", "Gb/kJ", "{:.1f}"),
    ("sessions_interrupted", "Intr", "{:.0f}"),
    ("mean_recovery_s", "Recov", "{:.0f}"),
    ("proactive_handovers", "HO", "{:.0f}"),
    ("mean_decision_ms", "Dec(ms)", "{:.3f}"),
]

DEFAULT_POLICIES = [
    "random", "fcfs/strongest", "priority/strongest", "edf/strongest",
    "sjf/strongest", "priority/nearest", "priority/least_loaded",
]


def main():
    ap = argparse.ArgumentParser(description="Run a config-defined X-NioS experiment.")
    ap.add_argument("--config", required=True, help="path to a JSON/YAML scenario config")
    ap.add_argument("--seeds", type=int, default=1,
                    help="number of seeds to average (0..N-1); default 1 (uses config seed)")
    ap.add_argument("--scheduler", default=None,
                    help="run a single policy e.g. 'edf/strongest'; default runs the baseline set")
    ap.add_argument("--oracle", action="store_true",
                    help="also compute the optimal-throughput ceiling and each policy's %% of it")
    ap.add_argument("--allocator", default="equal",
                    help="bandwidth allocator: equal / priority / demand / maxrate")
    ap.add_argument("--power-allocator", dest="power_allocator", default="fixed",
                    help="power allocator: fixed / adaptive / minenergy")
    ap.add_argument("--freq-allocator", dest="freq_allocator", default="coloring",
                    help="phased-array frequency allocator: same / coloring")
    args = ap.parse_args()

    cfg = load_config(args.config)
    sim_cfg = sim_config_from_config(cfg)
    n_seeds = max(1, args.seeds)                     # run_policies uses seeds 0..N-1
    policies = [args.scheduler] if args.scheduler else DEFAULT_POLICIES

    probe = scenario_from_config({**cfg, "seed": 0})
    n_beams = sum(g.num_beams for g in probe.stations)
    print("=" * 100)
    print(f"X-NioS experiment: {cfg.get('name', args.config)}")
    print(f"  {len(probe.satellites)} satellites / {len(probe.stations)} stations "
          f"({n_beams} beams) | {sim_cfg.duration_s/60:.0f} min | "
          f"{n_seeds} seed(s) averaged")
    print("=" * 100)

    print(f"  bandwidth: {args.allocator} | power: {args.power_allocator} | "
          f"freq: {args.freq_allocator}\n")
    cols = list(COLS)
    if args.oracle:
        rows, oracle_gbit, _ = run_with_oracle(cfg, policies, n_seeds,
                                               allocator=args.allocator,
                                               power_allocator=args.power_allocator,
                                               freq_allocator=args.freq_allocator)
        cols.append(("pct_optimal", "%Opt", "{:.0%}"))
        print(f"optimal-throughput ceiling (LP oracle): {oracle_gbit:.1f} Gb\n")
    else:
        rows, _ = run_policies(cfg, policies, n_seeds, allocator=args.allocator,
                               power_allocator=args.power_allocator,
                               freq_allocator=args.freq_allocator)
    by_name = {r["policy"]: r for r in rows}

    header = f"{'policy':<20}" + "".join(f"{lbl:>10}" for _, lbl, _ in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['policy']:<20}" + "".join(fmt.format(r[k]).rjust(10) for k, _, fmt in cols))

    print("-" * len(header))
    if len(rows) > 1:
        print(f"best throughput : {max(by_name, key=lambda n: by_name[n]['delivered_gbit'])}")
        print(f"best SLA        : {max(by_name, key=lambda n: by_name[n]['sla_compliance'])}")
        print(f"lowest wait     : {min(by_name, key=lambda n: by_name[n]['mean_wait_s'])}")


if __name__ == "__main__":
    main()
