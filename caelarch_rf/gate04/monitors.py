"""Online monitors: sampled pilot (1b), analog detector (1c), reference bracket.

Every function takes only an :class:`OnlineCapture`. Outputs are causal
:class:`PilotObservation` records, coverage intervals and *unattributed*
excursions. Nothing here has access to truth.

Sampled / analog pipeline (per block of accepted, timed samples):

1. **Mix down** with an NCO driven by *device timestamps* (sampled pilot only;
   the analog detector output is already at baseband). NCO frequency follows
   ``LO_RETUNE`` events: f_bb = pilot RF - current LO.
2. **Frequency acquisition, then tracking.** ACQUIRING: full-gain running mean
   of the slope. Tracking: gain ``freq_alpha``, lagged one block so the first
   half of a straddling step never trains the tracker.
3. **Estimate**: Hann-weighted block mean (rejects the scene tone); two
   half-block estimates give a noise variance.
4. **Track**: causal prediction, wrapped innovation, unwrapping only inside a
   continuity segment.
5. **Detect** (never attribute): only in state TRACKING, |innovation| >
   k*sigma or two consecutive innovations > k*sigma*sqrt(2). sigma is a 3-sigma
   winsorised EMA. Each TRACKING record carries the floor k*sigma that was
   known *at its availability time*.

Readiness (review finding, Sprint 1d): the monitor is
ACQUIRING -> WARMING -> TRACKING. After a gap it is REACQUIRING for at least
one block (no innovation, so no detection). A gap whose cause can change the
pilot's frequency, phase reference or amplitude (retune, LO/reference unlock,
gain change, clipping, timestamp discontinuity, pilot off/lost, untimed data,
anything unrecognised) forces full **requalification**: frequency, noise and
amplitude estimates are discarded and the monitor re-acquires from scratch.
Only a known-count overflow boundary is treated as benign.

No-fabrication rules: any unobserved interval ends the continuity segment; a
change beyond ``unwrap_limit_rad`` in one block is an *ambiguous* excursion
and ends the segment; an untimed tail ends processing.

``window_state`` takes an explicit decision time: evidence counts only if its
``available_ns`` is at or before that time.
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .observability import SENSORS
from .records import PilotObservation
from .states import PublicState

# Rejections that mean the monitor itself is faulty or absent -> INVALID.
FAULT_REASONS = ("CLIPPED_OR_NONFINITE", "OVERFLOW_UNKNOWN_DROP_UNTIMED", "TIME_REVERSAL_UNTIMED",
                 "UNTIMED", "REF_UNLOCKED", "LO_UNLOCKED", "PILOT_OFF", "PILOT_LOST",
                 "REF_LOCK_UNORDERED", "ALIGNMENT_VOID_AFTER_DISCONTINUITY")
# window_state reason prefix for a detected excursion (consumers must match the
# prefix, never a substring: other reasons may mention excursions too).
EXCURSION_PREFIX = "unattributed excursion"
# Gaps after which learned frequency/noise/amplitude may still be trusted.
BENIGN_GAP_REASONS = ("OVERFLOW_BOUNDARY",)


@dataclass(frozen=True)
class MonitorParams:
    block_samples: int = 100
    latency_ns: int = 50_000          # declared processing latency after the window ends
    k_sigma: float = 6.0              # detection threshold on the innovation
    acquisition_innovations: int = 4  # frequency acquisition (full-gain running mean)
    warmup_innovations: int = 8       # then this many before any detection is allowed
    unwrap_limit_rad: float = math.pi / 2
    freq_alpha: float = 0.2           # residual-frequency tracker gain
    sigma_alpha: float = 0.05         # innovation-variance EMA gain
    amp_alpha: float = 0.1
    min_amp_ratio: float = 0.5        # pilot considered lost below this x running amplitude
    refreq_after: int = 3             # consecutive excursions treated as a frequency change
    # The pilot is injected downstream of the selector, so selector settling does
    # not invalidate pilot samples. This rests on the planned physical ordering
    # (architecture plane P3) and must be confirmed on the bench (risk R14).
    ignore_reasons: Tuple[str, ...] = ("SETTLING_SWITCH",)


@dataclass(frozen=True)
class BracketParams:
    """Periodic switched-reference baseline (Gate 0.3 style, endpoint comparison).

    ``drift_allowance_rad_per_rts`` is a DECLARED, unqualified assumption about
    receiver phase random-walk between reference dwells; the threshold is
    k * sqrt(measurement variance + (allowance * sqrt(dt))^2). It is not learned
    from the capture, so the baseline is ready from its second dwell.
    """
    k_sigma: float = 6.0
    drift_allowance_rad_per_rts: float = 1.0
    min_samples: int = 200
    latency_ns: int = 50_000
    benign_between: Tuple[str, ...] = ("SETTLING_SWITCH", "OVERFLOW_BOUNDARY")


@dataclass(frozen=True)
class Interval:
    start_ns: int
    end_ns: Optional[int]   # None: open-ended (untimed tail)
    timebase: str
    status: str             # MONITORED | NOT_READY | UNMONITORED | BRACKET_ONLY
    reason: str


@dataclass(frozen=True)
class Excursion:
    available_ns: int
    window_start_ns: int
    window_end_ns: int
    timebase: str
    innovation_rad: float
    sigma_rad: float
    continuity_id: int
    ambiguous_2pi: bool                 # change exceeded the unwrap limit
    candidates: Tuple[str, ...]         # every term that moves this sensor
    attribution: str = "UNATTRIBUTED"


@dataclass
class MonitorResult:
    sensor: str                 # observation-graph sensor name
    kind: str                   # sampled_digital | analog_quadrature | reference_bracket
    stream_id: str
    timebase: str
    sample_period_ns: float
    observations: List[PilotObservation] = field(default_factory=list)
    coverage: List[Interval] = field(default_factory=list)
    excursions: List[Excursion] = field(default_factory=list)
    # Capture-wide RETROSPECTIVE summary for offline reports. Never an online
    # threshold: online decisions use each record's causal detection_floor_rad.
    envelope: Dict[str, object] = field(default_factory=dict)
    limitations: List[str] = field(default_factory=list)

    def usable(self):
        return [o for o in self.observations if o.usable]

    def tracking(self):
        return [o for o in self.observations if o.usable and o.monitor_state == "TRACKING"]

    def unmonitored(self):
        return [c for c in self.coverage if c.status == "UNMONITORED"]


def _wrap(x):
    return (x + math.pi) % (2 * math.pi) - math.pi


def _hann(n):
    return [0.5 - 0.5 * math.cos(2 * math.pi * (k + 0.5) / n) for k in range(n)]


def _block_status(sv, i0, i1, ignore):
    """None if every sample in [i0, i1) is usable, else the first blocking reason."""
    for r in sorted(sv.rejections, key=lambda r: r.start):
        if r.reason in ignore or r.stop <= i0 or r.start >= i1 or r.stop <= r.start:
            continue
        return r.reason
    for a, b in sv.pilot_off:
        if a < i1 and b > i0:
            return "PILOT_OFF"
    return None


class _Tracker:
    def __init__(self, p: MonitorParams, sensor: str, timebase: str, kind: str, bw):
        self.p, self.sensor, self.tb, self.kind, self.bw = p, sensor, timebase, kind, bw
        self.cont = -1
        self.requalify()

    def requalify(self):
        """Discard everything learned: frequency, noise, amplitude, readiness."""
        self.f_hat = 0.0
        self.sigma2: Optional[float] = None
        self.warm: List[float] = []
        self.n_innov = 0
        self.n_acq = 0
        self.amp: Optional[float] = None
        self.noise2: Optional[float] = None
        self.gap()

    def gap(self):
        """End the continuity segment; keep learned state (benign gaps only)."""
        self.prev = None
        self.run = 0
        self.last_innov = None
        self.pending_f = 0.0

    def ready(self):
        return (self.n_acq >= self.p.acquisition_innovations and self.sigma2 is not None
                and self.n_innov >= self.p.warmup_innovations)

    def block(self, x, t_rel, nco, t_abs_start, t_abs_end, offset_hz, res: MonitorResult):
        p = self.p
        B = len(x)
        w, h = _hann(B), _hann(B // 2)
        tc = sum(t_rel) / B
        rot = [x[k] * cmath.exp(-1j * (nco[k] + 2 * math.pi * self.f_hat * (t_rel[k] - tc) * 1e-9))
               for k in range(B)]
        z = sum(wk * r for wk, r in zip(w, rot)) / sum(w)
        z1 = sum(hk * r for hk, r in zip(h, rot[:B // 2])) / sum(h)
        z2 = sum(hk * r for hk, r in zip(h, rot[B // 2:B // 2 * 2])) / sum(h)
        v = abs(z1 - z2) ** 2 / 4
        self.noise2 = v if self.noise2 is None else self.noise2 + p.sigma_alpha * (v - self.noise2)
        amp = abs(z)
        available = t_abs_end + p.latency_ns
        common = dict(stream_id=res.stream_id, monitor_kind=self.kind, timebase=self.tb,
                      window_start_ns=t_abs_start, window_end_ns=t_abs_end, available_ns=available,
                      offset_hz=offset_hz + self.f_hat, detector_bandwidth_hz=self.bw,
                      freq_offset_hz=self.f_hat)
        if self.amp is not None and amp < p.min_amp_ratio * self.amp:
            self.requalify()
            res.observations.append(PilotObservation(
                phase_rad=None, unwrapped_phase_rad=None, phase_std_rad=None, snr_db=None,
                clipped=False, gap_in_window=True, pilot_enabled=True, monitor_state="UNMONITORED",
                reason="PILOT_LOST", **common))
            return "PILOT_LOST"
        self.amp = amp if self.amp is None else self.amp + p.amp_alpha * (amp - self.amp)
        phase = _wrap(cmath.phase(z))   # derotation centred on tc: phase vs NCO at tc
        std = math.sqrt(self.noise2 / 2) / amp if amp > 0 else None
        snr = 10 * math.log10(amp ** 2 / self.noise2) if self.noise2 > 0 and amp > 0 else None
        innov, exc, floor = None, None, None
        cands = tuple(sorted(SENSORS[self.sensor].terms))
        if self.prev is None:
            self.cont += 1
            unwrapped = phase
            state = "REACQUIRING" if self.ready() else "ACQUIRING"
        else:
            t_prev, ph_prev, un_prev = self.prev
            dt = (tc - t_prev) * 1e-9
            pred = ph_prev + 2 * math.pi * self.f_hat * dt
            innov = _wrap(phase - pred)
            ready = self.ready()
            sig = math.sqrt(self.sigma2) if self.sigma2 else None
            if abs(innov) > p.unwrap_limit_rad:
                # Ambiguous by 2*pi: reported in any state (needs no sigma); new segment.
                exc = Excursion(available, t_abs_start, t_abs_end, self.tb, innov,
                                sig if sig is not None else float("nan"), self.cont, True, cands)
                self.cont += 1
                unwrapped = phase
                self.run, self.last_innov, self.pending_f = 0, None, 0.0
                state = "TRACKING" if ready else ("ACQUIRING" if self.n_acq < p.acquisition_innovations
                                                  else "WARMING")
                floor = p.k_sigma * sig if ready else None
            elif self.n_acq < p.acquisition_innovations:
                # Innovations here are frequency error, not noise: never seed sigma or alarm.
                unwrapped = un_prev + (pred - ph_prev) + innov
                self.n_acq += 1
                self.f_hat += innov / (2 * math.pi * dt) / self.n_acq
                state = "ACQUIRING"
            else:
                unwrapped = un_prev + (pred - ph_prev) + innov
                pair = innov + self.last_innov if self.last_innov is not None else None
                single = ready and abs(innov) > p.k_sigma * sig
                double = ready and pair is not None and abs(pair) > p.k_sigma * sig * math.sqrt(2)
                state = "TRACKING" if ready else "WARMING"
                floor = p.k_sigma * sig if ready else None
                if single or double:
                    exc = Excursion(available, t_abs_start, t_abs_end, self.tb, innov if single else pair,
                                    sig, self.cont, False, cands)
                    self.run += 1
                    self.last_innov, self.pending_f = None, 0.0
                    if self.run >= p.refreq_after:   # persistent: a frequency change, re-acquire
                        self.f_hat += p.freq_alpha * innov / (2 * math.pi * dt)
                else:
                    self.run = 0
                    self.last_innov = innov
                    self.f_hat += self.pending_f        # commit the previous block's update
                    self.pending_f = p.freq_alpha * innov / (2 * math.pi * dt)
                    self.n_innov += 1
                    if self.sigma2 is None:
                        self.warm.append(innov * innov)
                        if len(self.warm) >= p.warmup_innovations:
                            self.sigma2 = max(sum(self.warm) / len(self.warm), 2 * (std or 0) ** 2)
                    else:
                        # Winsorised: sub-threshold parts of an excursion must not
                        # inflate the floor ahead of the rest of it.
                        self.sigma2 += p.sigma_alpha * (min(innov * innov, 9 * self.sigma2) - self.sigma2)
        if exc is not None:
            res.excursions.append(exc)
        self.prev = (tc, phase, unwrapped)
        res.observations.append(PilotObservation(
            phase_rad=phase, unwrapped_phase_rad=unwrapped, phase_std_rad=std, snr_db=snr,
            clipped=False, gap_in_window=False, pilot_enabled=True, continuity_id=self.cont,
            innovation_rad=innov, monitor_state=state, detection_floor_rad=floor,
            reason=None if state == "TRACKING" else state, **common))
        return None


def _coverage_from(res: MonitorResult, untimed_start: Optional[int]):
    def status(o):
        if o.monitor_state == "UNMONITORED":
            return "UNMONITORED", o.reason or "UNKNOWN"
        if o.monitor_state == "TRACKING":
            return "MONITORED", "OK"
        return "NOT_READY", o.monitor_state
    for o in res.observations:
        st, rs = status(o)
        if res.coverage and res.coverage[-1].status == st and res.coverage[-1].reason == rs:
            last = res.coverage[-1]
            res.coverage[-1] = Interval(last.start_ns, o.window_end_ns, res.timebase, st, rs)
        else:
            res.coverage.append(Interval(o.window_start_ns, o.window_end_ns, res.timebase, st, rs))
    if untimed_start is not None:
        res.coverage.append(Interval(untimed_start, None, res.timebase, "UNMONITORED", "UNTIMED"))


def _run(cap, stream_id, sensor, kind, params: MonitorParams, nco_hz_fn, bw, limitations):
    sv = cap.streams[stream_id]
    res = MonitorResult(sensor=sensor, kind=kind, stream_id=stream_id, timebase=sv.timebase,
                        sample_period_ns=1e9 / float(sv.fs), limitations=list(limitations))
    tr = _Tracker(params, sensor, sv.timebase, kind, bw)
    end = sv.timed_end()
    t0 = sv.time_of(0)
    times = [sv.time_of(n) for n in range(end)]
    x_all = sv.read_iq(0, end)
    nco = [0.0] * end
    for n in range(1, end):
        nco[n] = nco[n - 1] + 2 * math.pi * nco_hz_fn(n - 1) * float(times[n] - times[n - 1]) * 1e-9
    B = params.block_samples
    for i0 in range(0, end - B + 1, B):
        i1 = i0 + B
        ts, te = int(round(times[i0])), int(round(times[i1 - 1]))
        reason = _block_status(sv, i0, i1, set(params.ignore_reasons))
        if reason is None:
            t_rel = [float(times[k] - t0) for k in range(i0, i1)]
            tr.block(x_all[i0:i1], t_rel, nco[i0:i1], ts, te, nco_hz_fn(i0), res)
            continue
        if reason in BENIGN_GAP_REASONS:
            tr.gap()
        else:
            tr.requalify()
        res.observations.append(PilotObservation(
            stream_id=stream_id, monitor_kind=kind, timebase=sv.timebase, window_start_ns=ts,
            window_end_ns=te, available_ns=te + params.latency_ns, offset_hz=nco_hz_fn(i0),
            phase_rad=None, unwrapped_phase_rad=None, phase_std_rad=None, snr_db=None,
            clipped=reason == "CLIPPED_OR_NONFINITE", gap_in_window=reason != "PILOT_OFF",
            pilot_enabled=reason != "PILOT_OFF", detector_bandwidth_hz=bw,
            monitor_state="UNMONITORED", reason=reason))
    untimed = (int(round(times[-1])) if times else int(sv.meta["t0_ns"])) if sv.untimed_from is not None else None
    _coverage_from(res, untimed)
    sigs = [o.detection_floor_rad / params.k_sigma for o in res.observations if o.detection_floor_rad]
    sig = sigs[-1] if sigs else None
    res.envelope = {
        "retrospective": True,
        "sigma_innovation_rad_final": sig,
        "detection_floor_rad": params.k_sigma * sig if sig else None,
        "step_detection_floor_worst_case_rad": params.k_sigma * sig * math.sqrt(2) if sig else None,
        "per_block_change_ceiling_rad": params.unwrap_limit_rad,
        "temporal_resolution_ns": B * res.sample_period_ns,
        "detection_latency_max_ns": 2 * B * res.sample_period_ns + params.latency_ns,
        "k_sigma": params.k_sigma,
        "note": "Capture-wide summary computed AFTER the capture: for offline reports only. "
                "Online decisions use each record's causal detection_floor_rad.",
    }
    return res


def _lo_nco(cap, sv, stream_id, rf_hz):
    retunes = [ev["new_center_freq_hz"] for ev in cap.events
               if ev["type"] == "LO_RETUNE" and ev["stream_id"] == stream_id]
    changes = list(zip(sv.retune_indices, retunes))

    def nco_hz(n):
        center = sv.meta["center_freq_hz"]
        for idx, c in changes:
            if idx <= n:
                center = c
        return rf_hz - center
    return nco_hz


def extract_sampled_pilot(cap, stream_id="rx0", params: MonitorParams = MonitorParams()) -> MonitorResult:
    """Sprint 1b: digital pilot extraction from the SDR's own raw IQ."""
    sv = cap.streams[stream_id]
    if sv.meta.get("pilot") is None:
        raise ValueError(f"stream {stream_id!r} declares no pilot")
    nco_hz = _lo_nco(cap, sv, stream_id, sv.meta["center_freq_hz"] + sv.meta["pilot"]["offset_hz"])
    lim = [
        "Ideal point sampling in the 1a generator: events entirely between two samples are "
        "unobservable IN THIS MODEL. A real anti-alias filter leaks them partially; no claim "
        "of physical unobservability follows.",
        f"Temporal resolution is one block ({params.block_samples} samples); pulses much shorter "
        "than a block shift the block phase by about (width/block) x amplitude.",
        "Phase is known only modulo 2*pi: changes beyond the unwrap limit within one block are "
        "reported as ambiguous and end the continuity segment.",
        "Excursions are UNATTRIBUTED: the pilot phase moves with S, J, G, A, FG and FA alike.",
        "After a retune, unlock, gain change, clipping, clock jump or pilot interruption the "
        "monitor requalifies from scratch and is not TRACKING until warm again.",
    ]
    return _run(cap, stream_id, "pilot_sampled", "sampled_digital", params, nco_hz, None, lim)


