/**
 * Resource + link monitors — the "what is every station and every link doing
 * right now" half of V2 Phase 1. Both read straight from the current telemetry
 * record: no aggregation happens in the browser beyond formatting.
 */

import { html } from "htm/preact";
import { useMemo } from "preact/hooks";
import { ber, bps, cn, hz, pct } from "./format.js";
import { Badge, Bar, Empty } from "./ui.js";

/** Seconds in the units an operator reads a pass in. */
export function fmtDur(s) {
  if (s < 0) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m`;
}

/**
 * Upcoming contacts, straight from the analytical forecaster.
 *
 * Not a prediction: `xnios/forecast.py` solves the geometry in closed form and
 * agrees with the simulator to float precision. It is the answer to the question
 * the console previously could not answer at all — "when does this satellite get
 * another chance?" — and for a precessing LEO ground track that is often hours.
 */
export function ContactSchedule({ frame }) {
  if (!frame) return html`<${Empty}>awaiting telemetry<//>`;
  const sats = frame.record.satellites;
  const inContact = sats
    .filter((s) => s.time_to_los_s >= 0)
    .sort((a, b) => a.time_to_los_s - b.time_to_los_s);
  const upcoming = sats
    .filter((s) => s.time_to_los_s < 0 && s.next_contact_s >= 0 && s.backlog_bits > 0)
    .sort((a, b) => a.next_contact_s - b.next_contact_s)
    .slice(0, 8);
  const stranded = sats.filter((s) => s.next_contact_s < 0 && s.backlog_bits > 0).length;

  if (!inContact.length && !upcoming.length && !stranded)
    return html`<${Empty}>no contacts forecast<//>`;

  return html`
    <div>
      ${inContact.length > 0 &&
      html`<div class="fc-group">
        <span class="label">In contact — losing signal in</span>
        ${inContact.map(
          (s) => html`<div class="fc-row" key=${s.sat_id}>
            <span class="fc-id">${s.sat_id}</span>
            <span class="fc-mid">${s.current_station || "—"}</span>
            <span class=${cn("fc-t", s.time_to_los_s < 60 && "los-soon")}>
              ${fmtDur(s.time_to_los_s)}
            </span>
          </div>`,
        )}
      </div>`}
      ${upcoming.length > 0 &&
      html`<div class="fc-group">
        <span class="label">Next contact</span>
        ${upcoming.map(
          (s) => html`<div class="fc-row" key=${s.sat_id}>
            <span class="fc-id">${s.sat_id}</span>
            <span class="fc-mid">
              ${s.next_contact_station} · ${fmtDur(s.contact_window_s)} window
            </span>
            <span class="fc-t">${fmtDur(s.next_contact_s)}</span>
          </div>`,
        )}
      </div>`}
      ${stranded > 0 &&
      html`<p class="fc-note">
        ${`${stranded} satellite${stranded > 1 ? "s" : ""} with data have no contact inside 24 h — a geometry limit, not a scheduling one.`}
      </p>`}
    </div>
  `;
}

function toneFor(h) {
  if (!h) return "neutral";
  if (!h.up) return "crit";
  if (h.level === "critical") return "crit";
  if (h.level === "high" || h.level === "moderate") return "warn";
  return "ok";
}

