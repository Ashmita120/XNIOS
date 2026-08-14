/**
 * The planning console: a communication request in, a plan out.
 *
 * This is the only panel in the console that *asks the network for something*
 * rather than reading what it did. The form carries mission-level fields only —
 * satellite, volume, when it is needed, who is asking. Station, beam geometry,
 * frequency and exact timing are answers, so none of them appear as inputs.
 *
 * Quote and accept are deliberately two actions. A quote is free and books
 * nothing; accept is what consumes capacity and charges the account's quota, so
 * an operator can price a job before committing to it, and two operators racing
 * for the same pass both see the ledger.
 */

import { html } from "htm/preact";
import { useEffect, useState } from "preact/hooks";
import { api } from "./api.js";
import { clock } from "./format.js";
import { Badge, Button, Empty, Panel, Row, Select } from "./ui.js";

const INTENTS = ["asap", "by_deadline", "flexible"];
const PRIORITIES = ["", "low", "normal", "high", "critical"];

const TONE = {
  TRANSMIT_NOW: "ok",
  SCHEDULE: "ok",
  PARTIAL: "warn",
  REJECT: "crit",
};

/** A labelled free-text/number input, matching `Select`'s markup. */
function Field({ label, value, onChange, type = "text", placeholder, disabled }) {
  return html`
    <label class="field">
      <span class="label">${label}</span>
      <input
        type=${type}
        value=${value}
        placeholder=${placeholder}
        disabled=${disabled}
        onInput=${(e) => onChange(e.currentTarget.value)}
      />
    </label>
  `;
}

/** Admission read as a checklist, so a refusal says which gate closed. */
function Admission({ plan }) {
  if (!plan) return null;
  const q = plan.quota_remaining_gbit;
  const checks = [
    ["contact available", plan.schedule.length > 0],
    ["capacity reserved", plan.scheduled_gbit >= plan.data_volume_gbit - 1e-6],
    [
      "deadline satisfied",
      plan.meets_deadline === null || plan.meets_deadline === undefined
        ? null
        : plan.meets_deadline,
    ],
    ["no satellite conflict", true],
    ["quota available", q === null ? null : !plan.quota_limited],
  ];
  return html`
    <div class="checklist">
      ${checks.map(
        ([label, ok]) => html`
          <div key=${label} class="check">
            <span
              class="check-mark"
              style=${{
                color:
                  ok === null ? "var(--mute)" : ok ? "var(--st-ok)" : "var(--st-crit)",
              }}
              >${ok === null ? "–" : ok ? "✓" : "✕"}</span
            >
            <span>${label}${ok === null ? " (n/a)" : ""}</span>
          </div>
        `,
      )}
    </div>
  `;
}