def extract_analog_monitor(cap, stream_id="mon0", params: MonitorParams = MonitorParams(block_samples=20)
                           ) -> MonitorResult:
    """Sprint 1c: phase tracking of a logged analog quadrature detector output."""
    sv = cap.streams[stream_id]
    mon = sv.meta.get("monitor")
    if mon is None:
        raise ValueError(f"stream {stream_id!r} is not an analog monitor stream")
    sensor = "analog_monitor_P4" if mon["reference"] == "independent" else "analog_monitor_P4_srcref"
    bw = mon["detector_bandwidth_hz"]
    tau_us = 1e6 / (2 * math.pi * bw)
    lim = [
        f"Detector bandwidth {bw:g} Hz (time constant ~{tau_us:.1f} us): pulses shorter than this "
        "are attenuated roughly by width/time-constant and may fall below the detection floor.",
        "The tap is at P4, upstream of the SDR: internal SDR events (A: LO, IF, sample clock) "
        "are invisible to this monitor by construction.",
        "The detector's own reference/electronics phase M enters with sign -1 and is "
        "indistinguishable from pilot-path changes without a qualified bound (QR-M).",
        "Logger dead time leaves unmonitored intervals; they are reported, not interpolated.",
        "Scene leakage into the detector is NOT used as an observation channel and earns no "
        "attribution credit (see MONITORS.md).",
    ]
    if mon["reference"] == "pilot_source":
        lim.append("Source-referenced: pilot source phase S cancels, so S drift is not seen; "
                   "J and M still are.")
    return _run(cap, stream_id, sensor, "analog_quadrature", params, lambda n: 0.0, bw, lim)


