/**
 * The operator console.
 *
 * One scrolling page, three named jobs:
 *
 *   PLAN     ask the network for something — request in, plan out, capacity booked
 *   OPERATE  what the network is doing right now — status, segment, links, timeline
 *   STUDY    which configuration is better, and by how much
 *
 * Sections used to be grouped by data type (map / resources / decision /
 * scenario), which is why the planner never sat anywhere sensible: it is an
 * *action* surface and everything around it was monitoring. Grouping by job
 * puts each panel somewhere it belongs.
 *
 * Every panel in OPERATE and STUDY is a *reader* of one telemetry stream.
 * Nothing there computes simulation state, which is what lets the same
 * components serve a live network, a replay, or a forecast. PLAN is the
 * exception and the only writer — it talks to /api/plan/* and shares no state
 * with runs.
 */

import { Fragment, render } from "preact";
import { html } from "htm/preact";
import { useEffect, useState } from "preact/hooks";

import { api } from "./api.js";
import { bits, clock, pct } from "./format.js";
import { useRun } from "./state.js";
import { DownloadIcon, Eyebrow, Panel, Row } from "./ui.js";
import { Nav } from "./nav.js";
import { HealthHeader } from "./health.js";
import { NetworkMap } from "./map.js";
import { HealthChart, QueueChart, ThroughputChart, UtilisationChart } from "./charts.js";
import { ContactSchedule, EventFeed, LinkMonitor, ResourceMonitor } from "./resources.js";
import { DecisionPanel, IndicatorBreakdown } from "./decision.js";
import { RunControl } from "./control.js";
import { PlanningConsole } from "./plan.js";
import { TimeControl } from "./timeline.js";

const KPI_ORDER = [
  "delivered_gbit",
  "completion_rate",
  "sla_compliance",
  "fairness",
  "mean_wait_s",
  "beam_utilization",
  "energy_kj",
  "gb_per_kj",
  "sessions_interrupted",
  "proactive_handovers",
];

const FACES = ["network", "station", "link", "satellite", "event"];

/** PLAN / OPERATE / STUDY — names the job a group of sections belongs to. */
function ViewHead({ name, note, count }) {
  return html`
    <div class="viewhead">
      <span class="vname">${name}</span>
      <span class="vnote">${note}</span>
      <span class="vspace"></span>
      <span class="vcount">${count}</span>
    </div>
  `;
}

/** Numbered section head — the same grammar PLAN uses inside its own scope. */
function SecHead({ idx, title, note }) {
  return html`
    <div class="xhead">
      <span class="xidx">${idx}</span>
      <span class="xtitle">${title}</span>
      ${note && html`<span class="xnote">${note}</span>`}
    </div>
  `;
}

