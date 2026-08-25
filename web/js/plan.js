/**
 * PLAN — the only view that asks the network for something.
 *
 * Four sections, in the order an operator works: state the job, read the plan,
 * queue it against competing jobs, see what the network has promised.
 *
 *   01 Request   mission-level fields only, plus the account context they imply
 *   02 Plan      verdict first, then the three numbers, then the justification
 *   03 Queue     multi-request arbitration — where a tier finally decides something
 *   04 Ledger    committed capacity, and how to give it back
 *
 * Styling lives in `web/plan.css`, scoped to `.xn-plan` because the rest of the
 * console has not been ported to this palette yet.
 *
 * Quote and accept stay separate throughout, including for a batch: `commit`
 * defaults to false so policies can be compared without consuming capacity.
 */

import { html } from "htm/preact";
import { useEffect, useState } from "preact/hooks";
import { api } from "./api.js";
import { clock } from "./format.js";

const INTENTS = [
  ["asap", "ASAP"],
  ["by_deadline", "Deadline"],
  ["flexible", "Flexible"],
];
const PRIORITIES = ["", "low", "normal", "high", "critical"];
const TONE = { TRANSMIT_NOW: "", SCHEDULE: "", PARTIAL: "warn", REJECT: "crit" };
const TIER_CLASS = { emergency: "hi", military: "mid", commercial: "", research: "" };

const g1 = (v) => (v === null || v === undefined ? "—" : v.toFixed(1));

// ---------------------------------------------------------------- primitives
const Head = ({ idx, title, note }) => html`
  <div class="xhead">
    <span class="xidx">${idx}</span>
    <span class="xtitle">${title}</span>
    ${note && html`<span class="xnote">${note}</span>`}
  </div>
`;

const Panel = ({ cap, n, children, bare }) => html`
  <div class="xpanel">
    ${cap && html`<div class=${bare ? "xcap bare" : "xcap"}>
      <span>${cap}</span>${n && html`<span class="n">${n}</span>`}
    </div>`}
    ${children}
  </div>
`;

const Row = ({ k, v, u, tone }) => html`
  <div class="xrow">
    <span class="k">${k}</span>
    <span class=${tone ? `v ${tone}` : "v"}>${v}${u && html`<span class="u">${u}</span>`}</span>
  </div>
`;

const Field = ({ label, children }) => html`
  <label class="xfield"><span>${label}</span>${children}</label>
`;

/** Segmented meter — the quantity behind it is counted, so it gets ticks. */
const Meter = ({ filled, total = 8 }) => html`
  <div class="xmeter">
    ${Array.from({ length: total }, (_, i) =>
      html`<i key=${i} class=${i < filled ? "on" : ""}></i>`)}
  </div>
`;

const Hero = ({ label, value, unit, on, filled }) => html`
  <div class=${on ? "xhero on" : "xhero"}>
    <div class="l">${label}</div>
    <div class="n">${value}${unit && html`<em>${unit}</em>`}</div>
    <${Meter} filled=${filled} />
  </div>
`;

// ------------------------------------------------------------------ 02 plan
function Admission({ plan }) {
  const metered = plan.quota_remaining_gbit !== null && plan.quota_remaining_gbit !== undefined;
  const checks = [
    ["contact available", plan.schedule.length > 0],
    ["capacity reserved", plan.shortfall_gbit <= 1e-6],
    ["deadline satisfied", plan.meets_deadline === null || plan.meets_deadline === undefined
      ? null : plan.meets_deadline],
    ["no satellite conflict", plan.schedule.length > 0 ? true : null],
    ["quota available", metered ? !plan.quota_limited : null],
  ];
  return html`<div>
    ${checks.map(([label, ok]) => html`
      <div class="xchk" key=${label}>
        <span class=${`m ${ok === null ? "na" : ok ? "ok" : "bad"}`}>
          ${ok === null ? "–" : ok ? "✓" : "✕"}
        </span>
        <span>${label}${ok === null ? html` <span class="muted">(n/a)</span>` : ""}</span>
      </div>`)}
  </div>`;
}

