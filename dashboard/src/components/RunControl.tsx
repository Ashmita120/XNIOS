"use client";

/**
 * Scenario + policy selection. The four dropdowns are exactly the four pluggable
 * axes of the twin (`scheduler × bandwidth × power × frequency`) — which is why
 * this panel is also the seam for V2 Phase 5: the decision engine will set the
 * same four values, and the operator will be able to see and override them.
 */

import * as React from "react";
import { api } from "@/lib/api";
import type { Policies, Preset, RunInfo } from "@/lib/types";
import { Badge, Button, Empty, Select } from "./ui";
import { cn } from "@/lib/format";

export function RunControl({
  onStarted,
  current,
}: {
  onStarted: (run: RunInfo) => void;
  current: RunInfo | null;
}) {
  const [presets, setPresets] = React.useState<Preset[]>([]);
  const [policies, setPolicies] = React.useState<Policies | null>(null);
  const [preset, setPreset] = React.useState("india4-nominal");
  const [scheduler, setScheduler] = React.useState("fcfs/strongest");
  const [bw, setBw] = React.useState("equal");
  const [power, setPower] = React.useState("adaptive");
  const [freq, setFreq] = React.useState("coloring");
  const [live, setLive] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState<string | null>(null);

  React.useEffect(() => {
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
    return (
      <Empty>
        API unreachable — start it with{" "}
        <span className="ml-1 text-dim">uvicorn api.main:app --port 8000</span>
      </Empty>
    );
  }
  if (!policies) return <Empty>loading policies</Empty>;

  return (
    <div className="space-y-5">
      <div>
        <span className="label">Scenario</span>
        <div className="mt-2.5 space-y-px overflow-hidden rounded-panel border border-line bg-line">
          {presets.map((p) => (
            <button
              key={p.key}
              onClick={() => setPreset(p.key)}
              className={cn(
                "block w-full bg-bg px-4 py-3 text-left transition-colors duration-300 hover:bg-bg-2",
                preset === p.key && "bg-bg-2",
              )}
            >
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-[13px] font-medium">{p.name}</span>
                <span className="font-mono text-[10px] uppercase tracking-[.12em] text-mute">
                  {p.n_satellites} sat · {p.n_stations} gs
                </span>
              </div>
              {p.description && (
                <p className="mt-1 max-w-[52ch] text-[11.5px] leading-snug text-mute">
                  {p.description}
                </p>
              )}
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <Badge>{p.weather}</Badge>
                {p.failures && <Badge tone="warn">failures</Badge>}
                {p.handover && <Badge>handover</Badge>}
                <Badge>{p.duration_s / 60} min</Badge>
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Select
          label="Scheduler"
          value={scheduler}
          options={policies.schedulers}
          onChange={setScheduler}
          disabled={busy}
        />
        <Select
          label="Bandwidth"
          value={bw}
          options={policies.bandwidth_allocators}
          onChange={setBw}
          disabled={busy}
        />
        <Select
          label="Power"
          value={power}
          options={policies.power_allocators}
          onChange={setPower}
          disabled={busy}
        />
        <Select
          label="Frequency"
          value={freq}
          options={policies.freq_allocators}
          onChange={setFreq}
          disabled={busy}
        />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex cursor-pointer items-center gap-2.5 font-mono text-[11px] uppercase tracking-[.12em] text-mute">
          <span
            onClick={() => setLive((v) => !v)}
            className={cn(
              "relative h-[18px] w-[32px] rounded-pill border transition-colors duration-300",
              live ? "border-fg bg-fg" : "border-line-2",
            )}
          >
            <span
              className={cn(
                "absolute top-[2px] h-[12px] w-[12px] rounded-full transition-all duration-300 ease-arc",
                live ? "left-[17px] bg-bg" : "left-[2px] bg-mute",
              )}
            />
          </span>
          Live pacing
        </label>
        <Button solid onClick={start} disabled={busy}>
          {busy ? "Starting…" : "Run scenario"} <span className="font-mono">↗</span>
        </Button>
      </div>

      {chosen && (
        <p className="font-mono text-[10px] leading-relaxed text-mute">
          {chosen.duration_s / chosen.dt_s} steps at dt={chosen.dt_s}s · one telemetry record per
          step · {live ? "streamed at ~8 fps" : "streamed as fast as it computes"}
        </p>
      )}
      {err && <p className="font-mono text-[10px] text-status-crit">{err}</p>}
      {current?.status === "error" && (
        <p className="font-mono text-[10px] text-status-crit">Run failed: {current.error}</p>
      )}
    </div>
  );
}
