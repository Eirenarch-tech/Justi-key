"""Disturbance primitives for the Sprint 1a blind generator. OFFLINE ONLY.

A disturbance is an additive phase trajectory on one impairment term of the
observation graph, as a function of *physical* time (ns since capture start).
Only ``delay_step`` is frequency-dependent: a group-delay change of ``tau`` ns
on G (RF stage) or A (baseband stage) shifts each tone by ``-2*pi*f*tau``, so
it moves the scene and the pilot by different amounts (a selective impairment).
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from functools import lru_cache

from caelarch_rf.gate04.observability import ORDER

KINDS = ("step", "ramp", "pulse", "sine", "random_walk", "delay_step", "intersample_pulse")
WALK_GRID_NS = 10_000  # random-walk evaluation grid (10 us), held between points


@lru_cache(maxsize=256)
def _walk(seed: int, n: int):
    rng = random.Random(seed)
    acc, out = 0.0, [0.0]
    for _ in range(n):
        acc += rng.gauss(0.0, 1.0)
        out.append(acc)
    return tuple(out)


@dataclass(frozen=True)
class Disturbance:
    target: str            # impairment id (ORDER)
    kind: str              # KINDS
    t0_ns: int             # physical onset, ns since capture start
    value: float           # rad (step/ramp total/pulse/sine amplitude);
                           # rad/sqrt(s) (random_walk); ns of group delay (delay_step)
    duration_ns: int = 0   # ramp/pulse/random_walk length; sine length (0 = to end)
    freq_hz: float = 0.0   # sine only
    seed: int = 0          # random_walk only
    cause: str = "scheduled"  # 'scheduled' or the acquisition event that caused it

    def __post_init__(self):
        if self.target not in ORDER:
            raise ValueError(f"unknown target {self.target!r}")
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}")
        if not isinstance(self.t0_ns, int) or self.t0_ns < 0:
            raise ValueError("t0_ns must be a non-negative integer")
        if not math.isfinite(self.value):
            raise ValueError("value must be finite")
        if self.kind in ("ramp", "pulse", "random_walk") and self.duration_ns <= 0:
            raise ValueError(f"{self.kind} needs duration_ns > 0")
        if self.kind == "sine" and self.freq_hz <= 0:
            raise ValueError("sine needs freq_hz > 0")
        if self.kind == "delay_step" and self.target not in ("G", "A"):
            raise ValueError("delay_step applies to G (RF) or A (baseband) only")

    def phase(self, t_ns: float, f_hz: float = 0.0) -> float:
        """Phase (rad) at physical time ``t_ns`` for a tone at ``f_hz``."""
        dt = t_ns - self.t0_ns
        k = self.kind
        if dt < 0:
            return 0.0
        if k == "step":
            return self.value
        if k == "ramp":
            return self.value * min(1.0, dt / self.duration_ns)
        if k == "pulse":
            return self.value if dt < self.duration_ns else 0.0
        if k == "sine":
            if self.duration_ns and dt >= self.duration_ns:
                return 0.0
            return self.value * math.sin(2 * math.pi * self.freq_hz * dt * 1e-9)
        if k == "random_walk":
            n = self.duration_ns // WALK_GRID_NS
            w = _walk(self.seed, n)
            return self.value * math.sqrt(WALK_GRID_NS * 1e-9) * w[min(int(dt // WALK_GRID_NS), n)]
        if k == "delay_step":
            return -2 * math.pi * f_hz * self.value * 1e-9
        raise AssertionError("intersample_pulse must be resolved by the generator")

    def to_truth(self) -> dict:
        d = asdict(self)
        d["t_ns"] = d.pop("t0_ns")
        return d

    @staticmethod
    def from_truth(d: dict) -> "Disturbance":
        d = dict(d)
        d["t0_ns"] = d.pop("t_ns")
        return Disturbance(**d)


def scaled(template: Disturbance, target: str, factor: float) -> Disturbance:
    """Same trajectory shape on another term, amplitude scaled (matched pairs)."""
    return Disturbance(target, template.kind, template.t0_ns, template.value * factor,
                       template.duration_ns, template.freq_hz, template.seed, template.cause)
