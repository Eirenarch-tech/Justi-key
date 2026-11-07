"""CAE-RF Gate 0.4 measurement contract: schema and validator.

Research software. It validates the *format* of a capture; it does not
qualify an instrument, a phase-transfer plane, or a monitor.

Design rules enforced here (see ../docs/MEASUREMENT_CONTRACT.md):

* Strict allowlists. Unknown keys, unknown event types and free-text fields
  are rejected, because each is a channel through which disturbance truth
  could leak into online processing.
* Time is integer nanoseconds on a *named* timebase. Two timebases are never
  compared unless the manifest declares an alignment between them.
* Disturbance truth never appears in a capture. The manifest may carry only a
  salted commitment digest; the truth file lives elsewhere and is opened only
  by the offline scorer (``sealed_truth.py``, which this module never imports).

Stdlib only, so the validator can run on a bench host without numpy.
"""
from __future__ import annotations

import math
import re

SCHEMA_MANIFEST = "caelarch.capture/0.4"
SCHEMA_TRUTH = "caelarch.sealed_truth/0.4"
GRAPH_VERSION = "gate04-graph/0.1"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")
# Short descriptive labels (model names, driver versions). Deliberately
# narrow: no free prose, so a label cannot smuggle an event schedule.
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.,:+/()-]{0,79}$")

# Substrings that never belong in an online capture's identifiers or file
# names. The allowlist is the real guard; this catches truth smuggled into
# values that the allowlist permits (ids, file names, labels).
TRUTH_MARKERS = ("truth", "sealed", "oracle", "ground_truth", "groundtruth",
                 "label", "perturb", "injected_fault", "answer")

FORMATS = {
    # UHD rx_samples_to_file "short": interleaved little-endian int16 I,Q.
    "sc16_le": {"bytes_per_sample": 4},
    # UHD "float": interleaved little-endian float32 I,Q.
    "fc32_le": {"bytes_per_sample": 8},
}
CARRIES = ("scene", "reference", "pilot", "analog_monitor")
SELECTOR_PORTS = ("H", "R", "THRU", "TERM")
CLOCK_REF = ("internal", "external_10mhz", "gpsdo")
PPS_SOURCE = ("none", "external", "gpsdo")
TIMEBASE_KINDS = ("sdr_device", "instrument", "controller")
PROVENANCE_KINDS = ("synthetic", "bench_capture")

# Event types an SDR host/scheduler legitimately knows online. Anything a
# blind perturbation controller does is deliberately absent.
EVENT_FIELDS = {
    "LO_RETUNE": {"stream_id": "id", "new_center_freq_hz": "pos", "lo_locked": "bool"},
    "GAIN_CHANGE": {"stream_id": "id", "new_rx_gain_db": "num"},
    "SWITCH_COMMAND": {"port": SELECTOR_PORTS},
    "SETTLED": {"stream_id": "id", "cause": ("switch", "retune", "gain")},
    "OVERFLOW": {"stream_id": "id", "sample_index": "nat", "dropped_samples": "nat?"},
    "TIME_DISCONTINUITY": {"stream_id": "id", "sample_index": "nat", "jump_ns": "int"},
    "REF_LOCK": {"locked": "bool"},
    "PILOT_STATE": {"enabled": "bool"},
}
EVENT_COMMON = {"seq": "nat", "type": tuple(EVENT_FIELDS), "timebase": "id", "t_ns": "int"}

WITNESS_FIELDS = {
    "seq": "nat", "timebase": "id", "t_ns": "int", "instrument": "label",
    "plane_cal_id": "id", "ratio_re": "num", "ratio_im": "num",
    "phase_std_rad": "nonneg", "valid": "bool",
}

MANIFEST_FIELDS = {
    "schema": (SCHEMA_MANIFEST,),
    "capture_id": "id",
    "created_utc": "utc",
    "graph_version": (GRAPH_VERSION,),
    "provenance": {"kind": PROVENANCE_KINDS, "operator": "label", "software": "label"},
    "device": {"model": "label", "serial": "label?", "driver": "label",
               "fpga_image_sha256": "hex64?"},
    "clock": {"ref_source": CLOCK_REF, "pps_source": PPS_SOURCE,
              "ref_locked_at_start": "bool"},
    "timebases": "timebases",
    "timebase_alignments": "alignments",
    "settling": {"switch_guard_ns": "nat", "retune_guard_ns": "nat", "gain_guard_ns": "nat"},
    "streams": "streams",
    "events_file": "file", "events_sha256": "hex64",
    "witness_file": "file?", "witness_sha256": "hex64?",
    "qualification_ref": "qualref?",
    "truth_commitment": "commitment?",
}
STREAM_FIELDS = {
    "stream_id": "id", "channel": "nat", "file": "file", "sha256": "hex64",
    "format": tuple(FORMATS), "sample_count": "pos_int", "sample_rate_hz": "pos",
    "center_freq_hz": "pos", "rx_gain_db": "num", "timebase": "id", "t0_ns": "int",
    "clip_level": "pos", "carries": "carries", "pilot": "pilot?", "monitor": "monitor?",
    "reference_tone": "reftone?",
}
# Known RF source tone (operating metadata), offset from the initial
# center_freq_hz. Lets an online method measure the switched reference.
REFTONE_FIELDS = {"offset_hz": "num"}
# Analog quadrature detector output logged as its own stream (Sprint 1c).
MONITOR_FIELDS = {"kind": ("analog_quadrature",), "reference": ("independent", "pilot_source"),
                  "detector_bandwidth_hz": "pos", "tap_plane": ("P4",)}
