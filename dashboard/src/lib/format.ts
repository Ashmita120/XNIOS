export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/** Bits -> Gbit / Tbit, with the unit, because raw bit counts are unreadable. */
export function bits(v: number, digits = 1): string {
  if (!isFinite(v)) return "—";
  if (Math.abs(v) >= 1e12) return `${(v / 1e12).toFixed(digits)} Tb`;
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(digits)} Gb`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(digits)} Mb`;
  return `${v.toFixed(0)} b`;
}

export function bps(v: number, digits = 2): string {
  if (!isFinite(v) || v <= 0) return "0 bps";
  if (v >= 1e9) return `${(v / 1e9).toFixed(digits)} Gbps`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(digits)} Mbps`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(digits)} kbps`;
  return `${v.toFixed(0)} bps`;
}

export function hz(v: number, digits = 0): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(Math.max(digits, 2))} GHz`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(digits)} MHz`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(digits)} kHz`;
  return `${v.toFixed(0)} Hz`;
}

export function pct(v: number, digits = 0): string {
  return `${(100 * v).toFixed(digits)}%`;
}

/** Simulated seconds -> mm:ss, matching how an operator reads a pass. */
export function clock(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export function ber(v: number): string {
  if (v >= 0.5) return "no lock";
  if (v < 1e-12) return "<1e-12";
  return v.toExponential(1);
}

export function healthColor(score0to100: number): string {
  if (score0to100 >= 80) return "var(--st-ok)";
  if (score0to100 >= 60) return "var(--st-warn)";
  return "var(--st-crit)";
}

export function titleCase(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
