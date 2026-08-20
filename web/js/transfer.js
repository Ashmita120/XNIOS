/**
 * TRANSFER — what happened to the request the operator actually made.
 *
 * Everything traces back to the accepted plan. Nothing is driven by a scenario
 * preset, which is the invariant the whole split exists to enforce.
 *
 *   01 Status     promised / delivered / remaining, and the execute control
 *   02 Passes     a Gantt of booked windows with a live NOW marker
 *   03 Resources  what the request actually consumed — station, beam, channel,
 *                 bandwidth, power, pointing, scan angle. This is where the
 *                 phased array stops being an abstraction: you can see which
 *                 aperture formed which beam, where it steered, how far off
 *                 boresight that was, and what the resulting link carried.
 *   04 Delivery   the transfer's own curve against its promise, plus the
 *                 operational KPIs (wait, dropped, interruptions, handovers)
 *   05 Events     session start/end/handover/failure for these satellites only
 *
 * Links and events are ACCUMULATED by the shell, not read from the current
 * frame. A telemetry record carries only what happened in its own step, so a
 * finished run's last frame has no events and no visible links — reading it
 * directly shows an empty panel for a transfer that plainly worked.
 */

import { html } from "htm/preact";
import { bps, clock, hz } from "./format.js";

const g1 = (v) => (v === null || v === undefined ? "—" : v.toFixed(1));
const g2 = (v) => (v === null || v === undefined ? "—" : v.toFixed(2));

const Head = ({ idx, title, note }) => html`
  <div class="xhead">
    <span class="xidx">${idx}</span>
    <span class="xtitle">${title}</span>
    ${note && html`<span class="xnote">${note}</span>`}
  </div>
`;

const Meter = ({ filled, total = 8 }) => html`
  <div class="xmeter">
    ${Array.from({ length: total }, (_, i) => html`<i key=${i} class=${i < filled ? "on" : ""}></i>`)}
  </div>
`;

const Hero = ({ label, value, unit, on, filled }) => html`
  <div class=${on ? "xhero on" : "xhero"}>
    <div class="l">${label}</div>
    <div class="n">${value}${unit && html`<em>${unit}</em>`}</div>
    <${Meter} filled=${filled} />
  </div>
`;

const Row = ({ k, v, u, tone }) => html`
  <div class="xrow">
    <span class="k">${k}</span>
    <span class=${tone ? `v ${tone}` : "v"}>${v}${u && html`<span class="u">${u}</span>`}</span>
  </div>
`;

/** Cumulative delivered at time `t`, from the run history. */
function deliveredAt(history, t) {
  let v = 0;
  for (const p of history) {
    if (p.t > t) break;
    v = p.delivered_gbit;
  }
  return v;
}

/**
 * One lane per station, one bar per booked window, against a shared span.
 *
 * After a run, each bar is marked by whether data actually moved through it.
 * A booked window can go unused: the planner quotes at NOMINAL transmit power
 * while the engine runs an adaptive allocator that can push higher, so a
 * transfer often finishes before its later reservations open. Showing the
 * booking and the outcome as the same thing made that look like an error.
 */