function PlanCard({ plan, onAccept, onRelease, busy, booked }) {
  if (!plan) {
    return html`<${Empty}>submit a request to see a plan<//>`;
  }
  const w = plan.recommendation;
  const b = plan.beam_requirement;
  const n = plan.next_opportunity;

  return html`
    <div class="stack-5">
      <div class="plan-head">
        <${Badge} tone=${TONE[plan.decision] || "neutral"}>${plan.decision}<//>
        <span class="label">${plan.request_id} · ${plan.reason_code}</span>
      </div>

      <div>
        <${Row} k="Satellite" v=${plan.satellite_id} />
        ${plan.customer_id &&
        html`<${Row} k="Customer" v=${`${plan.customer_id}${plan.tier ? ` (${plan.tier})` : ""}`} />`}
        <${Row}
          k="Requested"
          v=${`${plan.data_volume_gbit.toFixed(1)} Gbit`}
        />
        <${Row}
          k="Scheduled"
          v=${`${plan.scheduled_gbit.toFixed(1)} Gbit`}
          accent=${plan.shortfall_gbit > 1e-6 ? "var(--st-warn)" : undefined}
        />
        ${plan.shortfall_gbit > 1e-6 &&
        html`<${Row}
          k="Shortfall"
          v=${`${plan.shortfall_gbit.toFixed(1)} Gbit`}
          accent="var(--st-warn)"
        />`}
        ${plan.quota_remaining_gbit !== null &&
        plan.quota_remaining_gbit !== undefined &&
        html`<${Row} k="Quota left" v=${`${plan.quota_remaining_gbit.toFixed(1)} Gbit`} />`}
      </div>

      ${w &&
      html`<div>
        <div class="label" style=${{ marginTop: "4px" }}>Recommendation</div>
        <${Row} k="Station" v=${w.station} />
        <${Row} k="Start" v=${`T+${clock(w.t_start)}`} />
        <${Row} k="Window" v=${`${w.duration_s.toFixed(0)} s`} />
        <${Row} k="Contacts" v=${plan.schedule.length} />
        ${plan.completes_at_s !== null &&
        html`<${Row} k="Completes" v=${`T+${clock(plan.completes_at_s)}`} />`}
      </div>`}

      ${b &&
      html`<div>
        <div class="label" style=${{ marginTop: "4px" }}>Beam requirement</div>
        <${Row} k="Beams" v=${b.count} />
        <${Row}
          k="Pointing"
          v=${`az ${b.az_deg.toFixed(1)}° · el ${b.elev_deg.toFixed(1)}°`}
        />
        <${Row}
          k="Scan angle"
          v=${`${b.scan_angle_deg.toFixed(1)}°`}
          accent=${b.within_scan_envelope ? undefined : "var(--st-crit)"}
        />
        <${Row} k="Beamwidth" v=${`${b.beamwidth_deg.toFixed(2)}°`} />
        <${Row} k="Frequency" v=${`${plan.frequency.band} · ${plan.frequency.channel}`} />
      </div>`}

      ${n &&
      html`<div>
        <div class="label" style=${{ marginTop: "4px" }}>Next opportunity</div>
        <${Row} k="Station" v=${n.station} />
        <${Row} k="In" v=${clock(n.in_s)} />
        <${Row} k="Capacity" v=${`${n.deliverable_gbit.toFixed(1)} Gbit`} />
      </div>`}

      <div>
        <div class="label">Admission</div>
        <${Admission} plan=${plan} />
      </div>

      ${plan.explanation.length > 0 &&
      html`<div>
        <div class="label">Why</div>
        <ul class="why">
          ${plan.explanation.map((e, i) => html`<li key=${i}>${e}</li>`)}
        </ul>
      </div>`}

      <div class="btn-row">
        <${Button}
          solid
          disabled=${busy || booked || !plan.admitted}
          onClick=${onAccept}
        >
          ${booked ? "Booked" : "Accept plan"}
        <//>
        <${Button} disabled=${busy || !booked} onClick=${onRelease}>Release<//>
      </div>
    </div>
  `;
}

