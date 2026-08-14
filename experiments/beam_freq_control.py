"""3B-0: does beam/frequency choice create a decision at all?

Before holding capacity constant, before joint optimisation, before any of the
Phase 3B machinery — establish that there is something to decide. If every
reachable beam/channel configuration produces the same outcome, Phase 3B closes
here and the answer is another legitimate null.

Frequency choice only exists when two beams at one station are close enough to
interfere. `GraphColorFreq` separates beams within `2 x beamwidth` onto
different channels; if no pair is ever that close, or if there are always spare
channels, every assignment is equivalent and the allocator is decorative.

Three measurements:

  1. CONCURRENCY   how often a station forms >1 simultaneous beam at all. With
                   one beam there is no assignment to make.
  2. CONFLICT      of those instants, how often any pair falls inside the
                   interference threshold — the only case where the channel
                   choice can change anything.
  3. OUTCOME       the same run under the best and worst channel policies
                   (`coloring` vs `same`, i.e. full reuse vs none). That
                   brackets what any frequency optimiser could possibly win:
                   if the bracket is empty, so is the decision.

Run under both beam models, because Model B widens beams as they steer off
boresight (`beamwidth / cos(scan)`) and therefore manufactures conflicts that
Model A cannot see. If frequency choice matters anywhere, it matters there.

Run:  python experiments/beam_freq_control.py
"""

from __future__ import annotations

import argparse
import copy
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_benchmark import build_config, SCENARIO_PROFILES

from xnios.allocators import (FreqAllocator, GraphColorFreq, make_allocator,
                              make_freq_allocator, make_power_allocator)
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler
from xnios.simulator import Simulator

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "experiments", "results")


class ProbeFreq(FreqAllocator):
    """Wraps the real allocator and records the decision space it faced.

    The allocator is handed exactly the information a frequency optimiser would
    have — the beams and a separation predicate — so counting conflicts here
    measures the decision directly rather than inferring it from outcomes.
    """

    name = "probe"

    def __init__(self):
        self.inner = GraphColorFreq()
        self.calls = 0
        self.beam_counts = Counter()
        self.calls_multi = 0            # >1 beam: an assignment exists
        self.calls_conflict = 0         # >=1 close pair: the assignment matters
        self.conflict_pairs = 0
        self.total_pairs = 0
        self.min_sep = float("inf")

    def allocate(self, beams, n_channels, sep_fn):
        self.calls += 1
        self.beam_counts[len(beams)] += 1
        if len(beams) > 1:
            self.calls_multi += 1
            conflicted = False
            for i, a in enumerate(beams):
                for b in beams[i + 1:]:
                    sep, thresh = sep_fn(a.sat_id, b.sat_id)
                    self.total_pairs += 1
                    self.min_sep = min(self.min_sep, sep)
                    if sep < thresh:
                        self.conflict_pairs += 1
                        conflicted = True
            if conflicted:
                self.calls_conflict += 1
        return self.inner.allocate(beams, n_channels, sep_fn)


def station_steps(cfg: dict) -> int:
    """Total (station, step) pairs in a run — the honest denominator.

    `_compute_rates` only calls the frequency allocator when a station already
    has more than one beam up, so counting inside the allocator can never see
    the single-beam case. Measuring "how often is there an assignment to make"
    against the allocator's own call count would report 100% by construction.
    """
    sim_cfg = sim_config_from_config(cfg)
    return int(round(sim_cfg.duration_s / sim_cfg.dt_s)) * len(cfg["stations"])


