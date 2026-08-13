# X-NioS AI Digital Twin — Telemetry, Health & Dashboard

**V2 Phase 1 (State Awareness).** The twin no longer only reports what a run
*delivered*; it now records what happened at *every step*, and turns that into
operator-facing health.

```
Simulator step ─► TelemetryRecord ─► sink ─┬─► HealthMonitor ─┐
                                           │                  ├─► FastAPI ─► Console
                                           ├─► JSONL on disk ─┘   (web/, served
                                           │                       by the same app)
                                           └─► (next) Feature layer ─► Forecast ─► Decision engine
```

One stream, many readers. That is the whole point of the ordering: telemetry is
simultaneously the dataset generator, the dashboard feed and — later — the RL
observation source, so it must exist before anything that consumes it.

---

## 1. Telemetry — [xnios/telemetry.py](xnios/telemetry.py)

One `TelemetryRecord` per step, with five faces of the same instant:

| face | rows | what it carries |
|---|---|---|
| `network` | 1 | throughput, queue, completion, beam/bandwidth utilisation, contention, coverage, energy, weather mix, session counters, decision latency |
| `stations` | 1 per station | up/degraded, beams total/available/active, bandwidth pool vs allocated, radiated power, weather + fade, connected sats, channels in use, mean SINR |
| `links` | 1 per **visible** pair | elevation/azimuth/range/scan angle, SNR, **SINR**, I/N, **BER**, rain fade, allocated bandwidth & power, achieved vs interference-free rate, bits moved, slewing, session age |
| `satellites` | 1 per satellite | sub-point lat/lon/alt (the map), state, backlog, delivered, wait, priority/tier/deadline, visible stations, current station+beam, achieved vs best-available rate |
| `decision` | 1 per decision | the four active algorithms, latency, accepted assignments, candidates offered vs left unserved, **`source` / `rationale` / `reasons` / `expected`** |
| `events` | 0..n | `session_start`, `session_end`, `complete`, `interrupt`, `recover`, `handover`, `station_fail/recover`, `beam_fail/recover`, `weather_change` |

### Three properties that matter

**It is a pure observer.** `Simulator(..., telemetry=None)` is the default and
costs one `is not None` per step. With recording on, every physical KPI is
**bit-identical** — asserted by T1 in the validation script. Instrumentation that
perturbs the system would invalidate every V1 result.

**Rejected candidates are recorded, not just chosen ones.** Every visible link
gets a row, with `active` separating the ones carrying a session. A learned
policy can only learn a *different* choice if the alternatives it was offered
are in the data.

**The explainability slots already exist.** `decision.rationale` / `reasons` /
`expected` are empty under V1's static configuration. They are in the schema now
so that when the decision engine lands, historical runs stay comparable and no
migration is needed.

### Usage

```python
from xnios.telemetry import TelemetryRecorder, MemorySink, JsonlSink, MultiSink
from xnios.simulator import Simulator

rec = TelemetryRecorder(sink=MemorySink(), config=cfg)
res = Simulator(scn, sched, sim_cfg, telemetry=rec).run()

rec.records[-1].network.beam_utilization
rec.records[-1].links[0].sinr_db
```

Streaming a training campaign to disk (flushed per record, so an interrupted run
still leaves usable data — the same lesson `bench_common.CsvWriter` applies):

```python
rec = TelemetryRecorder(sink=MultiSink(MemorySink(),
                                       JsonlSink("data/run-001.jsonl", compress=True)))
```

Cost levers for long or large runs:

```python
TelemetryRecorder(capture=("network",), every_n=5)      # cheap KPI series only
TelemetryRecorder(stride_s=30.0, include_idle_links=False)
```

Measured on `india4-congested` (180 steps × 40 satellites × 4 stations):

| configuration | wall time | on disk |
|---|---|---|
| no telemetry | 1.53 s | — |
| full, in memory | 1.70 s (+11%) | — |
| active links only | 1.67 s | — |
| `capture=("network",)`, `every_n=5` | 1.54 s (+1%) | — |
| full → JSONL | 1.89 s (+24%) | 4.19 MB |
| full → JSONL, gzipped | 2.13 s (+39%) | **0.31 MB** |

Compression is worth it for a campaign: 13× smaller for ~15% more wall time,
and the run is I/O-bound on the write either way.

