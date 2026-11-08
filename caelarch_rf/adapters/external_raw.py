"""Convert a headerless raw I/Q recording into a contract-conformant capture.

Supported source formats (interleaved I,Q):
  cu8   unsigned 8-bit (RTL-SDR). Zero is 127.5, so samples map losslessly to
        int16 as (2v - 255) * 128: no artificial DC is introduced. Rails 0/255
        map to -/+32640, so clip_level = 32640 flags exactly the ADC rails.
  cs8   signed 8-bit (HackRF-style). v * 256; rails -128/127 -> clip 32512.
  cs16  signed 16-bit little-endian. Copied unchanged; clip_level 32767
        (the true ADC bit depth behind the file is unknown).

What the adapter refuses to invent:
  * gain (rx_gain_db = null), absolute time (t0_ns = 0 on a relative device
    timebase), serial, events, witness, pilot, reference tone, disturbance
    truth (truth_commitment = null);
  * a partial trailing sample: a file whose length is not a whole number of
    I/Q pairs is refused, never silently truncated.

Provenance is ``external_recording``: real I/Q from third-party hardware. It
cannot support any Caelarch hardware claim.
"""
from __future__ import annotations

import array
import hashlib
import json
import os
import re
import sys

from caelarch_rf.gate04 import contract as C

SOURCE_FORMATS = {"cu8": (2, 32640), "cs8": (2, 32512), "cs16": (4, 32767)}  # bytes/pair, clip level
ADAPTER_VERSION = "external_raw 0.1"
_RTL433 = re.compile(r"_([0-9.]+)M_([0-9.]+)k\.(cu8|cs8|cs16)$")


def parse_rtl433_name(name):
    """rtl_433_tests naming: ..._<MHz>M_<kS/s>k.<fmt> -> (center_hz, rate_hz, fmt)."""
    m = _RTL433.search(name)
    if not m:
        raise ValueError(f"{name!r}: frequency/sample rate not encoded in the file name")
    return float(m.group(1)) * 1e6, float(m.group(2)) * 1e3, m.group(3)


def to_sc16(data: bytes, fmt: str) -> bytes:
    bpp, _ = SOURCE_FORMATS[fmt]
    if len(data) == 0 or len(data) % bpp:
        raise ValueError(f"{len(data)} bytes is not a whole number of {fmt} I/Q pairs; refusing to truncate")
    if fmt == "cs16":
        return data
    out = array.array("h")
    if fmt == "cu8":
        out.extend((2 * v - 255) * 128 for v in data)
    else:  # cs8
        src = array.array("b")
        src.frombytes(data)
        out.extend(v * 256 for v in src)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def convert(src_path, capture_dir, center_hz, rate_hz, fmt, created_utc, model):
    """Write a capture directory; return (manifest, adapter_record).

    The adapter record (source path, source sha256, conversion) is returned to
    the caller and must be stored OUTSIDE the capture directory: the loader
    refuses undeclared files.
    """
    with open(src_path, "rb") as f:
        raw = f.read()
    src_sha = hashlib.sha256(raw).hexdigest()
    sc16 = to_sc16(raw, fmt)
    os.makedirs(capture_dir, exist_ok=False)
    with open(os.path.join(capture_dir, "rx0.sc16"), "wb") as f:
        f.write(sc16)
    with open(os.path.join(capture_dir, "events.jsonl"), "wb") as f:
        pass
    manifest = {
        "schema": C.SCHEMA_MANIFEST, "capture_id": "ext-" + src_sha[:20], "created_utc": created_utc,
        "graph_version": C.GRAPH_VERSION,
        "provenance": {"kind": "external_recording", "operator": "public dataset",
                       "software": ADAPTER_VERSION},
        "device": {"model": model, "serial": None, "driver": "unknown (public recording)",
                   "fpga_image_sha256": None},
        "clock": {"ref_source": "internal", "pps_source": "none", "ref_locked_at_start": True},
        "timebases": [{"id": "sdr0", "kind": "sdr_device"}], "timebase_alignments": [],
        "settling": {"switch_guard_ns": 0, "retune_guard_ns": 0, "gain_guard_ns": 0},
        "streams": [{
            "stream_id": "rx0", "channel": 0, "file": "rx0.sc16",
            "sha256": hashlib.sha256(sc16).hexdigest(), "format": "sc16_le",
            "sample_count": len(sc16) // 4, "sample_rate_hz": rate_hz, "center_freq_hz": center_hz,
            "rx_gain_db": None, "timebase": "sdr0", "t0_ns": 0,
            "clip_level": SOURCE_FORMATS[fmt][1], "carries": ["scene"], "pilot": None,
        }],
        "events_file": "events.jsonl", "events_sha256": hashlib.sha256(b"").hexdigest(),
        "witness_file": None, "witness_sha256": None,
        "qualification_ref": None, "truth_commitment": None,
    }
    C.validate_manifest(manifest)
    with open(os.path.join(capture_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    record = {"capture_id": manifest["capture_id"], "source": src_path, "source_sha256": src_sha,
              "source_format": fmt, "conversion": {"cu8": "(2v-255)*128", "cs8": "v*256",
                                                   "cs16": "copy"}[fmt],
              "center_hz": center_hz, "rate_hz": rate_hz}
    return manifest, record