function Console() {
  const [runId, setRunId] = useState(null);
  const [started, setStarted] = useState(null);
  const [focus, setFocus] = useState(null);
  const { frame, history, info, connected } = useRun(runId);

  // resume the newest run on load, so a page refresh does not lose the view
  useEffect(() => {
    api
      .runs()
      .then((rs) => {
        if (rs.length) {
          setRunId(rs[0].run_id);
          setStarted(rs[0]);
        }
      })
      .catch(() => undefined);
  }, []);

  // Playback: `scrub` is null while following the live edge, otherwise the step
  // being inspected. Every "current record" panel reads `shown`, not `frame`.
  const [scrub, setScrub] = useState(null);
  const [scrubFrame, setScrubFrame] = useState(null);
  useEffect(() => {
    setScrub(null);
    setScrubFrame(null);
  }, [runId]);
  useEffect(() => {
    if (scrub === null || !runId) return;
    let cancelled = false;
    api
      .frame(runId, scrub)
      .then((f) => !cancelled && setScrubFrame(f))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [runId, scrub]);

  const run = info || started;
  const shown = scrub === null ? frame : scrubFrame || frame;
  const net = shown && shown.record.network;

  // keep a rolling event log across frames (each frame carries only its own)
  const [events, setEvents] = useState([]);
  useEffect(() => setEvents([]), [runId]);
  useEffect(() => {
    if (!frame || !frame.record.events.length) return;
    setEvents((prev) => [...prev, ...frame.record.events].slice(-200));
  }, [frame]);

  // Same idea for links. A frame lists only the pairs visible at that instant,
  // so a table bound straight to it empties itself the moment a pass ends. Fold
  // each frame into a per-run registry instead: rows persist with their last
  // known values, flagged in-view or not, plus the peak rate they reached.
  const [links, setLinks] = useState([]);
  useEffect(() => setLinks([]), [runId]);
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
          ...l,
          key,
          inView: true,
          t_last: t,
          peak_rate_bps: Math.max(l.rate_bps, (old && old.peak_rate_bps) || 0),
        });
      }
      return [...byKey.values()];
    });
  }, [shown]);

  return html`
    <${Fragment}>
      <${Nav}
        right=${run &&
        html`<span>
          ${connected && html`<span class="dotlive" style=${{ marginRight: "8px" }}></span>`}
          ${run.status === "running"
            ? `${pct(run.progress)} · T+${clock((frame && frame.record.t) || 0)}`
            : run.status}
        </span>`}
      />

      <main class="shell">
        <!-- ---------------------------------------------------------- masthead -->
        <section id="overview" class="animate-rise">
          <${Eyebrow}>Physics-driven communication planning<//>
          <h1 class="hero-title">Ask the network. Watch it work. Prove it was right.</h1>
          <p class="hero-lead">
            One request in, a plan out — station, timing, beam requirement and the capacity behind
            it, all from exact orbital mechanics rather than a learned model. Below that, the live
            network the plan executes on, and the experiments that decide which policy runs it.
          </p>

          <div class="meta-row">
            <span>
              <span class="dotlive" style=${{ marginRight: "8px" }}></span>
              <span class="on">${connected ? "live" : (run && run.status) || "idle"}</span>
            </span>
            <span>Records <span class="on">${(run && run.steps) || 0}</span>/${(run && run.total_steps) || 0}</span>
            <span>Sim clock <span class="on">T+${clock((net && net.t) || 0)}</span></span>
            <span>Schema <span class="on">${(shown && shown.record.schema_version) || "—"}</span></span>
            ${run &&
            run.meta &&
            html`<span>
              ${`${run.meta.n_satellites} sats · ${run.meta.n_stations} stations · ${run.meta.n_beams_total} beams`}
            </span>`}
          </div>
        </section>

        <!-- =============================================================== PLAN -->
        <div class="viewgroup" id="plan">
          <${ViewHead} name="PLAN" note="ask the network for something" count="3 sections" />
          <${PlanningConsole} />
        </div>

        <!-- ============================================================ OPERATE -->
        <div class="viewgroup" id="operate">
          <${ViewHead} name="OPERATE" note="what the network is doing right now" count="4 sections" />

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
              t=${(net && net.t) || 0}
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
              <div class="stack">
                <${Panel} title="Resource monitor" bodyClass="tight">
                  <${ResourceMonitor} frame=${shown} onFocus=${setFocus} focus=${focus} />
                <//>
                <${Panel} title="Contact forecast — analytical" bodyClass="tight">
                  <${ContactSchedule} frame=${shown} />
                <//>
                <${Panel} title="Events" bodyClass="tight">
                  <${EventFeed} events=${events} />
                <//>
              </div>
            </div>
          </section>

          <section class="section">
            <${SecHead} idx="03" title="Links"
                        note="per-link quality, and the network row behind it" />
            <div class="grid-links">
              <${Panel}
                title="Link quality monitor"
                bodyClass="tight-2"
                action=${runId &&
                html`<a class="export-link" href=${api.exportUrl(runId, "link")}>
                  <${DownloadIcon} size=${12} /> link.csv
                </a>`}
              >
                <${LinkMonitor} links=${links} />
              <//>

              <${Panel} title="Network row">
                ${net
                  ? html`<div>
                      <${Row} k="Delivered" v=${bits(net.bits_delivered_total)} />
                      <${Row} k="Queued" v=${bits(net.queue_bits)} />
                      <${Row} k="Completed" v=${`${net.n_completed}/${net.n_sats}`} />
                      <${Row} k="Waiting" v=${net.n_waiting} />
                      <${Row} k="Beams transmitting" v=${`${net.beams_active} / ${net.beams_total}`} />
                      <${Row}
                        k="Beams available"
                        v=${`${net.beams_available} / ${net.beams_total}`}
                        accent=${net.beams_available < net.beams_total ? "var(--st-warn)" : undefined}
                      />
                      ${/* the two rows that explain an empty link table: no visible
                            pair means the constellation is simply out of range */ null}
                      <${Row}
                        k="Visible pairs"
                        v=${net.n_visible_pairs}
                        accent=${net.n_visible_pairs === 0 ? "var(--st-warn)" : undefined}
                      />
                      <${Row} k="Sats with a link" v=${`${net.n_sats_with_link} / ${net.n_sats}`} />
                      <${Row} k="Contention" v=${net.contention_ratio.toFixed(2)} />
                      <${Row} k="Coverage" v=${pct(net.coverage)} />
                      <${Row} k="Mean SINR" v=${`${net.mean_sinr_db.toFixed(1)} dB`} />
                      <${Row} k="Radiated power" v=${`${net.power_w.toFixed(1)} W`} />
                      <${Row} k="Energy" v=${`${(net.energy_j_total / 1e3).toFixed(2)} kJ`} />
                      <${Row} k="Interruptions" v=${net.interruptions_total} />
                      <${Row}
                        k="Handovers"
                        v=${`${net.handovers_total} (${net.proactive_handovers_total} proactive)`}
                      />
                      <${Row} k="Decision latency" v=${`${net.decision_ms.toFixed(3)} ms`} />
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

        <!-- ============================================================== STUDY -->
        <div class="viewgroup" id="study">
          <${ViewHead} name="STUDY" note="which configuration is better, and by how much"
                       count="2 sections" />

          <section class="section flush">
            <${SecHead} idx="01" title="Scenario"
                        note="the four pluggable axes — scheduler × bandwidth × power × frequency" />
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
            <${SecHead} idx="02" title="Result"
                        note="a KPI vector, never one score" />
            <div class="grid-decision">
              <${Panel}
                title="Run summary"
                action=${runId &&
                html`<div class="export-links">
                  ${FACES.map(
                    (f) => html`<a key=${f} class="export-link" href=${api.exportUrl(runId, f)}>${f}</a>`,
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
                      ${run.meta &&
                      html`<${Fragment}>
                        <${Row} k="Weather model" v=${run.meta.weather_model} />
                        <${Row} k="Failures" v=${run.meta.dynamics ? "enabled" : "off"} />
                        <${Row} k="Handover" v=${run.meta.handover ? "enabled" : "off"} />
                        <${Row} k="Seed" v=${run.meta.seed === null ? "—" : run.meta.seed} />
                      <//>`}
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

        <footer class="footer">
          X-NioS · physics-driven communication planning · telemetry schema
          ${(shown && shown.record.schema_version) || "1.0"} · quote is free, accept consumes
          capacity · channel assigned at execution
        </footer>
      </main>
    <//>
  `;
}

const root = document.getElementById("root");
root.innerHTML = ""; // drop the boot placeholder
render(html`<${Console} />`, root);
