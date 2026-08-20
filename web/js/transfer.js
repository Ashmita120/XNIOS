/**
 * TRANSFER — what happened to the request the operator actually made.
 *
 * Everything here traces back to the accepted plan. Nothing is driven by a
 * scenario preset, which is the invariant the whole restructure exists to
 * enforce: an operator should never have to know that simulation presets exist.
 *
 *   01 Status    promised / delivered / remaining / completion, from the run
 *   02 Passes    a Gantt of the booked windows, with a live NOW marker — the
 *                answer to "when does my data go down, and from where"
 *   03 Execution per-satellite progress and the events for those satellites only
 *
 * The map lives in the engineer view. A world map answers "where is everything";
 * an operator asked "when is my pass", and a timeline answers that directly.
 */

import { html } from "htm/preact";
import { clock } from "./format.js";

const g1 = (v) => (v === null || v === undefined ? "—" : v.toFixed(1));

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

/**
 * Pass timeline. One lane per station, one bar per booked window, positioned
 * against a shared span so the lanes are comparable. The NOW marker is the live
 * simulation clock, so during execution you watch the marker cross your passes.
 */
function PassTimeline({ commitments, now }) {
  if (!commitments.length) return html`<div class="xempty">nothing booked</div>`;

  const t0 = 0;
  const t1 = Math.max(...commitments.map((c) => c.t_end)) * 1.05 || 1;
  const span = t1 - t0 || 1;
  const pct = (t) => `${((t - t0) / span) * 100}%`;

  const stations = [...new Set(commitments.map((c) => c.station))].sort();
  const ticks = Array.from({ length: 5 }, (_, i) => t0 + (span * i) / 4);

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
                      }`}
                      style=${{ left: pct(c.t_start), width: pct(c.t_end - c.t_start + t0) }}
                      title=${`${c.request_id} · ${c.gbit.toFixed(2)} Gbit`}
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
      <div class="gantt-lane axis">
        <div class="gantt-name"></div>
        <div class="gantt-track">
          ${ticks.map(
            (t, i) => html`<span key=${i} class="gantt-tick" style=${{ left: pct(t) }}>
              T+${clock(t)}
            </span>`,
          )}
        </div>
      </div>
    </div>
  `;
}

export function TransferConsole({ ledger, run, frame, onExecute, busy }) {
  const commitments = (ledger && ledger.commitments) || [];
  const promised = (ledger && ledger.total_gbit) || 0;

  if (!commitments.length) {
    return html`
      <div class="xn-plan">
        <section class="xsec">
          <${Head} idx="01" title="Your transfer"
                   note="accept a plan in PLAN and it appears here" />
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

  // events belonging to the booked satellites only
  const events = rec ? rec.events.filter((e) => booked.has(e.sat_id)) : [];

  return html`
    <div class="xn-plan">
      <!-- ------------------------------------------------------------ 01 -->
      <section class="xsec">
        <${Head} idx="01" title="Your transfer"
                 note=${run ? `run ${run.run_id} · ${run.status}` : "not executed yet"} />
        <div class="xpanel">
          <div class="xverdict">
            <span class="tag">
              ${!run ? "READY TO EXECUTE" : done ? "COMPLETE" : "EXECUTING"}
            </span>
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
                    onClick=${onExecute}>
              ${run ? "Re-execute" : "Execute plan"}
            </button>
            ${run &&
            html`<span class="xnote" style=${{ marginLeft: 0, alignSelf: "center" }}>
              ${run.steps}/${run.total_steps} steps · T+${clock(now || 0)}
            </span>`}
          </div>
        </div>
      </section>

      <!-- ------------------------------------------------------------ 02 -->
      <section class="xsec">
        <${Head} idx="02" title="Pass timeline"
                 note="when your data goes down, and from which station" />
        <div class="xpanel">
          <${PassTimeline} commitments=${commitments} now=${now} />
        </div>
      </section>

      <!-- ------------------------------------------------------------ 03 -->
      <section class="xsec">
        <${Head} idx="03" title="Execution"
                 note="only the satellites your request booked" />
        <div class="xcols" style=${{ gridTemplateColumns: "1fr 1fr" }}>
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
                      <${Meter}
                        filled=${s.backlog0_bits > 0
                          ? Math.round((s.delivered_bits / s.backlog0_bits) * 8)
                          : 0}
                      />
                    </div>
                  `,
                )
              : html`<div class="xempty">execute the plan to see progress</div>`}
          </div>

          <div class="xpanel">
            <div class="xcap">Events <span class="n">${events.length}</span></div>
            ${events.length
              ? html`<div class="xscroll">
                  <table>
                    <thead><tr><th>T</th><th>Event</th><th>Satellite</th><th>Station</th></tr></thead>
                    <tbody>
                      ${events.map(
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
              : html`<div class="xempty">no events for your satellites</div>`}
          </div>
        </div>
      </section>
    </div>
  `;
}