def extract_reference_bracket(cap, stream_id="rx0", params: BracketParams = BracketParams()) -> MonitorResult:
    """Periodic switched-reference baseline: phase of the known tone in each
    settled R dwell, compared with the previous comparable dwell.

    It observes X+R+G+A only at reference dwells. A clean bracket means the
    endpoints agree, never that the interior was stable.
    """
    sv = cap.streams[stream_id]
    tone = sv.meta.get("reference_tone")
    if tone is None:
        raise ValueError(f"stream {stream_id!r} declares no reference_tone")
    nco_hz = _lo_nco(cap, sv, stream_id, sv.meta["center_freq_hz"] + tone["offset_hz"])
    res = MonitorResult(sensor="reference", kind="reference_bracket", stream_id=stream_id,
                        timebase=sv.timebase, sample_period_ns=1e9 / float(sv.fs), limitations=[
                            "Endpoint comparison between reference dwells: excursions that return "
                            "before the next dwell are invisible (Gate 0.3 limitation).",
                            "Threshold uses a DECLARED, unqualified drift allowance, not a "
                            "qualified envelope; a clean bracket cannot make a window CONDITIONAL.",
                            "Phase between dwells is known only modulo 2*pi."])
    end = sv.timed_end()
    acc = sv.accepted_ranges()
    changes = sorted(sv.port_changes) + [(end, "END")]
    runs = []
    for (i0, port), (i1, _) in zip(changes, changes[1:]):
        if port != "R":
            continue
        best = None
        for a, b in acc:
            lo, hi = max(a, i0), min(b, i1, end)
            if hi - lo >= params.min_samples and (best is None or hi - lo > best[1] - best[0]):
                best = (lo, hi)
        if best:
            runs.append(best)
    prev = None
    cont = -1
    t_first = sv.time_of(0)
    for lo, hi in runs:
        x = sv.read_iq(lo, hi)
        ts = [sv.time_of(n) for n in range(lo, hi)]
        # NCO phase from device time since stream start (exact for a fixed LO;
        # piecewise-accumulated within the run if a retune occurred earlier).
        base = 2 * math.pi * nco_hz(lo) * float(ts[0] - t_first) * 1e-9
        ph_nco, acc_ph = [], 0.0
        for k in range(hi - lo):
            if k:
                acc_ph += 2 * math.pi * nco_hz(lo + k - 1) * float(ts[k] - ts[k - 1]) * 1e-9
            ph_nco.append(acc_ph)
        B = hi - lo
        w, h = _hann(B), _hann(B // 2)
        rot = [x[k] * cmath.exp(-1j * (base + ph_nco[k])) for k in range(B)]
        z = sum(a * b for a, b in zip(w, rot)) / sum(w)
        z1 = sum(a * b for a, b in zip(h, rot[:B // 2])) / sum(h)
        z2 = sum(a * b for a, b in zip(h, rot[B // 2:B // 2 * 2])) / sum(h)
        var_ph = (abs(z1 - z2) ** 2 / 4) / (2 * abs(z) ** 2) if abs(z) else float("inf")
        phase = cmath.phase(z)
        t_s, t_e = int(round(ts[0])), int(round(ts[-1]))
        t_c = (t_s + t_e) / 2
        avail = t_e + params.latency_ns
        comparable = prev is not None and not any(
            r.reason not in params.benign_between and r.stop > prev["hi"] and r.start < lo
            for r in sv.rejections)
        state, floor, innov = "REACQUIRING", None, None
        if comparable:
            dt = (t_c - prev["t_c"]) * 1e-9
            sig = math.sqrt(var_ph + prev["var"] + (params.drift_allowance_rad_per_rts ** 2) * dt)
            innov = _wrap(phase - prev["phase"])
            floor, state = params.k_sigma * sig, "TRACKING"
            if abs(innov) > floor:
                res.excursions.append(Excursion(avail, prev["t_s"], t_e, sv.timebase, innov, sig, cont,
                                                abs(innov) > math.pi / 2,
                                                tuple(sorted(SENSORS["reference"].terms))))
            res.coverage.append(Interval(prev["t_s"], t_e, sv.timebase, "BRACKET_ONLY", "ENDPOINTS"))
        else:
            cont += 1
        res.observations.append(PilotObservation(
            stream_id=stream_id, monitor_kind="reference_bracket", timebase=sv.timebase,
            window_start_ns=t_s, window_end_ns=t_e, available_ns=avail, offset_hz=nco_hz(lo),
            phase_rad=_wrap(phase), unwrapped_phase_rad=None, phase_std_rad=math.sqrt(var_ph),
            snr_db=None, clipped=False, gap_in_window=False, pilot_enabled=True,
            continuity_id=cont, innovation_rad=innov, monitor_state=state, detection_floor_rad=floor,
            reason=None if state == "TRACKING" else state))
        prev = {"hi": hi, "phase": phase, "var": var_ph, "t_c": t_c, "t_s": t_s}
    if sv.untimed_from is not None:
        res.coverage.append(Interval(int(round(sv.time_of(end - 1))), None, sv.timebase,
                                     "UNMONITORED", "UNTIMED"))
    res.envelope = {"retrospective": True, "dwells": len(runs),
                    "drift_allowance_rad_per_rts": params.drift_allowance_rad_per_rts,
                    "note": "Declared allowance, not a qualified envelope."}
    return res


def window_state(res: MonitorResult, t_start_ns: int, t_end_ns: int, t_decision_ns: int):
    """Conservative public state for [t_start, t_end] decided at ``t_decision_ns``.

    All times are on ``res.timebase``. Evidence counts only if its
    ``available_ns`` <= t_decision. INVALID: the monitor was faulty or absent;
    UNRESOLVED: part of the window not (yet) covered at stated sensitivity, an
    excursion, or a bracket-only monitor; CONDITIONAL: fully covered while
    TRACKING with no excursion. Never VALID_BOUNDED.
    """
    if t_decision_ns < t_end_ns:
        return PublicState.UNRESOLVED, "decision precedes the end of the window"
    for c in res.coverage:
        if c.reason == "UNTIMED" and t_end_ns > c.start_ns and t_decision_ns >= c.start_ns:
            return PublicState.INVALID, "monitor faulty or absent in window: UNTIMED"
    avail = [o for o in res.observations if o.available_ns <= t_decision_ns
             and o.window_start_ns <= t_end_ns and o.window_end_ns >= t_start_ns]
    faults = sorted({o.reason for o in avail if o.monitor_state == "UNMONITORED" and o.reason in FAULT_REASONS})
    if faults:
        return PublicState.INVALID, "monitor faulty or absent in window: " + ",".join(faults)
    ex = [e for e in res.excursions if e.available_ns <= t_decision_ns
          and e.window_start_ns <= t_end_ns and e.window_end_ns >= t_start_ns]
    if ex:
        return PublicState.UNRESOLVED, EXCURSION_PREFIX + "; candidates " + ",".join(ex[0].candidates)
    if res.kind == "reference_bracket":
        return PublicState.UNRESOLVED, ("bracket-only: endpoints compared, interior unobserved; "
                                        "no qualified AG envelope")
    if any(o.monitor_state != "TRACKING" for o in avail):
        states = sorted({o.monitor_state if o.monitor_state != "UNMONITORED" else (o.reason or "GAP")
                         for o in avail if o.monitor_state != "TRACKING"})
        return PublicState.UNRESOLVED, "monitor not at stated sensitivity: " + ",".join(states)
    # Every instant of the window must lie in an available TRACKING block.
    slack = 1.5 * res.sample_period_ns
    cur = t_start_ns
    for o in sorted(avail, key=lambda o: o.window_start_ns):
        if o.window_start_ns > cur + slack:
            break
        cur = max(cur, o.window_end_ns)
    if cur + slack < t_end_ns:
        return PublicState.UNRESOLVED, "window not fully covered by evidence available at decision time"
    return PublicState.CONDITIONAL, "covered while TRACKING; nothing above the causal floor"
