/**
 * Client for the FastAPI service in `api/`. Requests go to same-origin `/api/*`
 * (rewritten to the Python service by next.config.mjs), so there is no CORS
 * handling and the WebSocket upgrade shares the page's host.
 */

import type { Frame, Policies, Preset, RunInfo, TimelinePoint } from "./types";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${body ? ` — ${body}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  policies: () => json<Policies>("/api/policies"),
  presets: () => json<Preset[]>("/api/presets"),
  runs: () => json<RunInfo[]>("/api/runs"),
  run: (id: string) => json<RunInfo>(`/api/runs/${id}`),
  frame: (id: string, step?: number) =>
    json<Frame>(`/api/runs/${id}/frame${step === undefined ? "" : `?step=${step}`}`),
  timeline: (id: string, every = 4) =>
    json<TimelinePoint[]>(`/api/runs/${id}/timeline?every=${every}`),
  series: (id: string, every = 1) =>
    json<Record<string, number[]>>(`/api/runs/${id}/series?every=${every}`),
  start: (body: {
    preset: string;
    scheduler: string;
    bandwidth_allocator: string;
    power_allocator: string;
    freq_allocator: string;
    pace_ms: number;
    duration_s?: number;
  }) => json<RunInfo>("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  remove: (id: string) => json<{ deleted: boolean }>(`/api/runs/${id}`, { method: "DELETE" }),
  exportUrl: (id: string, face: string) => `/api/runs/${id}/export/${face}.csv`,
};

/**
 * WebSocket URL for a run's live frame stream.
 *
 * This one goes *directly* to the FastAPI origin rather than through the Next
 * rewrite: Next.js rewrites do not proxy WebSocket upgrades, so routing it like
 * the REST calls would silently fall back to polling. FastAPI's CORS middleware
 * already allows the dev origin.
 */
export function wsUrl(id: string, fromStep = 0): string {
  if (typeof window === "undefined") return "";
  const base = process.env.NEXT_PUBLIC_XNIOS_API ?? "http://127.0.0.1:8000";
  const url = new URL(base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/api/ws/runs/${id}`;
  url.search = `?from_step=${fromStep}`;
  return url.toString();
}
