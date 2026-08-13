"use client";

/**
 * The operator console.
 *
 * Layout follows the brief: headline health tiles, the satellite map, resource
 * and link monitors, the AI explanation panel, and scenario control. Section
 * rhythm, spacing and type are ARCTROPY's — fixed nav, hairline dividers,
 * wide-tracked eyebrows, a 1320px shell.
 *
 * Every panel is a *reader* of one telemetry stream. Nothing here computes
 * simulation state, which is what makes the same components work later for a
 * live network, a replayed historical run, or a forecast.
 */

import * as React from "react";
import { Download } from "lucide-react";
import { Nav } from "@/components/Nav";
import { HealthHeader } from "@/components/HealthHeader";
import { NetworkMap } from "@/components/NetworkMap";
import { HealthChart, QueueChart, ThroughputChart, UtilisationChart } from "@/components/Charts";
import { EventFeed, LinkMonitor, ResourceMonitor } from "@/components/Resources";
import { DecisionPanel, IndicatorBreakdown } from "@/components/Decision";
import { RunControl } from "@/components/RunControl";
import { Badge, Eyebrow, Panel, Row } from "@/components/ui";
import { api } from "@/lib/api";
import { useRun } from "@/lib/useRun";
import type { EventRecord, RunInfo } from "@/lib/types";
import { bits, clock, pct } from "@/lib/format";