def run(cfg: dict, freq, broadening: bool, beams: int, max_scan=None):
    c = copy.deepcopy(cfg)
    for st in c["stations"]:
        st["num_beams"] = beams
        st["beam_broadening"] = broadening
        if max_scan is not None:
            st["max_scan_deg"] = max_scan
    scn = scenario_from_config(c)
    res = Simulator(scn, make_scheduler("fcfs/strongest"), sim_config_from_config(c),
                    allocator=make_allocator("equal"),
                    power_allocator=make_power_allocator("adaptive"),
                    freq_allocator=freq).run()
    return res.summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--beams", type=int, default=4)
    ap.add_argument("--max-scan", type=float, default=None,
                    help="override the array's steering envelope (deg). Widening it "
                         "raises simultaneous visibility, which is the only thing that "
                         "can create beam/frequency decisions.")
    args = ap.parse_args()

    rows = []
    print("=" * 100)
    print(f"3B-0  does beam/frequency choice decide anything?   ({args.beams} beams/station)")
    print("=" * 100)

    for net, prof in [("india8", "congested"), ("india8", "baseline"),
                      ("global6", "congested")]:
        cfg, n_sats = build_config(net, [p for p in SCENARIO_PROFILES
                                         if p["name"] == prof][0], 0)
        for model, broad in (("A fixed-width", False), ("B broadening", True)):
            probe = ProbeFreq()
            s_col = run(cfg, probe, broad, args.beams, args.max_scan)
            s_same = run(cfg, make_freq_allocator("same"), broad, args.beams, args.max_scan)

            total_steps = station_steps(cfg)
            multi = probe.calls_multi / total_steps * 100 if total_steps else 0.0
            conf = (probe.calls_conflict / probe.calls_multi * 100
                    if probe.calls_multi else 0.0)
            span = s_col["delivered_gbit"] - s_same["delivered_gbit"]
            print(f"\n  {net}/{prof}  model {model}   ({n_sats} sats)")
            print(f"    total station-steps           : {total_steps}")
            print(f"    ... with >1 simultaneous beam : {probe.calls_multi} ({multi:.2f}%)"
                  f"   <- an assignment exists only here")
            print(f"    ... of those, any close pair  : {probe.calls_conflict} ({conf:.1f}%)"
                  f"   <- the assignment matters only here")
            print(f"    beam-count histogram          : "
                  f"{dict(sorted(probe.beam_counts.items()))}")
            print(f"    closest pair seen             : "
                  f"{probe.min_sep:.1f} deg (threshold 2 x beamwidth)")
            print(f"    delivered, full reuse         : {s_col['delivered_gbit']:.1f} Gbit "
                  f"(SINR {s_col['mean_sinr_db']:.1f} dB, INR {s_col['mean_inr']:.3f})")
            print(f"    delivered, NO reuse (worst)   : {s_same['delivered_gbit']:.1f} Gbit "
                  f"(SINR {s_same['mean_sinr_db']:.1f} dB, INR {s_same['mean_inr']:.3f})")
            print(f"    DECISION SPAN                 : {span:.1f} Gbit "
                  f"({span / max(s_same['delivered_gbit'], 1e-9) * 100:+.2f}%)")

            rows.append(dict(
                network=net, profile=prof, beam_model=model, beams=args.beams,
                station_steps=station_steps(cfg), calls_multi=probe.calls_multi,
                pct_multi=multi, calls_conflict=probe.calls_conflict,
                pct_conflict=conf, conflict_pairs=probe.conflict_pairs,
                total_pairs=probe.total_pairs, min_sep_deg=probe.min_sep,
                delivered_reuse=s_col["delivered_gbit"],
                delivered_noreuse=s_same["delivered_gbit"],
                span_gbit=span,
                sinr_reuse=s_col["mean_sinr_db"], sinr_noreuse=s_same["mean_sinr_db"],
                inr_reuse=s_col["mean_inr"], inr_noreuse=s_same["mean_inr"]))

    os.makedirs(RESULTS_DIR, exist_ok=True)
    # Tag the envelope into the filename: a --max-scan run answers a different
    # question from the default and must not overwrite it.
    tag = "" if args.max_scan is None else f"_scan{int(args.max_scan)}"
    path = os.path.join(RESULTS_DIR, f"beam_freq_control{tag}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print()
    print("=" * 100)
    print("VERDICT")
    print("=" * 100)
    worst = max(abs(r["span_gbit"]) / max(r["delivered_noreuse"], 1e-9) for r in rows)
    any_conflict = sum(r["conflict_pairs"] for r in rows)
    print(f"  conflicting beam pairs across every configuration : {any_conflict}")
    print(f"  largest gap between full reuse and no reuse       : {worst * 100:.2f}%")
    print(f"  -> {path}")


if __name__ == "__main__":
    main()