function PlanPanel({ plan, booked, busy, onAccept, onRelease }) {
  if (!plan) {
    return html`<${Panel} cap="Communication plan">
      <div class="xempty">submit a request to see a plan</div>
    <//>`;
  }
  const w = plan.recommendation;
  const b = plan.beam_requirement;
  const n = plan.next_opportunity;
  const frac = plan.data_volume_gbit > 0
    ? Math.min(1, plan.scheduled_gbit / plan.data_volume_gbit) : 0;
  // DELIVERABLE is the whole plan; the rows beside it described only the first
  // window, and the two read as one set of numbers. 256.9 Gbit next to "booked
  // for 80 s" implies 3.2 Gbps from a 275 Mbps link — the plan was right, the
  // card was comparing a total against one slice of it. Carry the totals so
  // the arithmetic closes on what is shown.
  const bookedTotal = plan.schedule.reduce((a, s) => a + s.duration_s, 0);
  const multi = plan.schedule.length > 1;

  return html`
    <div class="xpanel">
      <div class=${`xverdict ${TONE[plan.decision] || ""}`}>
        <span class="tag">${plan.decision.replace(/_/g, " ")}</span>
        <span class="meta">
          <b>${plan.reason_code}</b>
          ${plan.request_id} · ${booked ? "booked" : "quoted, not booked"}
        </span>
      </div>

      <div class="xheros">
        <${Hero} label="Deliverable" value=${g1(plan.scheduled_gbit)} unit="Gbit"
                 on=${plan.shortfall_gbit <= 1e-6} filled=${Math.round(frac * 8)} />
        <${Hero} label="Completes"
                 value=${plan.completes_at_s === null ? "—" : clock(plan.completes_at_s)}
                 filled=${plan.completes_at_s === null ? 0 : 3} />
        <${Hero} label="Scan at start" value=${b ? b.scan_angle_deg.toFixed(1) : "—"} unit="°"
                 filled=${b ? Math.max(1, Math.round((b.scan_angle_deg / 90) * 8)) : 0} />
      </div>

      ${w && html`<div class="xhalf">
        <div>
          <div class="xcap bare">First contact</div>
          <${Row} k="Station" v=${w.station} tone="accent" />
          <${Row} k="Start" v=${`T+${clock(w.t_start)}`} />
          ${/* Two different things that were both labelled "Window": how long the
                transfer occupies the contact, and how long the contact lasts. */ null}
          <${Row} k="Booked for" v=${w.duration_s.toFixed(0)} u="s" />
          <${Row} k="Contact lasts" v=${(w.contact_s || w.duration_s).toFixed(0)} u="s" />
          <${Row} k="Carries" v=${g1(w.deliverable_gbit)} u="Gbit" />
          <div class="xcap bare" style=${{ marginTop: "12px" }}>Whole plan</div>
          <${Row} k="Contacts" v=${plan.schedule.length} />
          ${multi && html`
            <${Row} k="Booked across all" v=${bookedTotal.toFixed(0)} u="s" />`}
          <${Row} k="Requested" v=${g1(plan.data_volume_gbit)} u="Gbit" />
          <${Row} k="Shortfall" v=${g1(plan.shortfall_gbit)} u="Gbit"
                  tone=${plan.shortfall_gbit > 1e-6 ? "warn" : ""} />
        </div>
        <div>
          ${b && html`
            <${Row} k="Beams" v=${b.count} />
            ${/* Geometry is instantaneous and this is the value at the START of
                  the first window — the highest elevation the transfer will see.
                  Unlabelled it looks like a property of the pass, and then
                  execution telemetry sampled a minute later "disagrees" with it
                  when both are simply the same satellite, further along. */ null}
            <${Row} k="Pointing at start"
                    v=${`az ${b.az_deg.toFixed(1)} / el ${b.elev_deg.toFixed(1)}`} u="°" />
            <${Row} k="Range at start" v=${b.range_km.toFixed(0)} u="km" />
            <${Row} k="Beamwidth" v=${b.beamwidth_deg.toFixed(2)} u="°" />
            <${Row} k="Envelope" v=${b.within_scan_envelope ? "within limit" : "EXCEEDED"}
                    tone=${b.within_scan_envelope ? "accent" : "crit"} />`}
          <${Row} k="Band" v=${plan.frequency.band || "—"} />
          <${Row} k="Channel" v=${html`<span class="muted">at execution</span>`} />
        </div>
      </div>`}

      <div class="xhalf" style=${{ marginTop: "16px", paddingTop: "12px", borderTop: "1px solid var(--p-line)" }}>
        <div>
          <div class="xcap bare">Admission</div>
          <${Admission} plan=${plan} />
        </div>
        <div>
          <div class="xcap bare">Next opportunity</div>
          ${n
            ? html`<div>
                <${Row} k="Station" v=${n.station} />
                <${Row} k="In" v=${clock(n.in_s)} />
                <${Row} k="Capacity" v=${g1(n.deliverable_gbit)} u="Gbit" />
              </div>`
            : html`<div class="xempty">none in horizon</div>`}
        </div>
      </div>

      ${plan.explanation.length > 0 && html`
        <ul class="xwhy">${plan.explanation.map((e, i) => html`<li key=${i}>${e}</li>`)}</ul>`}

      <div class="xbtnrow">
        <button class="xbtn" disabled=${busy || booked || !plan.admitted} onClick=${onAccept}>
          ${booked ? "Booked" : "Accept plan"}
        </button>
        <button class="xbtn ghost" disabled=${busy || !booked} onClick=${onRelease}>Release</button>
      </div>
    </div>
  `;
}

