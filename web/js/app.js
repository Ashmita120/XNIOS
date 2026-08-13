/**
 * The operator console.
 *
 * Layout follows the brief: headline health tiles, the satellite map, resource
 * and link monitors, the AI explanation panel, and scenario control. Section
 * rhythm, spacing and type are ARCTROPY's — fixed nav, hairline dividers,
 * wide-tracked eyebrows, a 1320px shell.
 *
 * Every panel is a *reader* of one telemetry stream. Nothing here computes
 * simulation state, which is what makes the same components work later for a
 * live network, a replayed historical run, or a forecast.
 */

import { Fragment, render } from "preact";
import { html } from "htm/preact";
import { useEffect, useState } from "preact/hooks";

import { api } from "./api.js";
import { bits, clock, pct } from "./format.js";
import { useRun } from "./state.js";
import { Badge, DownloadIcon, Eyebrow, Panel, Row } from "./ui.js";
import { Nav } from "./nav.js";
import { HealthHeader } from "./health.js";
import { NetworkMap } from "./map.js";
import { HealthChart, QueueChart, ThroughputChart, UtilisationChart } from "./charts.js";
import { ContactSchedule, EventFeed, LinkMonitor, ResourceMonitor } from "./resources.js";
import { DecisionPanel, IndicatorBreakdown } from "./decision.js";
import { RunControl } from "./control.js";
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
        <!-- ------------------------------------------------------------ header -->
        <section id="overview" class="animate-rise">
          <${Eyebrow}>AI Digital Twin — Phase 1 · State awareness<//>
          <h1 class="hero-title">The network, as it is right now.</h1>
          <p class="hero-lead">
            Every panel below reads one stream: a telemetry record per simulation step, carrying the
            whole network, every station, every link, every satellite and the decision that produced
            them. The same stream feeds the health monitor here, and — next — the feature layer,
            forecast and decision engine.
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

        <div style=${{ marginTop: "32px" }}>
          <${HealthHeader} frame=${shown} />
        </div>

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

        <!-- --------------------------------------------------------------- map -->
        <section id="map" class="section">
          <div class="section-head">
            <div>
              <${Eyebrow}>Satellite map<//>
              <h2 class="section-title">Ground segment</h2>
            </div>
            <p class="section-note">
              Stations, sub-satellite points and every active beam, drawn from the current record.
              Filled diamonds are transmitting; rings mark stations with beams committed. Drag to
              pan, scroll to zoom.
            </p>
          </div>

          <div class="grid-map mt-6">
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

        <!-- --------------------------------------------------------- resources -->
        <section id="resources" class="section">
          <${Eyebrow}>Resource &amp; link telemetry<//>
          <h2 class="section-title">What every beam is doing</h2>

          <div class="grid-charts mt-6">
            <${Panel} title="Throughput"><${ThroughputChart} data=${history} /><//>
            <${Panel} title="Utilisation · beams / bandwidth / coverage">
              <${UtilisationChart} data=${history} />
            <//>
            <${Panel} title="Backlog vs delivered"><${QueueChart} data=${history} /><//>
            <${Panel} title="Health · congestion · failure risk">
              <${HealthChart} data=${history} />
            <//>
          </div>

          <div class="grid-links mt-6">
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

        <!-- ---------------------------------------------------------- decision -->
        <section id="decision" class="section">
          <div class="section-head">
            <div>
              <${Eyebrow}>Decision &amp; explanation<//>
              <h2 class="section-title">Why the network looks like this</h2>
            </div>
            <${Badge}>Phase 4 slot — contract already live<//>
          </div>

          <div class="grid-decision mt-6">
            <${Panel} title="Active configuration"><${DecisionPanel} frame=${shown} /><//>
            <${Panel} title="Health breakdown — click any indicator">
              <${IndicatorBreakdown} frame=${shown} />
            <//>
          </div>
        </section>

        <!-- ---------------------------------------------------------- scenario -->
        <section id="scenario" class="section">
          <${Eyebrow}>Scenario control<//>
          <h2 class="section-title">Run the twin</h2>

          <div class="grid-scenario mt-6">
            <${Panel} title="Configuration">
              <${RunControl}
                current=${run}
                onStarted=${(r) => {
                  setStarted(r);
                  setRunId(r.run_id);
                }}
              />
            <//>

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
          </div>
        </section>

        <footer class="footer">
          X-NioS digital twin · telemetry schema ${(shown && shown.record.schema_version) || "1.0"} ·
          health scores are an explicit weighted scalarisation, computed outside the twin
        </footer>
      </main>
    <//>
  `;
}

const root = document.getElementById("root");
root.innerHTML = ""; // drop the boot placeholder
render(html`<${Console} />`, root);