Flattening for the feature layer / pandas / CSV:

```python
from xnios.telemetry import to_rows, write_csv
rows = to_rows(rec.records, "link")          # network|station|link|satellite|decision|event
write_csv(rec.records, "data/network.csv", "network")
```

### Honest limits

- **BER is derived, not modelled** ([link.py](xnios/link.py) `ber_from_sinr`):
  the closed-form *uncoded* QPSK curve. Nothing consumes it and no rate depends
  on it. A real DVB-S2X link runs LDPC/BCH and sits orders of magnitude lower at
  the same SINR — reporting the uncoded curve keeps it an interpretable proxy
  instead of a false claim about coded performance.
- **Station health has no degradation model yet.** `degraded` means "up but
  short of nameplate capacity", which is observation. Genuine failure
  *prediction* needs precursor signals; see §3.

---

## 2. Health monitor — [xnios/health.py](xnios/health.py)

`assess(record)` (or a window of records) → `HealthReport`:

```
Network Health  76%  (moderate)
  availability    100.0%  low       (good)
  link_quality     94.1%  low       (good)
  coverage         75.0%  moderate  (good)
  delivery         68.2%  moderate  (good)
  congestion       63.1%  high      (risk)
  failure_risk      0.0%  low       (risk)
  weather          10.0%  low       (risk)
  energy          100.0%            (good)
```

Every indicator carries the `factors` behind it — which is what makes the
dashboard's breakdown panel possible without a second computation path.

**Why this lives outside the twin.** `metrics.py` deliberately never collapses
the KPI vector to one score, because scalarisation weights are a *policy* choice.
A health score is exactly such a scalarisation, so it belongs downstream where
the weights are explicit and reported alongside the number:

```python
DEFAULT_WEIGHTS = {"availability": .25, "link_quality": .25,
                   "coverage": .20, "delivery": .20, "congestion": .10}
assess(record, weights={"coverage": .40})     # argue with them
```

**`failure_risk` is observed state, not a forecast**, and says so in
`report.notes`. In the current twin failures are a memoryless Poisson process:
the Bayes-optimal predictor is the constant hazard rate, so an ML failure
predictor here would be learning its own generator's parameter. Making failure
prediction real requires adding a **degradation model** to `dynamics.py` —
latent health that drifts (amplifier efficiency, calibration, error rate) before
an outage — so precursors exist to learn from. That is a prerequisite for V2
Phase 6, not part of Phase 1.

---

## 3. API — [api/](api/)

```bash
pip install fastapi "uvicorn[standard]"
uvicorn api.main:app --reload --port 8000
```

| route | purpose |
|---|---|
| `GET /api/policies` | schedulers / allocators / KPI keys / health weights |
| `GET /api/presets` | scenario presets (built-ins + everything in `configs/`) |
| `POST /api/runs` | start a run — returns `run_id` |
| `GET /api/runs`, `GET /api/runs/{id}` | status, metadata, final KPI vector |
| `GET /api/runs/{id}/frame?step=` | one record + its health report |
| `GET /api/runs/{id}/series`, `/timeline` | chart data |
| `GET /api/runs/{id}/export/{face}.csv` | download a telemetry face |
| `WS /api/ws/runs/{id}` | live frames as the simulation produces them |

The simulator runs in a worker thread; `pace_ms` deliberately slows it so the
console reads as live (the twin computes a 30-minute scenario in well under a
second, which is right for research and useless for watching).

Presets are built with `orbit.find_orbit_for_elevation` rather than hand-picked
RAANs — the documented coverage-gap failure mode is a plausible-looking
constellation that never actually flies over anything.

---

## 4. Console — [web/](web/)

```bash
python run_api.py                      # http://127.0.0.1:8000
```

That is the whole stack. The console is plain HTML/CSS/ES modules mounted on the
same FastAPI app (`StaticFiles` at the bottom of [api/main.py](api/main.py)), so
there is **no npm, no node_modules, no build step and no second origin** — edit a
file in `web/` and reload the page. Being same-origin also removes the CORS dance
and the old hard-coded `127.0.0.1:8000` WebSocket fallback: `/api/*` and the
frame stream now share the page's host.

### What it is built from

