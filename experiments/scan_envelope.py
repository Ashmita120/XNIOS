"""Scan envelope vs delivered data — does the advantage survive beam broadening?

The prior result was that widening a phased array's steering envelope from
+/-60 to +/-80 deg is worth +21% to +59% delivered data, because 74% of
geometric contact time sits below the 30 deg elevation floor that a 60 deg
envelope imposes. That was measured with a FIXED beam width, which is the part
that needed checking: steering off boresight foreshortens the aperture, so the
beam should also broaden, overlap its neighbours more, and raise co-channel
interference. If broadening is strong enough, the extra contact time buys
nothing.

This is a two-model A/B over one physics change.

    Model A   beam width fixed at beamwidth_deg          (the committed baseline)
    Model B   beam width = beamwidth_deg / cos(scan)     (projected aperture)

Nothing else differs. The scheduler is pinned to fcfs/strongest throughout,
because the policy benchmark showed every scheduler lands within 0.4% of the
MILP oracle here — varying it would only add noise to the causal chain under
test:

    scan envelope -> beam width -> angular overlap -> co-channel interference
                  -> SINR -> achievable rate -> delivered data

What Model B is NOT: a radiation-pattern model. Grating lobes, element patterns
and cross-polarisation are absent because element spacing is not represented in
the station configuration. Results at large scan angles are conditional on this
first-order aperture model and must not be read as hardware-feasible operation
beyond the grating-lobe limit. The cosine is floored at link.COS_SCAN_FLOOR
(0.05, scan ~87.1 deg) — the same floor the scan-loss term already uses — so
90 deg is a bounded edge case rather than a numerical singularity.

`usable contact-seconds` is analytical: summed over every (satellite, station)
pair, the time the link budget is above the lock threshold. It depends only on
the envelope, not on the beam model, so it is reported once per angle.

Run:
    python experiments/scan_envelope.py
    python experiments/scan_envelope.py --smoke
"""

from __future__ import annotations

import argparse
import copy
import csv
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase_benchmark import build_config, SCENARIO_PROFILES

from xnios import forecast as fc
from xnios.allocators import make_allocator, make_power_allocator, make_freq_allocator
from xnios.config import scenario_from_config, sim_config_from_config
from xnios.experiment import make_scheduler
from xnios.simulator import Simulator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(ROOT, "experiments", "results")

SCHEDULER = "fcfs/strongest"        # pinned: policy is not the variable under test
ANGLES = [60, 70, 75, 80, 85, 90]
SCENARIOS = [("india8", "congested"), ("india8", "baseline"), ("global6", "congested")]


def _profile(name: str) -> dict:
    return [p for p in SCENARIO_PROFILES if p["name"] == name][0]


def _variant(cfg: dict, max_scan: float, broadening: bool) -> dict:
    out = copy.deepcopy(cfg)
    for st in out["stations"]:
        st["max_scan_deg"] = max_scan
        st["beam_broadening"] = broadening
    return out


def _run(cfg: dict) -> dict:
    scn = scenario_from_config(cfg)
    sim_cfg = sim_config_from_config(cfg)
    res = Simulator(scn, make_scheduler(SCHEDULER), sim_cfg,
                    allocator=make_allocator("equal"),
                    power_allocator=make_power_allocator("adaptive"),
                    freq_allocator=make_freq_allocator("coloring")).run()
    return res.summary


def usable_contact_seconds(cfg: dict, step_s: float = 5.0) -> float:
    """Analytical link-seconds above the lock threshold, over every pair.

    Pure geometry plus the link budget — independent of scheduling, of how many
    beams exist, and of the beam-width model. This is the raw opportunity the
    envelope makes available.
    """
    scn = scenario_from_config(cfg)
    sim_cfg = sim_config_from_config(cfg)
    t = np.arange(0.0, sim_cfg.duration_s, step_s)
    total = 0.0
    for s in scn.satellites:
        for g in scn.stations:
            elev, rng = fc.elevation_series(s, g, t)
            rate = fc.rate_series(s, g, elev, rng,
                                  rain_zenith_db=scn.weather.fade_db(g.id, 0.0))
            usable = (elev >= g.elevation_mask_deg) & (rate > 0)
            total += float(usable.sum()) * step_s
    return total


def sweep(smoke: bool) -> list:
    scenarios = SCENARIOS[:1] if smoke else SCENARIOS
    angles = [60, 80] if smoke else ANGLES
    rows = []

    for net, pname in scenarios:
        base_cfg, n_sats = build_config(net, _profile(pname), 0)
        print()
        print("=" * 104)
        print(f"{net} / {pname} / {n_sats} satellites / scheduler {SCHEDULER}")
        print("=" * 104)
        hdr = (f"{'scan':>5s} {'model':>6s} {'Gbit':>8s} {'vs 60A':>8s} {'compl':>7s} "
               f"{'sla':>7s} {'contact ks':>11s} {'SINR dB':>8s} {'INR':>7s} "
               f"{'capped':>7s} {'outage':>7s}")
        print(hdr)
        print("-" * len(hdr))

        ref = None
        for ang in angles:
            contact_ks = usable_contact_seconds(_variant(base_cfg, ang, False)) / 1e3
            for model, broadening in (("A", False), ("B", True)):
                s = _run(_variant(base_cfg, ang, broadening))
                d = s["delivered_gbit"]
                if ref is None:
                    ref = d                       # 60 deg, Model A
                print(f"{ang:5d} {model:>6s} {d:8.1f} {(d/ref-1)*100:+7.1f}% "
                      f"{s['completion_rate']*100:6.1f}% {s['sla_compliance']*100:6.1f}% "
                      f"{contact_ks:11.1f} {s['mean_sinr_db']:8.2f} {s['mean_inr']:7.3f} "
                      f"{s['modcod_capped_frac']*100:6.1f}% {s['link_outage_frac']*100:6.1f}%")
                rows.append(dict(
                    network=net, profile=pname, n_sats=n_sats, scheduler=SCHEDULER,
                    max_scan_deg=ang, model=model, beam_broadening=broadening,
                    delivered_gbit=d, vs_60A_pct=(d / ref - 1) * 100,
                    completion_rate=s["completion_rate"],
                    sla_compliance=s["sla_compliance"],
                    usable_contact_s=contact_ks * 1e3,
                    mean_sinr_db=s["mean_sinr_db"], mean_inr=s["mean_inr"],
                    modcod_capped_frac=s["modcod_capped_frac"],
                    link_outage_frac=s["link_outage_frac"],
                    beam_utilization=s["beam_utilization"],
                    link_samples=s["link_samples"]))
            print()

        _knee(rows, net, pname)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, "scan_envelope.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n  -> {path}")
    return rows


def _knee(rows: list, net: str, pname: str) -> None:
    """Where does each model peak, and does broadening move the optimum?"""
    sub = [r for r in rows if r["network"] == net and r["profile"] == pname]
    for model in ("A", "B"):
        rs = [r for r in sub if r["model"] == model]
        if not rs:
            continue
        best = max(rs, key=lambda r: r["delivered_gbit"])
        base = min(rs, key=lambda r: r["max_scan_deg"])
        gain = (best["delivered_gbit"] / base["delivered_gbit"] - 1) * 100
        print(f"  Model {model}: optimum at {best['max_scan_deg']} deg "
              f"-> {best['delivered_gbit']:.1f} Gbit "
              f"({gain:+.1f}% vs {base['max_scan_deg']} deg)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    sweep(args.smoke)
    print(f"Done in {time.time() - t0:.0f} s.")


if __name__ == "__main__":
    main()
