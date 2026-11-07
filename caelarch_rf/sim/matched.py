"""Matched-observation disturbance pairs, per sensor configuration. OFFLINE ONLY.

For a configuration, every circuit of the observation graph (a minimal
confounding set) has a null vector v supported exactly on it. Split v into its
positive and negative parts: applying ``a*v+`` or ``a*v-`` with the same
trajectory shape moves every sensor of that configuration identically, although
the physical causes are disjoint. Rendered with the same seed, the two
captures must match to within quantisation (refinement 1: pairs are built
separately for each configuration, and a pair that matches in C0/C1 need not
match once a pilot is present).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Tuple

from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.observability import CONFIGS, circuit_vector, circuits

from .disturbances import Disturbance, scaled
from .generator import EMITTED, Scenario


def emittable_circuits(config) -> List[frozenset]:
    """Circuits of ``config`` that involve only terms the renderer can drive."""
    if config not in EMITTED:
        raise ValueError(f"{config} is not renderable yet")
    sensors = CONFIGS[config][0]
    return [c for c in circuits(sensors) if len(c) > 1 and "M" not in c]


def split_vector(config, circuit) -> Tuple[Dict[str, float], Dict[str, float]]:
    v = circuit_vector(CONFIGS[config][0], circuit)
    pos = {k: float(c) for k, c in v.items() if c > 0}
    neg = {k: float(-c) for k, c in v.items() if c < 0}
    return pos, neg


def matched_pair(base: Scenario, circuit, template: Disturbance) -> Tuple[Scenario, Scenario]:
    """Two scenarios that differ only by a configuration-invisible disturbance.

    ``template`` gives the trajectory shape (kind, time, amplitude); its target
    is ignored. Any disturbances already in ``base`` are kept in both.
    """
    pos, neg = split_vector(base.config, circuit)
    a = tuple(scaled(template, k, f) for k, f in pos.items())
    b = tuple(scaled(template, k, f) for k, f in neg.items())
    label = "{" + ",".join(sorted(circuit)) + "}"
    return (replace(base, disturbances=base.disturbances + a, case=f"matched{label}:+"),
            replace(base, disturbances=base.disturbances + b, case=f"matched{label}:-"))


def online_difference(dir_a, dir_b) -> Dict[str, object]:
    """Compare two captures exactly as an online method would see them.

    Returns the max |I/Q| difference in LSB, whether events, witness timing and
    the manifest (ignoring per-capture hashes, id and truth commitment) agree,
    and the max witness ratio difference.
    """
    ca, cb = load_capture(dir_a), load_capture(dir_b)
    out = {}
    for sid, sa in ca.streams.items():
        sb = cb.streams[sid]
        if sa.n != sb.n:
            out["max_iq_lsb"] = float("inf")
            break
        xa, xb = sa.read_iq(0, sa.n), sb.read_iq(0, sb.n)
        out["max_iq_lsb"] = max(max(abs(p.real - q.real), abs(p.imag - q.imag)) for p, q in zip(xa, xb))
    out["events_equal"] = ca.events == cb.events

    def timing(w):
        return [(r["seq"], r["timebase"], r["t_ns"], r["valid"]) for r in w]
    out["witness_timing_equal"] = timing(ca.witness) == timing(cb.witness)
    out["witness_max_abs"] = max([abs(complex(p["ratio_re"], p["ratio_im"]) - complex(q["ratio_re"], q["ratio_im"]))
                                  for p, q in zip(ca.witness, cb.witness)] or [0.0])

    def strip(m):
        m = {k: v for k, v in m.items()
             if k not in ("capture_id", "truth_commitment", "events_sha256", "witness_sha256")}
        m["streams"] = [{k: v for k, v in s.items() if k != "sha256"} for s in m["streams"]]
        return m
    out["manifest_equal"] = strip(ca.manifest) == strip(cb.manifest)
    out["equivalent"] = (out["max_iq_lsb"] <= 1 and out["events_equal"] and out["witness_timing_equal"]
                         and out["witness_max_abs"] <= 1e-9 and out["manifest_equal"])
    return out
