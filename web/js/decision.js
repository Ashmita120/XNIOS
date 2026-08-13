/**
 * Decision + explanation.
 *
 * This is the panel V2 Phase 4 (Explainable AI) will fill. It is built now,
 * against the real `DecisionRecord`, so the contract is fixed before the
 * decision engine exists: which four algorithms are in force, what they chose
 * this step, and — when a controller sets them — why, and what it expects.
 *
 * The indicator breakdown below it is already live: every health number can be
 * opened into the measurements that produced it, which is the same
 * "show your working" requirement applied to the monitor.
 */

import { html } from "htm/preact";
import { useState } from "preact/hooks";
import { cn, pct, titleCase } from "./format.js";
import { Badge, Bar, Empty, Row } from "./ui.js";

export function DecisionPanel({ frame }) {
  if (!frame || !frame.record.decision) {
    return html`<${Empty}>no decision recorded yet<//>`;
  }
  const d = frame.record.decision;
  const ai = d.source === "ai";

  return html`
    <div class="stack-5">
      <div class="grid-hair grid-config">
        ${[
          ["Scheduler", d.scheduler],
          ["Bandwidth", d.bandwidth_allocator],
          ["Power", d.power_allocator],
          ["Frequency", d.freq_allocator],
        ].map(
          ([k, v]) => html`
            <div class="config-cell" key=${k}>
              <div class="label">${k}</div>
              <div class="num">${v}</div>
            </div>
          `,
        )}
      </div>

      <div>
        <${Row} k="Source" v=${html`<${Badge} tone=${ai ? "ok" : "neutral"}>${d.source}<//>`} />
        <${Row} k="Candidates offered" v=${d.n_free_candidates} />
        <${Row} k="Assigned this step" v=${d.n_assigned} />
        <${Row}
          k="Left unserved"
          v=${d.n_unserved}
          accent=${d.n_unserved > 0 ? "var(--st-warn)" : undefined}
        />
        <${Row} k="Decision latency" v=${`${d.decision_ms.toFixed(3)} ms`} />
      </div>

      ${d.assignments.length > 0 &&
      html`
        <div>
          <div class="label" style=${{ marginBottom: "8px" }}>Assignments</div>
          <div class="chips">
            ${d.assignments.map(
              (a) => html`
                <span class="chip" key=${`${a.sat_id}-${a.station_id}`}>
                  ${a.sat_id} → ${a.station_id}<span class="muted">:b${a.beam}</span>
                  ${d.reasons[a.sat_id] && html`<span class="muted"> — ${d.reasons[a.sat_id]}</span>`}
                </span>
              `,
            )}
          </div>
        </div>
      `}

      <div class="explain">
        <div class="label" style=${{ marginBottom: "8px" }}>Explanation</div>
        ${ai && d.rationale
          ? html`
              <p class="live">${d.rationale}</p>
              ${Object.keys(d.expected).length > 0 &&
              html`<div class="expected">
                ${Object.entries(d.expected).map(
                  ([k, v]) => html`<span key=${k}>
                    ${titleCase(k)}${" "}
                    <span style=${{ color: v >= 0 ? "var(--st-ok)" : "var(--st-crit)" }}>
                      ${v > 0 ? "+" : ""}${pct(v, 1)}
                    </span>
                  </span>`,
                )}
              </div>`}
            `
          : html`
              <p>
                The operator selected this configuration; nothing was inferred, so there is nothing
                to explain. Every decision row already carries empty
                <span style=${{ color: "var(--dim)" }}> rationale</span>,
                <span style=${{ color: "var(--dim)" }}> reasons</span> and
                <span style=${{ color: "var(--dim)" }}> expected</span> fields, so when the decision
                engine lands, historical runs stay comparable and this panel needs no schema change.
              </p>
            `}
      </div>
    </div>
  `;
}

function Factors({ factors }) {
  const entries = Object.entries(factors || {});
  if (entries.length === 0) return null;
  return html`<div class="factors">
    ${entries.map(
      ([k, v]) => html`<span key=${k}>
        ${k.replace(/_/g, " ")}${" "}
        <span class="v">${typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
      </span>`,
    )}
  </div>`;
}

const ORDER = [
  "availability",
  "link_quality",
  "coverage",
  "delivery",
  "congestion",
  "failure_risk",
  "weather",
  "energy",
];

export function IndicatorBreakdown({ frame }) {
  const [open, setOpen] = useState(null);
  if (!frame) return html`<${Empty}>awaiting telemetry<//>`;

  const weights = frame.health.weights;

  return html`
    <div>
      ${ORDER.map((key) => {
        const ind = frame.health.indicators[key];
        if (!ind) return null;
        const isOpen = open === key;
        const accent = ind.severity
          ? ind.score > 0.6
            ? "var(--st-crit)"
            : ind.score > 0.35
              ? "var(--st-warn)"
              : "var(--st-ok)"
          : ind.score < 0.5
            ? "var(--st-crit)"
            : ind.score < 0.75
              ? "var(--st-warn)"
              : "var(--st-ok)";
        return html`
          <div class="ind" key=${key}>
            <button type="button" class="ind-btn" onClick=${() => setOpen(isOpen ? null : key)}>
              <span class="ind-name">
                <span>${titleCase(key)}</span>
                ${weights[key] !== undefined &&
                html`<span class="ind-tag">w ${weights[key].toFixed(2)}</span>`}
                ${ind.severity && html`<span class="ind-tag">risk</span>`}
              </span>
              <span class="ind-score" style=${{ color: accent }}>
                ${(100 * ind.score).toFixed(0)}%
              </span>
            </button>
            <${Bar} value=${ind.score} accent=${accent} />
            <div class=${cn("ind-more", isOpen && "open")}>
              <div>
                <${Factors} factors=${ind.factors} />
                ${ind.note && html`<div class="ind-note">${ind.note}</div>`}
              </div>
            </div>
          </div>
        `;
      })}

      ${frame.health.notes.length > 0 &&
      html`<div class="health-notes">
        ${frame.health.notes.map((n, i) => html`<p key=${i}>— ${n}</p>`)}
      </div>`}
    </div>
  `;
}
