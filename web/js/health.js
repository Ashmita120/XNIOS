/**
 * The headline row: Network Health, throughput, congestion, failure risk, and
 * the slot where the AI's recommendation will appear.
 *
 * The recommendation tile deliberately renders "advisory pending" today rather
 * than a fabricated suggestion: nothing in the twin decides yet, the decision
 * engine is V2 Phase 5, and the tile reads `decision.source` / `decision.rationale`
 * — the fields the controller will populate. It becomes live the moment the
 * engine writes them, with no change here.
 */

import { html } from "htm/preact";
import { bps, healthColor, pct, titleCase } from "./format.js";
import { Badge, Stat } from "./ui.js";

const PLACEHOLDERS = [
  "Network health",
  "Throughput",
  "Congestion",
  "Failure risk",
  "AI recommendation",
];

export function HealthHeader({ frame }) {
  if (!frame) {
    return html`<div class="grid-hair grid-health">
      ${PLACEHOLDERS.map(
        (l) => html`<${Stat} key=${l} label=${l} value="—" sub="awaiting telemetry" />`,
      )}
    </div>`;
  }

  const h = frame.health;
  const n = frame.record.network;
  const cong = h.indicators.congestion;
  const risk = h.indicators.failure_risk;
  const avail = h.indicators.availability;
  const dec = frame.record.decision;

  const aiActive = dec && dec.source === "ai" && dec.rationale;
  const [thrValue, thrUnit] = bps(n.throughput_bps, 2).split(" ");

  return html`
    <div class="grid-hair grid-health">
      <${Stat}
        label="Network health"
        value=${h.network_health.toFixed(0)}
        unit="%"
        accent=${healthColor(h.network_health)}
        meter=${h.network_health / 100}
        sub=${`${titleCase(h.level)} · ${avail ? avail.factors.stations_up : "—"} stations up`}
      />
      <${Stat}
        label="Throughput"
        value=${thrValue}
        unit=${thrUnit}
        meter=${Math.min(1, n.beam_utilization)}
        sub=${`${(n.bits_delivered_total / 1e9).toFixed(1)} Gb delivered · ${(n.queue_bits / 1e9).toFixed(1)} Gb queued`}
      />
      <${Stat}
        label="Congestion"
        value=${titleCase(cong.level)}
        accent=${cong.level === "low" ? undefined : "var(--st-warn)"}
        meter=${cong.score}
        sub=${`${n.beams_active}/${n.beams_available} beams busy · ${n.n_waiting} waiting`}
      />
      <${Stat}
        label="Failure risk"
        value=${titleCase(risk.level)}
        accent=${risk.level === "low" ? undefined : "var(--st-crit)"}
        meter=${risk.score}
        sub=${`${risk.factors.stations_down} down · ${risk.factors.beams_lost} beams lost · observed, not forecast`}
      />

      <div class="stat">
        <div style=${{ display: "flex", justifyContent: "space-between", gap: "8px" }}>
          <span class="label">AI recommendation</span>
          <${Badge} tone=${aiActive ? "ok" : "neutral"}>${aiActive ? "active" : "phase 5"}<//>
        </div>
        ${aiActive
          ? html`
              <div class="stat-value-row" style=${{ fontSize: "15px", fontWeight: 500, lineHeight: 1.35 }}>
                ${dec.rationale}
              </div>
              <div class="stat-sub">
                ${Object.entries(dec.expected)
                  .map(([k, v]) => `${titleCase(k)} ${v > 0 ? "+" : ""}${pct(v, 1)}`)
                  .join(" · ")}
              </div>
            `
          : html`
              <div
                class="stat-value-row"
                style=${{ fontSize: "15px", fontWeight: 500, lineHeight: 1.35, color: "var(--dim)" }}
              >
                Advisory pending
              </div>
              <div class="stat-sub">
                Running the operator's fixed configuration. The decision engine writes into${" "}
                <span style=${{ color: "var(--dim)" }}>decision.rationale</span> once built.
              </div>
            `}
      </div>
    </div>
  `;
}
