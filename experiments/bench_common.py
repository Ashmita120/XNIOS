"""Shared helpers for the exp*_*.py benchmark scripts (see the plan / testing.md).

Factors out what every experiment script needs: a phased-array station builder, a
one-call simulate-and-collect-KPIs function, tail/fairness metrics not already in
xnios/metrics.py (computed from Results.per_sat -- no simulator changes needed), and
an incremental CSV writer (flush-per-row, so a background run survives an interruption).
"""

from __future__ import annotations

import csv
import os
import time

from xnios.experiment import KPI_KEYS
from xnios.simulator import Simulator


def phased_station(id_: str, lat: float, lon: float, **overrides) -> dict:
    """A station config dict (schema xnios.config expects) with phased-array defaults.
    Pass overrides to change any field, e.g. num_beams=2, bandwidth_mhz=50,
    phased_array=False (plain dish)."""
    base = {
        "id": id_, "lat": lat, "lon": lon,
        "num_beams": 4, "g_over_t_dbk": 24, "weather": "clear",
        "bandwidth_mhz": 500, "phased_array": True, "beamwidth_deg": 3.0,
        "n_channels": 4, "dual_pol": True, "max_scan_deg": 60, "setup_time_s": 0.05,
    }
    base.update(overrides)
    return base


def run_kpis(scn, sim_cfg, scheduler, allocator, power_allocator, freq_allocator):
    """Run one Simulator and return (flat {KPI_KEYS...,'wall_time_s'} dict, Results)."""
    t0 = time.perf_counter()
    res = Simulator(scn, scheduler, sim_cfg, allocator=allocator,
                    power_allocator=power_allocator, freq_allocator=freq_allocator).run()
    wt = time.perf_counter() - t0
    row = {k: res.summary[k] for k in KPI_KEYS}
    row["wall_time_s"] = wt
    return row, res


def starvation_pct(per_sat: dict, eps_gbit: float = 0.01) -> float:
    """Fraction of satellites WITH demand that delivered ~nothing at all."""
    with_demand = [s for s in per_sat.values() if s["demand_gbit"] > 0]
    if not with_demand:
        return 0.0
    starved = sum(1 for s in with_demand if s["delivered_gbit"] < eps_gbit)
    return starved / len(with_demand)


def p95_wait(per_sat: dict) -> float:
    waits = sorted(s["wait_s"] for s in per_sat.values())
    if not waits:
        return 0.0
    idx = min(len(waits) - 1, int(round(0.95 * (len(waits) - 1))))
    return waits[idx]


def worst_wait(per_sat: dict) -> float:
    waits = [s["wait_s"] for s in per_sat.values()]
    return max(waits) if waits else 0.0


def gini(values) -> float:
    """Gini coefficient of a list of non-negative values (0 = perfect equality, ->(n-1)/n
    = maximally unequal for n values). Standard sorted-index formula."""
    xs = sorted(v for v in values if v is not None)
    n = len(xs)
    total = sum(xs)
    if n == 0 or total == 0:
        return 0.0
    weighted = sum((i + 1) * x for i, x in enumerate(xs))
    return (2.0 * weighted) / (n * total) - (n + 1.0) / n


class CsvWriter:
    """Incremental CSV writer: open, write header, flush after every row."""

    def __init__(self, path: str, fieldnames: list):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._f = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=fieldnames)
        self._w.writeheader()
        self._f.flush()

    def write(self, row: dict) -> None:
        self._w.writerow(row)
        self._f.flush()

    def close(self) -> None:
        self._f.close()
