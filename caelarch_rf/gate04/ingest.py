"""Raw-IQ capture ingestion for online processing (Gate 0.4).

``load_capture(path)`` validates a capture directory against the contract and
returns an :class:`OnlineCapture`: the only object online estimators, monitors
and policy code are allowed to receive. It exposes raw IQ, the online event
timeline, witness records and *derived* accepted/rejected sample ranges. It has
no route to disturbance truth: this module never imports ``sealed_truth``, and
a capture directory containing any undeclared file is refused outright.

Fail-closed rules (each documented in MEASUREMENT_CONTRACT.md):
  * overflow with unknown dropped count -> every later sample is untimed;
  * events/witness on a timebase with no declared alignment to a stream are
    not ordered against that stream (switch/retune/gain -> ContractError);
  * reference unlock, LO unlock, switching/retune/gain settling -> rejected;
  * clipped or non-finite samples -> rejected block.
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
import stat
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple

from . import contract as C

BLOCK = 4096  # samples per clip-accounting block
NS = 10 ** 9


def _reject_constant(name):
    raise C.ContractError(f"non-finite JSON constant {name} is not allowed")


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise C.ContractError(f"duplicate JSON key {k!r}")
        out[k] = v
    return out


def _loads(text, where):
    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_dupes)
    except json.JSONDecodeError as e:
        raise C.ContractError(f"{where}: invalid JSON ({e})") from None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_jsonl(path, where):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if not line.strip():
                raise C.ContractError(f"{where}: blank line {i + 1}")
            out.append(_loads(line, f"{where} line {i + 1}"))
    return out


@dataclass(frozen=True)
class Rejection:
    start: int  # first rejected sample index
    stop: int   # one past last rejected sample index
    reason: str


@dataclass(frozen=True)
class WitnessLookup:
    record: Optional[dict]
    age_ns: Optional[int]          # conservative (upper) age at decision time
    reason: str                    # 'OK' or why no usable witness exists


@dataclass
class StreamView:
    """Online view of one raw IQ stream. Sample index is the primary time axis."""
    meta: dict
    path: str
    # Piecewise sample->time map: (first_index, t_ns at that index). Samples at
    # or beyond ``untimed_from`` have no trustworthy timestamp.
    segments: List[Tuple[int, Fraction]] = field(default_factory=list)
    untimed_from: Optional[int] = None
    rejections: List[Rejection] = field(default_factory=list)
    retune_indices: List[int] = field(default_factory=list)
    port_changes: List[Tuple[int, str]] = field(default_factory=list)
    pilot_off: List[Tuple[int, int]] = field(default_factory=list)
    clip_counts: List[int] = field(default_factory=list)  # per BLOCK

    @property
    def stream_id(self):
        return self.meta["stream_id"]

    @property
    def n(self):
        return self.meta["sample_count"]

    @property
    def timebase(self):
        return self.meta["timebase"]

    @property
    def fs(self):
        return Fraction(str(self.meta["sample_rate_hz"]))

    def time_of(self, idx) -> Optional[Fraction]:
        """Exact time (ns, stream timebase) of sample ``idx``; None if untimed."""
        if not 0 <= idx < self.n or (self.untimed_from is not None and idx >= self.untimed_from):
            return None
        seg = max((s for s in self.segments if s[0] <= idx), key=lambda s: s[0])
        return seg[1] + (idx - seg[0]) * NS / self.fs

    def timed_end(self):
        """One past the last sample with a trustworthy timestamp."""
        return self.untimed_from if self.untimed_from is not None else self.n

    def index_at(self, t_ns, side="ceil") -> Optional[int]:
        """First timed sample at/after (ceil) or last at/before (floor) ``t_ns``.

        Times inside a dropout gap resolve to the gap's boundary samples.
        Times at/after the end of the timed region resolve to ``timed_end()``
        (ceil) or ``timed_end() - 1`` (floor): such samples, if any, are
        untimed and already rejected. Only floor before the first sample
        returns None.
        """
        t = Fraction(t_ns)
        segs = sorted(self.segments)
        end = self.timed_end()
        bounds = [s[0] for s in segs[1:]] + [end]
        for (i0, t0), i_end in zip(segs, bounds):
            i_end = min(i_end, end)
            if i_end <= i0:
                continue
            if t < t0:  # before this segment: in a gap, or before the capture
                if side == "ceil":
                    return i0
                return i0 - 1 if i0 > 0 else None
            t_last = t0 + (i_end - 1 - i0) * NS / self.fs
            if t <= t_last:
                x = i0 + (t - t0) * self.fs / NS
                return math.ceil(x) if side == "ceil" else math.floor(x)
        return end if side == "ceil" else end - 1

    def rejected_mask_ranges(self):
        """Merged rejected ranges, sorted."""
        rs = sorted((r.start, r.stop) for r in self.rejections if r.stop > r.start)
        merged = []
        for a, b in rs:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def accepted_ranges(self):
        """Complement of rejected ranges: samples an online method may use."""
        out, cur = [], 0
        for a, b in self.rejected_mask_ranges():
            if a > cur:
                out.append((cur, a))
            cur = max(cur, b)
        if cur < self.n:
            out.append((cur, self.n))
        return out

    def port_at(self, idx):
        """Selector port commanded at sample idx ('UNKNOWN' before any command)."""
        port = "UNKNOWN"
        for i, p in self.port_changes:
            if i <= idx:
                port = p
        return port

    def read_iq(self, start, stop) -> List[complex]:
        """Raw complex samples [start, stop), scaled by nothing. Stdlib only."""
        if not 0 <= start <= stop <= self.n:
            raise IndexError("sample range outside capture")
        fmt = self.meta["format"]
        bps = C.FORMATS[fmt]["bytes_per_sample"]
        code = "h" if fmt == "sc16_le" else "f"
        a = array.array(code)
        with open(self.path, "rb") as f:
            f.seek(start * bps)
            a.frombytes(f.read((stop - start) * bps))
        if sys.byteorder != "little":
            a.byteswap()
        return [complex(a[2 * k], a[2 * k + 1]) for k in range(stop - start)]


class OnlineCapture:
    """Everything an online method may see. Contains no disturbance truth."""

    def __init__(self, root, manifest, streams, events, witness):
        self.root = root
        self.manifest = manifest
        self.streams = streams
        self.events = events
        self.witness = witness
        self._align = {}
        for a in manifest["timebase_alignments"]:
            self._align[(a["from"], a["to"])] = (a["offset_ns"], a["uncertainty_ns"])
            self._align[(a["to"], a["from"])] = (-a["offset_ns"], a["uncertainty_ns"])
        # A declared alignment was measured against a continuous clock. From the
        # first TIME_DISCONTINUITY on a timebase onward it is void (fail closed).
        self._disc = {}
        for ev in events:
            if ev["type"] == "TIME_DISCONTINUITY":
                tb = streams[ev["stream_id"]].timebase
                self._disc.setdefault(tb, ev["t_ns"])

    @property
    def synthetic(self):
        return self.manifest["provenance"]["kind"] == "synthetic"

    def has_alignment(self, src, dst) -> bool:
        return src == dst or (src, dst) in self._align

    def map_time(self, t_ns, src, dst) -> Optional[Tuple[int, int]]:
        """(t on dst, uncertainty ns), or None when no declared alignment exists
        or the alignment is void because either clock has jumped by then."""
        if src == dst:
            return t_ns, 0
        if (src, dst) not in self._align:
            return None
        if src in self._disc and t_ns >= self._disc[src]:
            return None
        off, unc = self._align[(src, dst)]
        if dst in self._disc and t_ns + off - unc >= self._disc[dst]:
            return None
        return t_ns + off, unc

    def events_until(self, t_ns, timebase):
        """Events that had certainly occurred by ``t_ns`` on ``timebase``.

        Events on timebases with no alignment to ``timebase`` are excluded:
        they cannot be causally ordered against the decision time.
        """
        out = []
        for ev in self.events:
            m = self.map_time(ev["t_ns"], ev["timebase"], timebase)
            if m is not None and m[0] + m[1] <= t_ns:
                out.append(ev)
        return out

    def latest_witness(self, t_ns, timebase, max_age_ns) -> WitnessLookup:
        """Most recent *valid* witness record usable at decision time ``t_ns``."""
        if not self.witness:
            return WitnessLookup(None, None, "NO_WITNESS_FILE")
        best, unaligned, void = None, False, False
        for rec in self.witness:
            m = self.map_time(rec["t_ns"], rec["timebase"], timebase)
            if m is None:
                if self.has_alignment(rec["timebase"], timebase):
                    void = True
                else:
                    unaligned = True
                continue
            t_w, unc = m
            if not rec["valid"] or t_w + unc > t_ns:
                continue  # invalid, or not certainly in the past
            age = t_ns - (t_w - unc)
            if best is None or age < best[1]:
                best = (rec, age)
        if best is None:
            reason = ("NO_TIMEBASE_ALIGNMENT" if unaligned else
                      "ALIGNMENT_VOID_AFTER_DISCONTINUITY" if void else "NO_VALID_PRIOR_WITNESS")
            return WitnessLookup(None, None, reason)
        if best[1] > max_age_ns:
            return WitnessLookup(best[0], best[1], "STALE")
        return WitnessLookup(best[0], best[1], "OK")


def _check_directory(root, declared):
    names = set(os.listdir(root))
    for name in names:
        st = os.lstat(os.path.join(root, name))
        if stat.S_ISLNK(st.st_mode):
            raise C.ContractError(f"capture contains symlink {name!r}; refused")
        if not stat.S_ISREG(st.st_mode):
            raise C.ContractError(f"capture contains non-regular entry {name!r}; refused")
    undeclared = names - set(declared) - {"manifest.json"}
    if undeclared:
        raise C.ContractError(
            f"capture contains undeclared file(s) {sorted(undeclared)}; sealed truth and "
            "scratch files must live outside the capture directory")
    missing = set(declared) - names
    if missing:
        raise C.ContractError(f"declared file(s) missing: {sorted(missing)}")


def _scan_iq(sv: StreamView):
    """Hash already checked; count clipped / non-finite samples per BLOCK."""
    fmt = sv.meta["format"]
    bps = C.FORMATS[fmt]["bytes_per_sample"]
    code = "h" if fmt == "sc16_le" else "f"
    clip = sv.meta["clip_level"]
    counts = []
    with open(sv.path, "rb") as f:
        while True:
            raw = f.read(BLOCK * bps)
            if not raw:
                break
            a = array.array(code)
            a.frombytes(raw)
            if sys.byteorder != "little":
                a.byteswap()
            c = 0
            for k in range(0, len(a), 2):
                i, q = a[k], a[k + 1]
                if not (math.isfinite(i) and math.isfinite(q)) or abs(i) >= clip or abs(q) >= clip:
                    c += 1
            counts.append(c)
    sv.clip_counts = counts
    for b, c in enumerate(counts):
        if c:
            sv.rejections.append(Rejection(b * BLOCK, min((b + 1) * BLOCK, sv.n), "CLIPPED_OR_NONFINITE"))


def _build_time_map(sv: StreamView, events):
    sv.segments = [(0, Fraction(sv.meta["t0_ns"]))]
    for ev in events:
        if ev.get("stream_id") != sv.stream_id or ev["type"] not in ("OVERFLOW", "TIME_DISCONTINUITY"):
            continue
        idx = ev["sample_index"]
        if idx <= sv.segments[-1][0]:
            raise C.ContractError(
                f"events seq={ev['seq']}: OVERFLOW/TIME_DISCONTINUITY for stream {sv.stream_id!r} "
                "must be at sample_index >= 1 and in increasing sample order")
        if idx >= sv.n or (sv.untimed_from is not None and idx >= sv.untimed_from):
            continue
        prev = sv.time_of(idx)
        if prev is None:
            continue
        if ev["type"] == "OVERFLOW":
            if ev.get("dropped_samples") is None:
                sv.untimed_from = idx
                sv.rejections.append(Rejection(idx, sv.n, "OVERFLOW_UNKNOWN_DROP_UNTIMED"))
                continue
            shift = Fraction(ev["dropped_samples"]) * NS / sv.fs
            reason = "OVERFLOW_BOUNDARY"
        else:
            if prev + ev["jump_ns"] < (sv.time_of(idx - 1) if idx else prev):
                sv.untimed_from = idx  # time went backwards: fail closed
                sv.rejections.append(Rejection(idx, sv.n, "TIME_REVERSAL_UNTIMED"))
                continue
            shift = Fraction(ev["jump_ns"])
            reason = "TIME_DISCONTINUITY_BOUNDARY"
        sv.segments.append((idx, prev + shift))
        # Samples straddling a timing boundary cannot be used for phase tracking.
        sv.rejections.append(Rejection(max(0, idx - 1), idx + 1, reason))


def _place(cap: OnlineCapture, sv: StreamView, ev, side):
    """Map an event time to a sample index of sv, widened by alignment uncertainty."""
    m = cap.map_time(ev["t_ns"], ev["timebase"], sv.timebase)
    if m is None:
        return None
    t, unc = m
    t_edge = t - unc if side == "start" else t + unc
    return sv.index_at(t_edge, "ceil")


def _derive(cap: OnlineCapture, sv: StreamView):
    settle = cap.manifest["settling"]
    guards = {"switch": settle["switch_guard_ns"], "retune": settle["retune_guard_ns"],
              "gain": settle["gain_guard_ns"]}
    cause_of = {"SWITCH_COMMAND": "switch", "LO_RETUNE": "retune", "GAIN_CHANGE": "gain"}

    def applies(ev):
        sid = ev.get("stream_id")
        return sid is None or sid == sv.stream_id

    def settled_after(ev, cause):
        """Time of the first SETTLED(cause) for this stream after ev, mapped."""
        for e2 in cap.events:
            if (e2["type"] == "SETTLED" and e2["cause"] == cause and e2["stream_id"] == sv.stream_id
                    and e2["seq"] > ev["seq"]):
                m = cap.map_time(e2["t_ns"], e2["timebase"], sv.timebase)
                if m is not None:
                    return m[0] + m[1]
            if e2["seq"] > ev["seq"] and e2["type"] == ev["type"] and applies(e2):
                return None  # superseded before settling was reported
        return None

    lo_unlocked_from = None
    ref_unlocked_from = None if cap.manifest["clock"]["ref_locked_at_start"] else 0
    pilot_off_from = None
    for ev in cap.events:
        if not applies(ev):
            continue
        typ = ev["type"]
        if typ in cause_of:
            cause = cause_of[typ]
            if typ == "SWITCH_COMMAND" and not set(sv.meta["carries"]) & {"scene", "reference"}:
                continue
            start = _place(cap, sv, ev, "start")
            if start is None and cap.has_alignment(ev["timebase"], sv.timebase):
                # Alignment voided by a clock discontinuity: the event cannot be
                # placed, so nothing from the discontinuity onward is trusted.
                d = min((e["sample_index"] for e in cap.events if e["type"] == "TIME_DISCONTINUITY"
                         and e["stream_id"] == sv.stream_id), default=0)
                sv.rejections.append(Rejection(d, sv.n, "ALIGNMENT_VOID_AFTER_DISCONTINUITY"))
                continue
            if start is None:
                raise C.ContractError(
                    f"{typ} seq={ev['seq']} on timebase {ev['timebase']!r} cannot be placed on stream "
                    f"{sv.stream_id!r} (timebase {sv.timebase!r}): declare a timebase alignment")
            m = cap.map_time(ev["t_ns"], ev["timebase"], sv.timebase)
            end_t = m[0] + m[1] + guards[cause]
            s_t = settled_after(ev, cause)
            if s_t is not None:
                end_t = max(end_t, s_t)  # device settling may extend, never shorten, the guard
            stop = sv.index_at(end_t, "ceil")
            sv.rejections.append(Rejection(start, max(stop, start), f"SETTLING_{cause.upper()}"))
            if typ == "SWITCH_COMMAND":
                sv.port_changes.append((start, ev["port"]))
            if typ == "LO_RETUNE":
                sv.retune_indices.append(start)
                if not ev["lo_locked"]:
                    lo_unlocked_from = start if lo_unlocked_from is None else lo_unlocked_from
                elif lo_unlocked_from is not None:
                    sv.rejections.append(Rejection(lo_unlocked_from, start, "LO_UNLOCKED"))
                    lo_unlocked_from = None
        elif typ == "REF_LOCK":
            idx = _place(cap, sv, ev, "start")
            if idx is None:
                # Cannot order a lock-state change against this stream: fail closed.
                sv.rejections.append(Rejection(0, sv.n, "REF_LOCK_UNORDERED"))
                continue
            if not ev["locked"] and ref_unlocked_from is None:
                ref_unlocked_from = idx
            elif ev["locked"] and ref_unlocked_from is not None:
                stop = _place(cap, sv, {**ev, "t_ns": ev["t_ns"]}, "end")
                sv.rejections.append(Rejection(ref_unlocked_from, stop, "REF_UNLOCKED"))
                ref_unlocked_from = None
        elif typ == "PILOT_STATE" and "pilot" in sv.meta["carries"]:
            idx = _place(cap, sv, ev, "start")
            if idx is None:
                sv.pilot_off.append((0, sv.n))
                continue
            if not ev["enabled"] and pilot_off_from is None:
                pilot_off_from = idx
            elif ev["enabled"] and pilot_off_from is not None:
                sv.pilot_off.append((pilot_off_from, _place(cap, sv, ev, "end")))
                pilot_off_from = None
    if lo_unlocked_from is not None:
        sv.rejections.append(Rejection(lo_unlocked_from, sv.n, "LO_UNLOCKED"))
    if ref_unlocked_from is not None:
        sv.rejections.append(Rejection(ref_unlocked_from, sv.n, "REF_UNLOCKED"))
    if pilot_off_from is not None:
        sv.pilot_off.append((pilot_off_from, sv.n))


def load_capture(root) -> OnlineCapture:
    root = os.path.abspath(root)
    mpath = os.path.join(root, "manifest.json")
    if not os.path.isfile(mpath) or os.path.islink(mpath):
        raise C.ContractError("capture has no regular manifest.json")
    with open(mpath, "r", encoding="utf-8") as f:
        manifest = C.validate_manifest(_loads(f.read(), "manifest.json"))

    declared = {s["file"]: s["sha256"] for s in manifest["streams"]}
    declared[manifest["events_file"]] = manifest["events_sha256"]
    if manifest.get("witness_file"):
        declared[manifest["witness_file"]] = manifest["witness_sha256"]
    _check_directory(root, declared)
    for name, digest in declared.items():
        if _sha256(os.path.join(root, name)) != digest:
            raise C.ContractError(f"{name}: sha256 mismatch with manifest")

    tb_ids = {tb["id"] for tb in manifest["timebases"]}
    sids = {s["stream_id"] for s in manifest["streams"]}
    events = _read_jsonl(os.path.join(root, manifest["events_file"]), "events")
    for i, ev in enumerate(events):
        C.validate_event(i, ev, sids, tb_ids)
    C.check_ordering(events, "events")
    witness = []
    if manifest.get("witness_file"):
        witness = _read_jsonl(os.path.join(root, manifest["witness_file"]), "witness")
        for i, rec in enumerate(witness):
            C.validate_witness(i, rec, tb_ids)
        C.check_ordering(witness, "witness")

    streams = {}
    for s in manifest["streams"]:
        path = os.path.join(root, s["file"])
        expect = s["sample_count"] * C.FORMATS[s["format"]]["bytes_per_sample"]
        if os.path.getsize(path) != expect:
            raise C.ContractError(f"{s['file']}: size {os.path.getsize(path)} != "
                                  f"sample_count*bytes_per_sample = {expect}")
        streams[s["stream_id"]] = StreamView(meta=s, path=path)

    cap = OnlineCapture(root, manifest, streams, events, witness)
    for sv in streams.values():
        _build_time_map(sv, events)
        _scan_iq(sv)
        _derive(cap, sv)
    return cap
