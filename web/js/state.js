/**
 * Live-run state. Opens the run's WebSocket, keeps the newest frame for the
 * tiles/map/tables and a rolling history for the charts, and falls back to
 * polling `/frame` if the socket cannot be established.
 *
 * History is capped: a long run at dt=5s produces thousands of frames and the
 * charts only need a few hundred points, so old frames are thinned rather than
 * accumulated forever.
 */

import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, wsUrl } from "./api.js";

const MAX_HISTORY = 600;

function toPoint(f) {
  const n = f.record.network;
  const h = f.health;
  const ind = (h.indicators && h.indicators) || {};
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
    congestion: (ind.congestion && ind.congestion.score) || 0,
    failure_risk: (ind.failure_risk && ind.failure_risk.score) || 0,
    link_quality: (ind.link_quality && ind.link_quality.score) || 0,
  };
}

export function useRun(runId) {
  const [frame, setFrame] = useState(null);
  const [history, setHistory] = useState([]);
  const [info, setInfo] = useState(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    setFrame(null);
    setHistory([]);
    setInfo(null);
    setError(null);
    if (!runId) return;

    let closed = false;
    let ws = null;
    let poll = null;

    const push = (f) => {
      setFrame(f);
      setHistory((prev) => {
        const next = [...prev, toPoint(f)];
        // thin by dropping every other old point once the cap is hit, so the
        // chart keeps the full time span instead of only the recent tail
        if (next.length > MAX_HISTORY) {
          const head = next
            .slice(0, next.length - MAX_HISTORY / 2)
            .filter((_, i) => i % 2 === 0);
          return [...head, ...next.slice(next.length - MAX_HISTORY / 2)];
        }
        return next;
      });
    };

    const startPolling = () => {
      if (poll || closed) return;
      poll = setInterval(async () => {
        try {
          const [f, i] = await Promise.all([api.frame(runId), api.run(runId)]);
          if (closed) return;
          push(f);
          setInfo(i);
          if (i.status === "done" || i.status === "error") {
            clearInterval(poll);
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
        if (msg.type === "meta") setInfo(msg.run);
        else if (msg.type === "frame") push(msg);
        else if (msg.type === "end") {
          setInfo(msg.run);
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
      if (ws) ws.close();
    };
  }, [runId]);

  return { frame, history, info, connected, error };
}

/**
 * Element size, for the charts and the map.
 *
 * Recharts' <ResponsiveContainer> and MapLibre both did this internally; drawing
 * our own SVG means measuring the box ourselves. The callback ref (rather than
 * useRef + useEffect) is what makes it fire when a panel mounts inside a grid
 * that has not laid out yet.
 */
export function useMeasure() {
  const [size, setSize] = useState({ width: 0, height: 0 });
  const observer = useRef(null);

  const ref = useCallback((node) => {
    if (observer.current) {
      observer.current.disconnect();
      observer.current = null;
    }
    if (!node) return;
    const apply = () =>
      setSize({ width: node.clientWidth, height: node.clientHeight });
    apply();
    observer.current = new ResizeObserver(apply);
    observer.current.observe(node);
  }, []);

  useEffect(() => () => observer.current && observer.current.disconnect(), []);

  return [ref, size];
}
