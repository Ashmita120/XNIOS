"use client";

/**
 * Live-run state. Opens the run's WebSocket, keeps the newest frame for the
 * tiles/map/tables and a rolling history for the charts, and falls back to
 * polling `/frame` if the socket cannot be established.
 *
 * History is capped: a long run at dt=5s produces thousands of frames and the
 * charts only need a few hundred points, so old frames are thinned rather than
 * accumulated forever.
 */

import * as React from "react";
import { api, wsUrl } from "./api";
import type { Frame, RunInfo } from "./types";

const MAX_HISTORY = 600;

export interface HistoryPoint {
  t: number;
  step: number;
  throughput_gbps: number;
  delivered_gbit: number;
  queue_gbit: number;
  beam_utilization: number;
  bandwidth_utilization: number;
  contention: number;
  waiting: number;
  active: number;
  coverage: number;
  mean_sinr_db: number;
  power_w: number;
  energy_kj: number;
  health: number;
  congestion: number;
  failure_risk: number;
  link_quality: number;
}

function toPoint(f: Frame): HistoryPoint {
  const n = f.record.network;
  const h = f.health;
  return {
    t: n.t,
    step: f.record.step,
    throughput_gbps: n.throughput_bps / 1e9,
    delivered_gbit: n.bits_delivered_total / 1e9,
    queue_gbit: n.queue_bits / 1e9,
    beam_utilization: n.beam_utilization,
    bandwidth_utilization: n.bandwidth_utilization,
    contention: n.contention_ratio,
    waiting: n.n_waiting,
    active: n.beams_active,
    coverage: n.coverage,
    mean_sinr_db: n.mean_sinr_db,
    power_w: n.power_w,
    energy_kj: n.energy_j_total / 1e3,
    health: h.network_health,
    congestion: h.indicators.congestion?.score ?? 0,
    failure_risk: h.indicators.failure_risk?.score ?? 0,
    link_quality: h.indicators.link_quality?.score ?? 0,
  };
}

export function useRun(runId: string | null) {
  const [frame, setFrame] = React.useState<Frame | null>(null);
  const [history, setHistory] = React.useState<HistoryPoint[]>([]);
  const [info, setInfo] = React.useState<RunInfo | null>(null);
  const [connected, setConnected] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    setFrame(null);
    setHistory([]);
    setInfo(null);
    setError(null);
    if (!runId) return;

    let closed = false;
    let ws: WebSocket | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;

    const push = (f: Frame) => {
      setFrame(f);
      setHistory((prev) => {
        const next = [...prev, toPoint(f)];
        // thin by dropping every other old point once the cap is hit, so the
        // chart keeps the full time span instead of only the recent tail
        if (next.length > MAX_HISTORY) {
          const head = next.slice(0, next.length - MAX_HISTORY / 2).filter((_, i) => i % 2 === 0);
          return [...head, ...next.slice(next.length - MAX_HISTORY / 2)];
        }
        return next;
      });
    };

    const startPolling = () => {
      if (poll) return;
      poll = setInterval(async () => {
        try {
          const [f, i] = await Promise.all([api.frame(runId), api.run(runId)]);
          if (closed) return;
          push(f);
          setInfo(i);
          if (i.status === "done" || i.status === "error") {
            if (poll) clearInterval(poll);
            poll = null;
          }
        } catch {
          /* run not ready yet — keep polling */
        }
      }, 700);
    };

    try {
      ws = new WebSocket(wsUrl(runId));
      ws.onopen = () => setConnected(true);
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "meta") setInfo(msg.run as RunInfo);
        else if (msg.type === "frame") push(msg as Frame);
        else if (msg.type === "end") {
          setInfo(msg.run as RunInfo);
          setConnected(false);
        } else if (msg.type === "error") setError(msg.message);
      };
      ws.onerror = () => {
        setConnected(false);
        startPolling();
      };
      ws.onclose = () => setConnected(false);
    } catch {
      startPolling();
    }

    return () => {
      closed = true;
      if (poll) clearInterval(poll);
      ws?.close();
    };
  }, [runId]);

  return { frame, history, info, connected, error };
}
