# X-NioS — decision log

What has been settled, on what evidence, and what would legitimately reopen it.

This is deliberately not a report. `RESEARCH_PLAN.md` carries the V1/V2 narrative
and the ML feasibility work; this file records the *decisions* so they survive
without re-reading it, and so a future change is measured against a stated
condition rather than an argument.

Every entry names a runnable experiment. Every number below came from that
experiment's committed CSV in `experiments/results/`.

---

## Closed — not justified

### Frequency allocation optimisation
**Verdict: closed.** There is no decision to make, and where one exists the
shipped allocator is already optimal.

`experiments/beam_freq_control.py` → `results/beam_freq_control.csv`

A station forms more than one beam in **0.75–2.29 %** of station-steps and never
more than three. Bracketing the whole decision — full reuse (`coloring`) against
no reuse (`same`) — moves delivered data by **exactly 0.0 Gbit in 5 of the 6
configurations** tested (india8/congested, india8/baseline, global6/congested ×
both beam models). The sixth, global6/congested under Model B, moves 3.3 Gbit
(**+0.22 %**).

On india8/congested, removing reuse raises interference **30×** (INR 0.246 →
7.304) and costs **2.3 dB** of SINR while delivering the same 1733.5 Gbit to four
significant figures — the links sit at the modcod cap, where SINR that far above
the floor buys no rate.

With `n_channels=4 × dual_pol` = 8 orthogonal slots and at most 4 simultaneous
beams, graph colouring always finds a conflict-free assignment. `GraphColorFreq`
is therefore *provably* optimal in every instant measured — the headroom above it
is zero, not merely small.

**Reopens if:** orthogonal reuse slots drop below the number of simultaneous
beams. That is a provisioning question, and it is already priced — a
single-channel array loses 3.16 % (`results/scan_envelope.csv`, reuse sweep).

### Joint beam + frequency optimisation
**Verdict: closed.** Frequency contributes no independent headroom, so a joint
optimiser cannot beat optimising beams alone. Revisit only if the frequency
entry above reopens.

### A solver in the planning path
**Verdict: closed.** `experiments/policy_ladder.py` →
`results/policy_ladder.csv`

The dynamic opportunity-cost rule closes **78 %** of the FCFS→optimal gap at
slack load and **100 %** under real and severe contention, matching the MILP
exactly in 14 of 15 contended worlds, at 18.3 ms against the solver's 50.2 ms.

The residual 2.5 pp is combinatorial, not a bad score: three independent
scoring rules (`oppcost`, `ratio`, `w_avail`) all land on exactly 77.6 %. In the
one failing world `ratio` reorders correctly and still loses, because the
conflict is between a later pair no myopic rule can see.

**Reopens if:** a regime is found where the greedy ceiling is materially below
optimal *and* that regime is operationally relevant. The current gap appears only
at slack load, where the network is not under pressure.

### ML for the current twin
**Verdict: closed.** Detailed in `RESEARCH_PLAN.md` §15; summarised here because
the decision keeps needing restating.

Every candidate target failed a feasibility or value gate against an analytical
or trivial baseline. Link loss: the Stage-1 analytical forecaster reaches AUC
0.992 / 0.990 / 0.996 and the learned model is *worse calibrated* at two
horizons. Station health: a VSWR threshold beats the model on every criterion.

Nothing in the Phase 2/3 work revives it. The multi-request decision gap is
driven by explicit requests, known deadlines, known tiers and deterministic
forecast capacity — the conditions under which optimisation, not learning, is
the correct tool.

**Reopens if:** real hardware telemetry shows a *structured residual* between
measured and forecast quantities that materially changes a decision. The gate is
in §15.15 and is unchanged: reopen for a physically justified source of
uncertainty, never because a component is missing from a diagram.

---

## Open

### Beam configuration under conserved capacity
**Status: open, not worth implementing yet.**

The blocker is physical, not architectural. In the current model `g_over_t_dbk`
is applied **per link**, so N simultaneous beams receive N× the aperture gain for
free — "1 beam vs 4 beams" is literally 4× the hardware. Bandwidth is already
conserved (`station.bandwidth_hz` is a pool the allocators divide); receive gain
is not.

A conserving comparison needs subaperture division (G/T − 10·log10(N) per beam,
with matching width growth), which is one real phased-array architecture but not
the only one — full digital beamforming does not split aperture gain. Choosing
one silently would be inventing a conservation law the simulator does not
represent.

It is also weakly motivated: with stations serving a single satellite ~98 % of
the time, how an aperture is subdivided rarely arises. Concurrency has to rise
materially first, and the only lever that raises it is the scan envelope.

### Scan envelope — the dominant lever, and the least trustworthy number
**Status: open. Blocked on antenna modelling, not on software.**

Reached independently from three directions: coverage, throughput, and beam
concurrency.

`results/realtime_levers.csv` — widening ±60° → ±80° is worth **+27.2 %**
delivered, against ≤ +1.5 % for every other lever tested (satellite bandwidth,
station G/T, beams per station, handover mode, scheduling policy). The ±60°
envelope turns a configured 10° mask into a 30° effective one and discards
**74 %** of geometric contact-seconds.

`results/scan_envelope.csv` — the advantage survives first-order beam broadening:
Model A 807.2 → 1026.9 Gbit, Model B 806.8 → 1020.8. Broadening costs 0.1–0.7 pp,
and no knee appears in any configuration tested, including a no-reuse array.

**The magnitude is not yet a claim about hardware.** Model B is a first-order
projected-aperture model. Grating lobes, element spacing, element patterns and
cross-polarisation are all absent, and the curve flattens past 80° for a
configuration reason — the 10° elevation mask becomes binding — not a physical
one. The honest statement:

> Widening the scan envelope produces a large simulated benefit. The physically
> realisable optimum cannot be determined from the current antenna model.

**Next step:** element spacing → array factor → grating lobes → scan-dependent
pattern, then re-run `experiments/scan_envelope.py` unchanged. That is the only
open question capable of moving a headline number, and it is antenna work rather
than scheduling work.

---

## Standing rule

Four of the six questions asked since V2 have closed as nulls, one produced a
real gap that a 30-line heuristic then captured, and one remains open on physics.
The rule that produced that record:

> Establish that a decision exists before building something to make it. A
> positive control that shows the policies *can* differ comes first; if they
> cannot, the null is the result.

Applied to `0-trivial` in `multirequest_control.py`, to the reuse-slot sweep, and
to `beam_freq_control.py`. It is worth keeping.
