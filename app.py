"""X-NioS digital twin — Streamlit UI.

A thin front-end over the existing engine: the sidebar widgets build the SAME config
dict that `configs/example.json` expresses, hand it to `scenario_from_config()` +
`Simulator`, and render the resulting KPI vector as a table + charts. No simulator
code is touched here.

Run:  streamlit run app.py
"""

from __future__ import annotations

import itertools
import json
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xnios import orbit as orb
from xnios.config import scenario_from_config
from xnios.experiment import (POLICY_CHOICES, ALLOCATOR_CHOICES, POWER_ALLOCATOR_CHOICES,
                              FREQ_ALLOCATOR_CHOICES, run_policies, run_with_oracle)

# (summary key, column label, kind) — kind drives formatting
KPIS = [
    ("delivered_gbit", "Delivered (Gb)", "num"),
    ("completion_rate", "Completion", "pct"),
    ("sla_compliance", "SLA", "pct"),
    ("drop_rate", "Dropped", "pct"),
    ("mean_wait_s", "Wait (s)", "sec"),
    ("beam_utilization", "Beam util", "pct"),
    ("fairness", "Fairness", "num2"),
    ("energy_kj", "Energy (kJ)", "num0"),
    ("gb_per_kj", "Gb/kJ", "num2"),
    ("sessions_interrupted", "Interrupted", "num0"),
    ("mean_recovery_s", "Recovery (s)", "num0"),
    ("proactive_handovers", "Handovers", "num0"),
    ("mean_decision_ms", "Decision (ms)", "ms"),
]

WEATHER_OPTS = ["clear", "cloudy", "rain", "storm"]

