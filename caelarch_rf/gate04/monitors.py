"""Online pilot monitors (Sprint 1b sampled extractor, Sprint 1c analog monitor).

Both take only an :class:`OnlineCapture` and emit causal
:class:`PilotObservation` records, a coverage report and *unattributed*
excursions. Neither has access to truth.

Pipeline (per block of ``block_samples`` accepted, timed samples):

1. **Mix down** with an NCO driven by *device timestamps* (sampled pilot only;
   the analog detector output is already at baseband). The NCO frequency
   follows ``LO_RETUNE`` events: f_bb = pilot RF - current LO.
2. **Frequency-offset compensation**: derotate by the tracked residual
   frequency ``f_hat``, learned causally from past innovations only.
3. **Filter / estimate**: Hann-weighted mean of the block, which rejects
   the scene tone. Two half-block estimates give a noise variance.
4. **Track**: predict the phase from the previous block, compute the wrapped
   innovation, unwrap only inside a continuity segment.
5. **Detect** (not attribute): |innovation| > k x sigma_innov, or the sum of
   two consecutive innovations > k x sigma x sqrt(2) (a step that straddles a
   block boundary splits across two blocks) -> Excursion, listing every graph
   term that moves this sensor as a candidate cause.

No-fabrication rules:
* any unobserved interval (rejected sample, pilot off, pilot amplitude lost,
  overflow, timestamp boundary) ends the continuity segment; the next segment
  restarts unwrapping from its own wrapped phase;
* a change larger than ``unwrap_limit_rad`` within one block is reported as an
  *ambiguous* excursion (the 2*pi multiple is unknowable) and also ends the
  segment;
* an untimed tail (overflow with unknown drop count) ends processing: nothing
  after it is observed or reported.

Model limitations are returned in every result, because detection coverage
depends on the model (ideal point sampling, detector bandwidth).
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .observability import SENSORS
from .records import PilotObservation
from .states import PublicState

# Rejections that mean the monitor itself is faulty or absent -> INVALID.
FAULT_REASONS = ("CLIPPED_OR_NONFINITE", "OVERFLOW_UNKNOWN_DROP_UNTIMED", "TIME_REVERSAL_UNTIMED",
                 "UNTIMED", "REF_UNLOCKED", "LO_UNLOCKED", "PILOT_OFF", "PILOT_LOST",
                 "REF_LOCK_UNORDERED", "ALIGNMENT_VOID_AFTER_DISCONTINUITY")


@dataclass(frozen=True)
class MonitorParams:
    block_samples: int = 100
    latency_ns: int = 50_000          # declared processing latency after the window ends
    k_sigma: float = 6.0              # detection threshold on the innovation
    acquisition_innovations: int = 4  # initial frequency acquisition (full-gain running mean)
    warmup_innovations: int = 8       # then this many before any detection is allowed
    unwrap_limit_rad: float = math.pi / 2
    freq_alpha: float = 0.2           # residual-frequency tracker gain
    sigma_alpha: float = 0.05         # innovation-variance EMA gain
    amp_alpha: float = 0.1
    min_amp_ratio: float = 0.5        # pilot considered lost below this x running amplitude
    refreq_after: int = 3             # consecutive excursions treated as a frequency change
    # The pilot is injected downstream of the selector, so selector settling does
    # not invalidate pilot samples. This rests on the planned physical ordering
    # (architecture plane P3) and must be confirmed on the bench.
    ignore_reasons: Tuple[str, ...] = ("SETTLING_SWITCH",)


@dataclass(frozen=True)
class Interval:
    start_ns: int
    end_ns: Optional[int]   # None: open-ended (untimed tail)
    timebase: str
    status: str             # MONITORED | UNMONITORED
    reason: str


@dataclass(frozen=True)
class Excursion:
    available_ns: int
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
    sensor: str
    stream_id: str
    timebase: str
    observations: List[PilotObservation] = field(default_factory=list)
    coverage: List[Interval] = field(default_factory=list)
    excursions: List[Excursion] = field(default_factory=list)
    envelope: Dict[str, object] = field(default_factory=dict)
    limitations: List[str] = field(default_factory=list)

    def usable(self):
        return [o for o in self.observations if o.usable]

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
        self.f_hat = 0.0
        self.sigma2: Optional[float] = None
        self.n_innov = 0
        self.n_acq = 0            # frequency-acquisition innovations consumed
        self.warm: List[float] = []
        self.amp: Optional[float] = None
        self.noise2: Optional[float] = None
        self.cont = -1
        self.prev = None          # (t_center_ns, wrapped phase, unwrapped phase)
        self.run = 0              # consecutive excursions
        self.last_innov = None    # previous accepted (sub-threshold) innovation
        self.pending_f = 0.0      # frequency update held back one block (see below)

    def gap(self):
        self.prev = None
        self.run = 0
        self.last_innov = None
        self.pending_f = 0.0

    def block(self, x, t_rel, nco, t_abs_start, t_abs_end, offset_hz, res: MonitorResult):
        p = self.p
        B = len(x)
        w = _hann(B)
        h = _hann(B // 2)
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
            self.gap()
            res.observations.append(PilotObservation(
                phase_rad=None, unwrapped_phase_rad=None, phase_std_rad=None, snr_db=None,
                clipped=False, gap_in_window=True, pilot_enabled=True, **common))
            return "PILOT_LOST"
        self.amp = amp if self.amp is None else self.amp + p.amp_alpha * (amp - self.amp)
        phase = _wrap(cmath.phase(z) + 0.0)   # derotation was centred on tc: phase vs NCO at tc
        std = math.sqrt(self.noise2 / 2) / amp if amp > 0 else None
        snr = 10 * math.log10(amp ** 2 / self.noise2) if self.noise2 > 0 and amp > 0 else None
        innov, unwrapped, exc = None, None, None
        if self.prev is None:
            self.cont += 1
            unwrapped = phase
        else:
            t_prev, ph_prev, un_prev = self.prev
            dt = (tc - t_prev) * 1e-9
            pred = ph_prev + 2 * math.pi * self.f_hat * dt
            innov = _wrap(phase - pred)
            if self.n_acq < p.acquisition_innovations and abs(innov) <= p.unwrap_limit_rad:
                # Frequency acquisition: running mean of the observed slope. These
                # innovations are frequency error, not noise, so they never seed
                # sigma and never raise an excursion.
                unwrapped = un_prev + (pred - ph_prev) + innov   # uses the pre-update f_hat
                self.n_acq += 1
                self.f_hat += innov / (2 * math.pi * dt) / self.n_acq
                self.prev = (tc, phase, unwrapped)
                res.observations.append(PilotObservation(
                    phase_rad=phase, unwrapped_phase_rad=unwrapped, phase_std_rad=std, snr_db=snr,
                    clipped=False, gap_in_window=False, pilot_enabled=True, continuity_id=self.cont,
                    innovation_rad=innov, **common))
                return None
            ready = self.n_innov >= p.warmup_innovations and self.sigma2 is not None
            sig = math.sqrt(self.sigma2) if self.sigma2 else None
            pair = innov + self.last_innov if self.last_innov is not None else None
            if abs(innov) > p.unwrap_limit_rad:
                exc = Excursion(available, t_abs_end, self.tb, innov, sig or float("nan"), self.cont,
                                True, tuple(sorted(SENSORS[self.sensor].terms)))
                self.cont += 1
                unwrapped = phase
                self.run = 0
                self.last_innov = None
            else:
                unwrapped = un_prev + 2 * math.pi * self.f_hat * dt + innov
                single = ready and abs(innov) > p.k_sigma * sig
                double = ready and pair is not None and abs(pair) > p.k_sigma * sig * math.sqrt(2)
                if single or double:
                    exc = Excursion(available, t_abs_end, self.tb, innov if single else pair, sig,
                                    self.cont, False, tuple(sorted(SENSORS[self.sensor].terms)))
                    self.run += 1
                    self.last_innov = None
                    self.pending_f = 0.0             # half of a straddling step: discard
                    if self.run >= p.refreq_after:   # persistent: a frequency change, re-acquire
                        self.f_hat += p.freq_alpha * innov / (2 * math.pi * dt)
                else:
                    self.run = 0
                    self.last_innov = innov
                    # Commit the PREVIOUS block's frequency update only now that this
                    # block shows it was not the first half of a straddling step.
                    self.f_hat += self.pending_f
                    self.pending_f = p.freq_alpha * innov / (2 * math.pi * dt)
                    self.n_innov += 1
                    if self.sigma2 is None:
                        self.warm.append(innov * innov)
                        if len(self.warm) >= p.warmup_innovations:
                            floor = 2 * (std or 0) ** 2
                            self.sigma2 = max(sum(self.warm) / len(self.warm), floor)
                    else:
                        # Winsorised at 3 sigma: sub-threshold parts of a real excursion
                        # must not inflate the floor ahead of the rest of it.
                        self.sigma2 += p.sigma_alpha * (min(innov * innov, 9 * self.sigma2) - self.sigma2)
        if exc is not None:
            res.excursions.append(exc)
        self.prev = (tc, phase, unwrapped)
        res.observations.append(PilotObservation(
            phase_rad=phase, unwrapped_phase_rad=unwrapped, phase_std_rad=std, snr_db=snr,
            clipped=False, gap_in_window=False, pilot_enabled=True, continuity_id=self.cont,
            innovation_rad=innov, **common))
        return None


def _run(cap, stream_id, sensor, kind, params: MonitorParams, nco_hz_fn, bw, limitations):
    sv = cap.streams[stream_id]
    res = MonitorResult(sensor=sensor, stream_id=stream_id, timebase=sv.timebase,
                        limitations=list(limitations))
    tr = _Tracker(params, sensor, sv.timebase, kind, bw)
    end = sv.timed_end()
    t0 = sv.time_of(0)
    times = [sv.time_of(n) for n in range(end)]
    x_all = sv.read_iq(0, end)
    # NCO phase from device timestamps (continues across known gaps by elapsed time).
    nco = [0.0] * end
    for n in range(1, end):
        nco[n] = nco[n - 1] + 2 * math.pi * nco_hz_fn(n - 1) * float(times[n] - times[n - 1]) * 1e-9
    spans: List[Tuple[int, int, str, str]] = []   # (start_ns, end_ns, status, reason)
    B = params.block_samples
    for i0 in range(0, end - B + 1, B):
        i1 = i0 + B
        ts, te = int(round(times[i0])), int(round(times[i1 - 1]))
        reason = _block_status(sv, i0, i1, set(params.ignore_reasons))
        if reason is None:
            t_rel = [float(times[k] - t0) for k in range(i0, i1)]
            reason = tr.block(x_all[i0:i1], t_rel, nco[i0:i1], ts, te, nco_hz_fn(i0), res)
        else:
            tr.gap()
            res.observations.append(PilotObservation(
                stream_id=stream_id, monitor_kind=kind, timebase=sv.timebase, window_start_ns=ts,
                window_end_ns=te, available_ns=te + params.latency_ns, offset_hz=nco_hz_fn(i0),
                phase_rad=None, unwrapped_phase_rad=None, phase_std_rad=None, snr_db=None,
                clipped=reason == "CLIPPED_OR_NONFINITE", gap_in_window=reason != "PILOT_OFF",
                pilot_enabled=reason != "PILOT_OFF", detector_bandwidth_hz=bw))
        spans.append((ts, te, "MONITORED" if reason is None else "UNMONITORED", reason or "OK"))
    # Merge into coverage intervals.
    for ts, te, st, rs in spans:
        if res.coverage and res.coverage[-1].status == st and res.coverage[-1].reason == rs:
            last = res.coverage[-1]
            res.coverage[-1] = Interval(last.start_ns, te, sv.timebase, st, rs)
        else:
            res.coverage.append(Interval(ts, te, sv.timebase, st, rs))
    if sv.untimed_from is not None:
        start = int(round(times[-1])) if times else int(sv.meta["t0_ns"])
        res.coverage.append(Interval(start, None, sv.timebase, "UNMONITORED", "UNTIMED"))
    T = 1e9 / float(sv.fs)
    sig = math.sqrt(tr.sigma2) if tr.sigma2 else None
    res.envelope = {
        "sigma_innovation_rad": sig,
        "detection_floor_rad": params.k_sigma * sig if sig else None,
        # A step straddling a block boundary is caught by the two-block test.
        "step_detection_floor_worst_case_rad": params.k_sigma * sig * math.sqrt(2) if sig else None,
        "per_block_change_ceiling_rad": params.unwrap_limit_rad,
        "temporal_resolution_ns": B * T,
        "detection_latency_max_ns": 2 * B * T + params.latency_ns,
        "k_sigma": params.k_sigma,
        "note": "Floor/ceiling apply to step-like changes between consecutive monitored blocks "
                "at the noise and drift of THIS capture; they are not a universal threshold.",
    }
    return res


def extract_sampled_pilot(cap, stream_id="rx0", params: MonitorParams = MonitorParams()) -> MonitorResult:
    """Sprint 1b: digital pilot extraction from the SDR's own raw IQ."""
    sv = cap.streams[stream_id]
    if sv.meta.get("pilot") is None:
        raise ValueError(f"stream {stream_id!r} declares no pilot")
    pilot_rf = sv.meta["center_freq_hz"] + sv.meta["pilot"]["offset_hz"]
    retunes = []
    for ev in cap.events:
        if ev["type"] == "LO_RETUNE" and ev["stream_id"] == stream_id:
            retunes.append(ev["new_center_freq_hz"])
    lo_changes = list(zip(sv.retune_indices, retunes))

    def nco_hz(n):
        center = sv.meta["center_freq_hz"]
        for idx, c in lo_changes:
            if idx <= n:
                center = c
        return pilot_rf - center
    lim = [
        "Ideal point sampling in the 1a generator: events entirely between two samples are "
        "unobservable IN THIS MODEL. A real anti-alias filter leaks them partially; no claim "
        "of physical unobservability follows.",
        f"Temporal resolution is one block ({params.block_samples} samples); pulses much shorter "
        "than a block shift the block phase by about (width/block) x amplitude.",
        "Phase is known only modulo 2*pi: changes beyond the unwrap limit within one block are "
        "reported as ambiguous and end the continuity segment.",
        "Excursions are UNATTRIBUTED: the pilot phase moves with S, J, G, A, FG and FA alike.",
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
    ]
    if mon["reference"] == "pilot_source":
        lim.append("Source-referenced: pilot source phase S cancels, so S drift is not seen; "
                   "J and M still are.")
    return _run(cap, stream_id, sensor, "analog_quadrature", params, lambda n: 0.0, bw, lim)


