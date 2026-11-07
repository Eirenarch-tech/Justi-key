"""Blind batch generation: randomized cases, opaque capture ids. OFFLINE ONLY.

    python -m caelarch_rf.sim.batch --captures DIR --truth DIR [--n 30]
           [--config C2_sampled_pilot] [--seed-hex HEX]

Without ``--seed-hex`` a fresh 256-bit seed is drawn and written to
``<truth>/batch_seed.hex``. Case names, the seed and the case assignment exist
only under the truth directory; capture directories are named by keyed-hash
ids and are mutually indistinguishable apart from their physics and their
(online-visible) acquisition events.

Case templates receive only (rng, duration_ns, fs_hz): disturbance onset times
are drawn uniformly over the capture from the disturbance stream, never from
the operating schedule, so onsets carry no fixed relation to switch commands.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from typing import Callable, Dict, List, Tuple

from .disturbances import Disturbance
from .generator import (EMITTED, AcqFault, Scenario, _inside, capture_id_for, derive_bytes,
                        generate, normalize_seed, stream)

Template = Callable[[object, int, int], Tuple[Tuple[Disturbance, ...], Tuple[AcqFault, ...]]]


def _t(rng, dur):
    return int(rng.uniform(0.1, 0.9) * dur)


def _amp(rng, lo=0.1, hi=0.5):
    return rng.choice([-1, 1]) * rng.uniform(lo, hi)


def _step(target):
    return lambda r, d, fs: ((Disturbance(target, "step", _t(r, d), _amp(r)),), ())


def _fault(kind):
    # Each fault case also carries a G step, so faults interact with physics.
    return lambda r, d, fs: ((Disturbance("G", "step", _t(r, d), _amp(r)),), (AcqFault(kind),))


def _cancel(r, d, fs):
    t, a = _t(r, d), _amp(r)
    return (Disturbance("G", "step", t, a), Disturbance("J", "step", t, -a)), ()


def _both(x, y):
    def f(r, d, fs):
        t = _t(r, d)
        return (Disturbance(x, "step", t, _amp(r)), Disturbance(y, "step", t, _amp(r))), ()
    return f


LIBRARY: Dict[str, Template] = {
    "baseline": lambda r, d, fs: ((), ()),
    **{f"{k}_step": _step(k) for k in ("X", "H", "R", "K", "J", "S", "G", "A", "FG", "FA")},
    "G_ramp": lambda r, d, fs: ((Disturbance("G", "ramp", _t(r, d) // 2, _amp(r), int(r.uniform(1e6, 5e6))),), ()),
    "S_random_walk": lambda r, d, fs: ((Disturbance("S", "random_walk", _t(r, d) // 2, r.uniform(5, 20),
                                                    d // 3, seed=r.getrandbits(64)),), ()),
    "G_glitch_inband": lambda r, d, fs: ((Disturbance("G", "pulse", _t(r, d), _amp(r, 0.3, 1.0),
                                                      int(r.uniform(50e3, 500e3))),), ()),
    "G_glitch_intersample": lambda r, d, fs: ((Disturbance("G", "intersample_pulse", _t(r, d), _amp(r, 0.3, 1.0)),), ()),
    "G_sine": lambda r, d, fs: ((Disturbance("G", "sine", _t(r, d) // 2, _amp(r, 0.05, 0.3),
                                             freq_hz=r.choice([25.0, 100.0, 250.0, 1000.0])),), ()),
    "H_plus_G": _both("H", "G"),
    "H_plus_J": _both("H", "J"),
    "G_plus_J_cancel": _cancel,
    "G_delay_selective": lambda r, d, fs: ((Disturbance("G", "delay_step", _t(r, d), r.uniform(0.2, 2.0)),), ()),
    "A_delay_selective": lambda r, d, fs: ((Disturbance("A", "delay_step", _t(r, d), r.uniform(0.2, 2.0)),), ()),
    **{f"fault_{k}": _fault(k) for k in ("retune", "retune_unlocked", "gain_change", "ref_unlock",
                                         "overflow_known", "overflow_unknown", "time_discontinuity",
                                         "overload", "short_guard", "pilot_off", "witness_invalid")},
}


def cases_for(config) -> List[str]:
    pilot, witness = EMITTED[config]
    out = []
    for name in LIBRARY:
        if not pilot and name in ("fault_pilot_off",):
            continue
        if not witness and name == "fault_witness_invalid":
            continue
        out.append(name)
    return out


def generate_blind_batch(capture_root, truth_root, seed, n, config="C2_sampled_pilot",
                         cases=None, duration_ns=20_000_000, fs_hz=1_000_000) -> List[str]:
    """Generate ``n`` blind captures. Returns capture ids only (never cases)."""
    master = normalize_seed(seed)
    if _inside(truth_root, capture_root) or _inside(capture_root, truth_root):
        raise ValueError("capture and truth roots must be disjoint")
    cases = list(cases or cases_for(config))
    unknown = set(cases) - set(LIBRARY)
    if unknown:
        raise ValueError(f"unknown cases {sorted(unknown)}")
    assign = stream(master, "batch/assign")
    # Balanced: cycle through cases, then shuffle so position reveals nothing.
    chosen = [cases[i % len(cases)] for i in range(n)]
    assign.shuffle(chosen)
    os.makedirs(capture_root, exist_ok=True)
    os.makedirs(truth_root, exist_ok=True)
    ids, index = [], []
    for i, case in enumerate(chosen):
        cap_seed = derive_bytes(master, f"batch/capture/{i}")
        rng = stream(cap_seed, "disturbance")
        dists, faults = LIBRARY[case](rng, duration_ns, fs_hz)
        sc = Scenario(disturbances=dists, faults=faults, config=config,
                      duration_ns=duration_ns, fs_hz=fs_hz, case=case)
        cid = capture_id_for(cap_seed)
        generate(sc, cap_seed, os.path.join(capture_root, cid), os.path.join(truth_root, cid + ".truth.json"))
        ids.append(cid)
        index.append({"capture_id": cid, "case": case})
    with open(os.path.join(truth_root, "batch_index.json"), "w", encoding="utf-8") as f:
        json.dump({"config": config, "n": n, "captures": index}, f, indent=1)
    return ids


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--captures", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--config", default="C2_sampled_pilot", choices=sorted(EMITTED))
    ap.add_argument("--seed-hex")
    a = ap.parse_args(argv)
    seed = a.seed_hex or secrets.token_hex(32)
    os.makedirs(a.truth, exist_ok=True)
    if not a.seed_hex:
        with open(os.path.join(a.truth, "batch_seed.hex"), "w") as f:
            f.write(seed + "\n")
    ids = generate_blind_batch(a.captures, a.truth, seed, a.n, a.config)
    print(f"wrote {len(ids)} blind captures to {a.captures}; truth and seed under {a.truth}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