DEFAULT_STATIONS = pd.DataFrame([
    {"id": "GS-0", "dlat": 0.0,  "dlon": 0.0,  "num_beams": 4, "g_over_t_dbk": 19.0, "weather": "rain",   "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "GS-1", "dlat": 3.0,  "dlon": -3.0, "num_beams": 4, "g_over_t_dbk": 25.0, "weather": "clear",  "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "GS-2", "dlat": -3.0, "dlon": 3.0,  "num_beams": 4, "g_over_t_dbk": 24.0, "weather": "clear",  "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "GS-3", "dlat": 2.0,  "dlon": 5.0,  "num_beams": 4, "g_over_t_dbk": 18.0, "weather": "cloudy", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
])

# real Indian ground-station sites (editable lat/lon)
INDIA_STATIONS = pd.DataFrame([
    {"id": "Delhi",              "lat": 28.61, "lon": 77.21, "num_beams": 4, "g_over_t_dbk": 24.0, "weather": "clear", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "Bengaluru-ISTRAC",   "lat": 13.03, "lon": 77.51, "num_beams": 4, "g_over_t_dbk": 27.0, "weather": "clear", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "Ahmedabad-SAC",      "lat": 23.03, "lon": 72.58, "num_beams": 4, "g_over_t_dbk": 24.0, "weather": "clear", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "Hyderabad-NRSC",     "lat": 17.03, "lon": 78.18, "num_beams": 4, "g_over_t_dbk": 26.0, "weather": "clear", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "Guwahati",           "lat": 26.14, "lon": 91.74, "num_beams": 4, "g_over_t_dbk": 22.0, "weather": "clear", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "Thiruvananthapuram", "lat": 8.52,  "lon": 76.94, "num_beams": 4, "g_over_t_dbk": 23.0, "weather": "clear", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "Lucknow-ISTRAC",     "lat": 26.85, "lon": 80.95, "num_beams": 4, "g_over_t_dbk": 22.0, "weather": "clear", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
    {"id": "Port-Blair",         "lat": 11.62, "lon": 92.73, "num_beams": 4, "g_over_t_dbk": 22.0, "weather": "clear", "bw_mhz": 500, "phased": True, "beamwidth": 3.0, "channels": 4, "dual_pol": True},
])


def build_config(ui: dict) -> dict:
    """Assemble the engine config dict from the sidebar widget values."""
    india = ui.get("preset", "").startswith("India")
    if india:                                          # constellation that overflies India
        planes = [{"inc": 53, "raan": 95, "altitude_km": ui["alt"]},
                  {"inc": 53, "raan": 105, "altitude_km": ui["alt"]},
                  {"inc": 97.6, "raan": 100, "altitude_km": ui["alt"]}]
        spread = 40.0
    else:
        raans = [round(i * ui["raan_spacing"], 1) for i in range(ui["n_planes"])]
        planes = [{"inc": ui["inc"], "raan": r, "altitude_km": ui["alt"]} for r in raans]
        spread = ui["spread"]

    stations = []
    for _, row in ui["stations"].iterrows():
        s = {
            "id": str(row["id"]),
            "num_beams": int(row["num_beams"]),
            "g_over_t_dbk": float(row["g_over_t_dbk"]),
            "weather": str(row["weather"]),
            "bandwidth_mhz": float(row["bw_mhz"]),
            "phased_array": bool(row["phased"]),
            "beamwidth_deg": float(row["beamwidth"]),
            "n_channels": int(row["channels"]),
            "dual_pol": bool(row["dual_pol"]),
            "max_scan_deg": 60.0,                      # +-60 deg electronic steering (elev >= 30)
            "setup_time_s": 0.05,                      # phased-array beam switching (~50 ms)
        }
        if india:
            s["lat"], s["lon"] = float(row["lat"]), float(row["lon"])   # real coordinates
        else:
            s["place_under"] = {"plane": 0, "dlat": float(row["dlat"]), "dlon": float(row["dlon"])}
        stations.append(s)

    cfg = {
        "name": "streamlit experiment",
        "t_mid": ui["duration_s"] / 2.0,
        "sim": {"duration_s": ui["duration_s"], "dt_s": ui["dt_s"],
                "decision_interval_s": ui["dt_s"], "handover": ui.get("handover", False)},
        "stations": stations,
        "satellites": {
            "mode": "generate",
            "count": ui["count"],
            "planes": planes,
            "arg_lat_spread_deg": spread,
            "freq_ghz": ui["freq_ghz"],
            "bandwidth_mhz": ui["bw_mhz"],
            "tx_power_w": ui["power_w"],
            "backlog_gbit": {"classes": [ui["d_small"], ui["d_med"], ui["d_huge"]],
                             "weights": [0.35, 0.4, 0.25]},
            "tiers": ["research", "commercial", "commercial", "military", "emergency"],
            "tier_deadline_s": {"emergency": 90, "military": 180, "commercial": 300, "research": 550},
        },
    }
    mode = ui.get("weather_mode", "Table (static)")
    if mode.startswith("Dynamic"):
        cfg["weather"] = {"provider": "dynamic"}
    elif mode.startswith("Live"):
        cfg["weather"] = {"provider": "openmeteo"}     # free, no key
    if ui.get("inject_failures"):
        d = ui["duration_s"]
        cfg["dynamics"] = {"random": {"station_mtbf_s": d / 2.0, "station_mttr_s": d / 8.0}}
    return cfg


def fmt(val, kind):
    if kind == "pct":
        return f"{val*100:.0f}%"
    if kind == "sec":
        return f"{val:.0f}"
    if kind == "ms":
        return f"{val:.3f}"
    if kind == "num2":
        return f"{val:.2f}"
    if kind == "num0":
        return f"{val:.0f}"
    return f"{val:.1f}"


def fmt_wait(sec: float) -> str:
    if sec < 3600:
        return f"+{sec/60:.0f} min"
    if sec < 86400:
        return f"+{sec/3600:.1f} h"
    return f"+{sec/86400:.1f} d"


@st.cache_data(show_spinner=False)
def next_contacts(config_json: str, sat_ids: tuple) -> dict:
    """For each satellite id, the next station that will see it after the sim ends
    (the store-and-forward resume opportunity). Cached on (config, sat set)."""
    cfg = json.loads(config_json)
    scn = scenario_from_config({**cfg, "seed": 0})
    sat_by_id = {s.id: s for s in scn.satellites}
    t_end = cfg["sim"]["duration_s"]
    out = {}
    for sid in sat_ids:
        sat = sat_by_id.get(sid)
        out[sid] = orb.next_contact(sat, scn.stations, t_end) if sat else None
    return out


# --------------------------------------------------------------------------- #
st.set_page_config(page_title="X-NioS Digital Twin", page_icon="🛰️", layout="wide")
st.title("🛰️ X-NioS Digital Twin — Experiment Runner")
st.caption("Describe a satellite/ground-station world, pick scheduling policies, and "
           "compare how they move data, meet SLAs, and use the hardware.")

with st.sidebar:
    st.header("① The world")

    preset = st.radio("Ground-station layout",
                      ["Synthetic (under the pass)", "India — 8 real stations"],
                      help="Synthetic auto-places stations under the pass to force contention. "
                           "India uses real lat/lon + a constellation that overflies India — "
                           "pair it with live weather.")
    india_mode = preset.startswith("India")

    with st.expander("Satellites", expanded=True):
        count = st.slider("Number of satellites", 1, 200, 30)
        n_planes = st.slider("Orbital planes", 1, 4, 2)
        raan_spacing = st.slider("Plane spacing (° RAAN)", 0.0, 30.0, 6.0, 1.0)
        inc = st.slider("Inclination (°)", 0.0, 98.0, 53.0, 1.0)
        alt = st.slider("Altitude (km)", 400, 1200, 600, 50)
        spread = st.slider("Pass clustering (± arg-lat°)", 2.0, 90.0, 10.0, 1.0,
                           help="Small = satellites bunch up over the stations → more contention.")

    with st.expander("RF link"):
        freq_ghz = st.number_input("Frequency (GHz)", 1.0, 40.0, 8.2, 0.1)
        bw_mhz = st.number_input("Bandwidth (MHz)", 1.0, 1000.0, 50.0, 1.0)
        power_w = st.number_input("Tx power (W)", 0.5, 100.0, 5.0, 0.5)

    with st.expander("Data demand per satellite (Gbit)"):
        d_small = st.number_input("Small job", 0.5, 50.0, 2.0, 0.5)
        d_med = st.number_input("Medium job", 1.0, 100.0, 20.0, 1.0)
        d_huge = st.number_input("Huge job", 5.0, 500.0, 80.0, 5.0)
        st.caption("Mix is 35% small / 40% medium / 25% huge.")

    with st.expander("Ground stations", expanded=True):
        common_cols = {
            "num_beams": st.column_config.NumberColumn(min_value=1, max_value=16, step=1),
            "g_over_t_dbk": st.column_config.NumberColumn("G/T (dB/K)", min_value=5.0,
                                                          max_value=40.0, step=1.0),
            "weather": st.column_config.SelectboxColumn(options=WEATHER_OPTS),
            "bw_mhz": st.column_config.NumberColumn("BW pool (MHz)", min_value=10,
                                                    max_value=2000, step=10),
            "phased": st.column_config.CheckboxColumn("Phased array"),
            "beamwidth": st.column_config.NumberColumn("Beam° ", min_value=0.5,
                                                       max_value=10.0, step=0.5),
            "channels": st.column_config.NumberColumn("Channels", min_value=1,
                                                      max_value=16, step=1),
            "dual_pol": st.column_config.CheckboxColumn("Dual pol"),
        }
        if india_mode:
            st.caption("Real Indian sites — edit **lat/lon** directly. Add/remove rows freely.")
            stations_df = st.data_editor(
                INDIA_STATIONS, num_rows="dynamic", hide_index=True, use_container_width=True,
                column_config={**common_cols,
                               "lat": st.column_config.NumberColumn("Lat°", step=0.01),
                               "lon": st.column_config.NumberColumn("Lon°", step=0.01)})
        else:
            st.caption("Stations placed under the constellation (Δlat/Δlon from the pass).")
            stations_df = st.data_editor(
                DEFAULT_STATIONS, num_rows="dynamic", hide_index=True, use_container_width=True,
                column_config={**common_cols,
                               "dlat": st.column_config.NumberColumn("Δlat°", step=1.0),
                               "dlon": st.column_config.NumberColumn("Δlon°", step=1.0)})
        st.caption("**BW pool** = station bandwidth shared across beams. **Phased array** adds "
                   "scan loss + interference; give several **channels** (+ **dual pol**) so the "
                   "frequency allocator can separate close beams.")

    with st.expander("Weather & realism"):
        weather_mode = st.selectbox(
            "Weather source",
            ["Table (static)", "Dynamic (changes over run)", "Live (Open-Meteo)"],
            help="Static = the table's weather. Dynamic = a Markov chain so conditions "
                 "evolve mid-run. Live = real current conditions per lat/lon (free, no key).")
        inject_failures = st.checkbox(
            "Random station failures", value=False,
            help="Stations fail and recover during the run (Poisson). Watch the "
                 "Interrupted / Recovery columns — traffic self-heals onto other stations.")

    st.header("② Simulation")
    duration_min = st.slider("Duration (minutes)", 5, 120, 20, 5)
    dt_s = st.select_slider("Time step (s)", options=[1, 2, 5, 10, 30], value=5)
    handover = st.checkbox("Proactive handover (switch station before LOS)", value=False,
                           help="Move a satellite to another visible station just before its "
                                "pass ends — a make-before-break switch, no interruption.")

    st.header("③ Policies to compare")
    policies = st.multiselect("Schedulers (who / where)", POLICY_CHOICES,
                              default=["fcfs/strongest", "edf/strongest",
                                       "sjf/strongest", "priority/nearest"])
    allocators = st.multiselect("Bandwidth allocators (how much)", ALLOCATOR_CHOICES,
                                default=["equal"],
                                help="How each station's bandwidth pool is split across its "
                                     "simultaneous beams. Pick several to compare them.")
    power_allocs = st.multiselect("Power allocators (energy)", POWER_ALLOCATOR_CHOICES,
                                  default=["fixed"],
                                  help="How much transmit power each link uses — trades "
                                       "throughput vs energy (see the Energy / Gb-per-kJ columns).")
    freq_allocs = st.multiselect("Frequency allocators (phased array)", FREQ_ALLOCATOR_CHOICES,
                                 default=["coloring"],
                                 help="How channels are assigned to a phased array's beams to "
                                      "avoid interference. Only matters for phased-array stations "
                                      "with >1 channel. 'same' = no reuse; 'coloring' = graph reuse.")
    n_seeds = st.slider("Seeds to average", 1, 20, 3,
                        help="More seeds = smoother averages, longer runtime.")
    compare_optimal = st.checkbox(
        "Compare against optimal (LP oracle)", value=False,
        help="Computes the theoretical max throughput (ceiling) and each policy's "
             "% of optimal. Adds a little runtime.")

    run = st.button("▶ Run experiment", type="primary", use_container_width=True)

# world summary
ui = dict(count=count, n_planes=n_planes, raan_spacing=raan_spacing, inc=inc, alt=alt,
          spread=spread, freq_ghz=freq_ghz, bw_mhz=bw_mhz, power_w=power_w,
          d_small=d_small, d_med=d_med, d_huge=d_huge, stations=stations_df,
          duration_s=duration_min * 60.0, dt_s=float(dt_s),
          weather_mode=weather_mode, inject_failures=inject_failures,
          handover=handover, preset=preset)
config = build_config(ui)
total_beams = sum(int(r["num_beams"]) for _, r in stations_df.iterrows())

c1, c2, c3 = st.columns(3)
c1.metric("Satellites", count)
c2.metric("Stations", len(stations_df))
c3.metric("Total beams (capacity)", total_beams)

if run:
    if not policies:
        st.warning("Pick at least one scheduler to compare.")
        st.stop()
    if not allocators or not power_allocs or not freq_allocs:
        st.warning("Pick at least one of each allocator (bandwidth / power / frequency).")
        st.stop()
    if len(stations_df) == 0:
        st.warning("Add at least one ground station.")
        st.stop()
    bar = st.progress(0.0, text="Starting…")
    multi_bw, multi_pw, multi_fq = len(allocators) > 1, len(power_allocs) > 1, len(freq_allocs) > 1
    all_rows, per_sat, oracle_gbit = [], {}, None
    for a, p, f in itertools.product(allocators, power_allocs, freq_allocs):
        suffix = ((f" + {a}" if multi_bw else "") + (f" + {p}" if multi_pw else "")
                  + (f" + {f}" if multi_fq else ""))
        cb = lambda frac, name, seed, a=a, p=p, f=f: bar.progress(
            frac, text=f"[{a}/{p}/{f}] {name} (seed {seed})…")
        if compare_optimal:
            rws, oracle_gbit, ps = run_with_oracle(
                config, policies, n_seeds, allocator=a, power_allocator=p,
                freq_allocator=f, progress_cb=cb)
        else:
            rws, ps = run_policies(
                config, policies, n_seeds, allocator=a, power_allocator=p,
                freq_allocator=f, progress_cb=cb)
        for r in rws:
            all_rows.append({**r, "policy": r["policy"] + suffix})
        for k, v in ps.items():
            per_sat[k + suffix] = v
    bar.empty()
    st.session_state["df"] = pd.DataFrame(all_rows)
    st.session_state["per_sat"] = per_sat
    st.session_state["run_config"] = config     # config actually used for this run
    st.session_state["oracle_gbit"] = oracle_gbit

if "df" in st.session_state:
    df = st.session_state["df"]

    st.subheader("Results — mean KPIs")

    oracle_gbit = st.session_state.get("oracle_gbit")
    if oracle_gbit:
        st.info(f"**Optimal-throughput ceiling (LP oracle): {oracle_gbit:.0f} Gb.** "
                f"The best any scheduler could deliver with perfect foresight — the "
                f"**% of optimal** column shows how close each policy gets.")

    # best-of callouts — one per line, full policy label in code font (never truncates)
    st.markdown("**Winners by objective**")
    tp = df.loc[df["delivered_gbit"].idxmax()]
    st.markdown(f"- **Best throughput** — {tp['delivered_gbit']:.0f} Gb — `{tp['policy']}`")
    sla = df.loc[df["sla_compliance"].idxmax()]
    st.markdown(f"- **Best SLA** — {sla['sla_compliance']*100:.0f}% — `{sla['policy']}`")
    lw = df.loc[df["mean_wait_s"].idxmin()]
    st.markdown(f"- **Lowest wait** — {lw['mean_wait_s']:.0f} s — `{lw['policy']}`")
    eff = df.loc[df["gb_per_kj"].idxmax()]
    st.markdown(f"- **Most energy-efficient** — {eff['gb_per_kj']:.1f} Gb/kJ — `{eff['policy']}`")
    if "pct_optimal" in df.columns:
        op = df.loc[df["pct_optimal"].idxmax()]
        st.markdown(f"- **Closest to optimal** — {op['pct_optimal']*100:.0f}% — `{op['policy']}`")

    # formatted table
    disp = pd.DataFrame({"policy": df["policy"]})
    for k, lbl, kind in KPIS:
        disp[lbl] = df[k].map(lambda v, kind=kind: fmt(v, kind))
    if "pct_optimal" in df.columns:
        disp["% of optimal"] = df["pct_optimal"].map(lambda v: f"{v*100:.0f}%")
    st.dataframe(disp, hide_index=True, use_container_width=True)

    # bar charts for the headline KPIs
    st.subheader("Compare")
    g1, g2 = st.columns(2)
    idx = df.set_index("policy")
    g1.markdown("**Delivered (Gb)**"); g1.bar_chart(idx[["delivered_gbit"]])
    g2.markdown("**SLA compliance**"); g2.bar_chart(idx[["sla_compliance"]])
    g3, g4 = st.columns(2)
    g3.markdown("**Completion rate**"); g3.bar_chart(idx[["completion_rate"]])
    g4.markdown("**Mean wait (s)**"); g4.bar_chart(idx[["mean_wait_s"]])

    # Pareto view
    st.subheader("Trade-off (Pareto): throughput vs SLA")
    st.caption("Up-and-to-the-right is better. No single point usually dominates — "
               "that's the multi-objective trade-off.")
    pareto = df[["policy", "delivered_gbit", "sla_compliance"]].copy()
    pareto["SLA %"] = pareto["sla_compliance"] * 100
    st.scatter_chart(pareto, x="delivered_gbit", y="SLA %", color="policy")

    # per-satellite detail
    with st.expander("Per-satellite detail (first seed)"):
        pol = st.selectbox("Policy", list(st.session_state["per_sat"].keys()))
        ps = st.session_state["per_sat"][pol]
        st.caption("For satellites that didn't finish, **next contact** is the next time "
                   "any station will see them again (the store-and-forward resume "
                   "opportunity). Which station actually resumes the downlink is then the "
                   "scheduler's choice.")

        # look ahead only for the unfinished satellites of the selected policy
        run_cfg = st.session_state.get("run_config")
        incomplete = tuple(s for s, d in ps.items() if d["completed_t"] is None)
        nc = {}
        if incomplete and run_cfg is not None:
            nc = next_contacts(json.dumps(run_cfg, sort_keys=True), incomplete)

        rows = []
        for s, d in ps.items():
            done = d["completed_t"] is not None
            if done:
                nxt = "✓ done"
            elif run_cfg is None:
                nxt = "— (re-run)"          # stale session predating this feature
            else:
                info = nc.get(s)
                nxt = f"{info['station']} ({fmt_wait(info['wait_s'])})" if info else "none within 48 h"
            rows.append({
                "satellite": s,
                "demand (Gb)": round(d["demand_gbit"], 2),
                "delivered (Gb)": round(d["delivered_gbit"], 2),
                "remaining (Gb)": round(max(0.0, d["demand_gbit"] - d["delivered_gbit"]), 2),
                "wait (s)": round(d["wait_s"], 0),
                "served on": d["served_on"],
                "completed": done,
                "next contact": nxt,
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
else:
    st.info("Set up the world in the sidebar and click **Run experiment**.")