PILOT_FIELDS = {"offset_hz": "num", "waveform_id": "id"}
# ``valid_from_ns`` (on the ``to`` timebase) starts a requalified alignment
# epoch, e.g. after a TIME_DISCONTINUITY voided the previous one.
ALIGN_FIELDS = {"from": "id", "to": "id", "offset_ns": "int", "uncertainty_ns": "nat",
                "method": ("shared_pps_trigger", "loopback_xcorr", "manual_bench"),
                "valid_from_ns": "int?"}
QUALREF_FIELDS = {"record_id": "id", "record_sha256": "hex64"}
COMMITMENT_FIELDS = {"scheme": ("sha256-salted-v1",), "digest": "hex64"}


class ContractError(ValueError):
    """The capture violates the measurement contract. Never auto-repaired."""


def _fail(path, msg):
    raise ContractError(f"{path}: {msg}")


def _check_markers(path, value):
    low = value.lower()
    for m in TRUTH_MARKERS:
        if m in low:
            _fail(path, f"value {value!r} contains forbidden marker {m!r} "
                        "(disturbance truth must stay outside the capture)")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_value(path, spec, v):
    if isinstance(spec, tuple):
        if v not in spec:
            _fail(path, f"{v!r} not in {spec}")
        return
    if isinstance(spec, dict):
        _check_obj(path, spec, v)
        return
    optional = spec.endswith("?")
    kind = spec.rstrip("?")
    if v is None:
        if optional:
            return
        _fail(path, "required value is null")
    if kind == "id":
        if not isinstance(v, str) or not _ID.match(v):
            _fail(path, f"invalid identifier {v!r}")
        _check_markers(path, v)
    elif kind == "file":
        if not isinstance(v, str) or not _FILE.match(v) or v in (".", ".."):
            _fail(path, f"invalid file name {v!r} (plain names only, no paths)")
        _check_markers(path, v)
    elif kind == "label":
        if not isinstance(v, str) or not _LABEL.match(v):
            _fail(path, f"invalid label {v!r}")
        _check_markers(path, v)
    elif kind == "hex64":
        if not isinstance(v, str) or not _HEX64.match(v):
            _fail(path, "expected 64 lowercase hex characters")
    elif kind == "utc":
        if not isinstance(v, str) or not _UTC.match(v):
            _fail(path, "expected UTC timestamp like 2026-11-07T18:00:00Z")
    elif kind == "bool":
        if not isinstance(v, bool):
            _fail(path, "expected boolean")
    elif kind in ("int", "nat", "pos_int"):
        if not isinstance(v, int) or isinstance(v, bool):
            _fail(path, "expected integer")
        if kind == "nat" and v < 0 or kind == "pos_int" and v <= 0:
            _fail(path, "integer out of range")
    elif kind in ("num", "pos", "nonneg"):
        if not _is_num(v):
            _fail(path, "expected finite number")
        if kind == "pos" and v <= 0 or kind == "nonneg" and v < 0:
            _fail(path, "number out of range")
    elif kind == "carries":
        if (not isinstance(v, list) or not v or len(set(v)) != len(v)
                or any(c not in CARRIES for c in v)):
            _fail(path, f"expected non-empty unique subset of {CARRIES}")
    elif kind == "pilot":
        _check_obj(path, PILOT_FIELDS, v)
    elif kind == "monitor":
        _check_obj(path, MONITOR_FIELDS, v)
    elif kind == "reftone":
        _check_obj(path, REFTONE_FIELDS, v)
    elif kind == "qualref":
        _check_obj(path, QUALREF_FIELDS, v)
    elif kind == "commitment":
        _check_obj(path, COMMITMENT_FIELDS, v)
    elif kind == "timebases":
        if not isinstance(v, list) or not v:
            _fail(path, "expected non-empty list")
        for i, tb in enumerate(v):
            _check_obj(f"{path}[{i}]", {"id": "id", "kind": TIMEBASE_KINDS}, tb)
    elif kind == "alignments":
        if not isinstance(v, list):
            _fail(path, "expected list")
        for i, a in enumerate(v):
            _check_obj(f"{path}[{i}]", ALIGN_FIELDS, a)
    elif kind == "streams":
        if not isinstance(v, list) or not v:
            _fail(path, "expected non-empty list")
        for i, s in enumerate(v):
            _check_obj(f"{path}[{i}]", STREAM_FIELDS, s)
    else:  # pragma: no cover - programming error in the schema tables
        raise AssertionError(spec)


