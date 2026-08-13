/**
 * Scenario + policy selection. The four dropdowns are exactly the four pluggable
 * axes of the twin (`scheduler × bandwidth × power × frequency`) — which is why
 * this panel is also the seam for V2 Phase 5: the decision engine will set the
 * same four values, and the operator will be able to see and override them.
 */

import { html } from "htm/preact";
import { useEffect, useState } from "preact/hooks";
import { api } from "./api.js";
import { cn } from "./format.js";
import { Badge, Button, Empty, Select } from "./ui.js";

export function RunControl({ onStarted, current }) {
  const [presets, setPresets] = useState([]);
  const [policies, setPolicies] = useState(null);
  const [preset, setPreset] = useState("india4-nominal");
  const [scheduler, setScheduler] = useState("fcfs/strongest");
  const [bw, setBw] = useState("equal");
  const [power, setPower] = useState("adaptive");
  const [freq, setFreq] = useState("coloring");
  const [live, setLive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    Promise.all([api.presets(), api.policies()])
      .then(([p, pol]) => {
        setPresets(p);
        setPolicies(pol);
        if (p.length && !p.some((x) => x.key === "india4-nominal")) setPreset(p[0].key);
      })
      .catch((e) => setErr(String(e)));
  }, []);

  const chosen = presets.find((p) => p.key === preset);

  async function start() {
    setBusy(true);
    setErr(null);
    try {
      const run = await api.start({
        preset,
        scheduler,
        bandwidth_allocator: bw,
        power_allocator: power,
        freq_allocator: freq,
        // pace the stream so the console reads as live; the twin itself runs a
        // 30-minute scenario in well under a second
        pace_ms: live ? 120 : 0,
      });
      onStarted(run);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (err && !policies) {
    return html`<${Empty}>
      API unreachable — start it with
      <span style=${{ marginLeft: "4px", color: "var(--dim)" }}>python run_api.py</span>
    <//>`;
  }
  if (!policies) return html`<${Empty}>loading policies<//>`;

  return html`
    <div class="stack-5">
      <div>
        <span class="label">Scenario</span>
        <div class="preset-list">
          ${presets.map(
            (p) => html`
              <button
                type="button"
                key=${p.key}
                class=${cn("preset", preset === p.key && "on")}
                onClick=${() => setPreset(p.key)}
              >
                <div class="preset-head">
                  <span class="preset-name">${p.name}</span>
                  <span class="preset-size">${p.n_satellites} sat · ${p.n_stations} gs</span>
                </div>
                ${p.description && html`<p class="preset-desc">${p.description}</p>`}
                <div class="preset-badges">
                  <${Badge}>${p.weather}<//>
                  ${p.failures && html`<${Badge} tone="warn">failures<//>`}
                  ${p.handover && html`<${Badge}>handover<//>`}
                  <${Badge}>${p.duration_s / 60} min<//>
                </div>
              </button>
            `,
          )}
        </div>
      </div>

      <div class="controls">
        <${Select}
          label="Scheduler"
          value=${scheduler}
          options=${policies.schedulers}
          onChange=${setScheduler}
          disabled=${busy}
        />
        <${Select}
          label="Bandwidth"
          value=${bw}
          options=${policies.bandwidth_allocators}
          onChange=${setBw}
          disabled=${busy}
        />
        <${Select}
          label="Power"
          value=${power}
          options=${policies.power_allocators}
          onChange=${setPower}
          disabled=${busy}
        />
        <${Select}
          label="Frequency"
          value=${freq}
          options=${policies.freq_allocators}
          onChange=${setFreq}
          disabled=${busy}
        />
      </div>

      <div class="run-bar">
        <span class="switch" onClick=${() => setLive((v) => !v)}>
          <span class=${cn("switch-track", live && "on")}>
            <span class="switch-knob"></span>
          </span>
          Live pacing
        </span>
        <${Button} solid onClick=${start} disabled=${busy}>
          ${busy ? "Starting…" : "Run scenario"} <span class="mono">↗</span>
        <//>
      </div>

      ${chosen &&
      html`<p class="note">
        ${chosen.duration_s / chosen.dt_s} steps at dt=${chosen.dt_s}s · one telemetry record per
        step · ${live ? "streamed at ~8 fps" : "streamed as fast as it computes"}
      </p>`}
      ${err && html`<p class="note err">${err}</p>`}
      ${current && current.status === "error" &&
      html`<p class="note err">Run failed: ${current.error}</p>`}
    </div>
  `;
}
