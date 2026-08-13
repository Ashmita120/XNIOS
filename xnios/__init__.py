"""X-NioS digital twin — a measurable simulation world for satellite/ground-station
scheduling and resource-allocation experiments.

Build order (matches the research plan): entities -> orbit -> link -> weather ->
state -> metrics -> schedulers -> simulator -> scenarios.

The golden rule: the simulator (the "world") never decides anything. Every decision
maker is a pluggable `Scheduler`. Experiments = hold the scenario fixed, swap the
scheduler, compare the same KPI vector.
"""

from .entities import OrbitElements, Satellite, GroundStation
from .state import NetworkState, SatView, StationView, VisibilityView, Assignment
from .schedulers import (
    Scheduler,
    RandomScheduler,
    GreedyScheduler,
    FCFS,
    PriorityScheduler,
    EDF,
    SJF,
)
from .simulator import Simulator, SimConfig
from .metrics import MetricsCollector, Results

__all__ = [
    "OrbitElements", "Satellite", "GroundStation",
    "NetworkState", "SatView", "StationView", "VisibilityView", "Assignment",
    "Scheduler", "RandomScheduler", "GreedyScheduler", "FCFS", "PriorityScheduler",
    "EDF", "SJF",
    "Simulator", "SimConfig",
    "MetricsCollector", "Results",
]
