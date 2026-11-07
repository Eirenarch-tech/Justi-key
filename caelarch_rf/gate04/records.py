"""Online evidence records produced by monitors and consumed by policy.

These define the *interface* a Sprint 1 pilot extractor (P0.5) or analog
monitor model (P0.6) must emit. No extractor is implemented here.

Causality rule: a record may be used for a decision at time ``t`` only if
``available_ns <= t`` on the same timebase. ``available_ns`` must include
extraction latency (filter group delay, block processing), so a monitor that
needs future samples cannot be credited with a detection before it could
actually have produced one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

MONITOR_KINDS = ("sampled_digital", "analog_quadrature")


def _finite(name, v, allow_none=False):
    if v is None and allow_none:
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number")


@dataclass(frozen=True)
class PilotObservation:
    stream_id: str
    monitor_kind: str
    timebase: str
    window_start_ns: int      # first sample time contributing to the estimate
    window_end_ns: int        # last sample time contributing to the estimate
    available_ns: int         # earliest time the estimate existed online
    offset_hz: float          # pilot offset actually tracked
    phase_rad: Optional[float]            # wrapped [-pi, pi]; None = no lock
    unwrapped_phase_rad: Optional[float]  # None when unwrap continuity was lost
    phase_std_rad: Optional[float]
    snr_db: Optional[float]
    clipped: bool             # any contributing sample clipped/non-finite
    gap_in_window: bool       # overflow/discontinuity/settling inside window
    pilot_enabled: bool       # PILOT_STATE at window time
    detector_bandwidth_hz: Optional[float] = None  # analog: measured -3 dB BW
    # Unwrapped phase is meaningful only within one continuity segment. A new
    # id starts after ANY unobserved interval or ambiguous (> limit) change, so
    # no consumer can mistake two segments for one continuous track.
    continuity_id: Optional[int] = None
    freq_offset_hz: Optional[float] = None   # tracked residual pilot frequency
    innovation_rad: Optional[float] = None   # phase minus causal prediction

    def __post_init__(self):
        if self.monitor_kind not in MONITOR_KINDS:
            raise ValueError(f"monitor_kind must be one of {MONITOR_KINDS}")
        for n in ("window_start_ns", "window_end_ns", "available_ns"):
            if not isinstance(getattr(self, n), int) or isinstance(getattr(self, n), bool):
                raise ValueError(f"{n} must be integer ns")
        if not self.window_start_ns <= self.window_end_ns <= self.available_ns:
            raise ValueError("require window_start <= window_end <= available (no acausal estimates)")
        _finite("offset_hz", self.offset_hz)
        for n in ("phase_rad", "unwrapped_phase_rad", "phase_std_rad", "snr_db",
                  "detector_bandwidth_hz", "freq_offset_hz", "innovation_rad"):
            _finite(n, getattr(self, n), allow_none=True)
        if self.phase_rad is not None and abs(self.phase_rad) > math.pi + 1e-12:
            raise ValueError("phase_rad must be wrapped to [-pi, pi]")
        if self.monitor_kind == "analog_quadrature" and self.detector_bandwidth_hz is None:
            raise ValueError("analog monitor records must state detector_bandwidth_hz")
        if self.unwrapped_phase_rad is not None and self.continuity_id is None:
            raise ValueError("an unwrapped phase must name its continuity segment")

    @property
    def usable(self) -> bool:
        """Fit to enter policy at all. Not a statement about attribution."""
        return (self.pilot_enabled and not self.clipped and not self.gap_in_window
                and self.phase_rad is not None)

    def usable_at(self, t_ns: int) -> bool:
        return self.usable and self.available_ns <= t_ns
