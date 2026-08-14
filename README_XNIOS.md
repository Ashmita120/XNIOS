# X-NioS

**Physics-driven satellite communication planning and resource orchestration.**
A user submits a communication request; X-NioS decides whether it can be
accepted, when and where it should happen, and what the network must commit —
using exact orbital mechanics and an analytical capacity forecast rather than a
learned model.

```
                USER  ──►  CommunicationRequest
                                  │
                                  ▼
                          ┌───────────────┐
                          │ XNIOS PLANNER │  admission · SLA · quota
                          │               │  exact lookahead · oppcost
                          └───────┬───────┘
                                  ▼
                        CommunicationPlan
              station · timing · beam requirement · capacity
                                  │
                                  ▼
                        EXECUTOR  (scheduler, allocators)
                                  │
                                  ▼
        PHYSICS TWIN   orbit → visibility → link budget → capacity
```

ML sits **outside** this core, not inside it. Every predictive target tested so
far failed a feasibility or value gate against an analytical or trivial
baseline — see [DECISIONS.md](DECISIONS.md), which records what is settled, on
what evidence, and what would legitimately reopen it. "ML closed" means no
evidence it improves the current system, not that it is impossible.

**Status:** planner, admission control, quota, conformance and console complete.
Frequency and joint beam/frequency optimisation closed as measured nulls. The
scan envelope is the one open lever with a large simulated effect and an
unresolved physical optimum.

## Quick start

```
python run_api.py                            # planner + console at :8000
python experiments/planner_demo.py           # request -> plan, end to end
python experiments/plan_conformance.py       # 5 structural checks + execution
python experiments/phase1_validation.py      # 13/13, "SIMULATOR VALIDATED"
python experiments/telemetry_validation.py   # 40/40, "TELEMETRY LAYER VALIDATED"
```

Only dependency for the core: **numpy**. `xnios/telemetry.py` and
`xnios/health.py` are stdlib-only; the planner adds scipy for the reference
oracle only, which is not in the live path.

## What decides what

| Question | Answered by | How |
|---|---|---|
| Can this request be accepted? | `planner.plan` | capacity forecast vs commitment ledger |
| Which station, which window? | `planner.plan` | contact windows + cumulative-bits curves |
| Which request goes first? | `planner.plan_batch` | `oppcost`, re-scored after every booking |
| When does it complete? | `Pass.time_for_bits` | inverse of the capacity curve |
| Which satellite switches station? | `simulator` | `handover_mode="capacity"` |
| What channel? | `allocators.GraphColorFreq` | at execution, per step — provably optimal here |
| How far from optimal? | `request_oracle` | MILP reference, research/CI only |

## Experiments

Each is runnable and writes to `experiments/results/`.

```
experiments/realtime_benchmark.py    budget · policy-vs-oracle · levers
experiments/scan_envelope.py         Model A/B beam broadening sweep
experiments/multirequest_control.py  positive control: is there a decision gap?
experiments/policy_ladder.py         how much of the gap each rule closes
experiments/beam_freq_control.py     3B-0: does frequency choice decide anything?
experiments/plan_conformance.py      does the executor honour the plan?
```

## The UI (point-and-click)

```
streamlit run app.py
```

Opens in your browser. Set the world in the sidebar (satellite count, stations,
beams, frequency, bandwidth, data volumes, weather, sim length), pick the scheduling
policies to compare, and click **Run experiment** — you get the KPI table, bar-chart
comparisons, a throughput-vs-SLA Pareto scatter, and per-satellite detail. It's a
thin front-end over the same engine; no simulator code is involved.

## Define your own experiment (no code / CLI)

Everything is also set in a JSON config. Edit `configs/example.json` (or copy it):

```
python run_experiment.py --config configs/example.json --seeds 6      # baseline table
python run_experiment.py --config configs/example.json --scheduler edf/strongest
python run_experiment.py --config configs/example.json --seeds 6 --oracle   # + % of optimal
```

