/**
 * Formatting helpers. Ported verbatim from the dashboard's lib/format.ts — the
 * TypeScript annotations are the only thing that changed, so every number in the
 * console still reads exactly as it did.
 */

export function cn(...parts) {
  return parts.filter(Boolean).join(" ");
}

/** Bits -> Gbit / Tbit, with the unit, because raw bit counts are unreadable. */
export function bits(v, digits = 1) {
  if (!isFinite(v)) return "—";
  if (Math.abs(v) >= 1e12) return `${(v / 1e12).toFixed(digits)} Tb`;
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(digits)} Gb`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(digits)} Mb`;
  return `${v.toFixed(0)} b`;
}

export function bps(v, digits = 2) {
  if (!isFinite(v) || v <= 0) return "0 bps";
  if (v >= 1e9) return `${(v / 1e9).toFixed(digits)} Gbps`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(digits)} Mbps`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(digits)} kbps`;
  return `${v.toFixed(0)} bps`;
}

export function hz(v, digits = 0) {
  if (v >= 1e9) return `${(v / 1e9).toFixed(Math.max(digits, 2))} GHz`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(digits)} MHz`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(digits)} kHz`;
  return `${v.toFixed(0)} Hz`;
}

export function pct(v, digits = 0) {
  return `${(100 * v).toFixed(digits)}%`;
}

/** Simulated seconds -> mm:ss, matching how an operator reads a pass. */
/**
 * Elapsed time. Rolls over to hours — without it a next-contact 8.5 hours away
 * rendered as "510:53", which reads as eight and a half minutes to anyone who
 * does not stop to divide.
 */
export function clock(s) {
  const t = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export function ber(v) {
  if (v >= 0.5) return "no lock";
  if (v < 1e-12) return "<1e-12";
  return v.toExponential(1);
}

export function healthColor(score0to100) {
  if (score0to100 >= 80) return "var(--st-ok)";
  if (score0to100 >= 60) return "var(--st-warn)";
  return "var(--st-crit)";
}

export function titleCase(s) {
  return String(s)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
