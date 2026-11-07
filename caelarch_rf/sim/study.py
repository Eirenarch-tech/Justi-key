"""Sprint 1e study: blind batch -> freeze -> score -> controls -> report. OFFLINE.

    python -m caelarch_rf.sim.study --out DIR [--n 102] [--seed-hex HEX] [--config C6_pilot_analog]

Matched measurement budget: every detector (reference bracket, sampled pilot,
analog monitor) and every guard configuration is evaluated on the SAME
captures: identical disturbances, acquisition duration, reference schedule
and noise. Differences are therefore paired by construction; the cost of the
continuous monitors is accounted for separately (pilot, extra hardware), and
the cost of the reference is its dwell time.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import replace

from caelarch_rf.gate04.evaluate import capture_record, freeze
from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.monitors import MonitorParams
from caelarch_rf.gate04.observability import CONFIGS, leakage_breaks

from . import generator as G
from .batch import cases_for, generate_blind_batch
from .disturbances import Disturbance
from .leakage import pilot_into_scene, scene_into_analog, scene_into_pilot
from .matched import emittable_circuits, matched_pair
from .scorer import MatchPolicy, score

HARDWARE = {
    "reference_bracket": ["RF selector + reference path (already required)", "independent R witness"],
    "sampled_digital": ["pilot source", "injection coupler at P3", "pilot spectrum within SDR band"],
    "analog_quadrature": ["pilot source", "injection coupler", "P4 tap + quadrature detector",
                          "detector reference (oscillator or pilot-source tap)", "logging ADC + clock alignment"],
}


def matched_controls(work, seed, config, n_circuits=6):
    """Online outputs of matched pairs must be identical (attribution integrity)."""
    sensors = CONFIGS[config][0]
    circs = emittable_circuits(config)
    chosen = [c for c in circs if not leakage_breaks(sensors, c)][:n_circuits // 2] + \
             [c for c in circs if leakage_breaks(sensors, c)][:n_circuits - n_circuits // 2]
    tpl = Disturbance("G", "step", 9_030_000, 0.5)
    rows = []
    for i, circ in enumerate(chosen):
        sa, sb = matched_pair(G.Scenario(config=config), circ, tpl)
        recs = []
        for j, sc in enumerate((sa, sb)):
            d = os.path.join(work, f"pair{i}_{j}")
            G.generate(sc, seed, d, os.path.join(work, f"pair{i}_{j}.truth.json"))
            recs.append(capture_record(load_capture(d)))
        a, b = recs

        def sig(r):
            return ({k: [x["available_ns"] for x in v["excursions"]] for k, v in r["detectors"].items()},
                    {k: [(w["state"], tuple(w["consistent_single_causes"]), w["unique_attribution"])
                         for w in ws] for k, ws in r["guard"].items()})
        rows.append({"circuit": sorted(circ), "leakage_broken": leakage_breaks(sensors, circ),
                     "online_outputs_identical": sig(a) == sig(b),
                     "detected": any(v["excursions"] for v in a["detectors"].values())})
    return rows


def resources(frozen_path, truth_root, scenario):
    with open(frozen_path) as f:
        fr = json.load(f)
    out = {}
    span = sum(c["span_ns"][1] - c["span_ns"][0] for c in fr["captures"])
    for kind in ("reference_bracket", "sampled_digital", "analog_quadrature"):
        dets = [c["detectors"][kind] for c in fr["captures"] if kind in c["detectors"]]
        if not dets:
            continue
        mon = sum(e - s for d in dets for s, e, _ in d["monitored_intervals"] if e is not None)
        dead = sum(e - s for d in dets for s, e, r in d["unmonitored_intervals"]
                   if e is not None and r == "OVERFLOW_BOUNDARY")
        nr = sum(e - s for d in dets for s, e, _ in d["not_ready_intervals"] if e is not None)
        out[kind] = {"usable_exposure_fraction": mon / span, "not_ready_fraction": nr / span,
                     "logger_dead_time_fraction": dead / span if kind == "analog_quadrature" else 0.0,
                     "extra_hardware": HARDWARE[kind]}
    r_time = tot = 0
    for c in fr["captures"]:
        with open(os.path.join(truth_root, c["capture_id"] + ".truth.json")) as f:
            gen = json.load(f)["generator"]
        sw, dur = gen["switches"], gen["scenario"]["duration_ns"]
        r_time += sum((sw[i + 1]["t"] if i + 1 < len(sw) else dur) - s["t"] for i, s in enumerate(sw) if s["port"] == "R")
        tot += dur
    out["reference_bracket"]["reference_dwell_fraction_of_scene_time"] = r_time / tot
    block = MonitorParams().block_samples / scenario.fs_hz
    out["sampled_digital"]["pilot_level_rel_scene_db"] = scenario.pilot_rel_db
    out["sampled_digital"]["pilot_mainlobe_occupancy_hz"] = 2 / block
    return out


def run(out_dir, n=102, seed=None, config="C6_pilot_analog", duration_ns=20_000_000):
    seed = seed or secrets.token_hex(32)
    caps, truth = os.path.join(out_dir, "captures"), os.path.join(out_dir, "truth")
    os.makedirs(truth, exist_ok=True)
    with open(os.path.join(truth, "study_seed.hex"), "w") as f:
        f.write(seed + "\n")
    generate_blind_batch(caps, truth, seed, n, config=config, duration_ns=duration_ns)
    frozen = os.path.join(out_dir, "frozen_online.json")
    digest = freeze(caps, frozen)                       # online outputs fixed here...
    policy = MatchPolicy()
    report = score(frozen, truth, digest, policy)       # ...truth opened only after this check
    sc = G.Scenario(config=config, duration_ns=duration_ns)
    work = os.path.join(out_dir, "controls")
    os.makedirs(work, exist_ok=True)
    ctrl_seed = G.derive_bytes(bytes.fromhex(seed), "controls").hex()
    report["matched_pair_controls"] = matched_controls(work, ctrl_seed, config)
    report["leakage"] = {"pilot_into_scene": pilot_into_scene(ctrl_seed, replace(sc, duration_ns=10_000_000)),
                         "scene_into_pilot": scene_into_pilot(ctrl_seed, replace(sc, duration_ns=10_000_000)),
                         "scene_into_analog": scene_into_analog(ctrl_seed, replace(sc, duration_ns=10_000_000))}
    report["resources"] = resources(frozen, truth, sc)
    report["study"] = {"config": config, "n": n, "duration_ns": duration_ns, "cases": cases_for(config),
                       "seed_location": "truth/study_seed.hex (offline)"}
    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(report, f, indent=1, sort_keys=True, default=list)
    with open(os.path.join(out_dir, "report.md"), "w") as f:
        f.write(render(report))
    return report


def _ci(x):
    lo, hi = x if x else (None, None)
    return "n/a" if lo is None else f"[{lo:.3f}, {hi:.3f}]"


def _p(x):
    return "n/a" if x is None else f"{x:.3f}"


def render(r) -> str:
    L = ["# Sprint 1e blind evaluation (synthetic)\n",
         f"Captures: {r['n_captures']} ({r['study']['config']}, {r['study']['duration_ns'] / 1e6:g} ms each). "
         f"Frozen online outputs sha256 `{r['frozen_sha256'][:16]}…`; match policy "
         f"`{r['policy']['version']}` sha256 `{r['policy_sha256'][:16]}…`. All detectors scored on the "
         "same captures (paired, identical acquisition budget). **Synthetic, illustrative envelope; "
         "not a hardware or novelty claim.**\n",
         "## Detection (one credit per event; nothing removed from denominators)\n",
         "| Detector | Events | Detected | Missed | Below floor | Unmonitored | Unobs. (structural) | "
         "Unobs. (model) | Pd all [95% CI, clustered] | Pd observable | Pd observable+monitored |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, d in r["detectors"].items():
        c = d["counts"]
        L.append(f"| `{k}` | {d['events']} | {c['DETECTED']} | {c['MISSED']} | {c['MISSED_BELOW_FLOOR']} | "
                 f"{c['UNMONITORED']} | {c['UNOBSERVABLE_STRUCTURAL']} | {c['UNOBSERVABLE_MODEL']} | "
                 f"{_p(d['pd_all_events']['pd'])} {_ci(d['pd_all_events']['ci_cluster_bootstrap'])} | "
                 f"{_p(d['pd_observable']['pd'])} {_ci(d['pd_observable']['ci_cluster_bootstrap'])} | "
                 f"{_p(d['pd_observable_monitored']['pd'])} {_ci(d['pd_observable_monitored']['ci_cluster_bootstrap'])} |")
    L += ["\n## Latency and false alarms\n",
          "| Detector | Latency mean (us) | p95 (us) | jitter (us) | Duplicates | False alarms | "
          "Monitored exposure (min) | FA/min | FA/min 95% upper bound |", "|---|---|---|---|---|---|---|---|---|"]
    for k, d in r["detectors"].items():
        la, fa = d["latency_ns"], d["false_alarms"]
        us = (lambda v: "n/a" if v is None else f"{v / 1e3:.0f}")
        L.append(f"| `{k}` | {us(la['mean'])} | {us(la['p95'])} | {us(la['jitter_std'])} | {d['duplicates']} | "
                 f"{fa['count']} | {fa['exposure_minutes']:.3f} | {_p(fa['rate_per_minute'])} | "
                 f"{_p(fa['upper_bound_per_minute'])} |")
    L += ["\nZero observed false alarms is not a zero rate: the last column is the exact one-sided "
          "Poisson upper bound on the monitored exposure outside event windows.\n",
          "## Paired detector comparison (same events)\n", "| Pair | Pd difference | 95% CI (clustered) | Events |",
          "|---|---|---|---|"]
    for k, d in r["paired"].items():
        L.append(f"| {k} | {_p(d['pd_difference_all_events'])} | {_ci(d['ci_cluster_bootstrap'])} | {d['events']} |")
    L += ["\n## Online guard (1 ms windows, decided 300 us after window end)\n",
          "| Active | Windows | CONDITIONAL / UNRESOLVED / INVALID | P(CONDITIONAL \\| U changed) [CI] | "
          "P(CONDITIONAL \\| quiet) [CI] | VALID_BOUNDED | Unique attributions | Consistent set holds a true cause | "
          "Mean set size |", "|---|---|---|---|---|---|---|---|---|"]
    for k, g in r["guard"].items():
        s = g["states"]
        L.append(f"| `{k}` | {g['windows']} | {s.get('CONDITIONAL', 0)} / {s.get('UNRESOLVED', 0)} / "
                 f"{s.get('INVALID', 0)} | {_p(g['unsafe_conditional_given_U_change']['rate'])} "
                 f"{_ci(g['unsafe_conditional_given_U_change']['ci_cluster_bootstrap'])} | "
                 f"{_p(g['conditional_availability_quiet']['rate'])} "
                 f"{_ci(g['conditional_availability_quiet']['ci_cluster_bootstrap'])} | {g['valid_bounded_issued']} | "
                 f"{g['unique_attributions_issued']} | {_p(g['consistent_set_contains_a_true_cause'])} | "
                 f"{_p(g['mean_consistent_set_size'])} |")
    L += ["\nP(CONDITIONAL | U changed), stratified by the size and kind of the change inside the window:\n",
          "| Active | " + " | ".join(next(iter(r["guard"].values()))["unsafe_conditional_by_stratum"]) + " |",
          "|---|" + "---|" * len(next(iter(r["guard"].values()))["unsafe_conditional_by_stratum"])]
    for k, g in r["guard"].items():
        L.append(f"| `{k}` | " + " | ".join(
            f"{_p(v['rate'])} ({v['k']}/{v['n']}) {_ci(v['ci_cluster_bootstrap'])}"
            for v in g["unsafe_conditional_by_stratum"].values()) + " |")
    L += ["\n## Matched-pair controls\n", "| Circuit | Leakage-broken | Online outputs identical | Detected |",
          "|---|---|---|---|"]
    for row in r["matched_pair_controls"]:
        L.append(f"| `{{{','.join(row['circuit'])}}}` | {row['leakage_broken']} | "
                 f"{row['online_outputs_identical']} | {row['detected']} |")
    L += ["\n## Resource accounting\n", "| Detector | Usable exposure | Not ready | Logger dead time | Extra hardware |",
          "|---|---|---|---|---|"]
    for k, d in r["resources"].items():
        L.append(f"| `{k}` | {d['usable_exposure_fraction']:.3f} | {d['not_ready_fraction']:.3f} | "
                 f"{d['logger_dead_time_fraction']:.3f} | {'; '.join(d['extra_hardware'])} |")
    rb, sp = r["resources"]["reference_bracket"], r["resources"]["sampled_digital"]
    lk = r["leakage"]
    L += [f"\nReference dwell consumes {rb['reference_dwell_fraction_of_scene_time']:.1%} of scene time. "
          f"Pilot at {sp['pilot_level_rel_scene_db']:g} dB re scene, main-lobe occupancy ~{sp['pilot_mainlobe_occupancy_hz'] / 1e3:g} kHz. "
          f"Leakage RMS: pilot->scene {lk['pilot_into_scene']['rms_rad']:.2e} rad, scene->pilot "
          f"{lk['scene_into_pilot']['rms_rad']:.2e} rad, scene->analog {lk['scene_into_analog']['rms_rad']:.2e} rad.\n",
          "## Per-case detection (DETECTED / events)\n"]
    kinds = list(r["detectors"])
    L.append("| Case | " + " | ".join(f"`{k}`" for k in kinds) + " |")
    L.append("|---|" + "---|" * len(kinds))
    cases = sorted({c for d in r["detectors"].values() for c in d["by_case"]})
    for c in cases:
        cells = []
        for k in kinds:
            b = r["detectors"][k]["by_case"].get(c)
            cells.append("-" if not b else f"{b['DETECTED']}/{sum(b.values())}")
        L.append(f"| {c} | " + " | ".join(cells) + " |")
    L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=102)
    ap.add_argument("--seed-hex")
    ap.add_argument("--config", default="C6_pilot_analog", choices=["C6_pilot_analog", "C6s_pilot_analog_srcref"])
    a = ap.parse_args(argv)
    run(a.out, a.n, a.seed_hex, a.config)
    print(f"report: {os.path.join(a.out, 'report.md')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