**Optimal-throughput oracle** ([xnios/oracle.py](xnios/oracle.py)): an offline LP
(scipy/HiGHS) that computes the max data any scheduler could deliver with perfect
foresight — the ceiling. Every policy then reports its **% of optimal** (best
heuristics ≈ 84%). `--oracle` on the CLI, or the "Compare against optimal" checkbox
in the UI. (Uses scipy, not OR-Tools — see requirements.txt for why.)

Units are human-friendly: frequency in GHz, bandwidth in MHz, data in Gbit, power in
W. Stations take an explicit `lat`/`lon` or `place_under` a satellite plane. Set
`num_beams > 1` for a multi-satellite (proto phased-array) station.

---

## The one architectural rule

The **simulator never decides anything** — it only propagates physics and reports
consequences. Every decision maker is a pluggable `Scheduler` behind one interface:

```python
class Scheduler:
    def decide(self, state: NetworkState) -> list[Assignment]: ...
```

So every experiment is: *hold the scenario fixed, swap the scheduler, compare the
same KPI vector.* Rule-based, MIP, and RL policies all see the identical
`NetworkState` and return the identical `Assignment` list.

Because the objective is **multi-objective (Pareto)**, metrics are recorded as a
*vector* (throughput, latency, wait, utilisation, SLA, fairness) and never collapsed
to a single score inside the sim. Scalarisation weights belong to a scheduler/reward,
applied at analysis time.

---

## Module map

```
xnios/
  entities.py    Satellite, GroundStation, OrbitElements   (static config)
  orbit.py       circular-LEO propagation + elevation/azimuth/range  (swap → Skyfield later)
  link.py        RF link budget → achievable data rate      (Friis + G/T + rain fade → Shannon)
  weather.py     weather state → rain fade (dB)             (v0: static; Phase 9: stochastic)
  state.py       NetworkState / SatView / VisibilityView / Assignment   (the world↔policy contract)
  schedulers.py  Scheduler ABC + GreedyScheduler grid + FCFS/Priority/EDF/SJF presets
  metrics.py     MetricsCollector → Results — KPI vector: throughput, completion, SLA,
                 wait, latency, beam/station util (avg+peak), data-dropped, handovers,
                 fairness, decision-time (real-time feasibility)
  simulator.py   time-stepped engine; sticky sessions; snapshot() for rule tests
  scenarios.py   E1–E4 builders + heterogeneous_scenario() (diverse demand/deadlines/
                 stations) + congested_/random_scenario() for sweeps
experiments/
  phase1_validation.py   E1–E4 with asserted expected behaviour
```

### Design choices baked in (v0)
- **LEO**, synthetic-but-correct circular orbits (no TLE dependency yet).
- **Single-beam MVP** — one beam serves one satellite; no inter-beam interference yet.
- **Sticky sessions** — a session runs until the buffer drains or the pass ends
  (no thrashing when `decide` is called every step).

---

## The GreedyScheduler grid (covers plan Phases 2 & 3 today)

```python
from xnios.schedulers import GreedyScheduler
GreedyScheduler(order_key="edf", station_key="highest_elev")
```

| axis | values | plan phase |
|------|--------|-----------|
| `order_key`   | `fcfs`, `priority`, `edf`, `sjf`, `ljf`, `random` | Phase 2 (which satellite) |
| `station_key` | `nearest`, `highest_elev`, `strongest`, `least_loaded`, `random` | Phase 3 (which station) |