export function PlanningConsole() {
  const [net, setNet] = useState(null);
  const [sat, setSat] = useState("");
  const [volume, setVolume] = useState("18.4");
  const [intent, setIntent] = useState("asap");
  const [deadline, setDeadline] = useState("3600");
  const [priority, setPriority] = useState("");
  const [customer, setCustomer] = useState("");
  const [customers, setCustomers] = useState([]);
  const [plan, setPlan] = useState(null);
  const [booked, setBooked] = useState(false);
  const [ledger, setLedger] = useState(null);
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

  async function refreshLedger() {
    try {
      setLedger(await api.plan.ledger());
    } catch {
      /* the ledger is a read-only view; a stale one is not worth an error */
    }
  }

  async function quote() {
    setBusy(true);
    setErr(null);
    setBooked(false);
    try {
      const body = {
        satellite_id: sat,
        data_volume_gbit: Number(volume),
        timing: intent,
        t_now: 0,
      };
      if (intent === "by_deadline") body.deadline_s = Number(deadline);
      if (priority) body.priority = priority;
      if (customer) body.customer_id = customer;
      setPlan(await api.plan.quote(body));
    } catch (e) {
      setErr(String(e));
      setPlan(null);
    } finally {
      setBusy(false);
    }
  }

  async function accept() {
    setBusy(true);
    setErr(null);
    try {
      await api.plan.accept(plan.request_id);
      setBooked(true);
      await refreshLedger();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function release() {
    setBusy(true);
    try {
      await api.plan.release(plan.request_id);
      setBooked(false);
      await refreshLedger();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (err && !net) {
    return html`<${Empty}>
      Planning API unreachable — start it with
      <span style=${{ marginLeft: "4px", color: "var(--dim)" }}>python run_api.py</span>
    <//>`;
  }
  if (!net) return html`<${Empty}>loading network<//>`;

  return html`
    <div class="grid-decision">
      <${Panel} title="Communication request">
        <div class="stack-5">
          <${Select} label="Satellite" value=${sat} options=${net.satellites} onChange=${setSat} />
          <${Field}
            label="Data volume (Gbit)"
            type="number"
            value=${volume}
            onChange=${setVolume}
          />
          <${Select} label="Timing" value=${intent} options=${INTENTS} onChange=${setIntent} />
          ${intent === "by_deadline" &&
          html`<${Field}
            label="Deadline (s from now)"
            type="number"
            value=${deadline}
            onChange=${setDeadline}
          />`}
          <${Select}
            label="Priority (blank = from account)"
            value=${priority}
            options=${PRIORITIES}
            onChange=${setPriority}
          />
          <${Select}
            label="Customer"
            value=${customer}
            options=${["", ...customers.map((c) => c.customer_id)]}
            onChange=${setCustomer}
          />

          <div class="btn-row">
            <${Button} solid disabled=${busy || !sat} onClick=${quote}>Get plan<//>
          </div>

          ${err && html`<div class="err">${err}</div>`}

          <div class="meta-row" style=${{ marginTop: "4px" }}>
            <span>${net.preset}</span>
            <span>${net.satellites.length} sats · ${net.stations.length} stations</span>
            <span>${net.contacts_precomputed} contacts</span>
            <span>${net.horizon_s / 3600}h horizon</span>
          </div>
        </div>
      <//>

      <${Panel} title="Communication plan">
        <${PlanCard}
          plan=${plan}
          busy=${busy}
          booked=${booked}
          onAccept=${accept}
          onRelease=${release}
        />
      <//>
    </div>

    <div class="mt-6">
      <${Panel} title="Commitment ledger — what the network has promised">
        ${!ledger || !ledger.commitments.length
          ? html`<${Empty}>nothing booked<//>`
          : html`<div class="link-scroll">
              <table class="link-table">
                <thead>
                  <tr>
                    <th>request</th>
                    <th>satellite</th>
                    <th>station</th>
                    <th>start</th>
                    <th>end</th>
                    <th class="num">Gbit</th>
                  </tr>
                </thead>
                <tbody>
                  ${ledger.commitments.map(
                    (c, i) => html`<tr key=${i}>
                      <td>${c.request_id}</td>
                      <td>${c.satellite_id}</td>
                      <td>${c.station}</td>
                      <td>T+${clock(c.t_start)}</td>
                      <td>T+${clock(c.t_end)}</td>
                      <td class="num">${c.gbit.toFixed(2)}</td>
                    </tr>`,
                  )}
                </tbody>
              </table>
              <div class="meta-row" style=${{ marginTop: "12px" }}>
                <span>Total <span class="on">${ledger.total_gbit.toFixed(1)} Gbit</span></span>
                ${Object.entries(ledger.by_station).map(
                  ([g, v]) => html`<span key=${g}>${g} <span class="on">${v.toFixed(1)}</span></span>`,
                )}
              </div>
            </div>`}
      <//>
    </div>
  `;
}
