/**
 * Time-series panels over the telemetry history — hand-drawn SVG, replacing
 * Recharts.
 *
 * Colour rule (unchanged): the design is monochrome, so series are separated by
 * *form* — fill vs line, solid vs dashed, opacity — rather than hue, and the
 * three status colours stay reserved for severity. Every axis is labelled in the
 * same 10px wide-tracked mono the rest of the console uses.
 *
 * The curve is monotone cubic (Fritsch–Carlson), which is what Recharts'
 * `type="monotone"` drew: it smooths without the overshoot a plain cardinal
 * spline produces, so a backlog line never dips below zero between two samples.
 */

import { html } from "htm/preact";
import { useState } from "preact/hooks";
import { useMeasure } from "./state.js";
import { clock } from "./format.js";
import { Empty } from "./ui.js";

const H = 190;
const M = { top: 8, right: 6, bottom: 20, left: 44 };

/* ------------------------------------------------------------------ scales */

/** Round a domain out to human tick values (1/2/5 × 10^n). */
function niceTicks(min, max, count = 4) {
  if (!isFinite(min) || !isFinite(max) || max <= min) {
    return { lo: 0, hi: max > 0 ? max : 1, ticks: [0, max > 0 ? max : 1] };
  }
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 7.5 ? 10 : norm >= 3 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  // guard against fp drift accumulating past `hi`
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
  return { lo, hi, ticks };
}

/** Fritsch–Carlson tangents -> a cubic Bézier path with no overshoot. */
function monotonePath(pts) {
  const n = pts.length;
  if (n === 0) return "";
  if (n === 1) return `M${pts[0][0]},${pts[0][1]}`;

  const dx = [];
  const slope = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1][0] - pts[i][0];
    slope[i] = dx[i] === 0 ? 0 : (pts[i + 1][1] - pts[i][1]) / dx[i];
  }

  const m = [slope[0]];
  for (let i = 1; i < n - 1; i++) {
    m[i] = slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2;
  }
  m[n - 1] = slope[n - 2];

  for (let i = 0; i < n - 1; i++) {
    if (slope[i] === 0) {
      m[i] = 0;
      m[i + 1] = 0;
      continue;
    }
    const a = m[i] / slope[i];
    const b = m[i + 1] / slope[i];
    const s = a * a + b * b;
    if (s > 9) {
      const t = 3 / Math.sqrt(s);
      m[i] = t * a * slope[i];
      m[i + 1] = t * b * slope[i];
    }
  }

  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d +=
      `C${pts[i][0] + h},${pts[i][1] + m[i] * h}` +
      ` ${pts[i + 1][0] - h},${pts[i + 1][1] - m[i + 1] * h}` +
      ` ${pts[i + 1][0]},${pts[i + 1][1]}`;
  }
  return d;
}

const valueOf = (s, d) => (s.value ? s.value(d) : d[s.key]);

/* ------------------------------------------------------------------- chart */

/**
 * @param data     HistoryPoint[]  (must carry `t`)
 * @param series   [{ key|value, name, stroke, width, dash, area, fillOpacity }]
 * @param domain   [lo, hi] to fix the y-axis, or undefined to fit the data
 * @param tickFmt  y tick label formatter
 * @param refLines [{ y, stroke }] horizontal guides
 * @param unit     appended to tooltip values
 * @param id       unique per chart — namespaces the gradient/clip ids
 */
