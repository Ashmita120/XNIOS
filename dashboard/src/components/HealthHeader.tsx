"use client";

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

import * as React from "react";
import type { Frame } from "@/lib/types";
import { bps, healthColor, pct, titleCase } from "@/lib/format";
import { Stat, Badge } from "./ui";

export function HealthHeader({ frame }: { frame: Frame | null }) {
  if (!frame) {
    return (
      <div className="grid-hair grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5">
        {["Network health", "Throughput", "Congestion", "Failure risk", "AI recommendation"].map(
          (l) => (
            <Stat key={l} label={l} value="—" sub="awaiting telemetry" />
          ),
        )}
      </div>
    );
  }

  const h = frame.health;
  const n = frame.record.network;
  const cong = h.indicators.congestion;
  const risk = h.indicators.failure_risk;
  const dec = frame.record.decision;

  const aiActive = dec?.source === "ai" && dec.rationale;

  return (
    <div className="grid-hair grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5">
      <Stat
        label="Network health"
        value={h.network_health.toFixed(0)}
        unit="%"
        accent={healthColor(h.network_health)}
        meter={h.network_health / 100}
        sub={`${titleCase(h.level)} · ${String(h.indicators.availability.factors.stations_up)} stations up`}
      />
      <Stat
        label="Throughput"
        value={bps(n.throughput_bps, 2).split(" ")[0]}
        unit={bps(n.throughput_bps, 2).split(" ")[1]}
        meter={Math.min(1, n.beam_utilization)}
        sub={`${(n.bits_delivered_total / 1e9).toFixed(1)} Gb delivered · ${(n.queue_bits / 1e9).toFixed(1)} Gb queued`}
      />
      <Stat
        label="Congestion"
        value={titleCase(cong.level)}
        accent={cong.level === "low" ? undefined : "var(--st-warn)"}
        meter={cong.score}
        sub={`${n.beams_active}/${n.beams_available} beams busy · ${n.n_waiting} waiting`}
      />
      <Stat
        label="Failure risk"
        value={titleCase(risk.level)}
        accent={risk.level === "low" ? undefined : "var(--st-crit)"}
        meter={risk.score}
        sub={`${String(risk.factors.stations_down)} down · ${String(risk.factors.beams_lost)} beams lost · observed, not forecast`}
      />

      <div className="flex flex-col justify-between p-5">
        <div className="flex items-center justify-between gap-2">
          <span className="label">AI recommendation</span>
          <Badge tone={aiActive ? "ok" : "neutral"}>{aiActive ? "active" : "phase 5"}</Badge>
        </div>
        {aiActive ? (
          <>
            <div className="mt-4 text-[15px] font-medium leading-snug">{dec!.rationale}</div>
            <div className="mt-3 font-mono text-[11px] text-mute">
              {Object.entries(dec!.expected)
                .map(([k, v]) => `${titleCase(k)} ${v > 0 ? "+" : ""}${pct(v, 1)}`)
                .join(" · ")}
            </div>
          </>
        ) : (
          <>
            <div className="mt-4 text-[15px] font-medium leading-snug text-dim">
              Advisory pending
            </div>
            <div className="mt-3 font-mono text-[11px] leading-snug text-mute">
              Running the operator&apos;s fixed configuration. The decision engine writes into{" "}
              <span className="text-dim">decision.rationale</span> once built.
            </div>
          </>
        )}
      </div>
    </div>
  );
}
