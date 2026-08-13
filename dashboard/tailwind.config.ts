import type { Config } from "tailwindcss";

/**
 * The palette is ARCTROPY's, taken from the live site's CSS custom properties
 * (`--bg`, `--bg-2`, `--line`, `--line-2`, `--mute`, `--dim`, `--fg`). Every
 * colour here resolves through those variables rather than being hard-coded, so
 * the light/dark theme swap in globals.css drives the whole dashboard.
 *
 * ARCTROPY is strictly monochrome. The three `status` hues are the only added
 * colour, kept desaturated and used ONLY where an operator must distinguish
 * severity at a glance (health levels, station state) — never for decoration.
 */
const config: Config = {
  // Theming is done entirely through the CSS custom properties in globals.css
  // (body.theme-dark / body.theme-light), so Tailwind's own dark: variant is
  // never used — every colour token already swaps with the theme.
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        "bg-2": "var(--bg-2)",
        line: "var(--line)",
        "line-2": "var(--line-2)",
        mute: "var(--mute)",
        dim: "var(--dim)",
        fg: "var(--fg)",
        status: {
          ok: "var(--st-ok)",
          warn: "var(--st-warn)",
          crit: "var(--st-crit)",
        },
      },
      fontFamily: {
        sans: ['"Google Sans"', "system-ui", "sans-serif"],
        mono: ['"Google Sans"', "ui-monospace", "monospace"],
      },
      borderRadius: { card: "16px", panel: "14px", pill: "999px" },
      letterSpacing: { eyebrow: "0.28em", nav: "0.12em", brand: "0.34em" },
      maxWidth: { shell: "1320px" },
      transitionTimingFunction: { arc: "cubic-bezier(.16,1,.3,1)" },
      keyframes: {
        beat: {
          "0%": { boxShadow: "0 0 0 0 rgba(var(--viz-rgb),.5)" },
          "70%": { boxShadow: "0 0 0 7px rgba(var(--viz-rgb),0)" },
          "100%": { boxShadow: "0 0 0 0 rgba(var(--viz-rgb),0)" },
        },
        ping2: {
          "0%": { transform: "scale(.6)", opacity: "1" },
          "100%": { transform: "scale(2.2)", opacity: "0" },
        },
        rise: {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        beat: "beat 2.4s infinite",
        ping2: "ping2 3.2s cubic-bezier(.16,1,.3,1) infinite",
        rise: "rise .5s cubic-bezier(.16,1,.3,1) both",
      },
    },
  },
  plugins: [],
};

export default config;
