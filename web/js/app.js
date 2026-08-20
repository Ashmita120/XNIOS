/**
 * The console — two products behind one shell.
 *
 *   OPERATOR   PLAN (request → quote → accept) → TRANSFER (execute → telemetry)
 *   ENGINEER   OPERATE (network telemetry) + STUDY (scenarios, policies, KPIs)
 *
 * The invariant that motivates the split: **nothing in the operator view may be
 * driven by an unrelated scenario preset.** Every operator metric traces back to
 * the accepted request and its execution, via POST /api/plan/execute. An
 * operator should never have to learn that simulation presets exist.
 *
 * The engineer view keeps everything the research work produced — presets,
 * policy grid, oracle comparison, KPI vector, health breakdown, CSV export. That
 * material is valuable; the mistake was putting it on the same surface as the
 * operator console.
 *
 * The mode is a toggle for now. The two views already read different runs and
 * different endpoints, so promoting the engineer view to its own URL later is a
 * routing change rather than a rebuild.
 */

import { Fragment, render } from "preact";
import { html } from "htm/preact";
import { useEffect, useState } from "preact/hooks";

import { api } from "./api.js";
import { bits, clock, pct } from "./format.js";
import { useRun } from "./state.js";
import { DownloadIcon, Panel, Row } from "./ui.js";
import { Nav } from "./nav.js";
import { HealthHeader } from "./health.js";
import { NetworkMap } from "./map.js";
import { HealthChart, QueueChart, ThroughputChart, UtilisationChart } from "./charts.js";
import { ContactSchedule, EventFeed, LinkMonitor, ResourceMonitor } from "./resources.js";
import { DecisionPanel, IndicatorBreakdown } from "./decision.js";
import { RunControl } from "./control.js";
import { PlanningConsole } from "./plan.js";
import { TransferConsole } from "./transfer.js";
import { TimeControl } from "./timeline.js";

const KPI_ORDER = [
  "delivered_gbit", "completion_rate", "sla_compliance", "fairness",
  "mean_wait_s", "beam_utilization", "energy_kj", "gb_per_kj",
  "sessions_interrupted", "proactive_handovers",
];
const FACES = ["network", "station", "link", "satellite", "event"];

/** PLAN / TRANSFER / OPERATE / STUDY — names the job a group of sections owns. */
const ViewHead = ({ name, note, count }) => html`
  <div class="viewhead">
    <span class="vname">${name}</span>
    <span class="vnote">${note}</span>
    <span class="vspace"></span>
    ${count && html`<span class="vcount">${count}</span>`}
  </div>
`;

/** Numbered section head — the same grammar PLAN and TRANSFER use. */
const SecHead = ({ idx, title, note }) => html`
  <div class="xhead">
    <span class="xidx">${idx}</span>
    <span class="xtitle">${title}</span>
    ${note && html`<span class="xnote">${note}</span>`}
  </div>
`;

/** Identity left, live state right. No prose — this is an instrument. */
function Masthead({ net, ledger, run, connected }) {
  const t = run && run.steps ? null : null;
  return html`
    <div class="masthead">
      <div class="mast-id">
        <span class="mast-name">X-NioS</span>
        <span class="mast-sub">communication planning &amp; orchestration</span>
      </div>
      <div class="mast-stats">
        ${net &&
        html`<span>NET <b>${net.preset}</b></span>
          <span>${net.satellites.length} SAT · ${net.stations.length} GS</span>
          <span>${net.contacts_precomputed} CONTACTS</span>`}
        ${ledger &&
        html`<span>LEDGER <b>${ledger.total_gbit.toFixed(1)} Gbit</b> / ${ledger.commitments.length}</span>`}
        <span>
          <span class="dotlive" style=${{ marginRight: "8px" }}></span>
          <b>${connected ? "LIVE" : run ? run.status.toUpperCase() : "IDLE"}</b>
          ${run && html` T+${clock((run.steps && t) || 0)}`}
        </span>
      </div>
    </div>
  `;
}