function PassTimeline({ commitments, now, history = [] }) {
  if (!commitments.length) return html`<div class="xempty">nothing booked</div>`;
  const ran = history.length > 1;
  const moved = (c) =>
    !ran || deliveredAt(history, c.t_end) - deliveredAt(history, c.t_start) > 0.01;
  const t1 = Math.max(...commitments.map((c) => c.t_end)) * 1.05 || 1;
  const pct = (t) => `${(t / t1) * 100}%`;
  const stations = [...new Set(commitments.map((c) => c.station))].sort();
  const ticks = Array.from({ length: 5 }, (_, i) => (t1 * i) / 4);

  return html`
    <div class="gantt">
      ${stations.map(
        (st) => html`
          <div class="gantt-lane" key=${st}>
            <div class="gantt-name">${st}</div>
            <div class="gantt-track">
              ${commitments
                .filter((c) => c.station === st)
                .map(
                  (c, i) => html`
                    <div
                      key=${i}
                      class=${`gantt-bar ${now !== null && now >= c.t_end ? "done" : ""} ${
                        now !== null && now >= c.t_start && now < c.t_end ? "live" : ""
                      } ${!moved(c) ? "unused" : ""}`}
                      style=${{ left: pct(c.t_start), width: pct(c.t_end - c.t_start) }}
                      title=${`${c.request_id} · ${c.gbit.toFixed(2)} Gbit reserved · ${
                        moved(c) ? "used" : "not needed"
                      }`}
                    >
                      <span>${c.gbit.toFixed(1)}G</span>
                    </div>
                  `,
                )}
              ${now !== null &&
              html`<div class="gantt-now" style=${{ left: pct(Math.min(now, t1)) }}></div>`}
            </div>
          </div>
        `,
      )}
      ${ran &&
      commitments.some((c) => !moved(c)) &&
      html`<div class="gantt-note">
        ${commitments.filter((c) => !moved(c)).length} reserved window(s) were not
        needed — the transfer finished early. The plan quotes at nominal transmit
        power; execution runs an adaptive allocator that can beat it. Release them
        in PLAN to give the capacity back.
      </div>`}
      <div class="gantt-lane axis">
        <div class="gantt-name"></div>
        <div class="gantt-track">
          ${ticks.map(
            (t, i) => html`<span key=${i} class="gantt-tick" style=${{ left: pct(t) }}>T+${clock(t)}</span>`,
          )}
        </div>
      </div>
    </div>
  `;
}

/**
 * The resource picture. One card per link the request used.
 *
 * `beam` is which of the aperture's simultaneous beams was formed; `channel` is
 * the frequency/polarisation slot the allocator chose at execution — the value
 * the plan deliberately refused to guess. `scan` is the angle off boresight,
 * which is what drives gain loss and beam broadening on a phased array.
 */
function Resources({ links }) {
  if (!links.length) {
    return html`<div class="xempty">execute the plan to see the resources it used</div>`;
  }
  // "Resources in use" means USED. A station that merely had line of sight is
  // not a resource this request consumed, and listing it invites the obvious
  // question of why a transfer served from Ahmedabad is showing Delhi.
  //
  // Read from the `served` snapshot, not the live record: by the time a run
  // finishes the session has ended, so the current record reports beam:null for
  // a link that carried the entire payload.
  const served = links.filter((l) => l.ever_active && l.served);
  if (!served.length) {
    return html`<div class="xempty">execute the plan to see the resources it used</div>`;
  }

  return html`
    <div class="res-grid">
      ${served.map((row) => {
        const l = row.served;
        return html`
          <div class="res-card" key=${row.key}>
            <div class="xcap">
              <span>${row.sat_id} → ${row.station_id}</span>
              <span class="n">SERVED</span>
            </div>
            <${Row} k="Beam" v=${`#${l.beam}`} />
            ${/* The channel is only assigned when a station forms more than one
                  beam at once — with a single beam there is no co-channel
                  decision to make, which is the measured result, not a gap. */ null}
            <${Row}
              k="Channel"
              v=${l.channel === null || l.channel === undefined
                ? html`<span class="muted">single beam · no reuse</span>`
                : `CH-${l.channel}`}
              tone=${l.channel === null || l.channel === undefined ? "" : "accent"}
            />
            <${Row} k="Bandwidth" v=${l.alloc_bw_hz ? hz(l.alloc_bw_hz) : "—"} />
            <${Row} k="Tx power" v=${g2(l.alloc_power_w)} u="W" />
            <${Row} k="Pointing" v=${`az ${g1(l.az_deg)} / el ${g1(l.elev_deg)}`} u="°" />
            <${Row} k="Scan angle" v=${g1(l.scan_deg)} u="°"
                    tone=${l.scan_deg > 60 ? "warn" : ""} />
            <${Row} k="Range" v=${g1(l.range_km)} u="km" />
            <${Row} k="SINR" v=${g1(l.sinr_db)} u="dB"
                    tone=${l.sinr_db >= 10 ? "accent" : l.sinr_db >= 3 ? "warn" : "crit"} />
            <${Row} k="Interference"
                    v=${l.inr_db <= -100 ? html`<span class="muted">none</span>` : `${g1(l.inr_db)} dB`} />
            <${Row} k="Rain fade" v=${g1(l.rain_fade_db)} u="dB"
                    tone=${l.rain_fade_db > 1 ? "warn" : ""} />
            <${Row} k="Peak rate" v=${bps(row.peak_rate_bps || l.rate_bps)} tone="accent" />
          </div>
        `;
      })}
    </div>
  `;
}

