"""Synthetic capture writer for tests. Produces contract-conformant directories.

The waveform content is a trivial tone; these captures exercise the contract
and ingestion logic only and are marked provenance.kind = "synthetic".
"""
import array
import copy
import hashlib
import json
import math
import os
import sys

FS = 1_000_000  # 1 MS/s -> 1000 ns per sample
N = 10_000


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tone_sc16(n, amp=8000, cycles_per_sample=0.01, clip_at=None):
    a = array.array("h")
    for k in range(n):
        ph = 2 * math.pi * cycles_per_sample * k
        i, q = int(amp * math.cos(ph)), int(amp * math.sin(ph))
        if clip_at is not None and k == clip_at:
            i = 32767
        a.extend((i, q))
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def base_manifest():
    return {
        "schema": "caelarch.capture/0.4",
        "capture_id": "syn-0001",
        "created_utc": "2026-11-07T18:00:00Z",
        "graph_version": "gate04-graph/0.1",
        "provenance": {"kind": "synthetic", "operator": "unit-test", "software": "synth.py"},
        "device": {"model": "simulated", "serial": None, "driver": "none",
                   "fpga_image_sha256": None},
        "clock": {"ref_source": "internal", "pps_source": "none", "ref_locked_at_start": True},
        "timebases": [{"id": "sdr0", "kind": "sdr_device"},
                      {"id": "wit0", "kind": "instrument"}],
        "timebase_alignments": [{"from": "wit0", "to": "sdr0", "offset_ns": 0,
                                 "uncertainty_ns": 500, "method": "shared_pps_trigger"}],
        "settling": {"switch_guard_ns": 50_000, "retune_guard_ns": 200_000, "gain_guard_ns": 20_000},
        "streams": [{
            "stream_id": "rx0", "channel": 0, "file": "rx0.sc16", "sha256": "",
            "format": "sc16_le", "sample_count": N, "sample_rate_hz": FS,
            "center_freq_hz": 915e6, "rx_gain_db": 30.0, "timebase": "sdr0", "t0_ns": 0,
            "clip_level": 32000, "carries": ["scene", "reference", "pilot"],
            "pilot": {"offset_hz": 100_000.0, "waveform_id": "cw-100k"},
        }],
        "events_file": "events.jsonl", "events_sha256": "",
        "witness_file": "witness.jsonl", "witness_sha256": "",
        "qualification_ref": None,
        "truth_commitment": None,
    }


def base_events():
    return [
        {"seq": 0, "type": "SWITCH_COMMAND", "timebase": "sdr0", "t_ns": 1_000_000, "port": "H"},
        {"seq": 1, "type": "SWITCH_COMMAND", "timebase": "sdr0", "t_ns": 5_000_000, "port": "R"},
        {"seq": 2, "type": "SETTLED", "timebase": "sdr0", "t_ns": 5_080_000,
         "stream_id": "rx0", "cause": "switch"},
    ]


def base_witness():
    return [
        {"seq": 0, "timebase": "wit0", "t_ns": 2_000_000, "instrument": "VNA-placeholder",
         "plane_cal_id": "cal-p0p1-a", "ratio_re": 0.5, "ratio_im": 0.1,
         "phase_std_rad": 0.01, "valid": True},
        {"seq": 1, "timebase": "wit0", "t_ns": 6_000_000, "instrument": "VNA-placeholder",
         "plane_cal_id": "cal-p0p1-a", "ratio_re": 0.5, "ratio_im": 0.1,
         "phase_std_rad": 0.01, "valid": True},
    ]


def write_capture(root, manifest=None, events=None, witness=None, iq=None, fix_hashes=True):
    """Write a capture to ``root``. Hashes are filled in unless fix_hashes=False."""
    os.makedirs(root, exist_ok=True)
    m = copy.deepcopy(manifest if manifest is not None else base_manifest())
    events = base_events() if events is None else events
    witness = base_witness() if witness is None else witness
    s0 = m["streams"][0]
    with open(os.path.join(root, s0["file"]), "wb") as f:
        f.write(iq if iq is not None else tone_sc16(s0["sample_count"]))
    with open(os.path.join(root, m["events_file"]), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    if m.get("witness_file"):
        with open(os.path.join(root, m["witness_file"]), "w") as f:
            for w in witness:
                f.write(json.dumps(w) + "\n")
    if fix_hashes:
        s0["sha256"] = _sha(os.path.join(root, s0["file"]))
        m["events_sha256"] = _sha(os.path.join(root, m["events_file"]))
        if m.get("witness_file"):
            m["witness_sha256"] = _sha(os.path.join(root, m["witness_file"]))
    with open(os.path.join(root, "manifest.json"), "w") as f:
        json.dump(m, f, indent=1)
    return m
