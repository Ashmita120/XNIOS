# X-NioS benchmark sweep — real ground-station networks × scenarios × policies

Status: **designed, not yet run**. Saved here so it can be picked up in a later session without
re-deriving the design. Ask Claude to "run the benchmark sweep in testing.md" to execute it.

## Context

Goal: a systematic, reusable benchmark across the X-NioS digital twin — several **real-world
ground-station networks** (different counts, different real lat/lon, extending the India pattern
already in the repo), several **satellite counts / scenario stresses** (congestion, failures,
handover, weather), and the **greedy vs. optimization vs. allocator** policy grid — persisting
every run's **input scenario parameters and output KPI vector** as one row in a CSV, so results
are analyzable afterward without re-running anything.

This follows directly from two earlier findings in this project:
1. Walking through every module of `xnios/` (entities, orbit, link, weather, state, simulator,
   schedulers, allocators, metrics, dynamics, scenarios, oracle, config, experiment,
   weather_live) established what each scheduler/allocator/scenario knob actually does.
2. Diagnosing a poor India-preset UI run showed the earlier demo scenarios (`scenarios.py`,
   `configs/india.json`) work because satellites are tightly phased near the stations
   (`arg_lat_spread_deg` small, short duration) — a real random/global constellation needs a
   **full-orbit phase spread + a duration exceeding one orbital period** or most satellites are
   never visible at all (a coverage problem, not a scheduling one). This benchmark's constellation
   design bakes in that fix so results reflect real scheduling performance, not phasing luck.

Scope (confirmed with the user via AskUserQuestion before saving this doc):
- **Sweep size:** "Standard" (~30–60 min estimated, meant to run in the background)
- **Ground stations:** India (existing) + a new real Global-6 network
- **Oracle ceiling:** yes, once per (network × scenario) — not per policy

## New real ground-station data

Reuse the existing real Indian set (already in `xnios/config.py` / `configs/india.json`); add one
new real network for geographic contrast:

- **`india8`** — the 8 existing real Indian sites, unchanged.
- **`india4`** — a 4-station, geographically spread subset (Delhi, Bengaluru-ISTRAC, Guwahati,
  Port-Blair) — tests "different number of ground stations" within the *same* real network.
- **`global6`** — 6 new real, well-known LEO ground-station sites spanning continents and
  latitudes (polar → equatorial), approximate public coordinates, same phased-array parameters as
  the India set for a fair comparison (`num_beams=4, beamwidth=3°, n_channels=4, dual_pol=true,
  max_scan_deg=60`):

  | id | lat | lon | G/T (dB/K) | baseline weather |
  |---|---|---|---|---|
  | Svalbard | 78.23 | 15.41 | 25 | cloudy |
  | Fairbanks | 64.84 | -147.72 | 24 | clear |
  | PuntaArenas | -53.16 | -70.91 | 23 | cloudy |
  | Awarua-NZ | -46.53 | 168.38 | 24 | rain |
  | Hartebeesthoek | -25.89 | 27.68 | 26 | clear |
  | Singapore | 1.35 | 103.82 | 22 | rain |

All three networks share the **same** satellite constellation design (so only ground geography
varies, not orbital design): 4 planes — three at 53° inclination (RAAN 0/120/240) for
mid-latitude reach, one near-polar (97.6°, RAAN 60) so the high-latitude Global-6 sites get fair
coverage too — with satellites spread across the **full orbit** (`arg_lat_spread_deg=180`, not
the tight ±10° used in the earlier demo scenarios). Sim duration is **100 minutes**
(`duration_s=6000, dt_s=10`), just over one ~96.5-minute LEO period at 600 km, so satellites get a
real chance at a contact within the window.

## Scenario profiles (6)

Each isolates one stressor against `baseline`, except `stress_all` which combines everything:

| profile | sat count | dynamics (failures) | handover | weather |
|---|---|---|---|---|
| `baseline` | 1× network base (india8/global6=40, india4=20) | off | off | static (table) |
| `congested` | 2× base | off | off | static |
| `failures` | 1× base | random Poisson (`station_mtbf_s≈2000s, mttr≈600s, beam_mtbf_s≈1500s, mttr≈500s`) | off | static |
| `handover` | 1× base | off | on (`handover_lead_s=40`) | static |
| `weather_dynamic` | 1× base | off | off | dynamic (Markov, `dwell_s=300`) |
| `stress_all` | 2× base | on (same as `failures`) | on | dynamic |

Backlog distribution stays fixed (`classes=[2,20,80] Gb, weights=[.35,.4,.25]`) across profiles so
satellite-count is the only demand lever — keeps each profile a clean, isolated variable.

## Policy grid (16 combos)

- **Schedulers (4):** `fcfs/strongest` (greedy baseline), `edf/strongest` (greedy, deadline-aware
  — matters most under `congested`/`stress_all`), `hungarian/throughput` (exact instantaneous
  optimal matching), `mip` (MILP framework — same optimum as Hungarian here, but the extensible
  one)
- **Bandwidth allocator (2):** `equal`, `lp` (proven-optimal — brackets the practical range)
- **Power allocator (2):** `fixed`, `adaptive` (adaptive's value shows up specifically in the
  weather/failure scenarios — boosts rain-faded links)
- **Frequency allocator:** fixed to `coloring` (the sensible choice for every phased-array station
  here; not a central axis of this study)

4 × 2 × 2 = **16 combos**.