function TimeChart({ data, series, domain, tickFmt, refLines = [], unit = "", id }) {
  const [ref, size] = useMeasure();
  const [hover, setHover] = useState(null);

  if (data.length < 2) return html`<${Empty}>collecting telemetry<//>`;

  const W = size.width;
  const innerW = W - M.left - M.right;
  const innerH = H - M.top - M.bottom;

  // first paint happens before the ResizeObserver reports; hold the box open
  if (innerW <= 0) return html`<div class="chart" ref=${ref}></div>`;

  const t0 = data[0].t;
  const t1 = data[data.length - 1].t;
  const tSpan = t1 - t0 || 1;

  let dMin = Infinity;
  let dMax = -Infinity;
  for (const d of data) {
    for (const s of series) {
      const v = valueOf(s, d);
      if (!isFinite(v)) continue;
      if (v < dMin) dMin = v;
      if (v > dMax) dMax = v;
    }
  }
  if (!isFinite(dMin)) {
    dMin = 0;
    dMax = 1;
  }

  // A declared domain is a floor, not a clamp. Coverage genuinely reads a little
  // over 100% at times, and an axis fixed at [0,1] would have drawn that segment
  // outside the panel; growing the axis keeps the excursion visible and honest.
  // Zero stays the baseline when no domain is declared, as it did before.
  const n = niceTicks(
    domain ? Math.min(domain[0], dMin) : 0,
    (domain ? Math.max(domain[1], dMax) : dMax) || 1,
    4,
  );
  const lo = n.lo;
  const hi = n.hi;
  const ticks = n.ticks;

  const x = (t) => M.left + ((t - t0) / tSpan) * innerW;
  const y = (v) => M.top + innerH - ((v - lo) / (hi - lo || 1)) * innerH;

  const xTickCount = Math.max(2, Math.min(6, Math.floor(innerW / 70)));
  const xTicks = Array.from(
    { length: xTickCount },
    (_, i) => t0 + (tSpan * i) / (xTickCount - 1),
  );

  const paths = series.map((s) => {
    const pts = [];
    for (const d of data) {
      const v = valueOf(s, d);
      if (isFinite(v)) pts.push([x(d.t), y(v)]);
    }
    return { s, d: monotonePath(pts), first: pts[0], last: pts[pts.length - 1] };
  });

  const base = M.top + innerH;

  const onMove = (e) => {
    const box = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - box.left;
    const t = t0 + ((px - M.left) / innerW) * tSpan;
    // nearest sample, not the enclosing bucket — matches Recharts' cursor
    let best = 0;
    let bestD = Infinity;
    for (let i = 0; i < data.length; i++) {
      const dd = Math.abs(data[i].t - t);
      if (dd < bestD) {
        bestD = dd;
        best = i;
      }
    }
    setHover(best);
  };

  const point = hover === null ? null : data[hover];
  const tipX = point ? x(point.t) : 0;
  const tipRight = tipX > M.left + innerW / 2;

  return html`
    <div class="chart" ref=${ref}>
      <svg width=${W} height=${H} role="img">
        <defs>
          <clipPath id=${`${id}-clip`}>
            <rect x=${M.left} y=${M.top - 3} width=${innerW} height=${innerH + 6} />
          </clipPath>
          ${series
            .filter((s) => s.area)
            .map(
              (s) => html`
                <linearGradient id=${`${id}-${s.key}`} x1="0" y1="0" x2="0" y2="1" key=${s.key}>
                  <stop offset="0%" stop-color="rgb(var(--viz-rgb))" stop-opacity=${s.fillOpacity} />
                  <stop offset="100%" stop-color="rgb(var(--viz-rgb))" stop-opacity="0" />
                </linearGradient>
              `,
            )}
        </defs>

        ${ticks.map(
          (v) => html`
            <line
              key=${`g${v}`}
              class="chart-grid"
              x1=${M.left}
              x2=${M.left + innerW}
              y1=${y(v)}
              y2=${y(v)}
            />
          `,
        )}
        ${ticks.map(
          (v) => html`
            <text key=${`t${v}`} class="chart-tick" x=${M.left - 8} y=${y(v)} text-anchor="end" dominant-baseline="middle">
              ${tickFmt ? tickFmt(v) : String(v)}
            </text>
          `,
        )}
        ${xTicks.map((t, i) => {
          const anchor = i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle";
          return html`<text key=${`x${i}`} class="chart-tick" x=${x(t)} y=${H - 6} text-anchor=${anchor}>
            ${clock(t)}
          </text>`;
        })}
        ${refLines.map(
          (r) => html`
            <line
              key=${`r${r.y}`}
              x1=${M.left}
              x2=${M.left + innerW}
              y1=${y(r.y)}
              y2=${y(r.y)}
              stroke=${r.stroke}
              stroke-dasharray="2 4"
              stroke-opacity="0.5"
            />
          `,
        )}
        <g clip-path=${`url(#${id}-clip)`}>
        ${paths.map(
          ({ s, d, first, last }) => html`
            ${s.area && first && last
              ? html`<path
                  key=${`a${s.key}`}
                  d=${`${d}L${last[0]},${base}L${first[0]},${base}Z`}
                  fill=${`url(#${id}-${s.key})`}
                  stroke="none"
                />`
              : null}
            <path
              key=${`l${s.key}`}
              d=${d}
              fill="none"
              stroke=${s.stroke}
              stroke-width=${s.width}
              stroke-dasharray=${s.dash || null}
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          `,
        )}
        </g>
        ${point &&
        html`<line
          class="chart-cursor"
          x1=${tipX}
          x2=${tipX}
          y1=${M.top}
          y2=${base}
        />`}
        ${point &&
        paths.map(({ s }) => {
          const v = valueOf(s, point);
          return isFinite(v)
            ? html`<circle key=${`d${s.key}`} cx=${tipX} cy=${y(v)} r="2.5" fill=${s.stroke} />`
            : null;
        })}

        <rect
          x=${M.left}
          y=${M.top}
          width=${innerW}
          height=${innerH}
          fill="transparent"
          onPointerMove=${onMove}
          onPointerLeave=${() => setHover(null)}
        />
      </svg>

      ${point &&
      html`<div
        class="chart-tip"
        style=${{
          top: "6px",
          left: tipRight ? "auto" : `${tipX + 12}px`,
          right: tipRight ? `${W - tipX + 12}px` : "auto",
        }}
      >
        <div class="chart-tip-t">T+${clock(point.t)}</div>
        ${series.map((s) => {
          const v = valueOf(s, point);
          return html`<div class="chart-tip-row" key=${s.key}>
            <span style=${{ color: s.stroke }}>${s.name}</span>
            <b>${typeof v === "number" ? v.toFixed(2) : v}${unit}</b>
          </div>`;
        })}
      </div>`}
    </div>
  `;
}

/* ------------------------------------------------------------ the four panels */

export function ThroughputChart({ data }) {
  return html`<${TimeChart}
    id="thr"
    data=${data}
    unit=" Gbps"
    series=${[
      {
        key: "throughput_gbps",
        name: "throughput",
        stroke: "var(--fg)",
        width: 1.4,
        area: true,
        fillOpacity: 0.28,
      },
    ]}
  />`;
}

export function UtilisationChart({ data }) {
  return html`<${TimeChart}
    id="util"
    data=${data}
    domain=${[0, 1]}
    tickFmt=${(v) => `${Math.round(v * 100)}%`}
    series=${[
      { key: "beam_utilization", name: "beams", stroke: "var(--fg)", width: 1.4 },
      {
        key: "bandwidth_utilization",
        name: "bandwidth",
        stroke: "var(--mute)",
        width: 1.2,
        dash: "3 3",
      },
      { key: "coverage", name: "coverage", stroke: "var(--dim)", width: 1, dash: "1 3" },
    ]}
  />`;
}

export function QueueChart({ data }) {
  // mixes a filled backlog area with a delivered-total line — the pairing the
  // Recharts <ComposedChart> existed for
  return html`<${TimeChart}
    id="q"
    data=${data}
    unit=" Gb"
    series=${[
      {
        key: "queue_gbit",
        name: "backlog",
        stroke: "var(--mute)",
        width: 1.2,
        area: true,
        fillOpacity: 0.16,
      },
      { key: "delivered_gbit", name: "delivered", stroke: "var(--fg)", width: 1.4 },
    ]}
  />`;
}

export function HealthChart({ data }) {
  return html`<${TimeChart}
    id="health"
    data=${data}
    domain=${[0, 100]}
    refLines=${[
      { y: 80, stroke: "var(--st-ok)" },
      { y: 60, stroke: "var(--st-warn)" },
    ]}
    series=${[
      { key: "health", name: "health", stroke: "var(--fg)", width: 1.6 },
      {
        key: "congestion",
        name: "congestion",
        value: (d) => d.congestion * 100,
        stroke: "var(--st-warn)",
        width: 1,
        dash: "4 3",
      },
      {
        key: "failure_risk",
        name: "failure risk",
        value: (d) => d.failure_risk * 100,
        stroke: "var(--st-crit)",
        width: 1,
        dash: "4 3",
      },
    ]}
  />`;
}