export function ResourceMonitor({ frame, onFocus, focus }) {
  if (!frame) return html`<${Empty}>awaiting telemetry<//>`;
  const health = Object.fromEntries(frame.health.stations.map((h) => [h.station_id, h]));

  return html`
    <div>
      ${frame.record.stations.map((g) => {
        const h = health[g.station_id];
        return html`
          <div
            key=${g.station_id}
            class=${cn("gs-row", focus === g.station_id && "focused")}
            onMouseEnter=${() => onFocus && onFocus(g.station_id)}
            onMouseLeave=${() => onFocus && onFocus(null)}
          >
            <div class="gs-row-head">
              <span class="gs-row-id">${g.station_id}</span>
              <${Badge} tone=${toneFor(h)}>
                ${!g.up ? "offline" : g.degraded ? "degraded" : "nominal"}
              <//>
            </div>

            <div class="gs-row-grid">
              <div>
                <span class="label">Beams ${g.beams_active}/${g.beams_available}</span>
                <${Bar}
                  value=${g.beam_utilization}
                  accent=${g.beams_available < g.beams_total ? "var(--st-warn)" : undefined}
                />
                <div class="gs-row-sub">
                  ${g.beams_available < g.beams_total
                    ? `${g.beams_total - g.beams_available} lost of ${g.beams_total}`
                    : `${g.beams_total} fitted`}
                </div>
              </div>
              <div>
                <span class="label">Links ${g.connected_sats.length}/${g.visible_sats}</span>
                <${Bar}
                  value=${g.visible_sats ? g.connected_sats.length / g.visible_sats : 0}
                />
                <div class="gs-row-sub">serving / in view</div>
              </div>
              <div>
                <span class="label">BW ${pct(g.bandwidth_utilization)}</span>
                <${Bar} value=${g.bandwidth_utilization} />
                ${/* both in MHz on one line — "50 MHz of 300 MHz" wrapped the column */ null}
                <div class="gs-row-sub">
                  ${`${(g.bandwidth_alloc_hz / 1e6).toFixed(0)} / ${(g.bandwidth_pool_hz / 1e6).toFixed(0)} MHz`}
                </div>
              </div>
              <div>
                <span class="label">Rate</span>
                <div class="gs-row-val">${bps(g.rate_bps)}</div>
                <div class="gs-row-sub">
                  ${g.connected_sats.length ? `${g.mean_sinr_db.toFixed(1)} dB SINR` : "idle"}
                </div>
              </div>
            </div>

            ${g.connected_sats.length > 0 &&
            html`<div class="gs-serving">
              <span class="label">Serving</span>
              ${g.connected_sats.map((id) => html`<span class="chip-sm" key=${id}>${id}</span>`)}
            </div>`}

            <div class="gs-row-foot">
              <span>${g.weather} · ${g.rain_fade_db.toFixed(1)} dB</span>
              <span>${g.link_power_w.toFixed(1)} W radiated</span>
              <span>${g.channels_in_use}/${g.n_channels} ch · ${g.phased_array ? "phased" : "dish"}</span>
              ${h && html`<span class="reason">${h.reasons[0]}</span>`}
            </div>
          </div>
        `;
      })}
    </div>
  `;
}

const COLUMNS = ["Link", "Quality", "Elev", "LOS in", "BW", "Pwr", "Rate"];

/** Same margin the health monitor scores link_quality on (xnios/health.py):
 *  0 at the lock threshold (-2 dB), 1 at "comfortable" (+12 dB). Keeping the
 *  formula identical means a full bar here and a green Link Quality indicator
 *  in the health breakdown always agree. */
const SINR_FLOOR_DB = -2;
const SINR_GOOD_DB = 12;
const quality = (sinrDb) =>
  Math.max(0, Math.min(1, (sinrDb - SINR_FLOOR_DB) / (SINR_GOOD_DB - SINR_FLOOR_DB)));

const sinrTone = (sinrDb) =>
  sinrDb < 4 ? "var(--st-crit)" : sinrDb < 10 ? "var(--st-warn)" : "var(--st-ok)";

const STATE = {
  serving: { label: "serving", cls: "" },
  visible: { label: "visible", cls: "dim" },
  passed: { label: "passed", cls: "gone" },
};

/**
 * Every link the run has *ever* seen, not just the ones visible this instant.
 *
 * A LEO pass is minutes long inside a half-hour run, so a table bound to the
 * current frame empties itself the moment the satellite sets — which is exactly
 * when you want to look at what the link achieved. Rows therefore persist for
 * the whole run and carry their state: serving now, visible but unserved, or
 * passed (last-known values, held).
 */
