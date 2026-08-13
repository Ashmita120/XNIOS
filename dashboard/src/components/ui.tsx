"use client";

/**
 * The small set of primitives every panel is built from. These *are* the
 * shadcn/ui pattern — unstyled, composable local components in the project's
 * own design language — rather than the generic shadcn theme, because the
 * design language here is ARCTROPY's (hairline borders, 1px grid seams, wide
 * uppercase eyebrows, monochrome) and the default shadcn look would fight it.
 */

import * as React from "react";
import { cn } from "@/lib/format";

export function Eyebrow({
  children,
  plain,
  className,
}: {
  children: React.ReactNode;
  plain?: boolean;
  className?: string;
}) {
  return (
    <span className={cn("eyebrow", plain && "eyebrow-plain", className)}>{children}</span>
  );
}

export function Panel({
  title,
  action,
  children,
  className,
  bodyClassName,
}: {
  title?: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn("panel flex min-h-0 flex-col", className)}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-5 py-3.5">
          {typeof title === "string" ? <Eyebrow>{title}</Eyebrow> : title}
          {action}
        </header>
      )}
      <div className={cn("min-h-0 flex-1 p-5", bodyClassName)}>{children}</div>
    </section>
  );
}

/** A KPI tile: big number, wide-tracked label, optional meter and footnote. */
export function Stat({
  label,
  value,
  unit,
  sub,
  meter,
  accent,
  className,
}: {
  label: string;
  value: React.ReactNode;
  unit?: string;
  sub?: React.ReactNode;
  meter?: number;
  accent?: string;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col justify-between p-5", className)}>
      <span className="label">{label}</span>
      <div className="mt-4 flex items-baseline gap-1.5">
        <span
          className="num text-[clamp(1.7rem,3.2vw,2.5rem)] font-medium leading-none tracking-[-.02em]"
          style={accent ? { color: accent } : undefined}
        >
          {value}
        </span>
        {unit && <span className="num text-[13px] text-mute">{unit}</span>}
      </div>
      {meter !== undefined && (
        <div className="meter mt-4">
          <i
            style={{
              width: `${Math.max(0, Math.min(100, meter * 100))}%`,
              background: accent ?? "var(--fg)",
            }}
          />
        </div>
      )}
      {sub && <div className="mt-3 font-mono text-[11px] leading-snug text-mute">{sub}</div>}
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "ok" | "warn" | "crit";
  className?: string;
}) {
  const color =
    tone === "ok"
      ? "var(--st-ok)"
      : tone === "warn"
        ? "var(--st-warn)"
        : tone === "crit"
          ? "var(--st-crit)"
          : "var(--mute)";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-1 font-mono text-[10px] uppercase tracking-[.12em]",
        className,
      )}
      style={{ borderColor: color, color }}
    >
      {children}
    </span>
  );
}

export function Bar({ value, accent }: { value: number; accent?: string }) {
  return (
    <div className="meter">
      <i
        style={{
          width: `${Math.max(0, Math.min(100, value * 100))}%`,
          background: accent ?? "var(--fg)",
        }}
      />
    </div>
  );
}

export function Select({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <label className="flex flex-col gap-2">
      <span className="label">{label}</span>
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-pill border border-line-2 bg-transparent px-4 py-2 font-mono text-[12px] text-fg outline-none transition-colors duration-300 focus:border-mute disabled:text-mute"
      >
        {options.map((o) => (
          <option key={o} value={o} className="bg-bg text-fg">
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

export function Button({
  children,
  solid,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { solid?: boolean }) {
  return (
    <button {...rest} className={cn("pill", solid && "pill-solid", rest.className)}>
      {children}
    </button>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full min-h-[120px] items-center justify-center px-6 text-center font-mono text-[11px] uppercase tracking-[.16em] text-mute">
      {children}
    </div>
  );
}

/** A definition row — the compare/spec pattern from the site. */
export function Row({
  k,
  v,
  accent,
}: {
  k: React.ReactNode;
  v: React.ReactNode;
  accent?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line py-2.5 last:border-b-0">
      <span className="font-mono text-[11px] uppercase tracking-[.1em] text-mute">{k}</span>
      <span className="num text-[13px]" style={accent ? { color: accent } : undefined}>
        {v}
      </span>
    </div>
  );
}
