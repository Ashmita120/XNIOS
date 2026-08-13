/**
 * Run playback.
 *
 * The twin computes far faster than a pass lasts. In the India presets every
 * satellite crosses the ground segment inside the first ~5 simulated minutes —
 * about four seconds of wall clock at live pacing — and the remaining 25 minutes
 * are an empty sky. Following the live edge therefore shows you the interesting
 * part of the run for a moment and the aftermath forever.
 *
 * So the instant on screen is decoupled from the instant being computed: this
 * control scrubs the whole recorded run. Every panel that reads "the current
 * record" follows it, which is possible only because the API keeps every frame
 * (`GET /api/runs/{id}/frame?step=N`) rather than just the latest.
 */

import { html } from "htm/preact";
import { clock } from "./format.js";
import { cn } from "./format.js";

export function TimeControl({ steps, total, value, live, onScrub, onLive, t }) {
  const max = Math.max(0, steps - 1);
  const at = live ? max : Math.min(value, max);

  return html`
    <div class="timeline">
      <button
        type="button"
        class=${cn("timeline-live", live && "on")}
        onClick=${onLive}
        title=${live ? "Following the newest frame" : "Jump back to the live edge"}
      >
        ${live ? html`<span class="dotlive"></span>` : null} ${live ? "live" : "go live"}
      </button>

      <input
        class="timeline-range"
        type="range"
        min="0"
        max=${max}
        step="1"
        value=${at}
        disabled=${steps <= 1}
        onInput=${(e) => onScrub(Number(e.currentTarget.value))}
        aria-label="Scrub the run"
      />

      <span class="timeline-readout">
        T+${clock(t || 0)} · step <span class="on">${at}</span>/${Math.max(0, total - 1)}
      </span>
    </div>
  `;
}