function Console() {
  const [mode, setMode] = useState("operator");

  // --- operator state: the network, the ledger, and the run that executed it
  const [net, setNet] = useState(null);
  const [ledger, setLedger] = useState(null);
  const [planRunId, setPlanRunId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [execErr, setExecErr] = useState(null);

  // --- engineer state: scenario runs
  const [runId, setRunId] = useState(null);
  const [started, setStarted] = useState(null);
  const [focus, setFocus] = useState(null);

  const operator = mode === "operator";
  const activeId = operator ? planRunId : runId;
  const { frame, history, info, connected } = useRun(activeId);
  const run = info || (operator ? null : started);

  useEffect(() => {
    api.plan.network().then(setNet).catch(() => undefined);
    refreshLedger();
  }, []);

  // engineer mode resumes the newest *scenario* run, never a plan run
  useEffect(() => {
    if (operator || runId) return;
    api
      .runs()
      .then((rs) => {
        const r = rs.find((x) => x.kind !== "plan");
        if (r) {
          setRunId(r.run_id);
          setStarted(r);
        }
      })
      .catch(() => undefined);
  }, [operator]);

  function refreshLedger() {
    return api.plan.ledger().then(setLedger).catch(() => undefined);
  }

  async function execute() {
    setBusy(true);
    setExecErr(null);
    try {
      const r = await api.plan.execute({ pace_ms: 120 });
      setPlanRunId(r.run_id);
    } catch (e) {
      setExecErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  // A finished plan run reconciles its ledger — windows the transfer turned out
  // not to need are released — so the ledger must be re-read once it lands.
  useEffect(() => {
    if (operator && run && run.status === "done") refreshLedger();
  }, [operator, run && run.run_id, run && run.status]);

  // Playback scrubbing (engineer only — an operator watches their own execution)
  const [scrub, setScrub] = useState(null);
  const [scrubFrame, setScrubFrame] = useState(null);
  useEffect(() => {
    setScrub(null);
    setScrubFrame(null);
  }, [activeId]);
  useEffect(() => {
    if (scrub === null || !activeId) return;
    let cancelled = false;
    api
      .frame(activeId, scrub)
      .then((f) => !cancelled && setScrubFrame(f))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [activeId, scrub]);

  const shown = scrub === null ? frame : scrubFrame || frame;
  const netRec = shown && shown.record.network;

  const [events, setEvents] = useState([]);
  useEffect(() => setEvents([]), [activeId]);
  useEffect(() => {
    if (!frame || !frame.record.events.length) return;
    setEvents((prev) => [...prev, ...frame.record.events].slice(-200));
  }, [frame]);

  // A frame lists only the pairs visible at that instant, so a table bound
  // straight to it empties the moment a pass ends. Fold each frame into a
  // per-run registry instead: rows persist with their last known values.
  //
  // `served` keeps a snapshot from a step where the link was actually carrying
  // a session. Without it the registry ends up holding the moment *after* the
  // transfer finished — active:false, beam:null — so a link that moved the
  // entire payload reports "never used", which is exactly wrong.
  const [links, setLinks] = useState([]);
  useEffect(() => setLinks([]), [activeId]);
  useEffect(() => {
    if (!shown) return;
    const t = shown.record.t;
    setLinks((prev) => {
      const byKey = new Map();
      for (const r of prev) byKey.set(r.key, { ...r, inView: false });
      for (const l of shown.record.links) {
        const key = `${l.sat_id}|${l.station_id}`;
        const old = byKey.get(key);
        byKey.set(key, {
          ...l, key, inView: true, t_last: t,
          peak_rate_bps: Math.max(l.rate_bps, (old && old.peak_rate_bps) || 0),
          ever_active: l.active || (old && old.ever_active) || false,
          served: l.active ? { ...l } : (old && old.served) || null,
        });
      }
      return [...byKey.values()];
    });
  }, [shown]);

  return html`
    <${Fragment}>
      <${Nav}
        mode=${mode}
        onMode=${setMode}
        right=${run &&
        html`<span>
          ${connected && html`<span class="dotlive" style=${{ marginRight: "8px" }}></span>`}
          ${run.status === "running"
            ? `${pct(run.progress)} · T+${clock((frame && frame.record.t) || 0)}`
            : run.status}
        </span>`}
      />

      <main class="shell">
        <${Masthead} net=${net} ledger=${ledger} run=${run} connected=${connected} />

        ${operator
          ? html`
              <!-- ====================================================== PLAN -->
              <div class="viewgroup" id="plan">
                <${ViewHead} name="PLAN" note="ask the network for something" />
                <${PlanningConsole} onLedgerChange=${refreshLedger} />
              </div>

              <!-- ================================================== TRANSFER -->
              <div class="viewgroup" id="transfer">
                <${ViewHead} name="TRANSFER" note="what the network is doing with your request" />
                ${execErr && html`<div class="xn-plan"><div class="xerr">${execErr}</div></div>`}
                ${/* links and events are accumulated across the run, not read
                      from the current frame: a record carries only its own step,
                      so a finished run's last frame has neither */ null}
                <${TransferConsole}
                  ledger=${ledger}
                  run=${run}
                  frame=${shown}
                  history=${history}
                  links=${links}
                  events=${events}
                  busy=${busy}
                  onExecute=${execute}
                />
              </div>
            `
          : html`
              <!-- =================================================== OPERATE -->
              <div class="viewgroup" id="operate">
                <${ViewHead} name="OPERATE" note="the whole network, for engineering" count="4 sections" />

                <section class="section flush">
                  <${SecHead} idx="01" title="Network status"
                              note="the one glanceable band — everything below is detail" />
                  <${HealthHeader} frame=${shown} />
                  ${run &&
                  run.steps > 0 &&
                  html`<${TimeControl}
                    steps=${run.steps}
                    total=${run.total_steps}
                    value=${scrub === null ? run.steps - 1 : scrub}
                    live=${scrub === null}
                    t=${(netRec && netRec.t) || 0}
                    onScrub=${setScrub}
                    onLive=${() => setScrub(null)}
                  />`}
                </section>

                <section class="section">
                  <${SecHead} idx="02" title="Ground segment"
                              note="stations, sub-satellite points and every committed beam" />
                  <div class="grid-map">
                    <div class="map-frame">
                      <${NetworkMap} frame=${shown} focus=${focus} />
                    </div>
                    <${Panel} title="Resource monitor" bodyClass="tight">
                      <${ResourceMonitor} frame=${shown} onFocus=${setFocus} focus=${focus} />
                    <//>
                  </div>
                  ${/* second row so the left column cannot run out under a
                        taller right stack — the old layout left dead space */ null}
                  <div class="grid-pair mt-6">
                    <${Panel} title="Contact forecast — analytical" bodyClass="tight">
                      <${ContactSchedule} frame=${shown} />
                    <//>
                    <${Panel} title="Events" bodyClass="tight">
                      <${EventFeed} events=${events} />
                    <//>
                  </div>
                </section>

                <section class="section">
                  <${SecHead} idx="03" title="Links"
                              note="per-link quality, and the network row behind it" />
                  <div class="grid-links">
                    <${Panel}
                      title="Link quality monitor"
                      bodyClass="tight-2"
                      action=${activeId &&
                      html`<a class="export-link" href=${api.exportUrl(activeId, "link")}>
                        <${DownloadIcon} size=${12} /> link.csv
                      </a>`}
                    >
                      <${LinkMonitor} links=${links} />
                    <//>

                    <${Panel} title="Network row">
                      ${netRec
                        ? html`<div>
                            <${Row} k="Delivered" v=${bits(netRec.bits_delivered_total)} />
                            <${Row} k="Queued" v=${bits(netRec.queue_bits)} />
                            <${Row} k="Completed" v=${`${netRec.n_completed}/${netRec.n_sats}`} />
                            <${Row} k="Waiting" v=${netRec.n_waiting} />
                            <${Row} k="Beams transmitting" v=${`${netRec.beams_active} / ${netRec.beams_total}`} />
                            <${Row} k="Beams available" v=${`${netRec.beams_available} / ${netRec.beams_total}`}
                                    accent=${netRec.beams_available < netRec.beams_total ? "var(--st-warn)" : undefined} />
                            <${Row} k="Visible pairs" v=${netRec.n_visible_pairs}
                                    accent=${netRec.n_visible_pairs === 0 ? "var(--st-warn)" : undefined} />
                            <${Row} k="Sats with a link" v=${`${netRec.n_sats_with_link} / ${netRec.n_sats}`} />
                            <${Row} k="Contention" v=${netRec.contention_ratio.toFixed(2)} />
                            <${Row} k="Coverage" v=${pct(netRec.coverage)} />
                            <${Row} k="Mean SINR" v=${`${netRec.mean_sinr_db.toFixed(1)} dB`} />
                            <${Row} k="Radiated power" v=${`${netRec.power_w.toFixed(1)} W`} />
                            <${Row} k="Energy" v=${`${(netRec.energy_j_total / 1e3).toFixed(2)} kJ`} />
                            <${Row} k="Interruptions" v=${netRec.interruptions_total} />
                            <${Row} k="Handovers"
                                    v=${`${netRec.handovers_total} (${netRec.proactive_handovers_total} proactive)`} />
                            <${Row} k="Decision latency" v=${`${netRec.decision_ms.toFixed(3)} ms`} />
                          </div>`
                        : html`<div class="label">awaiting telemetry</div>`}
                    <//>
                  </div>
                </section>

                <section class="section">
                  <${SecHead} idx="04" title="Timeline"
                              note="the same telemetry stream, across the whole run" />
                  <div class="grid-charts">
                    <${Panel} title="Throughput"><${ThroughputChart} data=${history} /><//>
                    <${Panel} title="Utilisation · beams / bandwidth / coverage">
                      <${UtilisationChart} data=${history} />
                    <//>
                    <${Panel} title="Backlog vs delivered"><${QueueChart} data=${history} /><//>
                    <${Panel} title="Health · congestion · failure risk">
                      <${HealthChart} data=${history} />
                    <//>
                  </div>
                </section>
              </div>

              <!-- ===================================================== STUDY -->
              <div class="viewgroup" id="study">
                <${ViewHead} name="STUDY" note="which configuration is better, and by how much"
                             count="2 sections" />

                <section class="section flush">
                  <${SecHead} idx="01" title="Scenario"
                              note="engineering only — an operator never sees a preset" />
                  <div class="grid-scenario">
                    <${Panel} title="Configuration">
                      <${RunControl}
                        current=${run}
                        onStarted=${(r) => {
                          setStarted(r);
                          setRunId(r.run_id);
                        }}
                      />
                    <//>
                    <${Panel} title="Active configuration"><${DecisionPanel} frame=${shown} /><//>
                  </div>
                </section>

                <section class="section">
                  <${SecHead} idx="02" title="Result" note="a KPI vector, never one score" />
                  <div class="grid-decision">
                    <${Panel}
                      title="Run summary"
                      action=${activeId &&
                      html`<div class="export-links">
                        ${FACES.map(
                          (f) => html`<a key=${f} class="export-link" href=${api.exportUrl(activeId, f)}>${f}</a>`,
                        )}
                      </div>`}
                    >
                      ${run
                        ? html`<div>
                            <${Row} k="Run" v=${run.run_id} />
                            <${Row} k="Scenario" v=${run.name} />
                            <${Row} k="Status" v=${run.status} />
                            <${Row} k="Scheduler" v=${run.policy.scheduler} />
                            <${Row} k="Bandwidth" v=${run.policy.bandwidth_allocator} />
                            <${Row} k="Power" v=${run.policy.power_allocator} />
                            <${Row} k="Frequency" v=${run.policy.freq_allocator} />
                            ${run.summary &&
                            html`<${Fragment}>
                              <div class="label" style=${{ marginTop: "16px" }}>Final KPI vector</div>
                              ${KPI_ORDER.filter((k) => run.summary[k] !== undefined).map(
                                (k) => html`<${Row}
                                  key=${k}
                                  k=${k.replace(/_/g, " ")}
                                  v=${typeof run.summary[k] === "number"
                                    ? run.summary[k].toFixed(3)
                                    : String(run.summary[k])}
                                />`,
                              )}
                            <//>`}
                          </div>`
                        : html`<div class="label">no run yet</div>`}
                    <//>

                    <${Panel} title="Health breakdown — click any indicator">
                      <${IndicatorBreakdown} frame=${shown} />
                    <//>
                  </div>
                </section>
              </div>
            `}

        <footer class="footer">
          X-NioS · ${operator
            ? "every number here traces to your accepted request"
            : "engineering view — scenario presets and policy comparison"}
        </footer>
      </main>
    <//>
  `;
}

const root = document.getElementById("root");
root.innerHTML = ""; // drop the boot placeholder
render(html`<${Console} />`, root);