def window_state(res: MonitorResult, t_start_ns: int, t_end_ns: int):
    """Conservative public state for a decision window on the monitor's timebase.

    INVALID     the monitor was faulty/absent somewhere in the window;
    UNRESOLVED  part of the window is unmonitored, or an excursion was detected
                (detection is not attribution);
    CONDITIONAL fully monitored with no excursion. Never VALID_BOUNDED.
    """
    def overlaps(a, b):
        return a < t_end_ns and (b is None or b > t_start_ns)
    gaps = [c for c in res.unmonitored() if overlaps(c.start_ns, c.end_ns)]
    if any(c.reason in FAULT_REASONS for c in gaps):
        return PublicState.INVALID, "monitor faulty or absent in window: " + ",".join(
            sorted({c.reason for c in gaps if c.reason in FAULT_REASONS}))
    covered = [c for c in res.coverage if c.status == "MONITORED" and overlaps(c.start_ns, c.end_ns)]
    first = min((c.start_ns for c in res.coverage), default=None)
    last = max((c.end_ns for c in res.coverage if c.end_ns is not None), default=None)
    if gaps or not covered or first is None or first > t_start_ns or last is None or last < t_end_ns:
        return PublicState.UNRESOLVED, "unmonitored interval inside window"
    ex = [e for e in res.excursions if t_start_ns <= e.window_end_ns <= t_end_ns]
    if ex:
        return PublicState.UNRESOLVED, ("unattributed excursion; candidates "
                                        + ",".join(ex[0].candidates))
    return PublicState.CONDITIONAL, "monitored, no excursion above the capture's detection floor"