export default function Console() {
  const [runId, setRunId] = React.useState<string | null>(null);
  const [started, setStarted] = React.useState<RunInfo | null>(null);
  const [focus, setFocus] = React.useState<string | null>(null);
  const { frame, history, info, connected } = useRun(runId);

  // resume the newest run on load, so a page refresh does not lose the view
  React.useEffect(() => {
    api
      .runs()
      .then((rs) => {
        if (rs.length && !runId) {
          setRunId(rs[0].run_id);
          setStarted(rs[0]);
        }
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const run = info ?? started;
  const net = frame?.record.network;

  // keep a rolling event log across frames (each frame carries only its own)
  const [events, setEvents] = React.useState<EventRecord[]>([]);
  React.useEffect(() => setEvents([]), [runId]);
  React.useEffect(() => {
    if (!frame?.record.events.length) return;
    setEvents((prev) => [...prev, ...frame.record.events].slice(-200));
  }, [frame]);

  return (
    <>
      <Nav
        right={
          <div className="hidden items-center gap-3 md:flex">
            {run && (
              <span className="font-mono text-[11px] uppercase tracking-[.14em] text-mute">
                {connected && <span className="dotlive mr-2 align-middle" />}
                {run.status === "running"
                  ? `${pct(run.progress)} · T+${clock(frame?.record.t ?? 0)}`
                  : run.status}
              </span>
            )}
          </div>
        }
      />

      <main className="mx-auto max-w-shell px-[var(--pad)] pb-24 pt-[96px]">
        {/* ---------------------------------------------------------- header */}
        <section id="overview" className="animate-rise">
          <Eyebrow>AI Digital Twin — Phase 1 · State awareness</Eyebrow>
          <h1 className="mt-5 max-w-[18ch] text-[clamp(2rem,5vw,3.6rem)] font-medium leading-[1.02] tracking-[-.02em]">
            The network, as it is right now.
          </h1>
          <p className="mt-5 max-w-[62ch] text-[clamp(1rem,1.4vw,1.15rem)] font-light leading-relaxed text-dim">
            Every panel below reads one stream: a telemetry record per simulation step, carrying the
            whole network, every station, every link, every satellite and the decision that produced
            them. The same stream feeds the health monitor here, and — next — the feature layer,
            forecast and decision engine.
          </p>

          <div className="mt-8 flex flex-wrap items-center gap-x-8 gap-y-2 border-t border-line pt-4 font-mono text-[11px] uppercase tracking-[.14em] text-mute">
            <span>
              <span className="dotlive mr-2 align-middle" />
              <span className="text-fg">{connected ? "live" : run?.status ?? "idle"}</span>
            </span>
            <span>
              Records <span className="text-fg">{run?.steps ?? 0}</span>/{run?.total_steps ?? 0}
            </span>
            <span>
              Sim clock <span className="text-fg">T+{clock(net?.t ?? 0)}</span>
            </span>
            <span>
              Schema <span className="text-fg">{frame?.record.schema_version ?? "—"}</span>
            </span>
            {run?.meta && (
              <span>
                {run.meta.n_satellites} sats · {run.meta.n_stations} stations ·{" "}
                {run.meta.n_beams_total} beams
              </span>
            )}
          </div>
        </section>

        <div className="mt-8">
          <HealthHeader frame={frame} />
        </div>

        {/* ------------------------------------------------------------- map */}
        <section id="map" className="mt-16">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <Eyebrow>Satellite map</Eyebrow>
              <h2 className="mt-3 text-[clamp(1.5rem,2.6vw,2rem)] font-medium tracking-[-.02em]">
                Ground segment
              </h2>
            </div>
            <p className="max-w-[46ch] font-mono text-[11px] leading-relaxed text-mute">
              Stations, sub-satellite points and every active beam, drawn from the current record.
              Filled diamonds are transmitting; rings mark stations with beams committed.
            </p>
          </div>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[1.55fr_1fr]">
            <div className="h-[520px]">
              <NetworkMap frame={frame} focus={focus} />
            </div>
            <div className="flex flex-col gap-6">
              <Panel title="Resource monitor" bodyClassName="py-1">
                <ResourceMonitor frame={frame} onFocus={setFocus} focus={focus} />
              </Panel>
              <Panel title="Events" bodyClassName="py-1">
                <EventFeed events={events} />
              </Panel>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------- resources */}
        <section id="resources" className="mt-16">
          <Eyebrow>Resource &amp; link telemetry</Eyebrow>
          <h2 className="mt-3 text-[clamp(1.5rem,2.6vw,2rem)] font-medium tracking-[-.02em]">
            What every beam is doing
          </h2>

          <div className="mt-6 grid grid-cols-1 gap-6 xl:grid-cols-2">
            <Panel title="Throughput">
              <ThroughputChart data={history} />
            </Panel>
            <Panel title="Utilisation · beams / bandwidth / coverage">
              <UtilisationChart data={history} />
            </Panel>
            <Panel title="Backlog vs delivered">
              <QueueChart data={history} />
            </Panel>
            <Panel title="Health · congestion · failure risk">
              <HealthChart data={history} />
            </Panel>
          </div>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[1.4fr_1fr]">
            <Panel
              title="Link quality monitor"
              action={
                runId && (
                  <a
                    href={api.exportUrl(runId, "link")}
                    className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[.14em] text-mute transition-colors hover:text-fg"
                  >
                    <Download size={12} /> link.csv
                  </a>
                )
              }
              bodyClassName="py-2"
            >
              <LinkMonitor frame={frame} />
            </Panel>

            <Panel title="Network row">
              {net ? (
                <div>
                  <Row k="Delivered" v={bits(net.bits_delivered_total)} />
                  <Row k="Queued" v={bits(net.queue_bits)} />
                  <Row k="Completed" v={`${net.n_completed}/${net.n_sats}`} />
                  <Row k="Waiting" v={net.n_waiting} />
                  <Row k="Beams" v={`${net.beams_active}/${net.beams_available} of ${net.beams_total}`} />
                  <Row k="Contention" v={net.contention_ratio.toFixed(2)} />
                  <Row k="Coverage" v={pct(net.coverage)} />
                  <Row k="Mean SINR" v={`${net.mean_sinr_db.toFixed(1)} dB`} />
                  <Row k="Radiated power" v={`${net.power_w.toFixed(1)} W`} />
                  <Row k="Energy" v={`${(net.energy_j_total / 1e3).toFixed(2)} kJ`} />
                  <Row k="Interruptions" v={net.interruptions_total} />
                  <Row k="Handovers" v={`${net.handovers_total} (${net.proactive_handovers_total} proactive)`} />
                  <Row k="Decision latency" v={`${net.decision_ms.toFixed(3)} ms`} />
                </div>
              ) : (
                <div className="label">awaiting telemetry</div>
              )}
            </Panel>
          </div>
        </section>

        {/* -------------------------------------------------------- decision */}
        <section id="decision" className="mt-16">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <Eyebrow>Decision &amp; explanation</Eyebrow>
              <h2 className="mt-3 text-[clamp(1.5rem,2.6vw,2rem)] font-medium tracking-[-.02em]">
                Why the network looks like this
              </h2>
            </div>
            <Badge>Phase 4 slot — contract already live</Badge>
          </div>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Panel title="Active configuration">
              <DecisionPanel frame={frame} />
            </Panel>
            <Panel title="Health breakdown — click any indicator">
              <IndicatorBreakdown frame={frame} />
            </Panel>
          </div>
        </section>

        {/* -------------------------------------------------------- scenario */}
        <section id="scenario" className="mt-16">
          <Eyebrow>Scenario control</Eyebrow>
          <h2 className="mt-3 text-[clamp(1.5rem,2.6vw,2rem)] font-medium tracking-[-.02em]">
            Run the twin
          </h2>

          <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[1.2fr_1fr]">
            <Panel title="Configuration">
              <RunControl
                current={run}
                onStarted={(r) => {
                  setStarted(r);
                  setRunId(r.run_id);
                }}
              />
            </Panel>

            <Panel
              title="Run summary"
              action={
                runId && (
                  <div className="flex gap-3">
                    {["network", "station", "link", "satellite", "event"].map((f) => (
                      <a
                        key={f}
                        href={api.exportUrl(runId, f)}
                        className="font-mono text-[10px] uppercase tracking-[.14em] text-mute transition-colors hover:text-fg"
                      >
                        {f}
                      </a>
                    ))}
                  </div>
                )
              }
            >
              {run ? (
                <div>
                  <Row k="Run" v={run.run_id} />
                  <Row k="Scenario" v={run.name} />
                  <Row k="Status" v={run.status} />
                  <Row k="Scheduler" v={run.policy.scheduler} />
                  <Row k="Bandwidth" v={run.policy.bandwidth_allocator} />
                  <Row k="Power" v={run.policy.power_allocator} />
                  <Row k="Frequency" v={run.policy.freq_allocator} />
                  {run.meta && (
                    <>
                      <Row k="Weather model" v={run.meta.weather_model} />
                      <Row k="Failures" v={run.meta.dynamics ? "enabled" : "off"} />
                      <Row k="Handover" v={run.meta.handover ? "enabled" : "off"} />
                      <Row k="Seed" v={run.meta.seed ?? "—"} />
                    </>
                  )}
                  {run.summary && (
                    <>
                      <div className="mt-4 label">Final KPI vector</div>
                      {["delivered_gbit", "completion_rate", "sla_compliance", "fairness",
                        "mean_wait_s", "beam_utilization", "energy_kj", "gb_per_kj",
                        "sessions_interrupted", "proactive_handovers"].map((k) => (
                        <Row
                          key={k}
                          k={k.replace(/_/g, " ")}
                          v={
                            typeof run.summary![k] === "number"
                              ? (run.summary![k] as number).toFixed(3)
                              : String(run.summary![k])
                          }
                        />
                      ))}
                    </>
                  )}
                </div>
              ) : (
                <div className="label">no run yet</div>
              )}
            </Panel>
          </div>
        </section>

        <footer className="mt-20 border-t border-line pt-6 font-mono text-[10px] uppercase tracking-[.16em] text-mute">
          X-NioS digital twin · telemetry schema {frame?.record.schema_version ?? "1.0"} · health
          scores are an explicit weighted scalarisation, computed outside the twin
        </footer>
      </main>
    </>
  );
}