export function LinkMonitor({ links }) {
  const rows = useMemo(() => {
    const withState = links.map((l) => ({
      ...l,
      state: !l.inView ? "passed" : l.active ? "serving" : "visible",
    }));
    const rank = { serving: 0, visible: 1, passed: 2 };
    return withState.sort(
      (a, b) => rank[a.state] - rank[b.state] || b.sinr_db - a.sinr_db,
    );
  }, [links]);

  if (rows.length === 0) return html`<${Empty}>no links yet<//>`;

  const live = rows.filter((r) => r.state !== "passed").length;

  return html`
    <div class="link-wrap">
      <div class="link-summary">
        <span class="on">${rows.filter((r) => r.state === "serving").length}</span> serving ·${" "}
        <span class="on">${live}</span> in view ·${" "}
        <span class="on">${rows.length}</span> seen this run
      </div>
      <div class="link-scroll">
        <table class="link-table">
          <thead>
            <tr>
              ${COLUMNS.map((h) => html`<th key=${h} class="label">${h}</th>`)}
            </tr>
          </thead>
          <tbody>
            ${rows.map((l) => {
              const st = STATE[l.state];
              return html`
                <tr key=${l.key} class=${cn(st.cls)}>
                  <td>
                    <div class="link-id">
                      <span class=${cn("link-dot", l.state !== "serving" && "off")}></span>
                      <span>${l.sat_id}<span class="link-arrow"> → </span>${l.station_id}</span>
                      ${l.slewing && html`<span class="link-flag">slew</span>`}
                      ${l.channel !== null && html`<span class="link-flag">ch${l.channel}</span>`}
                      <span class="link-state">${st.label}</span>
                    </div>
                  </td>
                  <td>
                    <div class="link-q">
                      <${Bar} value=${quality(l.sinr_db)} accent=${sinrTone(l.sinr_db)} />
                      <span class="link-q-num" style=${{ color: sinrTone(l.sinr_db) }}>
                        ${l.sinr_db.toFixed(1)} dB
                      </span>
                    </div>
                    <span class="link-inr">
                      BER ${ber(l.ber)}${l.inr_db > -20 ? ` · I/N ${l.inr_db.toFixed(0)}` : ""}
                    </span>
                  </td>
                  <td>${l.elev_deg.toFixed(0)}°</td>
                  ${/* exact seconds to loss of signal, from xnios/forecast.py */ null}
                <td class=${cn(l.time_to_los_s >= 0 && l.time_to_los_s < 60 && "los-soon")}>
                  ${l.time_to_los_s >= 0 ? fmtDur(l.time_to_los_s) : "—"}
                </td>
                  <td>${hz(l.alloc_bw_hz)}</td>
                  <td>${l.alloc_power_w.toFixed(1)}W</td>
                  <td>
                    ${l.state === "serving" ? bps(l.rate_bps) : html`<span class="muted">peak ${bps(l.peak_rate_bps)}</span>`}
                  </td>
                </tr>
              `;
            })}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

const EVENT_TONE = {
  station_fail: "var(--st-crit)",
  beam_fail: "var(--st-warn)",
  interrupt: "var(--st-crit)",
  station_recover: "var(--st-ok)",
  beam_recover: "var(--st-ok)",
  recover: "var(--st-ok)",
  complete: "var(--st-ok)",
};

export function EventFeed({ events }) {
  if (events.length === 0) return html`<${Empty}>no events yet<//>`;
  return html`
    <div class="event-scroll">
      ${events
        .slice()
        .reverse()
        .map(
          (e, i) => html`
            <div class="event" key=${`${e.t}-${e.kind}-${i}`}>
              <span class="event-t">T+${e.t.toFixed(0)}s</span>
              <span class="event-kind" style=${{ color: EVENT_TONE[e.kind] || "var(--dim)" }}>
                ${e.kind.replace(/_/g, " ")}
              </span>
              <span class="event-who">${[e.sat_id, e.station_id].filter(Boolean).join(" · ")}</span>
            </div>
          `,
        )}
    </div>
  `;
}