Preact + htm, loaded from a CDN through an import map in
[index.html](web/index.html) — ~12 kB, and the only third-party code on the page.
htm gives JSX-like template literals parsed at runtime, so components stay
declarative without a compiler. To go fully offline, drop the three files into
`web/vendor/` and repoint the import map; nothing else changes.

Everything else is local:

| was | now | why |
|---|---|---|
| Recharts | [charts.js](web/js/charts.js) — SVG, monotone-cubic (Fritsch–Carlson) paths | same curve Recharts' `type="monotone"` drew, ~350 lines |
| MapLibre GL + react-map-gl | [map.js](web/js/map.js) — SVG equirectangular with drag-pan and wheel-zoom | the map never loaded tiles anyway (see below) |
| Tailwind | [styles.css](web/styles.css) | same tokens, named classes instead of utilities |
| lucide-react | inline SVG in [ui.js](web/js/ui.js) | three icons |
| TypeScript | JSDoc-free plain JS | the wire format is still the telemetry schema |

Dropping MapLibre cost nothing because the old `NetworkMap` **never used a tile
server**: the basemap was always a vector graticule drawn from the theme tokens,
to keep the map self-contained and monochrome. With no tiles to fetch there was
nothing left for a map engine to do. It also removed a workaround — MapLibre
parses paint colours itself and never resolves `var(...)`, so the React version
needed a `useThemeColors` hook to re-resolve every custom property on theme
change. SVG resolves them natively, so the theme swap is pure CSS again.

### Design system

ARCTROPY's, taken from the live site's CSS custom properties — `--bg #08080A`,
`--line #1B1B20`, `--mute #6B6B73`, `--fg #F2F2F3`, Google Sans, `cubic-bezier(.16,1,.3,1)`,
1px hairline panels, 1px grid seams, wide-tracked uppercase eyebrows, pill
buttons, the pinging brand dot, and the dark/light toggle. Tokens live at the top
of [styles.css](web/styles.css) and everything resolves through them, so the
theme swap drives the whole console.

ARCTROPY is strictly monochrome; the only colour added is three desaturated
status hues (`--st-ok/warn/crit`), used **only** where an operator must read
severity at a glance. Chart series separate by form (fill vs line, solid vs
dashed), not hue.

Panels: health tiles → satellite map + resource monitor + event feed →
throughput / utilisation / backlog / health charts + link-quality table →
decision & explanation → scenario control with CSV export.

The map draws no external tiles: a graticule rendered from theme tokens, with
stations, sub-satellite points and one line per active beam. No API key, works
offline, stays monochrome.

---

## 5. Validation

```bash
python experiments/phase1_validation.py       # 13/13 — the twin, unchanged
python experiments/telemetry_validation.py    # 40/40 — the new layer
```

`telemetry_validation.py` asserts, in order: pure-observer identity (T1),
internal consistency of every face (T2), real geometry (T3), event capture and
reconciliation with `metrics.py` (T4), decision provenance (T5), JSONL
round-trip and run metadata (T6), flattening/CSV (T7), the health monitor
including a forced station outage (T8), and the capture-control levers (T9).

---

## 6. What comes next

Phase 1 is done. The order from here — with the reasoning that put prediction
late:

1. **Feature layer** — telemetry is raw; models consume derived features
   (utilisation ratios, backlog percentiles, link-margin distribution,
   time-to-LOS). Freeze this contract early; everything downstream depends on it.
2. **Analytical forecast** — who is visible in 5 minutes is *physics*, not
   learning. Compute it from `orbit.py`. It is also the baseline any ML model
   must beat before it counts as a result.
3. **Historical memory** — durable storage of telemetry + features + forecast,
   keyed by `RunMeta.run_id`.
4. **Decision engine** — rules first, built from the V1 benchmark. Note the
   measured headroom: across the 18 benchmark scenarios, perfect per-scenario
   policy selection beats the best single fixed config by **+0.00%** on
   throughput and **+5.75%** on energy. The win is not "pick a better
   scheduler" — it is *within-run* adaptation (adaptive power is 0% in clear sky
   and +56% in heavy rain) and foresight.
5. **Prediction models** — only for what cannot be computed analytically, and
   only after a degradation model makes failure prediction learnable at all.
6. **Recommendation & planning** — batch what-if over the existing config system.