**Optimisation schedulers** (scipy, no OR-Tools) fill the gap between greedy and the
oracle: `hungarian/throughput`, `hungarian/priority` (exact optimal assignment via the
Hungarian algorithm), and `mip` (same optimum via a MILP — slower, but the framework
that extends to constraints Hungarian can't express). Finding: on realistic scenarios
the online *assignment* sub-problem is easy, so these edge greedy only slightly — the
oracle gap is mostly **foresight**, and the biggest optimisation win is the **LP
bandwidth allocator** (`lp`, ≈ +9% vs `maxrate`).

## Resource allocation — the extra axes ([allocators.py](xnios/allocators.py))

A scheduler decides *who/where*; allocators decide *how much*. Two axes, same plug-in
shape, so a run is **scheduler × bandwidth-allocator × power-allocator**:

- **Bandwidth** (`equal` / `priority` / `demand` / `maxrate`) — divides a station's
  `bandwidth_hz` pool across the beams sharing it.
- **Power** (`fixed` / `adaptive` / `minenergy`) — sets each link's transmit power
  (up to the satellite's `tx_power_max_w`), trading throughput against **energy**
  (new KPIs: `energy_kj`, `gb_per_kj`). `adaptive` boosts weak/rain-faded links;
  `fixed` is most energy-efficient.

```
python run_experiment.py --config configs/bandwidth_demo.json --allocator maxrate \
       --power-allocator adaptive --seeds 3
```

- **Frequency** (`same` / `coloring`) — assigns channels to a **phased-array** station's
  beams so angularly-close beams don't interfere (graph colouring). Only matters for
  `phased_array` stations with `n_channels > 1`.

Bandwidth allocation only bites when a station's pool is scarce vs. its beams' demand
(multi-beam / low `bandwidth_mhz`) — see `configs/bandwidth_demo.json`. In the UI, pick
several of each to compare; rows become `scheduler + bw + power + freq`.

## Realistic network conditions (opt-in)

- **Dynamic weather** — `weather.provider: "dynamic"` evolves each station via a Markov
  chain (clear↔cloudy↔rain↔storm) so link quality changes mid-pass.
- **Station / beam failures** ([dynamics.py](xnios/dynamics.py)) — `config["dynamics"]`
  with scripted `events` (`station_fail`/`recover`, `beam_fail`/`recover`, `bandwidth`)
  or `random` Poisson MTBF/MTTR. A failure kills in-progress sessions; they **re-acquire
  on healthy stations** (self-healing). New KPIs: `sessions_interrupted`,
  `mean_recovery_s`.
- **Dynamic capacity** — the same events change a station's usable beams or bandwidth
  pool over time; the scheduler adapts each epoch automatically.
- **Proactive handover** — `sim.handover: true` moves a satellite to another visible
  station *before* LOS (make-before-break, no gap); KPI `proactive_handovers`.
- Defaults are all off, so existing results/validation are unchanged. See
  `configs/failure_demo.json`. In the UI: "Weather & realism" + the handover checkbox.

## Phased-array stations & real weather

- **Phased array** (opt-in per station) models:
  - **Multiple beams** — `num_beams` electronic beams (default 4), each on a satellite.
  - **Scan loss** — gain ∝ cos(scan)^1.3 off boresight/zenith (0°→0 dB, 45°→−2 dB, 60°→−4 dB).
  - **Steering limit** — `max_scan_deg` (default 60°): a flat array reaches only elevation
    ≥ 30° (satellites lower than that are visible but unreachable).
  - **Interference** — co-channel angularly-close beams degrade each other via `C/(N+I)`.
  - **Frequency reuse** — `n_channels` + the frequency allocator; **`dual_pol`** doubles
    the reuse slots (channel × polarisation).
  - **Beam-switching delay** — `setup_time_s`: a (re)acquired beam transmits only after
    it finishes slewing.
  Default is a traditional dish (none of the above), so existing results are unchanged.
- **Real ground stations**: `configs/india.json` — 8 real Indian sites (Delhi, Bengaluru,
  Ahmedabad, Hyderabad, Guwahati, Thiruvananthapuram, Lucknow, Port Blair), G/T ≥ 22,
  phased arrays. In the **UI**, pick the "India — 8 real stations" layout to edit real
  lat/lon interactively (satellites use an India-overflying constellation).
- **Live weather** ([weather_live.py](xnios/weather_live.py)): set `weather.provider =
  "openmeteo"` — **free, no API key** — to fetch each station's current conditions by
  lat/lon at run start (verified live). `"openweathermap"` also works with a key. Any
  failure falls back to static weather. In the UI: the "Live weather" expander.

---

## Roadmap (each phase = one drop-in `Scheduler` or one sim-fidelity add)

| Plan phase | What to add | Where it plugs in |
|-----------|-------------|-------------------|
| 2 Baseline scheduling | already covered by `order_key` | `schedulers.py` |
| 3 Station selection | already covered by `station_key` | `schedulers.py` |
| 4 Beam allocation | raise `num_beams`; add MIP/greedy beam picker | new scheduler |
| 5 Frequency alloc | ✅ done — `FreqAllocator` (same/coloring) + interference in `link.py`/sim | `allocators.py` |
| 6 Bandwidth alloc | ✅ done — `Allocator` axis (equal/priority/demand/maxrate) | `allocators.py` |
| 7 Power alloc | ✅ done — `PowerAllocator` (fixed/adaptive/minenergy) + energy KPIs | `allocators.py` |
| 8 Link selection | ✅ done — `hungarian` + `mip` schedulers, `lp` allocator (scipy) | `schedulers.py`, `allocators.py` |
| 9 Weather | ✅ done — static + **live Open-Meteo** + **dynamic (Markov)** | `weather.py`, `weather_live.py` |
| 10 Congestion | `random_scenario(n_sats=500, n_stations=20)` | `scenarios.py` |
| 11 Failures | ✅ done — station/beam failures + self-healing + recovery metrics | `dynamics.py` |
| 12 Prediction | forecast weather/demand; feed the scheduler | new `predict/` module |
| 13 Optimisation | OR-Tools receding-horizon MPC | new scheduler |
| 14 RL | wrap `Simulator` as a Gymnasium env | new `envs/` + SB3 |
| 15–17 MARL / GNN / full X-NioS | multi-agent + graph policies | new schedulers |

Add a new scheduler → run it on the **same** `scenarios` → append to the benchmark
table. That table is the research result.

---

## V2 — the AI-assisted twin

V1 answered *"what did this run deliver?"*. V2 makes the twin **observe, explain
and eventually decide**. The build order puts prediction late on purpose:

```
Telemetry ─► Features ─► Analytical forecast ─► Historical memory
                                                      │
                                          Decision engine ─► Prediction models ─► Planning
```

**Phase 1 (done): state awareness.**

| module | what it adds |
|---|---|
| [xnios/telemetry.py](xnios/telemetry.py) | one `TelemetryRecord` per step — network / stations / links / satellites / decision / events; sinks for RAM, JSONL-on-disk and live callbacks |
| [xnios/health.py](xnios/health.py) | `assess()` → Network Health, congestion, failure risk, coverage, link quality — each with the factors behind it |
| [api/](api/) | FastAPI service: runs, frames, series, CSV export, WebSocket live stream — and it serves the console too |
| [web/](web/) | operator console in the ARCTROPY design language: plain HTML/CSS/ES modules + Preact/htm from a CDN, hand-drawn SVG charts and map. No npm, no build step — `python run_api.py` is the whole stack |

```python
from xnios.telemetry import TelemetryRecorder, MemorySink
rec = TelemetryRecorder(sink=MemorySink())
Simulator(scn, sched, cfg, telemetry=rec).run()      # opt-in; default None costs nothing
```

Telemetry is a **pure observer** — with recording on, every physical KPI is
bit-identical (asserted by `experiments/telemetry_validation.py` T1), so V1
results and the 13/13 validation are untouched. Health scores live *outside* the
twin because a single score is a scalarisation, and `metrics.py` keeps the KPI
vector a vector on purpose.

Full guide, honest limits (BER is a derived uncoded-QPSK indicator; failure risk
is observed state, not a forecast) and what comes next: **[DASHBOARD.md](DASHBOARD.md)**.
```
```
