# Satellite / Phased-Array Beam Scheduling — Research & Build Plan

*A step-by-step plan for someone starting from zero. Written in plain English.*

---

## 0. What you are actually building (one paragraph)

The hardware team gives you an antenna that can point an invisible "beam" of radio
energy in any direction **instantly, using math instead of motors**. Your software is
the **brain** that decides, moment by moment: *which satellite or ground station to
talk to, which beam to use, on which frequency, with how much power, and for how long* —
so that the most data gets moved in the least time, without beams interfering with each
other. This is a **real-time resource-allocation and optimization problem**, and AI is
added only in the places where prediction and complex decision-making beat fixed rules.

You are, in effect, writing the **operating system for the antenna**.

---

## 1. How to read this plan

- Work **top to bottom**. Each phase builds on the previous one.
- Every phase has: **Goal → What to build → Which algorithms (and where they come from)
  → Reuse or invent? → Libraries → Deliverable → "Done when…"**
- **You build a simulator first, then add AI.** You do not need hardware to do 90% of
  this work. Everything can be tested in software (a "digital twin").
- Golden rule: **never optimize something you cannot first simulate and measure.**

**My assumptions** (tell me if any are wrong and I'll adjust the plan):
1. You are strong in AI/optimization/programming (Python), but new to RF/communications.
2. This is research-oriented (thesis / paper / prototype), simulation-first.
3. Small team, timeline of several months to ~1 year.

---

## 2. The mental model + plain-English glossary

The full pipeline you are aiming for:

```
Satellite positions  ─┐
Ground-station status ─┤
Weather forecast     ─┼──►  (A) PREDICTION (ML)  ──►  (B) DECISION (RL / optimization) ──►  (C) CONTROL
Traffic history      ─┤        traffic, SNR,            which sat, beam, freq,               phase values
Orbit predictions    ─┘        rain fade                power, duration                      per antenna element
```

**Glossary (memorize these — they unlock everything):**

| Term | Plain meaning |
|---|---|
| **Element** | One tiny antenna. You have hundreds/thousands. |
| **Phased array** | The whole grid of tiny antennas working together. |
| **Phase** | A tiny time-shift on each element's wave. Changing phases steers the beam. |
| **Beamforming** | Choosing all the phases (and amplitudes) so energy focuses in one direction. |
| **Beam steering** | Moving the beam by changing phases (no motor). |
| **Weights (w)** | The set of numbers (phase + amplitude) applied to each element. Beamforming = "find the best weights." |
| **Gain** | How focused/strong the beam is in a direction. |
| **SNR** | Signal-to-Noise Ratio. Higher = cleaner link = more data. |
| **Link budget** | Adding up all gains and losses to predict if a link works. |
| **Elevation angle** | How high above the horizon the satellite is. Low = worse link. |
| **Azimuth** | Compass direction to the satellite. |
| **Rain fade** | Rain absorbs radio waves (bad above ~10 GHz). Weather matters. |
| **Doppler shift** | Frequency changes because the satellite moves fast. Must be corrected. |
| **LEO / GEO** | Low-Earth-Orbit (fast, ~90-min orbit, e.g. Starlink) / Geostationary (fixed in sky). |
| **TLE** | "Two-Line Element" — a text format giving a satellite's orbit. Free from Celestrak. |
| **Beam hopping** | One beam rapidly jumping between spots to serve many areas in turn. |
| **Frequency reuse** | Using the same frequency for two links that are far enough apart not to interfere. |

---

## 3. Prerequisites — the 20% of theory that matters (learn by doing)

Do **not** try to read a whole textbook first. Learn these five ideas, each by writing a
tiny Python script (details in the phases):

1. **Friis equation** — predicts received power. `Pr = Pt·Gt·Gr·(λ / 4πR)²`.
   *Meaning:* power drops with distance²; bigger antennas (gain) and lower frequency help.
2. **Shannon capacity** — the speed limit of a link. `C = B·log₂(1 + SNR)`.
   *Meaning:* more bandwidth `B` and more SNR = more data/second. This is your reward's core.
3. **Array factor** — how the beam pattern comes from the phases.
   `AF(θ) = Σ wₙ · e^(j·n·k·d·sinθ)`. *Meaning:* sum of all element waves in a direction.
4. **Steering phase** — the phase to point at angle θ: `φₙ = −k·d·sinθ`.
5. **SNR & noise** — noise power `N = k_B·T·B`. SNR = signal power / noise power.

Where the symbols come from: `k = 2π/λ` (wavenumber), `λ` = wavelength, `d` = spacing
between elements, `B` = bandwidth, `k_B` = Boltzmann constant, `T` = temperature.

**Free foundations (skim, don't binge):**
- Sutton & Barto, *Reinforcement Learning: An Introduction* (free PDF) — for later phases.
- Boyd & Vandenberghe, *Convex Optimization* (free PDF) — for the optimization layer.
- For RF intuition: any "antenna arrays for beginners" video series + Balanis *Antenna
  Theory* (reference only — look things up, don't read cover to cover).

---

## 4. Tech stack (and why)

**Language: Python** for everything. It has the best libraries for all three worlds
(comms, optimization, AI). Move hot loops to NumPy/C++ later only if speed demands it.

| Job | Library | Note |
|---|---|---|
| Math / arrays | **NumPy, SciPy, Matplotlib** | The base of everything. |
| Orbits / satellite positions | **Skyfield** (uses SGP4 + TLE) | Turns a TLE into "where is the satellite now." |
| Convex optimization | **CVXPY** | Write the math almost like on paper. |
| Integer/assignment optimization (MILP) | **Google OR-Tools**, **PuLP**, or **Gurobi**/CPLEX (free academic license) | For "assign beams to stations" problems. |
| Classic ML | **scikit-learn** | Baselines for prediction. |
| Deep learning | **PyTorch** | For neural nets, forecasting, RL. |
| Reinforcement learning | **Gymnasium** (env format) + **Stable-Baselines3** (algorithms) or **Ray RLlib** (scales to multi-agent) | Don't write PPO from scratch — use these. |
| Graph neural nets | **PyTorch Geometric (PyG)** | Model satellites+stations as a graph. |
| (Advanced) link-level RF simulation | **NVIDIA Sionna** | GPU channel models, realistic later. |
| (Advanced) LEO network simulation | **Hypatia** (open source) | Starlink-scale constellation networking. |

---

## 5. Suggested software structure

```
antenna-brain/
├── sim/                 # the digital twin (no AI here)
│   ├── link_budget.py       # Friis, SNR, Shannon
│   ├── array.py             # phased array + array factor + steering
│   ├── beamforming.py       # MVDR, zero-forcing, MMSE weights
│   ├── orbits.py            # Skyfield: satellite positions, elevation, azimuth
│   ├── channel.py           # path loss, rain fade (ITU-R), Doppler
│   └── environment.py       # Gym-style env: state, step(), reward
├── baselines/           # classical, non-AI decision-makers
│   ├── greedy.py
│   ├── hungarian.py         # assignment
│   ├── milp_scheduler.py    # OR-Tools / CVXPY
│   └── waterfilling.py      # power allocation
├── predict/             # the ML prediction layer
│   ├── traffic_forecast.py  # LSTM / Transformer
│   └── rain_fade.py         # ITU model + ML correction
├── agents/              # the AI decision-makers
│   ├── dqn.py / ppo.py / sac.py
│   ├── gnn_policy.py        # graph-based policy
│   └── hybrid.py            # RL proposes → optimizer refines
├── eval/                # measure & compare everything
│   ├── metrics.py           # throughput, latency, fairness, energy...
│   └── benchmark.py         # runs all methods on same scenarios
├── data/                # TLEs, weather, traffic traces
├── configs/             # scenario definitions (how many sats/stations...)
└── notebooks/           # experiments & plots
```

Build it in this order: `sim/` → `baselines/` → `predict/` → `agents/` → `eval/`.

---

## 6. The phases (the actual step-by-step)

### Phase 0 — Setup (a few days)
- Install Python + the libraries above (use a virtual environment / conda).
- Download real satellite TLEs from **Celestrak** (free) so your sim uses real orbits.
- Make one plot: satellite position over time. **You've started.**

---

### Phase 1 — Link-budget calculator *(learn comms by building)*
**Goal:** Predict, for a satellite–station pair, "will this link work and how fast?"

**What to build:** `link_budget.py` implementing:
- Free-space path loss (from **Friis**),
- noise power `N = k_B·T·B`,
- `SNR = signal / noise`,
- data rate from **Shannon**: `C = B·log₂(1+SNR)`.

**Algorithms / where they come from:** These are 60-year-old textbook laws (Friis 1946;
Shannon 1948). **100% reuse. Never reinvent.**

**Libraries:** NumPy only.

**Deliverable:** A function `data_rate(distance, freq, tx_power, gains, bandwidth)` → Mbps.

**Done when:** You can show "as the satellite rises higher (bigger elevation angle),
distance drops, SNR rises, data rate rises." Plot it.

---

### Phase 2 — Phased-array + beamforming simulator
**Goal:** Turn a set of per-element **phases** into an actual **beam pattern**, and
compute the best phases to point at, or listen to, a chosen direction.

**What to build:** `array.py` and `beamforming.py`:
- Array factor `AF(θ) = Σ wₙ e^(j n k d sinθ)` → plot the beam shape.
- Steering: given target angle θ, set `φₙ = −k d sinθ`. Watch the main lobe move.
- Receive beamforming (align phases to boost a weak signal → higher SNR).

**Algorithms and where they come from (classical, well-known):**
| Method | What it does | Source |
|---|---|---|
| Delay-and-sum (conventional) | Simplest steering | Textbook |
| **MVDR / Capon beamformer** | Points at your signal *and* nulls interference | Capon, *Proc. IEEE*, 1969 |
| **LCMV** | MVDR with multiple constraints | Frost, 1972 |
| **Zero-Forcing (ZF)** | Serve many users, cancel cross-talk | MIMO textbooks |
| **MMSE beamformer** | Balances noise vs interference | Digital comms textbooks |

**Reuse or invent?** **Reuse.** These are standard. Implement them as your toolbox.
(An *optional* research angle later: a neural network that outputs weights faster than
MVDR — but only after MVDR works as your baseline.)

**Libraries:** NumPy, Matplotlib; SciPy for matrix inverses.

**Deliverable:** Given "point at 30°, null interference at −10°," produce the weights and
plot a beam with a peak at 30° and a dip at −10°.

**Done when:** You can steer the beam anywhere and place a null on an interferer.

---

### Phase 3 — The digital twin (the "environment")
**Goal:** A simulated world with real orbits, many ground stations, weather, and traffic,
that any decision-maker (classical or AI) can act on. **This is the most important phase.**

**What to build:** `orbits.py`, `channel.py`, `environment.py`:
- **Orbits:** Skyfield turns TLEs → each satellite's position, and each station's
  **elevation** and **azimuth** to each satellite, over time. Links only exist when
  elevation is above a minimum (e.g. 10°).
- **Channel:** path loss (Phase 1) + **rain fade** using **ITU-R P.618** (the official
  model for rain attenuation) + **gaseous loss** (ITU-R P.676) + **Doppler shift**.
- **Environment:** wrap it in the **Gymnasium** interface — `state`, `step(action)`,
  `reward`. State = positions, loads, weather, beam occupancy, power. Action = pick
  station/beam/frequency/power. Reward = data moved − penalties (see Phase 6).

**Algorithms / sources:** **SGP4** (orbit propagation — reuse via Skyfield); **ITU-R
P.618/P.676** (propagation — reuse, these are international standards). **Reuse all.**

**Libraries:** Skyfield, NumPy, Gymnasium; TLEs from Celestrak; optionally **Sionna** for
realistic channels or **Hypatia** for constellation-scale networking later.

**Deliverable:** A `SatEnv` you can `reset()` and `step()` like an Atari game, but for
satellite scheduling.

**Done when:** A random scheduler runs in it and you can measure total TB downloaded.

---

### Phase 4 — Classical baselines *(no AI yet — this is what AI must beat)*
**Goal:** Solid, well-understood schedulers. Research value = "AI beats these," so they
must be strong and fair.

**The sub-problems and their standard algorithms:**

| Sub-problem | Classical algorithm(s) | Where it comes from | Reuse? |
|---|---|---|---|
| Assign stations↔satellites/beams | **Hungarian algorithm** | Kuhn, 1955 | Reuse (SciPy `linear_sum_assignment`) |
| Which link first / beam order | **Greedy**, priority queues | Textbook | Reuse |
| Joint assignment under constraints | **Mixed-Integer Linear Programming (MILP)**, Branch & Bound | Operations Research | Reuse (OR-Tools / Gurobi) |
| Smooth resource splits | **Convex optimization** | Boyd & Vandenberghe | Reuse (CVXPY) |
| Transmit **power** allocation | **Water-filling** | Information theory (Cover & Thomas) | Reuse |
| **Frequency** assignment (avoid interference) | **Graph coloring** | Graph theory | Reuse |
| Hard combos | **Metaheuristics** (Genetic Algo, Simulated Annealing, PSO) | Optimization | Reuse |
| Beam hopping (GEO) | **Beam-hopping time-plan** optimization | IEEE JSAC / TWC papers | Reuse formulation |

**Reuse or invent?** **Reuse the algorithms; your contribution is the *formulation*** —
how you write the objective and constraints for *this* satellite problem. That's where
research novelty legitimately begins.

**Libraries:** SciPy, OR-Tools, CVXPY, PuLP.

**Deliverable:** At least 3 baselines (greedy, Hungarian, MILP) runnable in `SatEnv`.

**Done when:** You have a table: method → TB downloaded, latency, fairness, energy.

---

### Phase 5 — Prediction layer (ML) *(add AI where the future is uncertain)*
**Goal:** Predict things the optimizer needs but can't know: future **traffic demand**,
future **link quality/SNR**, and **rain fade**. Better predictions → better decisions.

**What to predict and with what:**
| Target | Classical | Modern (AI) | Source |
|---|---|---|---|
| Traffic demand | ARIMA, exponential smoothing | **LSTM**, **Temporal Conv Net**, **Transformer (Informer)** | Time-series forecasting literature |
| Link quality / SNR | Kalman filter | LSTM / GRU | Signal processing + DL |
| Rain fade | **ITU-R P.618** physics model | ML correction on top of ITU | ITU + ML papers |

**Reuse or invent?** **Reuse the architectures.** Your novelty is *what* you predict for
this domain and *how the prediction plugs into the scheduler* — not inventing a new
neural net from scratch.

**Libraries:** scikit-learn (baselines), PyTorch (LSTM/Transformer).

**Deliverable:** A `predict/` module the scheduler can call for "expected demand/SNR next
N minutes," with error bars (uncertainty).

**Done when:** Your forecasts beat "assume tomorrow = today" on held-out data, and feeding
them into a baseline scheduler improves throughput.

---

### Phase 6 — Reinforcement-learning scheduler *(the adaptive decision-maker)*
**Goal:** An agent that *learns* the scheduling policy by trial and error in `SatEnv`,
instead of you hand-writing rules.

**Design (this is the heart of it):**
- **State:** satellite positions/elevations, station loads/queues, beam occupancy,
  weather, available power/frequency, and your Phase-5 predictions.
- **Action:** choose station(s), beam(s), frequency, power level, duration. (Discrete
  choices → DQN family; continuous power/steering → SAC/DDPG/PPO.)
- **Reward:** `+ TB downloaded − λ₁·latency − λ₂·energy − λ₃·packet_loss + λ₄·fairness
  − λ₅·beam_switching_cost`. **Reward design is where you'll spend real effort.**

**Algorithms and where they come from:**
| Algorithm | Use when | Paper |
|---|---|---|
| **Q-Learning / DQN** | Discrete actions | Mnih et al., *Nature*, 2015 |
| **PPO** | Robust default, discrete or continuous | Schulman et al., 2017 |
| **SAC** | Continuous actions (power, angle), sample-efficient | Haarnoja et al., ICML 2018 |
| **DDPG / TD3** | Continuous control | Lillicrap 2016 / Fujimoto 2018 |
| **Multi-agent (QMIX, MADDPG, MAPPO)** | Each beam/satellite = an agent | Rashid 2018 / Lowe 2017 |
| **GNN policy** (input to any of the above) | Model sats+stations as a *dynamic graph* | GCN (Kipf 2017), GAT (Veličković 2018), GraphSAGE (Hamilton 2017) |
| **Transformer policy** | Long-range dependencies across many links | Vaswani et al., 2017 |

**Reuse or invent?** **Reuse the RL algorithm implementations** (Stable-Baselines3 / RLlib
give you PPO/SAC/DQN ready-made). Your contribution = **state representation** (e.g. a GNN
over the satellite–station graph), **reward design**, and **action structure** for this
problem. That combination is publishable.

**Libraries:** Gymnasium, Stable-Baselines3 or Ray RLlib, PyTorch, PyTorch Geometric.

**Deliverable:** A trained agent that runs in `SatEnv` and beats the Phase-4 baselines on
your metrics.

**Done when:** On unseen scenarios (new orbits/weather), the RL agent moves more TB or
lowers latency than greedy/MILP — and you can show the learning curve.

---

### Phase 7 — Hybrid AI + optimization *(the likely real contribution)*
**Goal:** Combine the strengths: **AI adapts and predicts**, **optimization guarantees
feasible, physically valid, safe** decisions.

**The architecture (recommended):**
```
Predictions (Phase 5) ─► RL scheduler (Phase 6) proposes a good plan
                              │  (fast, adaptive, learns patterns)
                              ▼
        Convex/MILP layer (Phase 4) refines the plan
        enforcing HARD constraints: power limits, no beam overlap,
        frequency rules, minimum fairness  →  guaranteed-valid output
                              ▼
                     Phased-array controller (weights/phases)
```

**Why hybrid wins:** Pure RL can output an *illegal* action (too much power, two beams
clashing). Pure optimization is slow and can't predict the future. Letting RL **propose**
and optimization **project onto the feasible set** (this is called "safe RL" / "action
projection" / "optimization layers in networks," e.g. **OptNet**, Amos & Kolter, 2017) is
a strong, defensible design.

**Reuse or invent?** The **components are reused**; the **integration and problem
formulation are your novelty.** This is exactly the kind of "new algorithm" that gets into
IEEE TWC / JSAC / INFOCOM.

**Deliverable:** The full pipeline of section 2, end to end.

**Done when:** The hybrid beats both pure-RL and pure-optimization on throughput *and*
never violates a hard constraint.

---

### Phase 8 — Evaluation & benchmarking
**Goal:** Prove your system is better, fairly and reproducibly.

**Metrics (`eval/metrics.py`):** throughput (TB/hour), latency, fairness (Jain's index),
energy efficiency (bits/Joule), packet loss, beam-switching overhead, constraint
violations, and **inference time** (see Phase 9).

**Method:** Run **every** method (random, greedy, Hungarian, MILP, RL, hybrid) on the
**same** set of scenarios (same orbits, weather, traffic seeds). Report averages ±
variance. Plot. This table *is* your paper's results section.

**Done when:** You have one clear benchmark table + plots comparing all methods.

---

### Phase 9 — Real-time / deployment realism
**Goal:** Make sure it could actually run on hardware.

- **The hard constraint:** your decision must be computed in **much less time than the
  beam-update interval.** If beams update every few milliseconds, a 200 ms model is
  useless. Measure inference time as a first-class metric.
- **Fixes if too slow:** smaller network, distill the RL policy, precompute, or use the
  optimizer only occasionally while RL runs every step.
- **Robustness:** test under sensor noise, wrong predictions, sudden weather, satellite
  dropouts. A good scheduler degrades gracefully.

**Done when:** Inference time ≪ update interval, and performance holds under noise.

---

### Phase 10 — Where the novelty is / writing it up
Your paper's contribution will most likely be **one** of these (pick one, do it well):
1. A **novel state representation** (dynamic GNN/Transformer) for satellite scheduling.
2. A **new reward / joint formulation** that optimizes beam+power+frequency+assignment
   *together* instead of in separate modules.
3. The **hybrid RL+optimization architecture** with guaranteed feasibility.
4. A **prediction-aware scheduler** (weather/traffic forecasting feeding the decision).

**Target venues (to read and to submit to):** *IEEE Transactions on Wireless
Communications*, *IEEE JSAC*, *IEEE Trans. Communications*, *IEEE INFOCOM*, *GLOBECOM*,
*ICC*, *ACM SIGCOMM/IMC*. Start with **IEEE Communications Surveys & Tutorials** — its
*survey* papers on "machine learning for satellite / non-terrestrial networks" and
"resource allocation in LEO constellations" will hand you the whole map of prior work.

---

## 7. Algorithm cheat-sheet (problem → classical → AI → source)

| Problem | Classical algorithm | AI / modern approach | Key source |
|---|---|---|---|
| Predict where satellite is | SGP4 orbit propagation | — | Celestrak / Skyfield |
| Will the link work / how fast | Friis + Shannon | — | Textbooks (reuse) |
| Best beam weights | MVDR, ZF, MMSE | NN weight predictor | Capon 1969 |
| Assign stations↔beams | Hungarian, MILP | GNN + RL policy | Kuhn 1955; Kipf 2017 |
| Order of service | Greedy / priority | RL scheduler | Sutton & Barto |
| Power allocation | Water-filling | SAC / DDPG (continuous) | Cover & Thomas; Haarnoja 2018 |
| Frequency assignment | Graph coloring | RL / GNN | Graph theory |
| Predict traffic | ARIMA | LSTM / Transformer | Forecasting lit; Vaswani 2017 |
| Predict rain fade | ITU-R P.618 | ITU + ML correction | ITU-R |
| Joint everything | MILP (slow) | Hybrid RL + convex layer | Amos & Kolter 2017 (OptNet) |

---

## 8. Reuse vs. invent your own — the decision rule

**Default: REUSE.** You almost never invent a new low-level algorithm. Reinventing PPO,
MVDR, or SGP4 wastes months and looks naïve to reviewers.

**Invent (i.e., your contribution) only in these places:**
- **Problem formulation** — the exact objective + constraints for *your* satellite system.
- **Reward function** — how you trade throughput vs latency vs energy vs fairness.
- **State/representation** — e.g. modeling the network as a *dynamic graph* for a GNN.
- **Architecture / how pieces connect** — e.g. RL proposes, convex layer guarantees.
- **A constraint or metric others ignore** — e.g. minimizing beam-switching wear, or
  fairness under rain, done properly for the first time.

**The practical recipe:** (1) find a recent paper close to your problem, (2) reproduce its
result as your baseline, (3) your novelty is the **delta** — the one thing you change that
makes it better. "New algorithm" almost always means *new combination/formulation*, not
new math from scratch.

---

## 9. Datasets, simulators, and real tools (all free unless noted)

- **Celestrak** — real satellite orbits (TLE files).
- **Skyfield / SGP4** — compute positions from TLEs.
- **NVIDIA Sionna** — GPU link-level RF simulation, realistic channels (advanced).
- **Hypatia** (Kassing et al., ACM IMC 2020) — Starlink-scale LEO network simulator.
- **ns-3** (+ satellite modules) — packet-level network simulation.
- **ITU-R recommendations P.618 / P.676 / P.837** — official propagation & rain models.
- **Gymnasium** — the standard "environment" interface for RL.
- **Stable-Baselines3 / Ray RLlib** — ready-made RL algorithms.
- **STK (Systems Tool Kit)** — professional orbit/RF modeling (free tier; commercial).
- Weather data: NOAA / open weather APIs for realistic rain patterns.

---

## 10. Reading list (in order — skim, then deep-dive)

**Start here (the map):**
1. A recent **IEEE Communications Surveys & Tutorials** survey on *ML / DRL for satellite
   (non-terrestrial) networks* — gives you the whole landscape and reference list.
2. 3GPP **NTN** (Non-Terrestrial Networks) overview — how standards handle satellites.

**Foundations (reference, don't read cover-to-cover):**
3. Balanis, *Antenna Theory* — arrays & beamforming chapters.
4. Mailloux, *Phased Array Antenna Handbook*.
5. Pratt, *Satellite Communications* — link budgets, orbits.
6. Boyd & Vandenberghe, *Convex Optimization* (free) — for the optimizer layer.
7. Sutton & Barto, *Reinforcement Learning: An Introduction* (free) — for the agent.

**Key algorithm papers (read the ones you use):**
8. Capon (1969) — MVDR beamforming.
9. Kuhn (1955) — Hungarian assignment.
10. Mnih et al. (2015, *Nature*) — DQN. Schulman et al. (2017) — PPO. Haarnoja et al.
    (2018) — SAC.
11. Kipf & Welling (2017) — GCN. Veličković et al. (2018) — GAT. Vaswani et al. (2017) —
    Transformer.
12. Amos & Kolter (2017) — OptNet (optimization as a neural-net layer) — for the hybrid.

Then: search **IEEE Xplore / Google Scholar** for *"beam hopping optimization,"
"deep reinforcement learning satellite resource allocation," "LEO constellation
scheduling,"* and read the 5 most-cited recent papers.

---

## 11. Milestones & rough timeline (adjust to your team)

| Months | Milestone |
|---|---|
| 0–1 | Phases 0–1: setup, link budget, first plots. You understand the physics. |
| 1–2 | Phase 2: working beamforming + steering simulator. |
| 2–4 | Phase 3: the digital-twin environment (orbits + weather + traffic + Gym). |
| 4–5 | Phase 4: classical baselines + first benchmark table. |
| 5–6 | Phase 5: prediction layer (traffic/SNR/rain). |
| 6–9 | Phase 6: RL scheduler beating baselines. |
| 9–11 | Phase 7–8: hybrid architecture + full benchmark. |
| 11–12 | Phase 9–10: real-time hardening + write the paper. |

---

## 12. Common pitfalls (avoid these)

- **Jumping to RL before the simulator is solid.** RL only reflects the world you built.
- **Weak baselines.** If your greedy baseline is bad, "AI wins" means nothing.
- **Reward hacking.** The agent will exploit a sloppy reward (e.g. never switch beams).
  Iterate on the reward carefully.
- **Ignoring inference time** until the end. Design for speed from Phase 6.
- **Reinventing physics/algorithms.** Reuse SGP4, ITU models, PPO, MVDR. Innovate on top.
- **Testing on training scenarios.** Always hold out unseen orbits/weather.

---

## 13. Your first two weeks — exactly what to do

1. **Day 1–2:** Install Python + NumPy, SciPy, Matplotlib, Skyfield. Download a few TLEs
   from Celestrak.
2. **Day 3–4:** Plot a satellite's elevation angle over a ground station for 24 hours.
3. **Day 5–7:** Write `link_budget.py` (Friis + Shannon). Plot data rate vs elevation.
4. **Day 8–10:** Write `array.py` — plot an array factor; steer the beam to 30°.
5. **Day 11–14:** Add a null with MVDR; write a one-page note on what each parameter does.

When these work, you *understand* the domain — and you're ready for Phase 3 (the
environment), where the real research begins.

---

## 14. Topics to Master — study checklist (with free resources)

*Note: the 100 TB / 1–2 hr figure was illustrative — no real satellite does that. But
**time and speed are the real goal**, so the ⭐ topics below (the ones that set the speed
ceiling and the time windows) matter most. Since you're already strong in AI/optimization,
spend ~70% of your study time on the communications / RF / antenna / orbit topics — that's
your gap **and** what governs speed. Learn Tier 0 by **coding the link-budget script**
(Phase 1); you'll understand it far faster by watching numbers move than by reading.*

### ★ Tier 0 — the 8 essentials (learn these first)
- [ ] **Decibels (dB, dBm, dBi, dBW)** — every RF value is in dB; 30 min unlocks all papers. → [Microwaves101: Decibel](https://www.microwaves101.com/encyclopedias/decibel)
- [ ] **Link budget (Friis, free-space loss)** — will the link work, how strong? → [Microwaves101: Link Budget](https://www.microwaves101.com/encyclopedias/link-budget)
- [ ] **Noise & SNR** — the measure of link quality everything depends on. → Tse & Viswanath, *Fundamentals of Wireless Communication* (free PDF), ch. 5
- [ ] ⭐ **Shannon capacity `C = B·log₂(1+SNR)`** — THE speed limit. → [Wikipedia: Shannon–Hartley theorem](https://en.wikipedia.org/wiki/Shannon%E2%80%93Hartley_theorem)
- [ ] ⭐ **Spectral efficiency + MODCOD + ACM** — the real speed knob (SNR → actual bits/s). → [Wikipedia: DVB-S2X](https://en.wikipedia.org/wiki/DVB-S2X) + [Adaptive coding and modulation](https://en.wikipedia.org/wiki/Link_adaptation)
- [ ] **Antenna gain, beamwidth, EIRP, G/T** — how focused/strong the beam is → sets SNR. → [antenna-theory.com](https://www.antenna-theory.com/)
- [ ] **Array factor + beam steering (φ = −kd·sinθ)** — how phases point the beam. → [antenna-theory.com: Arrays](https://www.antenna-theory.com/arrays/main.php)
- [ ] ⭐ **Contact windows / passes / elevation angle** — why time is scarce (LEO: ~5–10 min/pass). → [ESA: Types of orbits](https://www.esa.int/Enabling_Support/Space_Transportation/Types_of_orbits)

### Tier 1 — Communications & RF (your biggest gap — go deepest here)
- [ ] **Frequency bands (L/S/C/X/Ku/Ka/V, optical) + bandwidth↔rain trade-off** — higher band = faster but rainier. → [Wikipedia: Satellite frequency bands](https://en.wikipedia.org/wiki/Radio_spectrum)
- [ ] **Path loss & rain/atmospheric attenuation (ITU-R P.618/P.676)** — what steals your speed. → [ITU-R P.618](https://www.itu.int/rec/R-REC-P.618/)
- [ ] **Doppler shift & compensation** — fast satellites shift frequency; must correct. → [Wikipedia: Doppler effect](https://en.wikipedia.org/wiki/Doppler_effect)
- [ ] **Multiple access, SINR, frequency reuse (FDMA/TDMA)** — sharing spectrum without killing speed. → Tse & Viswanath (free PDF), ch. 4 & 6
- [ ] **MIMO / multi-beam basics** — parallel links = more total speed. → Tse & Viswanath (free PDF), ch. 7–10

### Tier 1 — Antenna arrays & beamforming (the hardware you control)
- [ ] **Phased array fundamentals, spacing, grating & sidelobes** → Analog Devices, "Phased Array Antenna Patterns, Part 1–3" (analog.com, free series)
- [ ] **Beamforming algorithms: conventional, MVDR, ZF, MMSE** → Tse & Viswanath (ZF/MMSE); search "MVDR beamforming lecture notes" for MVDR
- [ ] **Analog vs digital vs hybrid beamforming** — sets how many beams at once. → Analog Devices beamforming articles (analog.com)
- [ ] **Beam hopping (satellite-specific)** → search IEEE Xplore: "beam hopping survey satellite"

### Tier 1 — Orbits & the "time" dimension
- [ ] **LEO vs MEO vs GEO** — decides minutes-of-visibility vs continuous. → [ESA: Types of orbits](https://www.esa.int/Enabling_Support/Space_Transportation/Types_of_orbits)
- [ ] **Elevation/azimuth, visibility, passes** → [Skyfield docs](https://rhodesmill.org/skyfield/)
- [ ] **TLE / SGP4 orbit propagation** → [Celestrak](https://celestrak.org/) (canonical SGP4/TLE docs)
- [ ] **Handover between satellites/ground stations** → search: "LEO satellite handover survey"
- [ ] **Propagation latency** — the pure speed-of-light "time" (big for GEO). → search: "Starlink latency Hypatia"
- [ ] **Store-and-forward & ground-station networks** → [SatNOGS](https://satnogs.org/) + [AWS Ground Station docs](https://aws.amazon.com/ground-station/)

### Tier 2 — Optimization (you know the field; learn these specifics)
- [ ] **Assignment problem / Hungarian** → [Wikipedia: Hungarian algorithm](https://en.wikipedia.org/wiki/Hungarian_algorithm) + SciPy `linear_sum_assignment`
- [ ] **Mixed-Integer Linear Programming (MILP)** → [Google OR-Tools guide](https://developers.google.com/optimization)
- [ ] **Convex optimization** → [Boyd & Vandenberghe, *Convex Optimization* (free)](https://web.stanford.edu/~boyd/cvxbook/) + Stanford EE364a lectures
- [ ] **Water-filling (power allocation)** — comms-specific. → Tse & Viswanath (free PDF), ch. 5
- [ ] **Graph coloring (frequency assignment)** → [Wikipedia: Graph coloring](https://en.wikipedia.org/wiki/Graph_coloring)
- [ ] **Queueing theory + Little's law** — links traffic, buffers, latency ("time"). → [Wikipedia: Little's law](https://en.wikipedia.org/wiki/Little%27s_law)
- [ ] **Multi-objective / Pareto optimization** — trading throughput vs latency vs fairness. → [Wikipedia: Multi-objective optimization](https://en.wikipedia.org/wiki/Multi-objective_optimization)

### Tier 2 — AI / ML / RL (you know these; just the project-specific angles)
- [ ] **Time-series forecasting (LSTM, Transformer/Informer)** — the prediction layer. → search: "Informer long sequence forecasting"
- [ ] **RL: MDP → DQN → PPO → SAC** → [OpenAI Spinning Up in Deep RL (free)](https://spinningup.openai.com/) + Sutton & Barto (free)
- [ ] **Graph Neural Networks** — sats+stations as a dynamic graph. → [Stanford CS224W (free)](http://web.stanford.edu/class/cs224w/) + PyTorch Geometric docs
- [ ] **Safe / constrained RL, action projection** — can't send unsafe hardware commands. → search: "Constrained Policy Optimization" + "safe reinforcement learning survey"
- [ ] **Sim-to-real & domain randomization** — make-or-break for real deployment. → search: "domain randomization sim-to-real survey"

### The topics that most directly govern SPEED & TIME (circle these)
1. ⭐ Shannon capacity + spectral efficiency + ACM/MODCOD — how fast a link goes.
2. SNR / link budget — because speed follows SNR.
3. ⭐ Contact windows + handover + ground-station networks — scarce minutes → continuous throughput.
4. Queueing theory + latency — the "time" the waiting data actually experiences.
5. Multi-beam / MIMO + beam hopping — parallelism = more total speed.
6. Scheduling optimization + RL — squeezing the most from all of the above.

**Suggested order:** Tier 0 → Tier 1 (comms → antennas → orbits) → Tier 2. Two free books
cover a huge fraction of this: **Tse & Viswanath, *Fundamentals of Wireless Communication***
(the comms/beamforming side) and **Sutton & Barto, *Reinforcement Learning*** (the agent side).

---

*Reuse the wheels. Invent the vehicle. Your contribution is the formulation, the reward,
the representation, and how AI and optimization are combined — not the low-level math.*

---

# 15. V2 — The Prediction Layer (final plan)

> **This supersedes §6 Phase 5.** That earlier sketch named model architectures before
> establishing that the targets were learnable at all. This section fixes the order:
> prove the signal exists, *then* pick a model.

### Status

| Stage | State | Note |
|---|---|---|
| 0 · Target contract | **done** | captured by the tables in §15.5–15.6 |
| 1 · Analytical forecaster | **done — shipped** | `xnios/forecast.py`, 8/8 on 3 presets; live in telemetry + console (§15.14) |
| 2 · Feasibility study | **done — gate fired** | **no target has ML headroom.** See §15.10 |
| 3 · Feature layer | **blocked** | nothing to build features *for* yet |
| 4 · Stressed dataset | **blocked** | — |
| 5 · First model | **blocked** | and no longer predetermined — see §15.10 |
| A · Traffic arrivals | **done — passes Stage 2, fails Stage 6** | demand is learnable but operationally negligible. See §15.13 |
| B · Latent station health | **done — observability fixed, gate still fails** | criterion 2: a VSWR threshold beats the model. See §15.12.1 |
| C · Continuous fade | queued | marginal at X-band; revisit at Ka |

**Current instruction: do not train a model.** The next milestone is to give the
simulator realistic, *structured* uncertainty and then re-run the Stage 2 gate
unchanged.

## 15.0 The one rule

**If physics, orbital mechanics, or the simulator can compute it exactly, do not predict it
with AI. Use AI only where the future is genuinely uncertain.**

Everything below follows from that sentence.

## 15.1 What we are actually building, and why it is defensible

Not "AI does what physics can't" — we own the simulator, so we could always just run it
forward. The honest framing:

> The prediction layer is a **fast, uncertainty-aware surrogate of the twin's own forward
> evolution.**

Two legitimate justifications:

| Justification | Why it holds |
|---|---|
| **Speed** | A controller doing rollouts needs microseconds per evaluation, not simulator runs. |
| **Irreducible uncertainty** | Weather realization and failure timing are genuinely unknown. The correct output is a *distribution*, not a number. |

**Consequence that shapes the whole plan: labels are free.** Every target is telemetry
shifted in time. Nothing is hand-annotated, and feasibility can be measured *before* any
model is written.

## 15.2 Four facts that set the order of work

1. **Labels are free** → audit feasibility first, train second.
2. **`india4-nominal` has almost no uncertainty.** Static weather, no failures, deterministic
   orbits: given the seed the future is exactly computable. A model would be
   reverse-engineering `orbit.py`. **Training data must come mostly from stochastic
   scenarios** (`india4-storm`, `failure_demo`, dynamic weather). This is the single biggest
   risk to V2.
3. **Two targets are blocked on simulator work.** Demand has no arrival process
   (`backlog_gbit` is set once at t=0 and drains). Failure is memoryless Poisson, so there
   are no precursors.
4. **Horizons must match the scenario.** A pass lasts ~5 simulated minutes, so "next 15
   minutes" spans the whole event plus empty sky. Use **30–120 s** at link level and
   **2–5 min** at network level, or lengthen scenarios to multi-orbit.

## 15.3 Architecture

```
                         STATE
                           |
             +-------------+-------------+
             |                           |
        Digital Twin                Real Network
      (simulator data)            (hardware telemetry)
             |                           |
             +-------------+-------------+
                           v
                  Common telemetry schema        <- xnios/telemetry.py   (V1, done)
                           v
                     Feature layer               <- xnios/features.py    (Stage 3)
                           v
         +-----------------+-----------------+
         v                                   v
  Analytical forecast                  ML prediction        <- xnios/predictors/
  xnios/forecast.py  (Stage 1)         (Stage 5+)
  - future elevation                   - P(link loss)
  - time-to-LOS                        - SLA risk
  - contact windows                    - congestion
  - interference-free rate             - throughput
         +-----------------+-----------------+
                           v
                    Decision engine
                           v
        Scheduler / Power / Beam / Bandwidth /
              Frequency / Handover
                           v
                    Network action
                           v
              Forecast scoring  -----------+     <- Stage 7
                           |               |
                           +---------------+  (twin grades its own predictions)
```

## 15.4 The stages

Each stage states: **what to do · what to use · what you get · what changes in the system.**

### Stage 0 — Freeze the target contract   *(1–2 days, no code)*

**Do:** one table, every candidate target, with these columns —
entity (link/satellite/station/network) · horizon · analytical|ML|measured · required
telemetry fields · source of uncertainty · the action it would change · evaluation metric.

**Use:** a markdown table. Nothing else.

**Get:** a frozen candidate list (~12 targets) that cannot silently drift.

**System impact:** none yet. This is the document every later stage is checked against.

---

### Stage 1 — Analytical forecaster   `xnios/forecast.py`

**Do:** exact future geometry — contact windows, time-to-LOS, future elevation, future
interference-free rate.

**Use:** existing `orbit.py` + `link.py`. numpy only. **No ML.**

**Get:** deterministic answers to everything in §15.0's "don't predict" list.

**System impact:** three things at once —
1. the **baseline** every ML model must beat,
2. a **feature** for every ML model (future elevation is the strongest predictor of future SNR),
3. a **shippable console feature on its own** — *"next contact in 04:20, 6-minute window."*

---

### Stage 2 — Target Feasibility & Predictability Study   ⭐ **HARD GATE**

**No ML work starts before this passes.**

**Do:** for each candidate target and horizon —
1. From a fixed state, run the simulator forward **N≈50 times with different seeds**. The
   spread is the **irreducible uncertainty** — the ceiling no model can beat.
2. Run the **analytical baseline** (propagate orbit, hold weather, assume policy unchanged).
   Measure its skill.
3. **Gap between analytical skill and the ceiling = the entire ML budget for that target.**

**Use:** the existing simulator, numpy, pandas, matplotlib. Runs are sub-second, so this is
compute-cheap.

**Get:** a feasibility table with a keep/kill decision per target:

| Target | Physics baseline | Uncertainty source | ML signal? | Decision |
|---|---|---|---|---|
| Future elevation | very strong | ~none | no | analytical |
| Time-to-LOS | very strong | ~none | no | analytical |
| Contact windows | very strong | ~none | no | analytical |
| Link loss | partial | weather, interference | yes | **ML candidate** |
| Throughput | partial | weather, failures, traffic | yes | ML candidate |
| SLA violation | partial | link quality, contention | yes | ML candidate |
| Congestion / queue | strong | demand, weather, failure | maybe | audit |
| Energy | strong | mostly policy-determined | maybe | audit |
| Demand | none today | traffic arrivals | no | needs traffic model |
| Station failure | none today | no latent degradation | no | needs health model |

**System impact:** kills 3–4 targets before any cost is sunk, and produces the figure that
scientifically justifies every model that follows. **This is V2's key methodological
contribution** — the reason to believe the AI is there for a reason.

**Gate rule — all four must hold.** Each was added after an experiment produced a
false pass without it.

1. **Positive skill.** `R²_learned > 0` (or a positive metric for classification).
   Beating a catastrophic baseline is not skill: §15.11's control shows "headroom"
   of +2.32 while the model scores −0.33.
2. **Material margin** over the strongest *non-ML* baseline. `82% → 94%` is
   interesting; `98% → 98.5%` is not. The baseline must be the best closed form
   available — §15.10 and §15.11 both inflated headroom with an under-powered one.
3. **Control attribution.** The advantage must *disappear* when the source of
   predictability is removed. Structured vs memoryless arrivals is the template.
4. **World generalisation.** It must survive held-out **worlds** — different
   constellations and phasing — not merely held-out seeds inside one world.

**And the target must be observable.** Four ways a genuinely uncertain quantity
turns out to be unlearnable, all four found by measurement:

| Failure mode | Where it appeared | Why prediction fails |
|---|---|---|
| Memorylessness | Poisson failures; Poisson arrivals | no precursor exists |
| Aggregation | network-level demand | independent sources average the signal away |
| Observation noise | coarse arrival chunks | precursor buried in shot noise |
| Observability duty cycle | station health (§15.12) | precursor exists but is only measurable 2.3% of the time |

---

### Stage 3 — Feature layer   `xnios/features.py`

**Do:** pure functions, telemetry in → feature rows out, at four entity levels (link,
satellite, station, network). Include the Stage 1 analytical forecasts as features.

**Use:** numpy only. **No ML dependencies** — same discipline as `telemetry.py`, so the twin
still runs anywhere.

**Get:** one feature schema shared by simulator and, later, real hardware.

**System impact:** **this is the sim-to-real seam.** The model never learns whether a row
came from the simulator or an antenna.

---

### Stage 4 — Stressed dataset

**Do:** sweep presets × schedulers × allocators × weather models × seeds. Deliberately
over-weight dynamic weather and failures. Apply **domain randomization**: vary SNR noise,
measurement noise, latency, station differences, traffic.

**Use:** the existing `experiments/` harness. Store per face; record config hash + schema
version with every run.

**Get:** reproducible training data.

**Two hard rules:**
- **Split by run, never by row.** Rows inside a run are massively autocorrelated; a random
  row split produces a spectacular and completely fake score.
- **Hold out entire policies.** Train on some schedulers, test on unseen ones, to measure
  degradation when the controller changes behaviour.

**System impact:** datasets become versioned artifacts, not one-off files.

---

### Stage 5 — First model: link degradation

Start here and *only* here. Richest sample count (every link × every step), closes a real V1
gap, and has a direct operational action.

| | |
|---|---|
| **Entity** | active satellite–station link |
| **Horizons** | 30 / 60 / 120 s |
| **Outputs** | `P(link unusable)` + SNR quantiles (10th / 50th / 90th) |
| **Model** | **scikit-learn `HistGradientBoosting`** — tabular data, small dataset, needs feature attributions for the explainability panel, and adds no dependency risk to the existing numpy/scipy/TF environment |
| **Uncertainty** | **conformal prediction** for the SNR interval (distribution-free, guaranteed coverage, no crossing quantiles — see §15.6); **isotonic calibration** for the loss probability |

**Gates it must pass:**
- beats **persistence** *and* the **analytical baseline** on held-out runs;
- **calibration is blocking, not optional.** A "78% chance of link loss" that fires 40% of
  the time drives worse handovers than no model at all. Check reliability curves and Brier
  score, not only AUC.

**System impact:** new prediction fields available to the decision engine.

---

### Stage 6 — Predictive handover   ⭐ **the result that matters**

**Do:** drive handover from `P(link loss)` instead of geometric LOS alone. A/B against the
V1 handover using the existing benchmark harness, with the MILP oracle as the ceiling.

**Use:** existing scheduler/handover hooks + `experiments/` + `xnios/oracle.py`.

**Get:** the headline number.

> No prediction metric is the deliverable. **This is.**
> "AUC 0.89" convinces nobody. "Predictive handover cut session interruptions 41% and raised
> `delivered_gbit` 6% over the V1 baseline, reaching 78% of the MILP ceiling" is the project
> in one sentence.

**System impact:** the decision engine goes live. `decision.source` becomes `"ai"`,
`decision.rationale` and `decision.expected` get populated — the contract V1 already
reserved, so **the dashboard's "AI recommendation" tile lights up with no schema change.**

---

### Stage 7 — Live forecast scoring

**Do:** the twin continuously grades its own past predictions against what actually happened,
and surfaces the error on the console.

**Get:** drift detection, calibration monitoring.

**System impact:** this is what makes it a **twin** rather than a dashboard with a model
bolted on, and it is the early-warning system for distribution shift when real hardware
arrives.

---

### Stage 8 — Expand, guided strictly by Stage 2

**Not** "implement the remaining predictions." **Only** targets that survived the feasibility
study, in the order their headroom justifies. This is what prevents V2 becoming twelve
mediocre models.

## 15.5 Parallel simulator workstreams (prerequisites, not ML work)

All three **break V1 bit-identity** (`experiments/telemetry_validation.py` T1 asserts
bit-identical KPIs). Gate each behind a config flag and bump the schema version so V1 results
stay reproducible.

**A. Traffic arrival process** — Poisson / bursty / tier-dependent, optionally diurnal.
*Unblocks:* demand prediction. *Also makes* congestion genuinely uncertain rather than
computable.

**B. Latent station health** — the degradation chain:
`health → PA efficiency → calibration drift → error rate → SNR variance → failure hazard`.
*Unblocks:* failure prediction.
**Critical:** the latent health state must be **hidden from the feature layer**, or the model
simply reads the answer instead of learning precursors.

**C. Continuous rain fade + real precipitation** — see §15.5.1 below. *Unblocks:* every
weather-related target. Today there is nothing there to predict.

### 15.5.1 Weather — why it is a simulator job before it is an ML job

**The finding.** `xnios/weather.py` defines fade as a **7-entry lookup table**
(`FADE_DB = {"clear": 0.0, "cloudy": 0.5, "rain": 3.0, "storm": 8.0, ...}`), and the Markov
chain in `DynamicWeatherModel` only ever reaches four of those states. So "predict the weather's
impact on the link" currently means **predicting which of four numbers applies** — a model would
be learning a Python dictionary. Zero headroom; the Stage 2 gate would kill it immediately.

Note this is *not* a data problem. `xnios/weather_live.py` already pulls real per-station
conditions from Open-Meteo with no API key. Its limits are that it fetches **current conditions
only** and holds them **constant for the whole run**.

**Do these in order.**

1. **Real precipitation time series → scenario generation.** *(biggest win, and not a prediction
   problem at all.)* Replace the synthetic Markov chain with real observed precipitation for the
   four Indian sites. Monsoon weather is bursty, spatially correlated and seasonally structured
   in ways the chain is not. This improves the **training distribution**, which is what actually
   decides whether models survive contact with hardware.
2. **Continuous fade model.** Replace `FADE_DB` with **ITU-R P.838** (specific attenuation
   `γ = k·R^α` from rain rate) plus **ITU-R P.618** (slant-path integration over elevation and
   rain height). Fade becomes a continuous function of mm/h, frequency, elevation and
   polarisation instead of a four-value step. This is **analytical, not ML** — and it is the
   prerequisite that makes any weather-related model meaningful. `weather.py` already anticipates
   this: *"Phase 9 will replace `fade_db` with a stochastic (Markov/ERA5-driven) time series."*
3. **Then the learnable target: forecast error.** Open-Meteo exposes both an archive and a
   historical-*forecast* endpoint (what was forecast at a past time). That yields
   `(forecast, actual)` pairs, and the **site-specific forecast residual** is a genuine ML problem
   with genuine headroom. Predicting the weather is the provider's job; predicting how wrong they
   are over *your* station is ours.

**Two caveats that decide how much any of this is worth.**

- **Band.** The presets run at `freq_ghz: 8.2` — X-band. Rain attenuation scales roughly as
  `f²` in this range: at X-band heavy rain costs single-digit dB (the table's 8 dB storm is about
  right), while at **Ka-band it is tens of dB and rain fade dominates every other effect**.
  Against observed SINR of 12–17 dB, an 8 dB fade hurts but rarely breaks the link.
  **Decide the target band before investing here.** At X-band weather is a secondary effect and
  effort belongs elsewhere; at Ka/Q/V it moves to the top of the roadmap.
- **Timescale.** Standard forecasts are hourly; the link-loss horizon is 30–120 s. An hourly
  forecast says "it is raining" and nothing about the next minute. Sub-minute fade prediction
  needs **radar nowcasting** (rain-cell motion tracking), a different data product entirely.
  So weather helps the **multi-minute network-level** targets (throughput, congestion, energy)
  far more than the **30-second link-loss** decision that is the first model.

**Decision:** keep weather out of the first ML model. Do steps 1 and 2 — they are cheap, they are
physics rather than ML, and they improve every downstream target at once. Then let the Stage 2
study decide whether the forecast residual earns a model of its own.

## 15.6 Which model for which target

**Rule: every model must earn its place by beating the simpler one on a network-level
objective, not on an ML metric.**

### Why gradient boosting is the default

Not habit — it fits this problem on eight specific counts:

| Reason | Why it matters here |
|---|---|
| Heterogeneous units | Features mix dB, seconds, Hz, counts and categorical weather states. Trees split on order, not magnitude — no scaling, no normalisation bugs. |
| Native missing values | `HistGradientBoosting` handles NaN directly. Links appear and disappear, so features are *genuinely absent*, not zero. |
| Interactions for free | `elevation × rain fade × allocated bandwidth` is exactly the physics, found without being specified. |
| Small data | 10⁴–10⁶ rows. Deep learning wants more. |
| Seconds to train | A feasibility-gated plan retrains constantly; iteration speed is the bottleneck. |
| Microseconds to infer | This is the *entire* justification for building a surrogate at all. |
| Feature attributions | The Phase 4 explainability panel requires them. |
| No new dependency | scikit-learn only. `requirements.txt` already documents avoiding packages that fight TensorFlow's numpy/protobuf pins. |

Plus the empirical result that tree ensembles still beat deep learning on tabular data at this
scale (Grinsztajn et al., 2022).

### The known weakness: trees cannot extrapolate

Outside the training range a tree returns the boundary value — predictions silently flatten
rather than failing loudly. That is a real sim-to-real risk when hardware produces SNR the
simulator never generated, and a further argument for **training on the physics residual**
(see the closing note of this section): the analytical part extrapolates correctly, and the
model only ever corrects a bounded error.

### Per target

| Target | Start with | Upgrade to — *only if it wins* |
|---|---|---|
| Link loss | HistGradientBoosting | GRU / GNN |
| SNR degradation | HistGradientBoosting | GRU |
| SNR **intervals** | point model + **conformal prediction** | quantile GBDT / NGBoost |
| SLA violation | HistGradientBoosting | temporal / GNN |
| Congestion | HistGradientBoosting | temporal / GNN |
| Throughput | HistGradientBoosting | temporal |
| Traffic demand | **seasonal-naive / exponential smoothing**, then GBDT on lag + rolling features | LSTM / Transformer |
| Station health | **discrete-time hazard (survival analysis)** | temporal |
| Anomaly detection | Isolation Forest | autoencoder |
| Topology effects | — | GNN |
| Long-horizon control | — | RL |

**Three rows that are deliberately *not* plain gradient boosting:**

- **SNR intervals.** Quantile GBDT needs one model per quantile and the quantiles can *cross*
  (a 10th above the 50th). **Conformal prediction** wraps any point model, is distribution-free,
  and gives guaranteed coverage — a better fit for an operator-facing "±" figure.
- **Traffic demand.** A genuine time series with autocorrelation and likely daily seasonality.
  The baseline must be a classical forecaster; GBDT only competes once lag and rolling-window
  features are hand-built.
- **Station health.** Once the §15.5B degradation chain exists this is a *time-to-event* problem,
  not a classification one. Discrete-time hazard models estimate "probability of failure in the
  next window given survival so far", which is the quantity the controller actually needs.

**Trigger conditions for the advanced models — do not add them before these fire:**

- **Temporal (GRU/LSTM):** only once experiments show SNR history carries information a
  single snapshot does not. The paper claim must be *"adding temporal history improved
  prediction from X to Y."* If it doesn't, keep the simpler model.
- **GNN:** only once tabular features demonstrably miss relational effects — competing
  satellites for the same station, shared beam contention, interference neighbourhoods.
- **RL:** only after `prediction → controller → measured improvement` exists. Without that
  baseline you cannot tell whether RL improved anything. RL chooses **actions**; it does not
  predict.

**Also useful:** where the analytical baseline is strong, **train on the residual**
(`actual − analytical`) rather than the raw value. Physics comes free, the model only learns
what physics can't, and the result is far more sample-efficient and much easier to defend.

## 15.7 Designing now for real phased arrays

Five rules, adopted from day one, so simulator-trained models can transfer.

1. **One schema for both worlds.** Simulator SNR and hardware SNR both map to `link.snr_db`.
   The feature layer never knows which produced the row.
2. **Expect a sim-to-real gap.** Hardware adds noise, calibration error, antenna-pattern
   imperfection, oscillator drift, unmodelled interference, telemetry delay. Architecture
   must support `simulation data + real telemetry → fine-tuning → production model`.
3. **Domain randomization from the start** (Stage 4). The model must learn *"across plausible
   network conditions this relationship holds,"* not *"this simulator behaves exactly so."*
4. **Version every prediction** with `model_version`, `feature_schema_version`, timestamp,
   confidence — so any past decision can be traced to the model that produced it.
5. **Shadow mode before control.** On real hardware: telemetry → AI → *recommendation* →
   operator. Compare against the incumbent controller. Only after validation does AI drive
   anything, and then behind safety limits and constraint checks.

## 15.8 What V2 is explicitly *not*

Deferred with a stated trigger, not abandoned: GNN, RL, temporal deep learning, anomaly
detection (in a simulator, anomalies are whatever you inject — you would be grading your own
homework), and any target the Stage 2 study kills.

## 15.9 The contribution

> Written before the study ran, and left in place: the hypothesis was that
> analytical forecasting *plus learned uncertainty* would let the twin anticipate
> problems and act early. Half of that survived contact with measurement. See
> §15.15 for what V2 actually concluded.

- **V1:** *understand the network, and determine when each mechanism matters.*
- **V2 (as planned):** *use that understanding, together with analytical
  forecasting and learned uncertainty, to anticipate network problems and act
  before they occur.*
- **V2 (as measured):** *establish, experimentally, where prediction adds
  operational value — and find that in this twin it does not.*


---

## 15.10 Stage 2 result — the gate fired

Run: `python experiments/feasibility_study.py --runs 8 --seeds 20`
(24 runs across `india4-nominal` / `-congested` / `-storm`; 7,357 link samples,
4,320 network samples; **split by run**, every third held out.)

**Verdict: no candidate target has usable ML headroom in the current simulator.**
This is not "the model failed" — it is the data-generating process not yet
containing enough structured uncertainty for the question to be meaningful.

### What each target did

| Target | Baseline | Learned | Why it was killed |
|---|---|---|---|
| **Link loss** ≤30/60/120 s | AUC **0.992 / 0.990 / 0.996** (analytical) | 0.996 / 0.999 / 1.000 | The Stage 1 forecaster already answers it, and the model is **worse calibrated** at two horizons (Brier 0.0030→0.0088, 0.0070→0.0106). Nothing to add, and a miscalibrated probability drives worse handovers than none. |
| **Link SNR** @ +30/60/120 s | R² 0.51 / −0.18 / −0.77 | 0.99 | Decomposition showed the *entire* analytical error is the adaptive power allocator: `corr(error, ΔP) = +1.0000`, residual sd **0.00 dB**. Geometry contributes zero. This is un-modelled **determinism**, not uncertainty — fix the baseline, not the model. |
| **Throughput / beam util / energy** | mostly negative R² | 0.997–0.999 | 83–95 % of held-out targets are **zero** (the network is idle after the ~5-minute pass). At +300 s the target has no variance at all. The R² is largely "predict zero". |
| **Queue** @ +60/180/300 s | R² **0.87–0.97** (persistence) | 0.998–1.000 | Real but tiny headroom: **+0.03 to +0.06**. Not worth a model to build and maintain. |

### The ceiling: the model is already at it

Branching one world into 24 redrawn weather futures (identical satellites,
stations, orbits and backlogs; only the post-branch weather walk redrawn):

| Target | Horizon | Irreducible sd | Ceiling R² | Learned R² | |
|---|---|---|---|---|---|
| throughput | 60 s | 0.030 Gb/s | 0.996 | 0.998 | at ceiling |
| throughput | 180 s | 0.041 Gb/s | 0.862 | 0.999 | at ceiling |
| queue | 60 s | 1.35 Gb | 1.000 | 1.000 | at ceiling |
| queue | 180 s | 2.66 Gb | 0.9998 | 0.998 | at ceiling |

Weather-driven uncertainty is **2–3 % CV at 30–60 s**, rising to **33 % at 180 s**.
What little irreducible uncertainty exists sits at *long network horizons*, not at
the 30–60 s link horizon where the plan proposed to start.

### Three experiment bugs found before trusting the result

The first run reported headroom of +0.68 to +21 everywhere. That was implausible,
and investigating it is what produced the real answer:

1. **The "analytical" SNR baseline was accidentally persistence** — `snr_db_at`
   was written but never called. Identical persist/analytic columns gave it away.
2. **Buffer exhaustion was being labelled as link loss.** `Simulator._visibility`
   stops emitting a pair once the satellite drains, so "row disappeared" ≠ "link
   lost". Dropping drained samples moved the analytical AUC **0.757 → 0.992** and
   flipped the verdict on the flagship target.
3. **The weather-branching experiment branched after the event.** With
   `dwell_s=240` and a branch at t=120, the redrawn weather only began at t=240 —
   after the pass had ended — producing a fake irreducible sd of exactly 0.0000.
   Forcing `dwell_s=30` and branching at t=30/60/90 produced the numbers above.

A feasibility study whose first answer is "enormous headroom everywhere" is
reporting its own bugs. Chasing all three is what made the gate trustworthy.

### Root cause

Every link in the chain is deterministic:

```
orbit -> visibility -> allocator -> power -> SNR -> rate -> throughput -> energy
```

Weather adds little at X-band (measured in Stage 1: fade up to 6 dB does not move
the contact window at all — the 30° phased-array scan limit binds first), and
failures are a **memoryless** Poisson process, so by construction no telemetry
predicts them. Variance without memory is noise, not signal.

### Consequences for the roadmap

**1. Stage 5 no longer has a predetermined first model.** The rule is now:

> The first ML model is whichever target survives the feasibility gate *after*
> the simulator realism work — link degradation, demand, queue, SLA risk, station
> health, or none of them.

**2. The loop becomes:**

```
simulator realism  ->  re-run Stage 2 (unchanged)  ->  headroom?
                                                        |
                                              no -> stop, keep the closed form
                                              yes -> Stage 3+
```

**3. Stage 1 stands on its own.** `contact_windows` / `time_to_los` /
`next_contact` are validated exact, ~360× cheaper than re-simulating, useful to
the scheduler today, and transferable to real phased-array telemetry. Killing the
first ML model did not waste it — it *is* the deliverable for link loss.

**4. A warning carried into workstream A.** A memoryless arrival process would
repeat the failure-prediction mistake: it adds variance but no predictability.
Demand only becomes learnable if arrivals carry **temporal structure** — bursts
with dwell, diurnal envelopes, tier correlation — so that recent history informs
the near future. Poisson arrivals should be implemented as the *control* that is
expected to show no headroom.


---

## 15.11 Workstream A result — traffic arrivals

`xnios/traffic.py` (opt-in; absent `traffic` block ⇒ V1, verified by
`telemetry_validation.py` 40/40). Run:
`python experiments/traffic_feasibility.py --runs 12`

**Summary.** Traffic arrivals successfully introduce realistic stochastic demand,
but only *sufficiently fine* telemetry exposes the predictable burst structure.
Per-satellite demand prediction now passes the feasibility gate — cleanly, against
a working control. Network-level demand still fails. Queue prediction remains
unresolved and is **not** attributable to traffic.

### The processes (6/6)

| Check | Result |
|---|---|
| `traffic=none` deterministic | identical KPIs — V1 untouched |
| poisson / bursty long-run rate | +9% / +14% of configured |
| poisson autocorrelation | **−0.020** (memoryless, as intended) |
| bursty autocorrelation | **+0.344** (has memory) |
| latent burst state hidden from telemetry | absent |

The rate check caught a modelling bug: solving for a zero OFF-rate has no solution
when `burst_ratio > 1/duty`, and clamping the negative result to zero ran the
process **129% over** its configured rate. Both scales are now normalised so the
mean is exact for any ratio.

### The gate, per-satellite demand — the control discriminates

| Horizon | poisson (memoryless) learned R² | bursty (memory) learned R² |
|---|---|---|
| +60 s | **−0.129** | **+0.532** |
| +180 s | −0.212 | +0.302 |
| +300 s | −0.328 | +0.122 |

Memoryless arrivals leave the model **worse than predicting the mean**; structured
arrivals make it genuinely predictive. That is a claim about the *process*, not
about the model, and it is what the control was for.

**Three conditions are all required.** Remove any one and the signal disappears:

1. **Memory in the process.** Poisson fails at every horizon.
2. **Per-satellite targets.** Network-level demand fails even for bursty
   (learned R² **0.007** at +60 s) — summing 20 independent ON/OFF chains averages
   the burst structure away.
3. **Observable granularity.** At `chunk_gbit=0.25` the shot-noise CV is 2.12 and
   the state is buried; a sweep shows learned R² rising from **−0.318 → +0.240** as
   granularity is refined, then headroom collapsing again at 0.01 Gb as persistence
   catches up. There is an observability optimum, around `chunk_gbit ≈ 0.04`.

### Two methodology fixes without which this result was wrong

**The queue baseline was under-powered.** It used *instantaneous throughput ×
horizon*, which assumes the current contact lasts the whole horizon — wrong by
most of a 180 s horizon given ~5-minute passes. Rebuilt on the Stage 1 forecaster
(integrate the real contact windows, then scale by the delivery efficiency
observed in the current row — a ratio estimator, no fitted constant):

| Queue baseline | +60 s | +180 s | +300 s |
|---|---|---|---|
| naive (`throughput × h`) | 0.978 | 0.502 | **−0.919** |
| physics (capacity bound) | 0.254 | −2.901 | −4.199 |
| **physics × observed efficiency** | **0.981** | **0.523** | **−0.057** |

**Run-level splits were not separating worlds.** The India presets specify
satellites explicitly, so every seed shared an *identical constellation* — only
the traffic realisation differed. The model could memorise the deterministic
visibility backbone and meet it unchanged in the held-out runs. Once each run also
draws its own orbital phasing and backlogs, queue headroom at +60 s collapsed from
**+0.29 to +0.007**.

> Splitting by run is not enough when every run shares the same world. Hold out
> **worlds**, not just realisations.

### Queue: unresolved, and not traffic-derived

| Horizon | best baseline | learned | headroom | poisson arm |
|---|---|---|---|---|
| +60 s | 0.981 | 0.988 | **+0.007** | +0.009 |
| +180 s | 0.706 | 0.938 | +0.231 | +0.239 |
| +300 s | 0.444 | 0.911 | +0.467 | +0.453 |

At +60 s the physics baseline matches the model — dead. At longer horizons there
is headroom, but it is **the same size in the memoryless control**, so it is not
created by the arrival process. It is the model out-fitting the delivery dynamics,
and it needs its own investigation before it counts as a candidate.

### A criterion this exposed

**Headroom only counts when the learned R² is itself meaningfully positive.** The
poisson arm shows "headroom" of +0.39 / +1.38 / +2.32 while the model scores
−0.13 / −0.21 / −0.33 — beating a catastrophic baseline is not evidence of skill.
Both numbers have to be read together.

### Status

- **Per-satellite demand: PASSES the gate.** Learned R² 0.53 @ +60 s, 0.30 @ +180 s,
  against a control at −0.13. Reproducible and discriminated.
- **Operational value: unproven.** Whether R² 0.53 changes a scheduling decision is
  a Stage 6 question, not an R² question.
- **Next: workstream B (latent station health)** — a deliberately constructed
  degradation chain should produce a stronger, more observable precursor than one
  buried in arrival shot noise.


---

## 15.12 Workstream B result — latent station health

`xnios/degradation.py` (opt-in; no `degradation` block ⇒ V1, `telemetry_validation.py`
40/40). Run: `python experiments/health_feasibility.py --runs 12`

**Summary.** The causal chain works and the precursor is real, but it is
**observable only while the station is carrying traffic — 2.3 % of the run**. By
the time an outage happens the newest measurement is typically an hour old, so
outage prediction still scores at chance. This is a *telemetry* limitation, not an
ML one, and the fix is physical rather than statistical.

### The mechanism works

| Check | Result |
|---|---|
| degradation off ⇒ V1 | bit-identical |
| health declines over the run | 1.00 → 0.00 |
| degradation-driven outages | 9 across 4 stations / 3 h |
| **SNR residual vs physics, degraded** | **mean −0.18 dB, sd 0.51 dB** |
| **same, memoryless control** | **mean +0.00 dB, sd 0.00 dB** |
| latent health hidden from telemetry | absent |

The precursor is a **residual against physics**: `forecast.snr_db_at` reproduces
the link budget exactly and knows nothing about degradation, so
`measured − forecast` is identically zero for a healthy station and grows as one
decays. Stage 1 is the instrument that makes workstream B observable at all — and
the same residual is computable on real hardware from a real G/T and ephemeris.

**And it is informative:** on steps where a link is active,
`corr(residual, latent health) = +0.597`.

### But outage prediction still fails

| Horizon | poisson control AUC | degraded AUC |
|---|---|---|
| +60 s | 0.477 | 0.498 |
| +180 s | 0.478 | 0.554 |
| +300 s | 0.485 | 0.485 |

The reason, measured on one station over a 3-hour run:

```
steps with any active link (residual observable) :   25 / 1080  = 2.3 %
outage onsets                                    :    3
  t=3480 s   last fresh observation  52.5 min earlier
  t=6240 s   last fresh observation   6.2 min earlier
  t=9280 s   last fresh observation  56.8 min earlier
```

A ground station's health is only measurable *through a link*, and a LEO pass is
~5 minutes out of every ~96. The precursor is real, correlated and hidden from the
features exactly as designed — and almost always an hour stale when it is needed.

### The fix is telemetry, not modelling

Real ground stations do **not** learn about themselves only when a satellite is
overhead. They report continuously: PA current and temperature, VSWR, calibration
residual, receiver noise figure, tracking error. The twin currently exposes
station health *solely* through the link budget, which is the modelling gap.

**Next step for workstream B:** add station-local telemetry sampled every step,
independent of whether any satellite is visible, driven by the same latent health.
Then re-run this experiment unchanged. That converts a 2.3 % duty cycle into 100 %
and is the physically realistic representation besides.

Until then: **failure prediction remains blocked**, now for a different and more
interesting reason than in §15.10 — no longer "there is no precursor" but "the
precursor is not observable often enough to act on".

### Two bugs this experiment caught

1. **Degradation reached the recorded SNR only through some paths.** `_visibility`
   and the allocator rate functions applied the G/T penalty, but `_compute_rates`
   — which produces the `snr_db` telemetry records — did not. The residual read
   exactly 0.00 dB despite health falling to 0.29. That was also a genuine
   inconsistency in the simulator: rates saw degradation, reported SNR did not.
2. **The degradation clock did not match the scenario clock.** Over a 30-minute
   run every pass finishes inside the first ~5 minutes, so a drift quoted per hour
   is worth ~0.008 dB while any link is up, and health never reaches the failure
   threshold. The experiment now runs ~1.9 orbits so successive passes see a
   station in successively worse condition.


### 15.12.1 The observability fix — and what it revealed

`degradation.housekeeping()` adds five station-local instruments sampled **every
step regardless of visibility**, each a noisy monotone read of the same latent
health: PA current, temperature, VSWR, calibration residual, noise figure. The
latent scalar itself is still never exposed. Recorded on `StationRecord`; zero
unless a `degradation` block is configured, so V1 stays bit-identical (40/40).

The experiment was re-run **completely unchanged** apart from observability —
same degradation process, hidden state, outage definition, horizons, world split,
model and metrics — as a four-arm design.

| Arm | +60 s | +180 s | +300 s |
|---|---|---|---|
| poisson, link-only *(control)* | 0.477 | 0.478 | 0.485 |
| degraded, link-only | 0.498 | 0.554 | 0.485 |
| **degraded + housekeeping** | **0.921** | **0.902** | **0.906** |
| poisson + housekeeping *(control)* | 0.477 | 0.478 | 0.485 |

**Observability was the binding constraint.** AUC 0.50 → 0.92 with no change to
the process, the model or the target. Brier improves too (0.0115 → 0.0089 at
+60 s), so it is better calibrated, not merely better ranked.

**The control is the strongest part of the result.** The memoryless arm has the
identical five instruments and gains *exactly nothing* — 0.477 / 0.478 / 0.485,
unchanged to three decimals. The channels carry information only when there is a
latent state for them to carry, which is attribution rather than correlation.

#### But the gate still fails, on criterion 2

A univariate threshold — the alarm any operator already has — was tested as the
strongest non-ML baseline:

| Horizon | pa | temp | **vswr** | cal | noise | **best single** | full model |
|---|---|---|---|---|---|---|---|
| +60 s | 0.917 | 0.906 | **0.926** | 0.891 | 0.892 | **0.926** | 0.921 |
| +180 s | 0.908 | 0.906 | **0.921** | 0.893 | 0.890 | **0.921** | 0.902 |
| +300 s | 0.912 | 0.909 | **0.922** | 0.896 | 0.896 | **0.922** | 0.906 |

**A VSWR threshold beats the model at every horizon.** Against the four criteria:

| | Criterion | Verdict |
|---|---|---|
| 1 | positive skill | **pass** — AUC 0.90+ on a 1–6 % base rate |
| 2 | material margin over the strongest non-ML baseline | **FAIL** — −0.005 to −0.019 |
| 3 | control attribution | **pass** — control gains exactly 0.000 |
| 4 | world generalisation | **pass** — held-out worlds |

#### Why, and what it means

The instrumentation was built *too cleanly*. Every channel is a monotone function
of the same scalar `1 − health` plus independent Gaussian noise, so the problem
collapses to univariate detection and the best-conditioned channel is very nearly
an optimal detector. There is no interaction, no confounding and no multi-modality
for a model to exploit.

That is a statement about the **degradation model**, not about ML. Real hardware
is messier in ways that matter:

* different failure modes drive **different channel signatures** (a failing PA and
  a drifting calibration do not look alike), so one channel is not sufficient;
* readings are **confounded** — temperature tracks ambient conditions and traffic
  load as well as health, so a raw threshold produces false alarms;
* noise across channels is **correlated**, and degradation is non-monotone
  (partial self-recovery, service actions).

**Verdict: station-failure prediction does not pass the gate.** The correct
deliverable here is a **threshold alarm on VSWR**, which is simpler, cheaper and
slightly better than the model — and is a genuinely useful operator feature.

**If workstream B is revisited**, the change required is a multi-mode, confounded
degradation model, not a bigger predictor. Until such a model exists, adding one
would be fitting noise around an alarm that already works.

#### The fifth failure mode

| Failure mode | Why ML is not the answer |
|---|---|
| Memorylessness | no precursor exists |
| Aggregation | independent sources average the signal away |
| Observation noise | precursor buried in shot noise |
| Observability duty cycle | precursor measurable only 2.3 % of the time |
| **Univariate sufficiency** | **precursor observable, informative — and a single threshold already extracts it** |


---

## 15.13 Stage 6 — does predicted demand change a decision?

Run: `python experiments/demand_control.py --preset india4-congested --runs 12 --beams 1`

Per-satellite bursty demand was V2's only surviving ML candidate (§15.11,
R² ≈ 0.53 @ +60 s). Stage 6 asks the only question that decides whether it ships.
Four schedulers, identical apart from the ordering key, on held-out worlds:

| Arm | Ordering |
|---|---|
| `fcfs/strongest` | V1 baseline |
| `ljf/strongest` | longest-queue-first — strongest **non-predictive** policy |
| demand (model) | `backlog + predicted arrivals` |
| demand (**oracle**) | `backlog + TRUE future arrivals` |

The oracle arm decides the workstream: if perfect knowledge of future demand does
not beat `ljf`, **no predictor can**, whatever its R².

### Result

| KPI | fcfs | ljf | model | oracle | oracle−ljf | model−ljf |
|---|---|---|---|---|---|---|
| **delivered_gbit** | 411.13 | **451.59** | 451.64 | 451.68 | **+0.090** | +0.050 |
| completion_rate | 0.300 | 0.256 | 0.256 | 0.256 | +0.000 | +0.000 |
| sla_compliance | 0.269 | 0.250 | 0.250 | 0.250 | +0.000 | +0.000 |
| fairness | 0.304 | 0.269 | 0.269 | 0.269 | +0.000 | +0.000 |
| mean_wait_s | 1161.4 | 1165.1 | 1165.6 | 1165.8 | +0.63 | +0.50 |

**The positive control works:** switching FCFS → LJF is worth **+40.5 Gb**. The
experiment is demonstrably able to detect a scheduling change of that size.

**Against that, perfect demand knowledge is worth +0.09 Gb** (sd 0.191 across
worlds) — **0.2 % of the effect the lever provably produces**, and statistically
indistinguishable from zero.

### Why

Over the 60 s horizon where the model is accurate, predicted arrivals are
~0.33 Gbit per satellite while backlogs are 8–60 Gbit. The demand term is ~1 % of
the term it is added to, so `backlog + demand` almost never reorders anything.
Longer horizons do not rescue it: arrivals grow to ~1.7 Gbit at +300 s while the
model's R² falls to 0.12.

**The prediction is real and operationally negligible.** Current backlog dominates
future arrivals by roughly two orders of magnitude at every horizon the model can
serve.

### The first null was invalid — and the check that caught it

The first run used the default 4 beams/station and reported all four arms tied to
three decimals. That was an artifact: **zero steps in the entire run had beams
exhausted while satellites waited.** With 4 stations × 4 beams there are always
enough beams for every simultaneously-visible satellite, so the ordering key can
never change an outcome and *every* policy ties, demand-aware or not. The 16–28
"waiting" satellites were waiting for **visibility**, not for beams.

Reducing to 1 beam/station produced 15 contended steps and the FCFS→LJF gap that
makes the measurement meaningful.

> **A null result requires a positive control.** Before believing "X makes no
> difference", show the experiment detecting something that does. Here that is the
> +40.5 Gb FCFS→LJF gap; without it the first run's tie was unfalsifiable.

### Verdict

**Demand prediction does not ship.** It passes Stage 2 and fails Stage 6, which is
exactly the separation Stage 6 exists to enforce: predictability is necessary, not
sufficient.

### V2 feasibility: closed

Every candidate is now resolved.

| Target | Outcome | Winner |
|---|---|---|
| Geometry, LOS, contact windows | analytical, exact | `xnios/forecast.py` |
| Link loss | physics AUC 0.99, better calibrated than ML | analytical |
| SNR | error was the deterministic power allocator | analytical |
| Queue @ +60 s | physics×efficiency baseline matches the model | analytical |
| Queue @ +180/300 s | headroom identical in the memoryless control | **unresolved** |
| Network demand | aggregation destroys the signal | — |
| Station failure | precursor real; a VSWR threshold beats the model | simple rule |
| **Per-satellite demand** | **R² 0.53, but +0.09 Gb of a +40.5 Gb lever** | **no action** |

**No ML target survives.** The honest conclusion is not that the models failed but
that **this twin, as modelled, is a domain where orbital mechanics and simple
operational rules are sufficient** — and V2's contribution is the method that
establishes that, rather than an AI component built because one was planned.

The two things worth shipping from V2 are both non-ML: the **analytical
forecaster** (exact, ~360× cheaper than re-simulating) and a **VSWR threshold
alarm**. The one open thread is queue at long horizons.

---

## 15.14 Shipped — the analytical forecaster

The one V2 component that earned its place, now integrated end to end.

**Telemetry.** `_ForecastCache` builds every contact window once per run and
serves lookups thereafter (~430 ms once; recomputing per step would dominate).
New fields, all exact rather than predicted:

* `SatelliteRecord.next_contact_s` / `next_contact_station` / `contact_window_s`
* `SatelliteRecord.time_to_los_s`, `LinkRecord.time_to_los_s`

Telemetry remains a pure observer — `telemetry_validation.py` 40/40.

**Console.** A *Contact forecast — analytical* panel, the exact seconds-to-LOS in
the link table, and the forecast on each satellite's map tooltip. Verified in a
real browser: zero console errors, no horizontal overflow.

**The lookahead is 24 hours, and that is the point.** LEO ground tracks precess
~24° west per orbit, so a satellite does *not* revisit the same station next
orbit. Measured on `india4-nominal`, SAT-002 gets **8 windows in 24 h with gaps up
to 728 minutes**. The console now reads:

```
IN CONTACT — LOSING SIGNAL IN
  SAT-202   Delhi                                     76s
NEXT CONTACT
  SAT-304   Ahmedabad-SAC · 2m 42s window          1h 30m
  SAT-004   Delhi         · 3m 52s window          8h 24m
  SAT-102   Bengaluru     · 3m 06s window         13h 46m
```

A satellite that misses its pass waits **8 to 14 hours**. Nothing else in the
system could tell an operator that, and it reframes every scheduling decision the
console displays.

**Not yet done:** the proactive-handover trigger still uses the V1 lead-time
heuristic, and it compares against `elevation_mask_deg` rather than the *effective*
mask — for a phased array with `max_scan_deg=60` the beam is unsteerable below
30° while the configured mask says 10°, so the trigger can believe a pass is
continuing when the link is already unusable. `forecast.time_to_los` answers this
exactly. That is a **behaviour change**, so it needs its own A/B against V1 rather
than being folded in silently.

### 15.14.1 Handover A/B — the trigger was dead code, and fixing it changes nothing here

`python experiments/handover_ab.py --preset india4-storm --runs 12`

One variable: how proactive handover decides a pass is ending.
**A** = V1, elevation at `t+lead` vs the *configured* mask.
**B** = `forecast.time_to_los`, which folds in the mask, the steering limit and
the SNR floor.

**The V1 defect is larger than expected.** Over one SAT-002 pass at Delhi:

```
353 s  look visible to the V1 trigger (elev >= configured 10 deg)
 17 s  are actually usable            (elev >= effective 30 deg)
336 s  DEAD BAND = 95% of apparent visibility
```

Consequently **V1 performs 0.000 proactive handovers per run**: it waits for
elevation to fall below 10 deg, which happens ~336 s after the link died, and
`_release_lost_sessions` has long since ended the session. *V1 proactive handover
is inert for phased-array stations* — a latent bug, not a tuning issue.

**And the fix produces no benefit here.**

| metric | A elevation | B forecast | delta |
|---|---|---|---|
| sessions_interrupted | 1.417 | 1.417 | +0.000 |
| proactive_handovers | **0.000** | **0.583** | +0.583 |
| delivered_gbit | 266.61 | 266.25 | **−0.364** |
| completion_rate | 0.479 | 0.479 | +0.000 |
| sla_compliance | 0.479 | 0.479 | +0.000 |

The experiment *can* show a difference — 30 % of pass endings have an alternative
station visible — so this is a valid null. Two reasons for it:

1. **The usable window is 17 seconds.** A handover costs `setup_time_s` (2 s of
   slewing). Switching into the tail of a 17 s window rarely repays that, and B's
   0.58 handovers/run cost slightly more than they recover.
2. `sessions_interrupted` counts **failure**-driven interruptions, not LOS, so it
   was the wrong primary metric for this intervention. The outcome metrics that do
   apply — delivered and completion — show no gain.

**Decision: keep `handover_mode="forecast"` opt-in; the default stays
`"elevation"`.** The fix is *correct* — inert code becomes working code — but it
is KPI-neutral-to-slightly-negative in this geometry, and adopting it by default
would be a change with no measured benefit. It becomes worth switching on wherever
usable windows are long enough for a 2 s slew to repay: dish stations (no scan
limit), lower bands, or higher-elevation geometries.

This is the same conclusion pattern as everywhere else in V2 — the mechanism was
genuinely broken, fixing it genuinely changed behaviour, and the behaviour change
did not improve the network.

---

# 15.15 V2 closed — conclusion and reusable method

## The conclusion

> The current X-NioS digital twin is sufficiently deterministic that its
> operationally relevant behaviour is better handled by analytical physics and
> simple control rules. Every tested source of uncertainty either lacked an
> observable precursor, had its signal destroyed by aggregation, noise or
> observability limits, or provided no incremental decision value over a simpler
> baseline.

This was **measured, not assumed**. Each rejection has an experiment and a
control behind it.

| Area | Final answer |
|---|---|
| Orbital geometry, LOS, contact windows | analytical — `xnios/forecast.py` |
| Link-loss prediction | no ML value; physics AUC 0.99 and better calibrated |
| SNR prediction | error was the deterministic power allocator, not uncertainty |
| Demand prediction | predictable (R² 0.53) but **no decision value** (+0.09 of a +40.5 Gb lever) |
| Station failure | a VSWR threshold (AUC 0.926) beats the model (0.921) |
| Proactive handover | V1 trigger is inert for phased arrays; the analytical fix is correct but KPI-neutral here |
| Queue @ +180/300 s | **open** — a simulator investigation, not an ML target |

**Shipped, none of it ML:** the analytical forecaster (live in telemetry and the
console), a VSWR threshold alarm, and an opt-in forecast handover mode with the
latent V1 bug documented rather than silently defaulted on.

## The method worth reusing

The order matters more than any individual result.

```
physics baseline
      -> feasibility gate
      -> apparent headroom?
      -> investigate it before believing it
      -> correct the artifact
      -> re-run with a control
      -> decision-value test
      -> reject unless value is incremental
```

**The four gate criteria** (§15.2) — positive skill, material margin over the
*strongest non-ML* baseline, control attribution, world generalisation — plus a
fifth learned at Stage 6: **decision value**. Predictability is necessary and not
sufficient.

**Five ways a genuinely uncertain quantity turns out unlearnable**, all found by
measurement, not theory:

| Failure mode | Found in |
|---|---|
| Memorylessness — no precursor exists | Poisson failures; Poisson arrivals |
| Aggregation — independent sources average the signal away | network-level demand |
| Observation noise — precursor buried in shot noise | coarse arrival chunks |
| Observability duty cycle — measurable only 2.3 % of the time | station health via the link |
| Univariate sufficiency — one threshold already extracts it | station housekeeping |

**Three experimental-validity checks**, each of which caught a wrong answer here:

1. **A null needs a positive control.** Stage 6's first result had every policy
   tied — because with 4 beams/station the scheduler was never constrained. The
   FCFS→LJF gap (+40.5 Gb) is what makes the eventual null meaningful.
2. **Split by *world*, not by run.** The India presets pin satellites explicitly,
   so every seed shared one visibility backbone; the model memorised it and met it
   again in the held-out runs. Fixing this collapsed queue headroom from +0.29 to
   +0.007.
3. **Check the baseline is the strongest one available.** An under-powered
   baseline manufactures headroom: the SNR "gap" was the power allocator, and the
   queue "gap" was `throughput × horizon` assuming a contact lasts forever.

And one measurement rule: **a metric computed inside the object it is measuring is
circular.** The handover dead band read 0 % when measured inside `contact_windows`
(which returns only usable time) and 95 % when measured over the geometric window.

## When to reopen

Not because the outcome contains no ML, and not by manufacturing a harder
degradation model or a bigger network so the project has an AI component.

Reopen when a **physically justified** new source of uncertainty arrives — real
hardware telemetry, realistic interference, multi-mode hardware degradation, a
substantially different band (Ka and above, where rain fade stops being marginal),
or a realistic traffic environment. Then re-run these same gates unchanged.