// ----------------------------------------------------------------- 03 queue
function QueuePanel({ queue, onRemove, onClear, onCommitted }) {
  const [cmp, setCmp] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  async function compare(commit) {
    setBusy(true);
    setErr(null);
    try {
      const body = (policy) => ({ requests: queue, policy, commit, allow_partial: true });
      if (commit) {
        const r = await api.plan.batch(body("oppcost"));
        setCmp({ oppcost: r, fcfs: cmp && cmp.fcfs, committed: true });
        if (onCommitted) onCommitted();      // capacity was consumed
      } else {
        const [f, o] = await Promise.all([
          api.plan.batch(body("fcfs")),
          api.plan.batch(body("oppcost")),
        ]);
        setCmp({ fcfs: f, oppcost: o, committed: false });
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const fs = cmp && cmp.fcfs && cmp.fcfs.summary;
  const os = cmp && cmp.oppcost && cmp.oppcost.summary;

  return html`
    <${Panel} cap="Competing requests" n=${`${queue.length} queued`}>
      ${queue.length === 0
        ? html`<div class="xempty">
            add requests with QUEUE IT above — nothing is booked, the queue is local
            until you compare, and only COMMIT OPPCOST consumes capacity
          </div>`
        : html`<div class="xscroll">
            <table>
              <thead><tr>
                <th>#</th><th>Satellite</th><th>Customer</th><th>Timing</th>
                <th class="num">Volume</th><th class="num">Deadline</th><th></th>
              </tr></thead>
              <tbody>
                ${queue.map((r, i) => html`<tr key=${r.request_id}>
                  <td class="muted">${String(i + 1).padStart(2, "0")}</td>
                  <td>${r.satellite_id}</td>
                  <td>${r.customer_id || html`<span class="muted">—</span>`}</td>
                  <td>${r.timing.replace(/_/g, " ")}</td>
                  <td class="num">${r.data_volume_gbit.toFixed(1)}</td>
                  <td class="num">${r.deadline_s ? `T+${clock(r.deadline_s)}` : "—"}</td>
                  <td class="num">
                    <button class="xdel" title="remove" onClick=${() => onRemove(r.request_id)}>×</button>
                  </td>
                </tr>`)}
              </tbody>
            </table>
          </div>`}

      <div class="xbtnrow">
        <button class="xbtn" disabled=${busy || !queue.length} onClick=${() => compare(false)}>
          Compare policies
        </button>
        <button class="xbtn ghost" disabled=${busy || !cmp || cmp.committed} onClick=${() => compare(true)}>
          Commit oppcost
        </button>
        <button class="xbtn ghost" disabled=${busy || !queue.length} onClick=${onClear}>Clear</button>
      </div>
      ${err && html`<div class="xerr">${err}</div>`}

      ${fs && os && html`<div style=${{ marginTop: "16px" }}>
        ${/* Same queue + same ledger + same t_now = same allocation. Naming the
              ledger it planned against is what makes that checkable — without
              it, a comparison run before and after a booking looks random. */ null}
        <div class="xnote" style=${{ marginLeft: 0, marginBottom: "8px" }}>
          planned against ${cmp.oppcost.baseline.commitments} existing booking(s)
          · ${cmp.oppcost.baseline.consumed_gbit.toFixed(1)} Gbit already committed
          · clock frozen at T+${cmp.oppcost.baseline.t_now.toFixed(0)}s
          ${cmp.oppcost.baseline.commitments > 0
            ? html` — release them to compare against an empty network`
            : ""}
        </div>
        <div class="xcmp">
          <div>
            <div class="l">fcfs — submission order</div>
            <div class="n">${(fs.weighted_completion * 100).toFixed(1)}<em>%</em></div>
            <div class="d">
              weighted completion · ${fs.fully_met} of ${fs.requests} met ·
              ${g1(fs.scheduled_gbit)} Gbit
            </div>
          </div>
          <div class=${os.weighted_completion >= fs.weighted_completion ? "win" : ""}>
            <div class="l">oppcost — opportunity cost</div>
            <div class="n">${(os.weighted_completion * 100).toFixed(1)}<em>%</em></div>
            <div class="d">
              weighted completion · ${os.fully_met} of ${os.requests} met ·
              ${g1(os.scheduled_gbit)} Gbit
            </div>
          </div>
        </div>
        <div class="xscroll" style=${{ marginTop: "16px" }}>
          <table>
            <thead><tr>
              <th>#</th><th>Request</th><th>Tier</th><th>Satellite</th>
              <th class="num">Volume</th><th class="num">Scheduled</th><th>Outcome</th>
            </tr></thead>
            <tbody>
              ${cmp.oppcost.plans.map((p, i) => {
                const met = p.shortfall_gbit <= 1e-6 && p.schedule.length > 0;
                const partial = p.schedule.length > 0 && !met;
                return html`<tr key=${p.request_id}>
                  <td class="muted">${String(i + 1).padStart(2, "0")}</td>
                  <td>${p.request_id}</td>
                  <td><span class=${`xbadge ${TIER_CLASS[p.tier] || ""}`}>${p.tier || "—"}</span></td>
                  <td>${p.satellite_id}</td>
                  <td class="num">${g1(p.data_volume_gbit)}</td>
                  <td class="num">${g1(p.scheduled_gbit)}</td>
                  <td><span class=${`xbadge ${met ? "hi" : partial ? "mid" : "lo"}`}>
                    ${met ? "met" : partial ? "partial" : "rejected"}
                  </span></td>
                </tr>`;
              })}
            </tbody>
          </table>
        </div>
        <div class="xnote" style=${{ marginTop: "12px", marginLeft: 0 }}>
          booked order · ${cmp.oppcost.booked_order.join(" → ")}
        </div>
      </div>`}
    <//>
  `;
}

// ---------------------------------------------------------------- 04 ledger
const LedgerPanel = ({ ledger }) => html`
  <${Panel} cap="Bookings"
            n=${ledger && ledger.commitments.length
              ? `${ledger.total_gbit.toFixed(1)} GBIT TOTAL` : ""}>
    ${!ledger || !ledger.commitments.length
      ? html`<div class="xempty">nothing booked</div>`
      : html`<div class="xscroll">
          <table>
            <thead><tr>
              <th>Request</th><th>Satellite</th><th>Station</th>
              <th class="num">Start</th><th class="num">End</th><th class="num">Gbit</th>
            </tr></thead>
            <tbody>
              ${ledger.commitments.map((c, i) => html`<tr key=${i}>
                <td>${c.request_id}</td>
                <td>${c.satellite_id}</td>
                <td>${c.station}</td>
                <td class="num">T+${clock(c.t_start)}</td>
                <td class="num">T+${clock(c.t_end)}</td>
                <td class="num">${c.gbit.toFixed(2)}</td>
              </tr>`)}
            </tbody>
          </table>
        </div>`}
  <//>
`;

// ======================================================================= view
export function PlanningConsole({ onLedgerChange }) {
  const [net, setNet] = useState(null);
  const [customers, setCustomers] = useState([]);
  const [sat, setSat] = useState("");
  const [volume, setVolume] = useState("18.4");
  const [intent, setIntent] = useState("asap");
  const [deadline, setDeadline] = useState("3600");
  const [priority, setPriority] = useState("");
  const [customer, setCustomer] = useState("");
  const [plan, setPlan] = useState(null);
  const [booked, setBooked] = useState(false);
  const [ledger, setLedger] = useState(null);
  const [queue, setQueue] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    Promise.all([api.plan.network(), api.plan.customers(), api.plan.ledger()])
      .then(([n, c, l]) => {
        setNet(n);
        setCustomers(c);
        setLedger(l);
        if (n.satellites.length) setSat(n.satellites[0]);
      })
      .catch((e) => setErr(String(e)));
  }, []);

  const account = customers.find((c) => c.customer_id === customer);

  function body() {
    const b = { satellite_id: sat, data_volume_gbit: Number(volume), timing: intent, t_now: 0 };
    if (intent === "by_deadline") b.deadline_s = Number(deadline);
    if (priority) b.priority = priority;
    if (customer) b.customer_id = customer;
    return b;
  }

  /** Re-read the ledger and tell the shell, so the masthead and TRANSFER follow.
   *  Every path that changes bookings must go through here — accept, release,
   *  and a committed batch. */
  const refreshLedger = () =>
    api.plan
      .ledger()
      .then((l) => {
        setLedger(l);
        if (onLedgerChange) onLedgerChange();
      })
      .catch(() => undefined);

  async function quote() {
    setBusy(true); setErr(null); setBooked(false);
    try { setPlan(await api.plan.quote(body())); }
    catch (e) { setErr(String(e)); setPlan(null); }
    finally { setBusy(false); }
  }

  async function accept() {
    setBusy(true); setErr(null);
    try { await api.plan.accept(plan.request_id); setBooked(true); await refreshLedger(); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }

  async function release() {
    setBusy(true);
    try { await api.plan.release(plan.request_id); setBooked(false); await refreshLedger(); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }

  /**
   * Add the current form values to the local comparison queue.
   *
   * Books nothing and calls nothing — the queue lives in this component until
   * "Compare policies" sends it as a dry run. It is deliberately separate from
   * Accept, which is the only control that consumes capacity.
   *
   * The timing intent is passed through as chosen. An earlier version forced
   * every queued request to by_deadline on the belief that a batch needed a
   * bound; it does not — asap and flexible both plan fine — and the override
   * silently rewrote an intent the operator had selected, using a deadline
   * field that is not even visible unless Deadline is picked.
   */
  function enqueue() {
    const b = body();
    b.request_id = `Q-${String(queue.length + 1).padStart(2, "0")}`;
    setQueue((q) => [...q, b]);
  }

  if (err && !net) {
    return html`<div class="xn-plan">
      <div class="xempty">
        Planning API unreachable — start it with
        <span style=${{ color: "var(--p-mid)" }}> python run_api.py</span>
      </div>
    </div>`;
  }
  if (!net) return html`<div class="xn-plan"><div class="xempty">loading network</div></div>`;

  return html`
    <div class="xn-plan">
      <!-- ------------------------------------------------------ 01 request -->
      <section class="xsec">
        <${Head} idx="01" title="Communication request"
                 note="mission-level input only — station, timing and beam are answers" />
        <div class="xcols">
          <div class="xpanel">
            <div class="xcap">
              <span>Request</span>
              <span class="n">${net.preset}</span>
            </div>
            <${Field} label="Satellite">
              <select value=${sat} onChange=${(e) => setSat(e.currentTarget.value)}>
                ${net.satellites.map((s) => html`<option key=${s} value=${s}>${s}</option>`)}
              </select>
            <//>
            <${Field} label="Data volume (Gbit)">
              <input type="number" step="0.1" min="0.1" value=${volume}
                     onInput=${(e) => setVolume(e.currentTarget.value)} />
            <//>
            <div class="xfield">
              <label style=${{ display: "block" }}>Timing intent</label>
              <div class="xseg">
                ${INTENTS.map(([v, l]) => html`
                  <button key=${v} class=${intent === v ? "on" : ""}
                          onClick=${() => setIntent(v)}>${l}</button>`)}
              </div>
            </div>
            ${intent === "by_deadline" && html`
              <${Field} label="Deadline (s from now)">
                <input type="number" step="60" min="1" value=${deadline}
                       onInput=${(e) => setDeadline(e.currentTarget.value)} />
              <//>`}
            <${Field} label="Customer">
              <select value=${customer} onChange=${(e) => setCustomer(e.currentTarget.value)}>
                <option value="">— none —</option>
                ${customers.map((c) => html`
                  <option key=${c.customer_id} value=${c.customer_id}>${c.customer_id}</option>`)}
              </select>
            <//>
            <${Field} label="Priority override">
              <select value=${priority} onChange=${(e) => setPriority(e.currentTarget.value)}>
                ${PRIORITIES.map((p) => html`
                  <option key=${p} value=${p}>${p || "— from account —"}</option>`)}
              </select>
            <//>

            <div class="xbtnrow">
              <button class="xbtn" disabled=${busy || !sat} onClick=${quote}>Get plan</button>
              <button class="xbtn ghost" disabled=${busy || !sat} onClick=${enqueue}>Queue it</button>
            </div>
            ${err && html`<div class="xerr">${err}</div>`}

            <div style=${{ marginTop: "16px", paddingTop: "12px", borderTop: "1px solid var(--p-line)" }}>
              <${Row} k="Tier" v=${account ? account.tier : "—"}
                      u=${account ? ` w${account.priority}` : ""} />
              <${Row} k="SLA" v=${account ? (account.sla_availability * 100).toFixed(1) : "—"} u="%" />
              <${Row} k="Quota" v=${account && account.quota_gbit !== null
                        ? g1(account.quota_gbit) : "unmetered"}
                      u=${account && account.quota_gbit !== null ? "Gbit" : ""} />
            </div>
          </div>

          <${PlanPanel} plan=${plan} booked=${booked} busy=${busy}
                        onAccept=${accept} onRelease=${release} />
        </div>
      </section>

      <!-- -------------------------------------------------------- 03 queue -->
      <section class="xsec">
        <${Head} idx="02" title="Multi-request arbitration"
                 note="dry run — nothing is booked until you commit" />
        <${QueuePanel} queue=${queue} onCommitted=${refreshLedger}
                       onRemove=${(id) => setQueue((q) => q.filter((r) => r.request_id !== id))}
                       onClear=${() => setQueue([])} />
      </section>

      <!-- ------------------------------------------------------- 04 ledger -->
      <section class="xsec">
        <${Head} idx="03" title="Commitment ledger"
                 note="capacity is consumed, not re-promised" />
        <${LedgerPanel} ledger=${ledger} />
      </section>
    </div>
  `;
}
