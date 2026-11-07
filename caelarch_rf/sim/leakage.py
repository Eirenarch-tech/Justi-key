"""Pilot <-> scene measurement interference. OFFLINE study (uses counterfactuals).

Quantified by rendering the same seed twice, so that noise, operating schedule
and background are identical and the difference isolates one component:

* pilot -> scene: C2 (pilot on) vs C1 (no pilot). A Hann-windowed scene-phase
  estimator at the known scene tone frequency runs on settled H/R blocks of
  both. The phase difference is the scene error caused by the pilot.
* scene -> pilot: C2 with scene on vs off. The online sampled extractor runs on
  both captures; the difference in reported pilot phase is the error caused by
  the scene.
* scene -> analog detector: the same comparison for the analog monitor (C6).

Also returns the analytic steady-state bound (a_other / a_self) x |W(df)|/W(0)
for the Hann window. Step transients on the stronger scene splatter beyond
that bound: the empirical max covers them, the bound does not.
"""
from __future__ import annotations

import cmath
import math
import os
import tempfile
from dataclasses import replace

from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.monitors import MonitorParams, extract_analog_monitor, extract_sampled_pilot

from . import generator as G


def hann_response(df_hz, block, fs_hz):
    """|W(df)| / W(0) for the extractor's Hann window."""
    w = [0.5 - 0.5 * math.cos(2 * math.pi * (k + 0.5) / block) for k in range(block)]
    s = sum(wk * cmath.exp(2j * math.pi * df_hz * k / fs_hz) for k, wk in enumerate(w))
    return abs(s) / sum(w)


def _scene_phases(sim, block):
    """Hann scene-phase estimates on fully settled H/R blocks (float IQ, pre-quantization)."""
    out = {}
    f_bb = G.SCENE_RF_HZ - G.CENTER0_HZ
    w = [0.5 - 0.5 * math.cos(2 * math.pi * (k + 0.5) / block) for k in range(block)]
    for i0 in range(0, sim.clock.n0 - block + 1, block):
        ports = set(sim.port_phys[i0:i0 + block])
        if len(ports) != 1 or ports.pop() not in ("H", "R"):
            continue
        z = sum(w[k] * sim.iq_float[i0 + k] * cmath.exp(-2j * math.pi * f_bb * sim.tp[i0 + k] * 1e-9)
                for k in range(block))
        out[i0] = cmath.phase(z)
    return out


def _stats(diffs):
    if not diffs:
        return {"n": 0, "rms_rad": None, "max_rad": None}
    return {"n": len(diffs), "rms_rad": math.sqrt(sum(d * d for d in diffs) / len(diffs)),
            "max_rad": max(abs(d) for d in diffs)}


def _wrap(x):
    return (x + math.pi) % (2 * math.pi) - math.pi


def pilot_into_scene(seed, scenario: G.Scenario, block=100):
    base = replace(scenario, config="C2_sampled_pilot")
    with_p = G.simulate(base, seed)
    without = G.simulate(replace(base, config="C1_witness"), seed)
    a, b = _scene_phases(with_p, block), _scene_phases(without, block)
    diffs = [_wrap(a[k] - b[k]) for k in a if k in b]
    df = G.PILOT_RF_HZ - G.SCENE_RF_HZ
    ratio = 10 ** (scenario.pilot_rel_db / 20)
    bound = math.asin(min(1.0, ratio * hann_response(df, block, scenario.fs_hz)))
    return {**_stats(diffs), "analytic_bound_rad": bound, "delta_f_hz": df, "block": block}


def _extract_pair(seed, sc_on, sc_off, fn):
    with tempfile.TemporaryDirectory() as d:
        out = []
        for i, sc in enumerate((sc_on, sc_off)):
            cap = os.path.join(d, f"c{i}")
            G.generate(sc, seed, cap, os.path.join(d, f"t{i}.json"))
            out.append(fn(load_capture(cap)))
    on, off = out
    on_ph = {o.window_start_ns: o.phase_rad for o in on.observations if o.usable}
    diffs = [_wrap(on_ph[o.window_start_ns] - o.phase_rad) for o in off.observations
             if o.usable and o.window_start_ns in on_ph]
    return diffs


def scene_into_pilot(seed, scenario: G.Scenario, params=MonitorParams()):
    base = replace(scenario, config="C2_sampled_pilot")
    diffs = _extract_pair(seed, base, replace(base, scene_on=False),
                          lambda c: extract_sampled_pilot(c, params=params))
    df = G.SCENE_RF_HZ - G.PILOT_RF_HZ
    ratio = 10 ** (-scenario.pilot_rel_db / 20)
    lin = ratio * hann_response(df, params.block_samples, scenario.fs_hz)
    return {**_stats(diffs), "analytic_bound_rad": math.asin(min(1.0, lin)), "delta_f_hz": df}


def scene_into_analog(seed, scenario: G.Scenario, params=MonitorParams(block_samples=20)):
    base = replace(scenario, config="C6_pilot_analog")
    diffs = _extract_pair(seed, base, replace(base, scene_on=False),
                          lambda c: extract_analog_monitor(c, params=params))
    return {**_stats(diffs), "analytic_bound_rad": None,
            "note": "detector LPF + logger block averaging; no closed-form bound computed"}