**Seeds: 1 per combo** (not 2 — recommended trade-off to hit the "Standard" time budget; see
below). Recalibrated runtime: 3 networks × 6 profiles × 16 combos = 288 runs. Using the observed
~62µs/(sat×station visibility eval) rate from the user's own prior run, average run cost here
(40–80 sats × 6–8 stations × 600 steps) is roughly ~10–15s/run ⇒ **288 runs ≈ 45–70 minutes**.
Going to 2 seeds would double that to ~1.5–2.5 hours — flag this if re-running with more seeds
for smoother averages; the script should take `--seeds N` so this is a one-line change once the
real per-run cost on this machine is known.

Oracle: once per (network × profile) = 18 extra LP solves (`integer=False`, `slot_s=20`) — fast,
adds only a couple of minutes total.

## New file to write: `experiments/phase_benchmark.py`

Follows the existing pattern in `experiments/phase1_validation.py` (`sys.path.insert` for repo
root, imports from `xnios`). Reuses existing library code — **no changes to `xnios/` itself**:

- `xnios.config.scenario_from_config` / `sim_config_from_config` — build each scenario from a
  plain config dict (same schema as `configs/*.json`), so station networks + scenario profiles
  are just dict fragments merged together.
- `xnios.simulator.Simulator` — run each policy combo.
- `xnios.experiment.make_scheduler` — turn a policy string (`"edf/strongest"`, `"mip"`, ...) into
  a `Scheduler` instance (already handles all four scheduler types needed).
- `xnios.allocators.make_allocator` / `make_power_allocator` / `make_freq_allocator`.
- `xnios.oracle.optimal_throughput` — the per-scenario ceiling, called directly (not via
  `run_with_oracle`, since it's wanted once per scenario, not once per policy).
- `xnios.dynamics.failure_events` — for the `failures`/`stress_all` profiles.

**Structure:**
1. `STATION_NETWORKS` — dict of network name → list of station config dicts (india8/india4 reuse
   the real values already in the repo; global6 is the new table above).
2. `SCENARIO_PROFILES` — list of dicts with the overrides from the table above.
3. `POLICY_GRID` — the 16 `(scheduler, bw_allocator, power_allocator)` tuples.
4. `build_config(network, profile, seed)` — assembles one full config dict (stations + satellite
   generation block + sim block + weather/dynamics/handover overrides).
5. Main loop: for each network × profile → build scenario once, run the oracle once, then for
   each of the 16 policy combos → run the simulator, collect `Results.summary` (all of
   `experiment.KPI_KEYS`) plus every input parameter plus `pct_optimal` (vs. that scenario's
   oracle) plus wall-clock seconds for that run, append as a CSV row.
6. **Write incrementally** (open the CSV, write the header, `csv.DictWriter.writerow` + `f.flush()`
   after every run) — so a background run that gets interrupted still leaves partial, usable
   results instead of losing everything.
7. Print a one-line progress log per run (`[42/288] global6 | stress_all | mip+lp+adaptive+coloring seed=0 -> 38.2 Gb (11.4s)`) so a background log is checkable at any time.
8. At the end, also compute and print/save a **summary** (`experiments/results/benchmark_summary.csv`):
   for each (network, profile), the best policy combo per objective (best throughput, best SLA,
   lowest wait, best fairness, best Gb/kJ, lowest drop rate) — mirrors the "Winners by objective"
   panel already in `app.py`, so it directly answers "find the best one in different scenarios."

**Output:** `experiments/results/benchmark_results.csv` (one row per run — 288 policy rows + 18
oracle-ceiling rows, ~306 rows) and `experiments/results/benchmark_summary.csv` (one row per
scenario, ~18 rows). CSV columns:

- **Input columns:** `station_network, n_stations, n_beams_total, n_satellites, scenario_profile,
  congestion_level, failures_enabled, station_mtbf_s, station_mttr_s, beam_mtbf_s, beam_mttr_s,
  handover_enabled, weather_mode, duration_s, dt_s, seed, scheduler, bandwidth_allocator,
  power_allocator, freq_allocator`
- **Output columns:** every key in `experiment.KPI_KEYS` (`delivered_gbit, completion_rate,
  sla_compliance, drop_rate, mean_wait_s, beam_utilization, fairness, mean_decision_ms,
  energy_kj, gb_per_kj, sessions_interrupted, mean_recovery_s, proactive_handovers`) +
  `pct_optimal` + `oracle_delivered_gbit` + `wall_time_s`

## Execution steps (when ready to run)

1. Write the script (`experiments/phase_benchmark.py`).
2. **Smoke-test first**: run it with the grid temporarily restricted to 1 network × 1 profile ×
   2 policies (foreground, ~30s) to catch bugs before committing to the full background run.
3. Launch the full sweep via `python experiments/phase_benchmark.py` in the **background**, since
   it's expected to take ~45–70 minutes.
4. Report back when it completes: the CSV paths, row counts, total elapsed time, and the summary
   table (best policy per objective per scenario) — plus flag anything that looks like the India
   coverage-gap pattern found earlier (e.g., a scenario where `pct_optimal` is high but
   `completion_rate` is low — capacity-bound, not a scheduling issue).

## Verification (when run)

- Smoke-test output (step 2) sanity-checked before the full run: non-zero `delivered_gbit` for at
  least one policy, no exceptions, CSV rows well-formed.
- After the full run: row-count check that `benchmark_results.csv` has the expected ~306 rows and
  `benchmark_summary.csv` has ~18, then skim a few rows for sane KPI ranges (e.g.,
  `completion_rate` and `sla_compliance` in [0,1], `pct_optimal` in [0,1]).
