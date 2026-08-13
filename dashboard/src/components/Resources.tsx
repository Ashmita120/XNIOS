"use client";

/**
 * Resource + link monitors — the "what is every station and every link doing
 * right now" half of V2 Phase 1. Both read straight from the current telemetry
 * record: no aggregation happens in the browser beyond formatting.
 */

import * as React from "react";
import type { Frame, StationHealth, StationRecord } from "@/lib/types";
import { ber, bps, cn, hz, pct } from "@/lib/format";
import { Bar, Badge, Empty } from "./ui";

function toneFor(h?: StationHealth) {
  if (!h) return "neutral" as const;
  if (!h.up) return "crit" as const;
  if (h.level === "critical") return "crit" as const;
  if (h.level === "high" || h.level === "moderate") return "warn" as const;
  return "ok" as const;
}

export function ResourceMonitor({
  frame,
  onFocus,
  focus,
}: {
  frame: Frame | null;
  onFocus?: (id: string | null) => void;
  focus?: string | null;
}) {
  if (!frame) return <Empty>awaiting telemetry</Empty>;
  const health = Object.fromEntries(frame.health.stations.map((h) => [h.station_id, h]));

  return (
    <div className="flex flex-col">
      {frame.record.stations.map((g: StationRecord) => {
        const h = health[g.station_id];
        const tone = toneFor(h);
        return (
          <div
            key={g.station_id}
            onMouseEnter={() => onFocus?.(g.station_id)}
            onMouseLeave={() => onFocus?.(null)}
            className={cn(
              "border-b border-line py-3.5 transition-colors duration-300 last:border-b-0",
              focus === g.station_id && "bg-bg-2",
            )}
          >
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-[14px] font-medium">{g.station_id}</span>
              <Badge tone={tone}>
                {!g.up ? "offline" : g.degraded ? "degraded" : "nominal"}
              </Badge>
            </div>

            <div className="mt-2.5 grid grid-cols-2 gap-x-5 gap-y-2 sm:grid-cols-4">
              <div>
                <div className="label mb-1.5">
                  Beams {g.beams_active}/{g.beams_available}
                </div>
                <Bar
                  value={g.beam_utilization}
                  accent={g.beams_available < g.beams_total ? "var(--st-warn)" : undefined}
                />
              </div>
              <div>
                <div className="label mb-1.5">BW {pct(g.bandwidth_utilization)}</div>
                <Bar value={g.bandwidth_utilization} />
              </div>
              <div>
                <div className="label mb-1">Channels</div>
                <div className="num text-[12px]">
                  {g.channels_in_use}/{g.n_channels}
                  <span className="ml-1.5 text-mute">{g.phased_array ? "phased" : "dish"}</span>
                </div>
              </div>
              <div>
                <div className="label mb-1">Rate</div>
                <div className="num text-[12px]">{bps(g.rate_bps)}</div>
              </div>
            </div>

            <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[10px] uppercase tracking-[.1em] text-mute">
              <span>
                {g.weather} · {g.rain_fade_db.toFixed(1)} dB
              </span>
              <span>{g.link_power_w.toFixed(1)} W radiated</span>
              <span>{hz(g.bandwidth_pool_hz)} pool</span>
              <span>{g.visible_sats} visible</span>
              {h && <span className="text-dim">{h.reasons[0]}</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function LinkMonitor({ frame }: { frame: Frame | null }) {
  const links = React.useMemo(() => {
    const all = frame?.record.links ?? [];
    return [...all].sort((a, b) => Number(b.active) - Number(a.active) || b.sinr_db - a.sinr_db);
  }, [frame]);

  if (!frame || links.length === 0) return <Empty>no links in view</Empty>;

  return (
    <div className="-mx-1 max-h-[340px] overflow-y-auto">
      <table className="w-full border-collapse">
        <thead className="sticky top-0 bg-bg">
          <tr className="border-b border-line text-left">
            {["Link", "Elev", "SINR", "BER", "BW", "Pwr", "Rate"].map((h) => (
              <th key={h} className="label px-1 pb-2 font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {links.map((l) => (
            <tr
              key={`${l.sat_id}-${l.station_id}`}
              className={cn(
                "border-b border-line/60 last:border-b-0",
                !l.active && "text-mute",
              )}
            >
              <td className="px-1 py-2">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      l.active ? "bg-fg" : "border border-line-2",
                    )}
                  />
                  <span className="num text-[11px]">
                    {l.sat_id}
                    <span className="text-mute"> → </span>
                    {l.station_id}
                  </span>
                  {l.slewing && (
                    <span className="font-mono text-[9px] uppercase tracking-[.14em] text-mute">
                      slew
                    </span>
                  )}
                  {l.channel !== null && (
                    <span className="font-mono text-[9px] text-mute">ch{l.channel}</span>
                  )}
                </div>
              </td>
              <td className="num px-1 py-2 text-[11px]">{l.elev_deg.toFixed(0)}°</td>
              <td
                className="num px-1 py-2 text-[11px]"
                style={{
                  color:
                    l.active && l.sinr_db < 4
                      ? "var(--st-crit)"
                      : l.active && l.sinr_db < 10
                        ? "var(--st-warn)"
                        : undefined,
                }}
              >
                {l.sinr_db.toFixed(1)}
                {l.inr_db > -20 && (
                  <span className="ml-1 text-[9px] text-mute">I{l.inr_db.toFixed(0)}</span>
                )}
              </td>
              <td className="num px-1 py-2 text-[11px]">{ber(l.ber)}</td>
              <td className="num px-1 py-2 text-[11px]">{hz(l.alloc_bw_hz)}</td>
              <td className="num px-1 py-2 text-[11px]">{l.alloc_power_w.toFixed(1)}W</td>
              <td className="num px-1 py-2 text-[11px]">{l.active ? bps(l.rate_bps) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function EventFeed({ events }: { events: { t: number; kind: string; sat_id: string | null; station_id: string | null }[] }) {
  if (events.length === 0) return <Empty>no events yet</Empty>;
  const tone: Record<string, string> = {
    station_fail: "var(--st-crit)",
    beam_fail: "var(--st-warn)",
    interrupt: "var(--st-crit)",
    station_recover: "var(--st-ok)",
    beam_recover: "var(--st-ok)",
    recover: "var(--st-ok)",
    complete: "var(--st-ok)",
  };
  return (
    <div className="max-h-[240px] space-y-0 overflow-y-auto">
      {events
        .slice()
        .reverse()
        .map((e, i) => (
          <div
            key={`${e.t}-${e.kind}-${i}`}
            className="flex items-baseline gap-3 border-b border-line py-2 font-mono text-[11px] last:border-b-0"
          >
            <span className="w-14 shrink-0 text-mute">T+{e.t.toFixed(0)}s</span>
            <span
              className="w-32 shrink-0 uppercase tracking-[.1em]"
              style={{ color: tone[e.kind] ?? "var(--dim)" }}
            >
              {e.kind.replace(/_/g, " ")}
            </span>
            <span className="truncate text-mute">
              {[e.sat_id, e.station_id].filter(Boolean).join(" · ")}
            </span>
          </div>
        ))}
    </div>
  );
}
