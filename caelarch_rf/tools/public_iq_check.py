"""Run public raw-I/Q recordings through the adapter, validator and loader.

    python -m caelarch_rf.tools.public_iq_check --src REPO_DIR --list FILES.txt --out OUT

For each recording: convert (adapters.external_raw), validate, load with the
same fail-closed loader used for synthetic captures, then measure hardware
artefacts the synthetic generator does not model (DC offset, IQ imbalance,
clipping, quantisation, bursts), and run the sampled extractor on the DC
component as a stand-in pilot (robustness only: these recordings contain no
Caelarch pilot, so nothing here is a monitor-performance result).

Offline analysis tool; uses numpy (not part of the stdlib-only packages).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time

import numpy as np

from caelarch_rf.adapters.external_raw import convert, parse_rtl433_name
from caelarch_rf.gate04.contract import ContractError
from caelarch_rf.gate04.ingest import load_capture
from caelarch_rf.gate04.monitors import extract_sampled_pilot, window_state

MODEL = {"cu8": "RTL-SDR class (cu8, unverified)", "cs8": "HackRF class (cs8, unverified)",
         "cs16": "unknown 16-bit SDR (cs16)"}


def characterize(path, clip):
    z16 = np.fromfile(path, dtype="<i2").astype(np.float64)
    i, q = z16[0::2], z16[1::2]
    z = i + 1j * q
    n = len(z)
    rms = math.sqrt(float(np.mean(np.abs(z) ** 2))) or 1.0
    dc = complex(np.mean(z))
    ic, qc = i - i.mean(), q - q.mean()
    si, sq = float(ic.std()), float(qc.std())
    rho = float(np.mean(ic * qc) / (si * sq)) if si and sq else 0.0
    g, phi = (si / sq if sq else 1.0), math.asin(max(-1.0, min(1.0, rho)))
    irr = 10 * math.log10(abs(1 + g * np.exp(1j * phi)) ** 2 / max(abs(1 - g * np.exp(1j * phi)) ** 2, 1e-30))
    B = 1024
    nb = n // B
    pw = np.mean(np.abs(z[: nb * B].reshape(nb, B)) ** 2, axis=1) if nb else np.array([rms ** 2])
    p10, p95 = np.percentile(pw, 10), np.percentile(pw, 95)
    # Averaged spectrum: DC bin vs median bin.
    if nb:
        w = np.hanning(B)
        spec = np.mean(np.abs(np.fft.fft(z[: nb * B].reshape(nb, B) * w, axis=1)) ** 2, axis=0)
        dc_spike_db = 10 * math.log10(spec[0] / np.median(spec))
    else:
        dc_spike_db = None
    return {
        "samples": n,
        "clipped_fraction": float(np.mean((np.abs(i) >= clip) | (np.abs(q) >= clip))),
        "dc_rel_rms_db": 20 * math.log10(abs(dc) / rms) if abs(dc) > 0 else -300.0,
        "iq_gain_imbalance_db": 20 * math.log10(g) if g > 0 else None,
        "iq_phase_imbalance_deg": math.degrees(phi),
        "image_rejection_db": irr,
        "noise_floor_dbfs": 10 * math.log10(p10 / 32768 ** 2) if p10 > 0 else None,
        "burst_dynamic_range_db": 10 * math.log10(p95 / p10) if p10 > 0 else None,
        "distinct_i_levels": int(len(np.unique(i))),
        "dc_spike_over_median_bin_db": dc_spike_db,
    }


def dc_pilot_robustness(cap):
    """Run the sampled extractor on the DC component (in-memory pseudo-pilot)."""
    sv = cap.streams["rx0"]
    sv.meta = dict(sv.meta, pilot={"offset_hz": 0.0, "waveform_id": "dc-analysis-only"})
    r = extract_sampled_pilot(cap, "rx0")
    states = {}
    for o in r.observations:
        states[o.monitor_state] = states.get(o.monitor_state, 0) + 1
    win = {"CONDITIONAL": 0, "UNRESOLVED": 0, "INVALID": 0}
    t0, t1 = int(sv.time_of(0)), int(sv.time_of(sv.timed_end() - 1))
    t = t0
    while t + 1_000_000 <= t1:
        st, _ = window_state(r, t, t + 1_000_000, t + 1_300_000)
        win[st.value] += 1
        t += 1_000_000
    return {"blocks": len(r.observations), "states": states, "excursions": len(r.excursions),
            "ambiguous_2pi": sum(e.ambiguous_2pi for e in r.excursions),
            "pilot_lost_blocks": sum(1 for o in r.observations if o.reason == "PILOT_LOST"),
            "windows_1ms": win}


def run(src_dir, files, out_dir, created_utc):
    caps = os.path.join(out_dir, "captures")
    os.makedirs(caps, exist_ok=True)
    rows = []
    for rel in files:
        row = {"source": rel}
        try:
            center, rate, fmt = parse_rtl433_name(rel)
            cid_dir = os.path.join(caps, f"tmp-{len(rows)}")
            m, rec = convert(os.path.join(src_dir, rel), cid_dir, center, rate, fmt, created_utc, MODEL[fmt])
            final = os.path.join(caps, m["capture_id"])
            os.replace(cid_dir, final)
            row.update(rec, source=rel)
            t = time.time()
            cap = load_capture(final)
            sv = cap.streams["rx0"]
            row["load_seconds"] = round(time.time() - t, 3)
            row["loader"] = {"status": "LOADED", "sample_count": sv.n,
                             "duration_ms": sv.n / rate * 1e3,
                             "accepted_fraction": sum(b - a for a, b in sv.accepted_ranges()) / sv.n,
                             "rejections": sorted({r.reason for r in sv.rejections})}
            row["artefacts"] = characterize(os.path.join(final, "rx0.sc16"), m["streams"][0]["clip_level"])
            row["dc_pilot_robustness"] = dc_pilot_robustness(cap)
        except (ContractError, ValueError) as e:
            row["loader"] = {"status": "REFUSED", "error": str(e)}
        rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--list", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--created-utc", default="2026-11-08T03:00:24Z")
    a = ap.parse_args(argv)
    with open(a.list) as f:
        files = [x.strip() for x in f if x.strip()]
    rows = run(a.src, files, a.out, a.created_utc)
    with open(os.path.join(a.out, "public_iq_report.json"), "w") as f:
        json.dump(rows, f, indent=1)
    ok = sum(r["loader"]["status"] == "LOADED" for r in rows)
    print(f"{ok}/{len(rows)} loaded; report {os.path.join(a.out, 'public_iq_report.json')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
