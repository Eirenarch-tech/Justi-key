"""Online evaluation and FREEZING of outputs before any truth is revealed.

``freeze(capture_root, out_path)`` runs every available monitor and the guard
over every capture under ``capture_root`` and writes one JSON file. It takes
no truth path and imports nothing that can open truth (enforced by the
isolation test). The scorer (``sim/scorer.py``) records this file's SHA-256
before it opens any truth, and refuses a file whose digest differs from the
one the caller recorded at freeze time.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List

from .guard import ACTIVE, assess
from .ingest import load_capture
from .monitors import (BracketParams, MonitorParams, MonitorResult, extract_analog_monitor,
                       extract_reference_bracket, extract_sampled_pilot)

SCHEMA_FROZEN = "caelarch.frozen_online/0.1"


def run_monitors(cap) -> Dict[str, MonitorResult]:
    out = {}
    for sid, sv in cap.streams.items():
        if sv.meta.get("reference_tone"):
            out["reference_bracket"] = extract_reference_bracket(cap, sid, BracketParams())
        if sv.meta.get("pilot"):
            out["sampled_digital"] = extract_sampled_pilot(cap, sid, MonitorParams())
        if sv.meta.get("monitor"):
            out["analog_quadrature"] = extract_analog_monitor(cap, sid, MonitorParams(block_samples=20))
    return out


def guard_windows(cap, results, active, window_ns=1_000_000, decision_delay_ns=300_000, timebase="sdr0"):
    sv = next(s for s in cap.streams.values() if s.timebase == timebase)
    t0 = int(sv.time_of(0))
    t_end = int(sv.time_of(sv.timed_end() - 1))
    out = []
    t = t0
    while t + window_ns <= t_end:
        out.append(assess(cap, results, active, t, t + window_ns, t + window_ns + decision_delay_ns, timebase))
        t += window_ns
    return out


def _intervals(res: MonitorResult, status):
    return [[c.start_ns, c.end_ns, c.reason] for c in res.coverage if c.status == status]


def capture_record(cap, actives=tuple(ACTIVE)) -> dict:
    results = run_monitors(cap)
    det = {}
    for kind, r in results.items():
        det[kind] = {
            "sensor": r.sensor, "timebase": r.timebase, "sample_period_ns": r.sample_period_ns,
            "excursions": [{"available_ns": e.available_ns, "window_start_ns": e.window_start_ns,
                            "window_end_ns": e.window_end_ns, "innovation_rad": e.innovation_rad,
                            "ambiguous_2pi": e.ambiguous_2pi, "attribution": e.attribution}
                           for e in r.excursions],
            "tracking": [[o.window_start_ns, o.window_end_ns, o.available_ns, o.detection_floor_rad]
                         for o in r.observations if o.monitor_state == "TRACKING" and o.usable],
            "monitored_intervals": _intervals(r, "MONITORED") + _intervals(r, "BRACKET_ONLY"),
            "not_ready_intervals": _intervals(r, "NOT_READY"),
            "unmonitored_intervals": _intervals(r, "UNMONITORED"),
        }
    guard = {}
    for a in actives:
        if any(k not in results for k in ACTIVE[a] if k != "reference_bracket"):
            continue
        guard[a] = [{"t_start_ns": g.t_start_ns, "t_end_ns": g.t_end_ns, "t_decision_ns": g.t_decision_ns,
                     "state": g.state.value, "detected": g.detected, "moved": list(g.moved),
                     "consistent_single_causes": list(g.consistent_single_causes),
                     "multi_cause_explanations": g.multi_cause_explanations,
                     "unique_attribution": g.unique_attribution, "reasons": list(g.reasons)}
                    for g in guard_windows(cap, results, a)]
    sv0 = cap.streams["rx0"]
    return {"capture_id": cap.manifest["capture_id"], "truth_commitment": cap.manifest["truth_commitment"],
            "streams": {sid: {"timebase": s.timebase, "format": s.meta["format"],
                              "sample_rate_hz": s.meta["sample_rate_hz"], "n": s.n,
                              "monitor": s.meta.get("monitor"), "pilot": s.meta.get("pilot")}
                        for sid, s in cap.streams.items()},
            "span_ns": [int(sv0.time_of(0)), int(sv0.time_of(sv0.timed_end() - 1))],
            "detectors": det, "guard": guard}


def freeze(capture_root, out_path) -> str:
    """Run online processing on every capture; write and return the SHA-256."""
    caps: List[dict] = []
    for name in sorted(os.listdir(capture_root)):
        caps.append(capture_record(load_capture(os.path.join(capture_root, name))))
    doc = {"schema": SCHEMA_FROZEN,
           "params": {"monitor": MonitorParams().__dict__, "analog_block_samples": 20,
                      "bracket": BracketParams().__dict__, "guard_window_ns": 1_000_000,
                      "guard_decision_delay_ns": 300_000},
           "captures": caps}
    data = (json.dumps(doc, sort_keys=True, default=list) + "\n").encode()
    with open(out_path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()
