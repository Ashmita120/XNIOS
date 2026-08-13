"use client";

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

import * as React from "react";
import type { Frame, Indicator } from "@/lib/types";
import { cn, pct, titleCase } from "@/lib/format";
import { Badge, Bar, Empty, Row } from "./ui";

export function DecisionPanel({ frame }: { frame: Frame | null }) {
  if (!frame?.record.decision) return <Empty>no decision recorded yet</Empty>;
  const d = frame.record.decision;
  const ai = d.source === "ai";

  return (
    <div className="space-y-5">
      <div className="grid-hair grid grid-cols-2 lg:grid-cols-4">
        {[
          ["Scheduler", d.scheduler],
          ["Bandwidth", d.bandwidth_allocator],
          ["Power", d.power_allocator],
          ["Frequency", d.freq_allocator],
        ].map(([k, v]) => (
          <div key={k} className="p-4">
            <div className="label">{k}</div>
            <div className="num mt-2 text-[13px]">{v}</div>
          </div>
        ))}
      </div>

      <div>
        <Row k="Source" v={<Badge tone={ai ? "ok" : "neutral"}>{d.source}</Badge>} />
        <Row k="Candidates offered" v={d.n_free_candidates} />
        <Row k="Assigned this step" v={d.n_assigned} />
        <Row
          k="Left unserved"
          v={d.n_unserved}
          accent={d.n_unserved > 0 ? "var(--st-warn)" : undefined}
        />
        <Row k="Decision latency" v={`${d.decision_ms.toFixed(3)} ms`} />
      </div>

      {d.assignments.length > 0 && (
        <div>
          <div className="label mb-2">Assignments</div>
          <div className="flex flex-wrap gap-1.5">
            {d.assignments.map((a) => (
              <span
                key={`${a.sat_id}-${a.station_id}`}
                className="rounded-pill border border-line px-2.5 py-1 font-mono text-[10px] text-dim"
              >
                {a.sat_id} → {a.station_id}
                <span className="text-mute">:b{a.beam}</span>
                {d.reasons[a.sat_id] && (
                  <span className="ml-1.5 text-mute">— {d.reasons[a.sat_id]}</span>
                )}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="rounded-panel border border-line p-4">
        <div className="label mb-2">Explanation</div>
        {ai && d.rationale ? (
          <>
            <p className="text-[13px] leading-relaxed text-dim">{d.rationale}</p>
            {Object.keys(d.expected).length > 0 && (
              <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[11px] text-mute">
                {Object.entries(d.expected).map(([k, v]) => (
                  <span key={k}>
                    {titleCase(k)}{" "}
                    <span style={{ color: v >= 0 ? "var(--st-ok)" : "var(--st-crit)" }}>
                      {v > 0 ? "+" : ""}
                      {pct(v, 1)}
                    </span>
                  </span>
                ))}
              </div>
            )}
          </>
        ) : (
          <p className="text-[13px] leading-relaxed text-mute">
            The operator selected this configuration; nothing was inferred, so there is nothing to
            explain. Every decision row already carries empty{" "}
            <span className="text-dim">rationale</span>,{" "}
            <span className="text-dim">reasons</span> and{" "}
            <span className="text-dim">expected</span> fields, so when the decision engine lands,
            historical runs stay comparable and this panel needs no schema change.
          </p>
        )}
      </div>
    </div>
  );
}

function Factors({ factors }: { factors: Record<string, unknown> }) {
  const entries = Object.entries(factors);
  if (entries.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] text-mute">
      {entries.map(([k, v]) => (
        <span key={k}>
          {k.replace(/_/g, " ")}{" "}
          <span className="text-dim">
            {typeof v === "object" ? JSON.stringify(v) : String(v)}
          </span>
        </span>
      ))}
    </div>
  );
}

export function IndicatorBreakdown({ frame }: { frame: Frame | null }) {
  const [open, setOpen] = React.useState<string | null>(null);
  if (!frame) return <Empty>awaiting telemetry</Empty>;

  const order = [
    "availability",
    "link_quality",
    "coverage",
    "delivery",
    "congestion",
    "failure_risk",
    "weather",
    "energy",
  ];
  const weights = frame.health.weights;

  return (
    <div>
      {order.map((key) => {
        const ind: Indicator | undefined = frame.health.indicators[key];
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
        return (
          <div key={key} className="border-b border-line py-3 last:border-b-0">
            <button
              onClick={() => setOpen(isOpen ? null : key)}
              className="flex w-full items-baseline justify-between gap-4 text-left"
            >
              <span className="flex items-baseline gap-2">
                <span className="text-[13px]">{titleCase(key)}</span>
                {weights[key] !== undefined && (
                  <span className="font-mono text-[9px] uppercase tracking-[.14em] text-mute">
                    w {weights[key].toFixed(2)}
                  </span>
                )}
                {ind.severity && (
                  <span className="font-mono text-[9px] uppercase tracking-[.14em] text-mute">
                    risk
                  </span>
                )}
              </span>
              <span className="num text-[13px]" style={{ color: accent }}>
                {(100 * ind.score).toFixed(0)}%
              </span>
            </button>
            <div className="mt-2">
              <Bar value={ind.score} accent={accent} />
            </div>
            <div
              className={cn(
                "grid transition-all duration-500 ease-arc",
                isOpen ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
              )}
            >
              <div className="overflow-hidden">
                <Factors factors={ind.factors} />
                {ind.note && (
                  <div className="mt-1.5 font-mono text-[10px] italic text-mute">{ind.note}</div>
                )}
              </div>
            </div>
          </div>
        );
      })}

      {frame.health.notes.length > 0 && (
        <div className="mt-4 space-y-2">
          {frame.health.notes.map((n, i) => (
            <p key={i} className="font-mono text-[10px] leading-relaxed text-mute">
              — {n}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
