"use client";

import * as React from "react";

/**
 * Resolve the theme's CSS custom properties to concrete values.
 *
 * Needed because MapLibre parses paint colours itself and never resolves CSS
 * variables — passing `rgba(var(--viz-rgb),.1)` into a layer's paint silently
 * fails. Everything drawn on the map canvas therefore has to read the tokens
 * through here, and re-read them when the theme toggles.
 */
export interface ThemeColors {
  fg: string;
  bg: string;
  bg2: string;
  line: string;
  line2: string;
  mute: string;
  dim: string;
  vizRgb: string;
  ok: string;
  warn: string;
  crit: string;
}

const FALLBACK: ThemeColors = {
  fg: "#F2F2F3",
  bg: "#08080A",
  bg2: "#0C0C0F",
  line: "#1B1B20",
  line2: "#2A2A30",
  mute: "#6B6B73",
  dim: "#9A9AA2",
  vizRgb: "255,255,255",
  ok: "#6F9C78",
  warn: "#B39056",
  crit: "#B3625C",
};

function read(): ThemeColors {
  if (typeof window === "undefined") return FALLBACK;
  const s = getComputedStyle(document.body);
  const v = (name: string, fb: string) => s.getPropertyValue(name).trim() || fb;
  return {
    fg: v("--fg", FALLBACK.fg),
    bg: v("--bg", FALLBACK.bg),
    bg2: v("--bg-2", FALLBACK.bg2),
    line: v("--line", FALLBACK.line),
    line2: v("--line-2", FALLBACK.line2),
    mute: v("--mute", FALLBACK.mute),
    dim: v("--dim", FALLBACK.dim),
    vizRgb: v("--viz-rgb", FALLBACK.vizRgb),
    ok: v("--st-ok", FALLBACK.ok),
    warn: v("--st-warn", FALLBACK.warn),
    crit: v("--st-crit", FALLBACK.crit),
  };
}

export function useThemeColors(): ThemeColors {
  const [colors, setColors] = React.useState<ThemeColors>(FALLBACK);

  React.useEffect(() => {
    setColors(read());
    // the theme toggle swaps a class on <body>; re-resolve when it does
    const obs = new MutationObserver(() => setColors(read()));
    obs.observe(document.body, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  return colors;
}

/** `rgba()` from the theme's viz triplet, for map layers and canvases. */
export function viz(colors: ThemeColors, alpha: number): string {
  return `rgba(${colors.vizRgb},${alpha})`;
}
