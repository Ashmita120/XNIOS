"use client";

/**
 * Time-series panels over the telemetry history.
 *
 * Colour rule: the design is monochrome, so series are separated by *form*
 * (fill vs line, solid vs dashed, opacity) rather than hue, and the three
 * status colours are reserved for severity. Every axis is labelled in the same
 * 10px wide-tracked mono the rest of the console uses.
 */

import * as React from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { HistoryPoint } from "@/lib/useRun";
import { clock } from "@/lib/format";
import { Empty } from "./ui";

const AXIS = {
  stroke: "var(--mute)",
  fontSize: 10,
  fontFamily: '"Google Sans", monospace',
  letterSpacing: "0.08em",
};

function ChartTooltip({ active, payload, label, unit }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-line bg-bg px-3 py-2 font-mono text-[10px] leading-relaxed text-dim shadow-none">
      <div className="mb-1 uppercase tracking-[.14em] text-mute">T+{clock(label)}</div>
      {payload.map((p: any) => (
        <div key={p.dataKey} className="flex items-center justify-between gap-4">
          <span style={{ color: p.stroke ?? p.fill }}>{p.name}</span>
          <span className="tabular-nums text-fg">
            {typeof p.value === "number" ? p.value.toFixed(2) : p.value}
            {unit ?? ""}
          </span>
        </div>
      ))}
    </div>
  );
}

function ChartFrame({ children, data }: { children: React.ReactNode; data: HistoryPoint[] }) {
  if (data.length < 2) return <Empty>collecting telemetry</Empty>;
  return (
    <div className="h-[190px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        {children as React.ReactElement}
      </ResponsiveContainer>
    </div>
  );
}

export function ThroughputChart({ data }: { data: HistoryPoint[] }) {
  return (
    <ChartFrame data={data}>
      <AreaChart data={data} margin={{ top: 6, right: 4, left: -18, bottom: 0 }}>
        <defs>
          <linearGradient id="thr" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(var(--viz-rgb))" stopOpacity={0.28} />
            <stop offset="100%" stopColor="rgb(var(--viz-rgb))" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="var(--line)" vertical={false} />
        <XAxis dataKey="t" tickFormatter={clock} {...AXIS} tickLine={false} axisLine={false} />
        <YAxis {...AXIS} tickLine={false} axisLine={false} width={44} />
        <Tooltip content={<ChartTooltip unit=" Gbps" />} cursor={{ stroke: "var(--line-2)" }} />
        <Area
          type="monotone"
          dataKey="throughput_gbps"
          name="throughput"
          stroke="var(--fg)"
          strokeWidth={1.4}
          fill="url(#thr)"
          isAnimationActive={false}
        />
      </AreaChart>
    </ChartFrame>
  );
}

export function UtilisationChart({ data }: { data: HistoryPoint[] }) {
  return (
    <ChartFrame data={data}>
      <LineChart data={data} margin={{ top: 6, right: 4, left: -18, bottom: 0 }}>
        <CartesianGrid stroke="var(--line)" vertical={false} />
        <XAxis dataKey="t" tickFormatter={clock} {...AXIS} tickLine={false} axisLine={false} />
        <YAxis
          {...AXIS}
          tickLine={false}
          axisLine={false}
          width={44}
          domain={[0, 1]}
          tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
        />
        <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--line-2)" }} />
        <Line
          type="monotone"
          dataKey="beam_utilization"
          name="beams"
          stroke="var(--fg)"
          strokeWidth={1.4}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="bandwidth_utilization"
          name="bandwidth"
          stroke="var(--mute)"
          strokeWidth={1.2}
          strokeDasharray="3 3"
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="coverage"
          name="coverage"
          stroke="var(--dim)"
          strokeWidth={1}
          strokeDasharray="1 3"
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartFrame>
  );
}

export function HealthChart({ data }: { data: HistoryPoint[] }) {
  return (
    <ChartFrame data={data}>
      <LineChart data={data} margin={{ top: 6, right: 4, left: -18, bottom: 0 }}>
        <CartesianGrid stroke="var(--line)" vertical={false} />
        <XAxis dataKey="t" tickFormatter={clock} {...AXIS} tickLine={false} axisLine={false} />
        <YAxis {...AXIS} tickLine={false} axisLine={false} width={44} domain={[0, 100]} />
        <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--line-2)" }} />
        <ReferenceLine y={80} stroke="var(--st-ok)" strokeDasharray="2 4" strokeOpacity={0.5} />
        <ReferenceLine y={60} stroke="var(--st-warn)" strokeDasharray="2 4" strokeOpacity={0.5} />
        <Line
          type="monotone"
          dataKey="health"
          name="health"
          stroke="var(--fg)"
          strokeWidth={1.6}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey={(d: HistoryPoint) => d.congestion * 100}
          name="congestion"
          stroke="var(--st-warn)"
          strokeWidth={1}
          strokeDasharray="4 3"
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey={(d: HistoryPoint) => d.failure_risk * 100}
          name="failure risk"
          stroke="var(--st-crit)"
          strokeWidth={1}
          strokeDasharray="4 3"
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartFrame>
  );
}

export function QueueChart({ data }: { data: HistoryPoint[] }) {
  // ComposedChart, not AreaChart: this panel mixes a filled backlog area with a
  // delivered-total line, and only the composed chart supports both children.
  return (
    <ChartFrame data={data}>
      <ComposedChart data={data} margin={{ top: 6, right: 4, left: -18, bottom: 0 }}>
        <defs>
          <linearGradient id="q" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(var(--viz-rgb))" stopOpacity={0.16} />
            <stop offset="100%" stopColor="rgb(var(--viz-rgb))" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="var(--line)" vertical={false} />
        <XAxis dataKey="t" tickFormatter={clock} {...AXIS} tickLine={false} axisLine={false} />
        <YAxis {...AXIS} tickLine={false} axisLine={false} width={44} />
        <Tooltip content={<ChartTooltip unit=" Gb" />} cursor={{ stroke: "var(--line-2)" }} />
        <Area
          type="monotone"
          dataKey="queue_gbit"
          name="backlog"
          stroke="var(--mute)"
          strokeWidth={1.2}
          fill="url(#q)"
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="delivered_gbit"
          name="delivered"
          stroke="var(--fg)"
          strokeWidth={1.4}
          dot={false}
          isAnimationActive={false}
        />
      </ComposedChart>
    </ChartFrame>
  );
}
