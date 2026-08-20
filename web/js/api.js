/**
 * Client for the FastAPI service in `api/`.
 *
 * The console is now served by that same service (see the StaticFiles mount at
 * the bottom of api/main.py), so every path here is same-origin: no CORS, no
 * dev-server rewrite, and — unlike the Next.js setup this replaces — the
 * WebSocket upgrade goes to the same host as the REST calls instead of needing a
 * hard-coded 127.0.0.1:8000 fallback.
 *
 * Opening index.html straight off the filesystem still works: set
 * `window.XNIOS_API = "http://127.0.0.1:8000"` before the module loads and every
 * request retargets there.
 */

const BASE = (globalThis.XNIOS_API || "").replace(/\/$/, "");

async function json(path, init) {
  const res = await fetch(BASE + path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init && init.headers) },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${body ? ` — ${body}` : ""}`);
  }
  return res.json();
}

export const api = {
  policies: () => json("/api/policies"),
  presets: () => json("/api/presets"),
  runs: () => json("/api/runs"),
  run: (id) => json(`/api/runs/${id}`),
  frame: (id, step) =>
    json(`/api/runs/${id}/frame${step === undefined ? "" : `?step=${step}`}`),
  timeline: (id, every = 4) => json(`/api/runs/${id}/timeline?every=${every}`),
  series: (id, every = 1) => json(`/api/runs/${id}/series?every=${every}`),
  start: (body) => json("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  remove: (id) => json(`/api/runs/${id}`, { method: "DELETE" }),
  exportUrl: (id, face) => `${BASE}/api/runs/${id}/export/${face}.csv`,

  /**
   * The planning surface. Distinct from runs above: a run is a closed-loop
   * what-if simulation, this is the operational request -> plan path, and the
   * two share no state. `quote` books nothing — `accept` is what consumes
   * capacity and charges the account's quota.
   */
  plan: {
    network: () => json("/api/plan/network"),
    bind: (body) => json("/api/plan/network", { method: "POST", body: JSON.stringify(body) }),
    customers: () => json("/api/plan/customers"),
    addCustomer: (body) =>
      json("/api/plan/customers", { method: "POST", body: JSON.stringify(body) }),
    quote: (body) => json("/api/plan", { method: "POST", body: JSON.stringify(body) }),
    batch: (body) => json("/api/plan/batch", { method: "POST", body: JSON.stringify(body) }),
    // run the booked ledger through the twin — the join between planning and
    // execution, and the only telemetry an operator should ever see
    execute: (body) => json("/api/plan/execute", { method: "POST", body: JSON.stringify(body || {}) }),
    accept: (id) => json(`/api/plan/${id}/accept`, { method: "POST" }),
    release: (id) => json(`/api/plan/${id}`, { method: "DELETE" }),
    ledger: () => json("/api/plan/ledger"),
  },
};

/** WebSocket URL for a run's live frame stream. */
export function wsUrl(id, fromStep = 0) {
  const url = new URL(BASE || window.location.origin, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/api/ws/runs/${id}`;
  url.search = `?from_step=${fromStep}`;
  return url.toString();
}
