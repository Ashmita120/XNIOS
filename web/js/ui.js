/**
 * The small set of primitives every panel is built from — the same components
 * the dashboard's ui.tsx exposed, in the project's own design language
 * (hairline borders, 1px grid seams, wide uppercase eyebrows, monochrome).
 *
 * Styling moved out of utility classes and into named rules in styles.css;
 * the component API is unchanged.
 */

import { html } from "htm/preact";
import { cn } from "./format.js";

export function Eyebrow({ children, plain, class: cls }) {
  return html`<span class=${cn("eyebrow", plain && "eyebrow-plain", cls)}>${children}</span>`;
}

export function Panel({ title, action, children, class: cls, bodyClass }) {
  return html`
    <section class=${cn("panel", cls)}>
      ${(title || action) &&
      html`<header class="panel-head">
        ${typeof title === "string" ? html`<${Eyebrow}>${title}<//>` : title}${action}
      </header>`}
      <div class=${cn("panel-body", bodyClass)}>${children}</div>
    </section>
  `;
}

/** A KPI tile: big number, wide-tracked label, optional meter and footnote. */
export function Stat({ label, value, unit, sub, meter, accent }) {
  return html`
    <div class="stat">
      <span class="label">${label}</span>
      <div class="stat-value-row">
        <span class="stat-value" style=${accent ? { color: accent } : null}>${value}</span>
        ${unit && html`<span class="stat-unit">${unit}</span>`}
      </div>
      ${meter !== undefined && html`<${Bar} value=${meter} accent=${accent} />`}
      ${sub && html`<div class="stat-sub">${sub}</div>`}
    </div>
  `;
}

const TONE = {
  ok: "var(--st-ok)",
  warn: "var(--st-warn)",
  crit: "var(--st-crit)",
  neutral: "var(--mute)",
};

export function Badge({ children, tone = "neutral" }) {
  return html`<span class="badge" style=${{ color: TONE[tone] || TONE.neutral }}>${children}</span>`;
}

export function Bar({ value, accent }) {
  const w = Math.max(0, Math.min(100, (Number(value) || 0) * 100));
  return html`<div class="meter">
    <i style=${{ width: `${w}%`, background: accent || "var(--fg)" }}></i>
  </div>`;
}

export function Select({ label, value, options, onChange, disabled }) {
  return html`
    <label class="field">
      <span class="label">${label}</span>
      <select
        value=${value}
        disabled=${disabled}
        onChange=${(e) => onChange(e.currentTarget.value)}
      >
        ${options.map((o) => html`<option key=${o} value=${o}>${o}</option>`)}
      </select>
    </label>
  `;
}

export function Button({ children, solid, ...rest }) {
  return html`<button ...${rest} class=${cn("pill", solid && "pill-solid", rest.class)}>
    ${children}
  </button>`;
}

export function Empty({ children }) {
  return html`<div class="empty">${children}</div>`;
}

/** A definition row — the compare/spec pattern from the site. */
export function Row({ k, v, accent }) {
  return html`
    <div class="row">
      <span class="row-k">${k}</span>
      <span class="row-v" style=${accent ? { color: accent } : null}>${v}</span>
    </div>
  `;
}

/* --------------------------------------------------------------------------
   Icons — the three lucide-react glyphs the console used, inlined at their
   source geometry so there is no icon package to install.
   -------------------------------------------------------------------------- */

function Svg({ size = 16, children }) {
  return html`<svg
    width=${size}
    height=${size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    ${children}
  </svg>`;
}

export function MoonIcon({ size }) {
  return html`<${Svg} size=${size}><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" /><//>`;
}

export function SunIcon({ size }) {
  return html`<${Svg} size=${size}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
  <//>`;
}

export function DownloadIcon({ size = 12 }) {
  return html`<${Svg} size=${size}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <polyline points="7 10 12 15 17 10" />
    <line x1="12" x2="12" y1="15" y2="3" />
  <//>`;
}