/** Cumulative delivered against the promise. The operator's whole question. */
function DeliveryCurve({ history, promised }) {
  const pts = history.filter((p) => p.delivered_gbit !== undefined);
  if (pts.length < 2) return html`<div class="xempty">no delivery recorded yet</div>`;

  const W = 100, H = 40;
  const t1 = Math.max(...pts.map((p) => p.t)) || 1;
  const yMax = Math.max(promised, ...pts.map((p) => p.delivered_gbit)) || 1;
  const path = pts
    .map((p, i) => `${i ? "L" : "M"}${(p.t / t1) * W} ${H - (p.delivered_gbit / yMax) * H}`)
    .join(" ");
  const target = H - (promised / yMax) * H;

  return html`
    <svg class="curve" viewBox=${`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
         aria-label="cumulative delivered against the promised volume">
      <line x1="0" y1=${target} x2=${W} y2=${target} class="curve-target" />
      <path d=${`${path} L${W} ${H} L0 ${H} Z`} class="curve-fill" />
      <path d=${path} class="curve-line" />
    </svg>
    <div class="curve-legend">
      <span><i class="k-line"></i> delivered</span>
      <span><i class="k-target"></i> promised ${g1(promised)} Gbit</span>
      <span class="muted">T+0 → T+${clock(t1)}</span>
    </div>
  `;
}

export function TransferConsole({ ledger, run, frame, history = [], links = [], events = [],
                                  onExecute, busy }) {
  const commitments = (ledger && ledger.commitments) || [];
  const promised = (ledger && ledger.total_gbit) || 0;

  if (!commitments.length) {
    return html`
      <div class="xn-plan">
        <section class="xsec">
          <${Head} idx="01" title="Your transfer" note="accept a plan in PLAN and it appears here" />
          <div class="xpanel"><div class="xempty">
            nothing booked yet — request capacity above, then accept the plan
          </div></div>
        </section>
      </div>
    `;
  }

  const rec = frame && frame.record;
  const now = rec ? rec.t : null;
  const booked = new Set(commitments.map((c) => c.satellite_id));
  const sats = rec ? rec.satellites.filter((s) => booked.has(s.sat_id)) : [];
  const delivered = sats.reduce((a, s) => a + s.delivered_bits, 0) / 1e9;
  const remaining = Math.max(0, promised - delivered);
  const done = run && run.status === "done";
  const frac = promised > 0 ? Math.min(1, delivered / promised) : 0;

  const myLinks = links.filter((l) => booked.has(l.sat_id));
  const myEvents = events.filter((e) => booked.has(e.sat_id));
  const sum = (run && run.summary) || null;

  return html`
    <div class="xn-plan">
      <!-- ------------------------------------------------------------ 01 -->
      <section class="xsec">
        <${Head} idx="01" title="Your transfer"
                 note=${run ? `run ${run.run_id} · ${run.status}` : "not executed yet"} />
        <div class="xpanel">
          <div class="xverdict">
            <span class="tag">${!run ? "READY TO EXECUTE" : done ? "COMPLETE" : "EXECUTING"}</span>
            <span class="meta">
              <b>${commitments.length} window(s) · ${new Set(commitments.map((c) => c.request_id)).size} request(s)</b>
              ${[...booked].join(" · ")}
            </span>
          </div>

          <div class="xheros">
            <${Hero} label="Promised" value=${g1(promised)} unit="Gbit" filled=${8} />
            <${Hero} label="Delivered" value=${g1(delivered)} unit="Gbit"
                     on=${frac >= 0.999} filled=${Math.round(frac * 8)} />
            <${Hero} label="Remaining" value=${g1(remaining)} unit="Gbit"
                     filled=${Math.round((remaining / (promised || 1)) * 8)} />
          </div>

          <div class="xbtnrow">
            <button class="xbtn" disabled=${busy || (run && run.status === "running")}
                    onClick=${onExecute}>${run ? "Re-execute" : "Execute plan"}</button>
            ${run &&
            html`<span class="xnote" style=${{ marginLeft: 0, alignSelf: "center" }}>
              ${run.steps}/${run.total_steps} steps · T+${clock(now || 0)}
            </span>`}
          </div>
        </div>
      </section>

      <!-- ------------------------------------------------------------ 02 -->
      <section class="xsec">
        <${Head} idx="02" title="Pass timeline" note="when your data goes down, and from where" />
        <div class="xpanel">
            <${PassTimeline} commitments=${commitments} now=${now} history=${history} />
          </div>
      </section>

      <!-- ------------------------------------------------------------ 03 -->
      <section class="xsec">
        <${Head} idx="03" title="Resources in use"
                 note="the aperture, the beam it formed, and where it steered" />
        <div class="xpanel"><${Resources} links=${myLinks} /></div>
      </section>

      <!-- ------------------------------------------------------------ 04 -->
      <section class="xsec">
        <${Head} idx="04" title="Delivery" note="your transfer against its promise" />
        <div class="xcols" style=${{ gridTemplateColumns: "1.4fr 1fr" }}>
          <div class="xpanel">
            <div class="xcap">Cumulative delivered</div>
            <${DeliveryCurve} history=${history} promised=${promised} />
          </div>
          <div class="xpanel">
            <div class="xcap">Outcome</div>
            ${sum
              ? html`<div>
                  <${Row} k="Completed" v=${`${(sum.completion_rate * 100).toFixed(0)}%`}
                          tone=${sum.completion_rate >= 0.999 ? "accent" : "warn"} />
                  <${Row} k="Dropped" v=${g2(sum.dropped_gbit)} u="Gbit"
                          tone=${sum.dropped_gbit > 1e-3 ? "warn" : ""} />
                  <${Row} k="Mean wait" v=${g1(sum.mean_wait_s)} u="s" />
                  <${Row} k="In system" v=${g1(sum.mean_latency_s)} u="s" />
                  <${Row} k="Throughput" v=${g2(sum.throughput_mbps)} u="Mbps" />
                  <${Row} k="Beam use" v=${`${(sum.beam_utilization * 100).toFixed(1)}%`} />
                  <${Row} k="Interruptions" v=${sum.sessions_interrupted}
                          tone=${sum.sessions_interrupted ? "warn" : ""} />
                  <${Row} k="Handovers" v=${sum.handovers} />
                  <${Row} k="Energy" v=${g2(sum.energy_kj)} u="kJ" />
                </div>`
              : html`<div class="xempty">available when the run finishes</div>`}
          </div>
        </div>
      </section>

      <!-- ------------------------------------------------------------ 05 -->
      <section class="xsec">
        <${Head} idx="05" title="Execution log"
                 note="only the satellites your request booked" />
        <div class="xcols" style=${{ gridTemplateColumns: "1fr 1.4fr" }}>
          <div class="xpanel">
            <div class="xcap">Progress <span class="n">${sats.length} satellite(s)</span></div>
            ${sats.length
              ? sats.map(
                  (s) => html`
                    <div key=${s.sat_id} style=${{ marginBottom: "12px" }}>
                      <${Row} k=${s.sat_id} v=${s.state}
                              tone=${s.state === "transmitting" ? "accent" : ""} />
                      <${Row} k="Delivered" v=${g1(s.delivered_bits / 1e9)} u="Gbit" />
                      <${Row} k="Remaining" v=${g1(s.backlog_bits / 1e9)} u="Gbit" />
                      <${Meter} filled=${s.backlog0_bits > 0
                        ? Math.round((s.delivered_bits / s.backlog0_bits) * 8) : 0} />
                    </div>
                  `,
                )
              : html`<div class="xempty">execute the plan to see progress</div>`}
          </div>

          <div class="xpanel">
            <div class="xcap">Events <span class="n">${myEvents.length}</span></div>
            ${myEvents.length
              ? html`<div class="xscroll">
                  <table>
                    <thead><tr><th>T</th><th>Event</th><th>Satellite</th><th>Station</th></tr></thead>
                    <tbody>
                      ${myEvents
                        .slice()
                        .reverse()
                        .map(
                          (e, i) => html`<tr key=${i}>
                            <td class="muted">T+${clock(e.t)}</td>
                            <td>${e.kind.replace(/_/g, " ")}</td>
                            <td>${e.sat_id}</td>
                            <td>${e.station_id || "—"}</td>
                          </tr>`,
                        )}
                    </tbody>
                  </table>
                </div>`
              : html`<div class="xempty">no events yet — execute the plan</div>`}
          </div>
        </div>
      </section>
    </div>
  `;
}