def _check_obj(path, fields, obj):
    if not isinstance(obj, dict):
        _fail(path, "expected object")
    unknown = set(obj) - set(fields)
    if unknown:
        _fail(path, f"unknown field(s) {sorted(unknown)}; the contract is an allowlist")
    for key, spec in fields.items():
        optional = isinstance(spec, str) and spec.endswith("?")
        if key not in obj:
            if optional:
                continue
            _fail(path, f"missing required field {key!r}")
        _check_value(f"{path}.{key}", spec, obj[key])


def validate_manifest(m):
    """Validate a parsed manifest. Returns it unchanged or raises ContractError."""
    if isinstance(m, dict) and m.get("schema") == SCHEMA_TRUTH:
        _fail("manifest", "this is a sealed-truth file; online ingestion must never open it")
    _check_obj("manifest", MANIFEST_FIELDS, m)

    tb_ids = [tb["id"] for tb in m["timebases"]]
    if len(set(tb_ids)) != len(tb_ids):
        _fail("manifest.timebases", "duplicate timebase id")
    epochs = set()
    for i, a in enumerate(m["timebase_alignments"]):
        p = f"manifest.timebase_alignments[{i}]"
        if a["from"] not in tb_ids or a["to"] not in tb_ids or a["from"] == a["to"]:
            _fail(p, "alignment must join two distinct declared timebases")
        key = (frozenset((a["from"], a["to"])), a.get("valid_from_ns"))
        if key in epochs:
            _fail(p, "duplicate alignment epoch for this timebase pair")
        epochs.add(key)

    sids, files = set(), {m["events_file"]}
    if (m.get("witness_file") is None) != (m.get("witness_sha256") is None):
        _fail("manifest", "witness_file and witness_sha256 must be given together")
    if m.get("witness_file"):
        if m["witness_file"] in files:
            _fail("manifest.witness_file", "file declared twice")
        files.add(m["witness_file"])
    for i, s in enumerate(m["streams"]):
        p = f"manifest.streams[{i}]"
        if s["stream_id"] in sids:
            _fail(p, "duplicate stream_id")
        sids.add(s["stream_id"])
        if s["file"] in files:
            _fail(p, "file declared twice")
        files.add(s["file"])
        if s["timebase"] not in tb_ids:
            _fail(p, f"unknown timebase {s['timebase']!r}")
        if ("pilot" in s["carries"]) != (s.get("pilot") is not None):
            _fail(p, "a stream carries a pilot iff it declares the pilot block")
        if ("analog_monitor" in s["carries"]) != (s.get("monitor") is not None):
            _fail(p, "a stream carries analog_monitor iff it declares the monitor block")
        if "analog_monitor" in s["carries"] and len(s["carries"]) != 1:
            _fail(p, "an analog monitor stream carries nothing else")
        if s.get("reference_tone") is not None:
            if not set(s["carries"]) & {"scene", "reference"}:
                _fail(p, "reference_tone needs a stream carrying scene/reference")
            if abs(s["reference_tone"]["offset_hz"]) >= s["sample_rate_hz"] / 2:
                _fail(p, "reference tone outside the stream's Nyquist band")
        if s.get("pilot") is not None and abs(s["pilot"]["offset_hz"]) >= s["sample_rate_hz"] / 2:
            _fail(p, "pilot offset outside the stream's Nyquist band")
        if s["format"] == "sc16_le" and not s["clip_level"] <= 32767:
            _fail(p, "sc16 clip_level cannot exceed 32767")
    if "manifest.json" in files:
        _fail("manifest", "manifest.json cannot be declared as a data file")
    return m


def validate_event(i, ev, stream_ids, timebase_ids):
    p = f"events[{i}]"
    if not isinstance(ev, dict) or ev.get("type") not in EVENT_FIELDS:
        _fail(p, f"unknown event type {ev.get('type') if isinstance(ev, dict) else ev!r}; "
                 "online events are an allowlist (perturbation commands belong in sealed truth)")
    _check_obj(p, {**EVENT_COMMON, **EVENT_FIELDS[ev["type"]]}, ev)
    if ev["timebase"] not in timebase_ids:
        _fail(p, f"unknown timebase {ev['timebase']!r}")
    if "stream_id" in ev and ev["stream_id"] not in stream_ids:
        _fail(p, f"unknown stream_id {ev['stream_id']!r}")
    return ev


def validate_witness(i, rec, timebase_ids):
    p = f"witness[{i}]"
    _check_obj(p, WITNESS_FIELDS, rec)
    if rec["timebase"] not in timebase_ids:
        _fail(p, f"unknown timebase {rec['timebase']!r}")
    return rec


def check_ordering(records, path):
    """seq strictly increasing; t_ns non-decreasing within each timebase."""
    last_seq, last_t = -1, {}
    for i, r in enumerate(records):
        if r["seq"] <= last_seq:
            _fail(f"{path}[{i}]", "seq must be strictly increasing")
        last_seq = r["seq"]
        tb = r["timebase"]
        if tb in last_t and r["t_ns"] < last_t[tb]:
            _fail(f"{path}[{i}]", f"time went backwards on timebase {tb!r}")
        last_t[tb] = r["t_ns"]
