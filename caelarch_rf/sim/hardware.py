"""Receiver hardware artefacts for the generator. OFFLINE ONLY.

Signal chain applied to the SDR stream, in physical order:

  interference (OOK-keyed carrier bursts, Poisson arrivals)
  -> front-end compression (Rapp model, AM/AM only)
  -> IQ imbalance        y = mu*v + nu*conj(v), mu=(1+g e^{-j phi})/2, nu=(1-g e^{j phi})/2
  -> DC offset / LO leakage (fixed complex offset, random phase)
  -> N-bit ADC           midrise quantiser with hard rails; 8 bits reproduces the
                         RTL-SDR cu8 levels exactly as the external_raw adapter maps them

Presets are calibrated on the 29 RTL-SDR (cu8) public recordings measured in
docs/PUBLIC_IQ_CHECK.md (median and 90th-percentile values). Caveat: those
recordings were saved BECAUSE a burst was present, so burst rate and clipping
are biased high; a shielded conducted bench should be far cleaner. Treat the
interference settings as a stress model until bench measurements exist.

Not modelled: AM/PM conversion, frequency-dependent IQ imbalance, DC drift,
AGC, ADC non-linearity (INL/DNL), interference reaching the analog P4
detector (it is applied to the SDR stream only).
"""
from __future__ import annotations

import cmath
import math
import random
from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple

FULL_SCALE = 32768.0


@dataclass(frozen=True)
class HardwareModel:
    name: str = "custom"
    adc_bits: int = 16
    dc_offset_dbfs: Optional[float] = None          # |DC| re full scale; None = none
    iq_gain_db: float = 0.0
    iq_phase_deg: float = 0.0
    sat_level_dbfs: Optional[float] = None          # Rapp saturation amplitude; None = linear
    rapp_p: float = 2.0
    burst_rate_hz: float = 0.0                      # Poisson arrivals of interference bursts
    burst_duration_ns: Tuple[int, int] = (250_000, 1_000_000)   # log-uniform
    burst_level_dbfs: Tuple[float, float] = (-20.0, 3.0)        # uniform, peak amplitude
    burst_baud: Tuple[float, float] = (1_000.0, 10_000.0)       # OOK symbol rate, uniform
    burst_band_fraction: float = 0.8                # carriers uniform in +/- fraction*fs/2

    def __post_init__(self):
        if not 4 <= self.adc_bits <= 16:
            raise ValueError("adc_bits must be 4..16")
        if self.burst_rate_hz < 0 or not 0 < self.burst_band_fraction <= 1:
            raise ValueError("invalid interference settings")

    @property
    def step(self) -> float:
        return 2 * FULL_SCALE / 2 ** self.adc_bits

    @property
    def rail(self) -> int:
        """Largest output magnitude (the value an ADC rail produces)."""
        if self.adc_bits == 16:
            return 32767
        return int((2 ** (self.adc_bits - 1) - 0.5) * self.step)

    def to_dict(self):
        return asdict(self)


PRESETS = {
    # Medians of the public RTL-SDR recordings.
    "rtl_sdr_typical": HardwareModel(name="rtl_sdr_typical", adc_bits=8, dc_offset_dbfs=-57.5,
                                     iq_gain_db=0.003, iq_phase_deg=0.055, sat_level_dbfs=6.0,
                                     burst_rate_hz=15.6),
    # 90th percentiles (worse DC, imbalance, much more interference).
    "rtl_sdr_adverse": HardwareModel(name="rtl_sdr_adverse", adc_bits=8, dc_offset_dbfs=-51.2,
                                     iq_gain_db=0.04, iq_phase_deg=0.31, sat_level_dbfs=6.0,
                                     burst_rate_hz=270.0, burst_level_dbfs=(-15.0, 6.0)),
    # Worst IQ imbalance seen in any public recording (a cs16 file), no interference.
    "iq_worst_observed": HardwareModel(name="iq_worst_observed", adc_bits=16, iq_gain_db=0.81,
                                       iq_phase_deg=1.89),
}


@dataclass(frozen=True)
class Burst:
    t0_ns: float
    duration_ns: float
    level_dbfs: float
    freq_hz: float
    baud: float
    seed: int


def draw_bursts(hw: HardwareModel, rng: random.Random, duration_ns: int, fs_hz: float) -> List[Burst]:
    out, t = [], 0.0
    if hw.burst_rate_hz <= 0:
        return out
    lo, hi = math.log(hw.burst_duration_ns[0]), math.log(hw.burst_duration_ns[1])
    while True:
        t += rng.expovariate(hw.burst_rate_hz) * 1e9
        if t >= duration_ns:
            return out
        out.append(Burst(t, math.exp(rng.uniform(lo, hi)), rng.uniform(*hw.burst_level_dbfs),
                         rng.uniform(-1, 1) * hw.burst_band_fraction * fs_hz / 2,
                         rng.uniform(*hw.burst_baud), rng.getrandbits(64)))


def interference(bursts: List[Burst], tp: List[float]):
    """Sparse additive interference {sample index: complex} at ADC level."""
    add = {}
    if not bursts:
        return add
    n0 = len(tp)
    T = tp[1] - tp[0] if n0 > 1 else 1.0
    for b in bursts:
        amp = FULL_SCALE * 10 ** (b.level_dbfs / 20)
        bits = random.Random(b.seed)
        sym_ns = 1e9 / b.baud
        keyed = {}
        i0 = max(0, int(b.t0_ns / T))
        for n in range(i0, n0):
            t = tp[n]
            if t >= b.t0_ns + b.duration_ns:
                break
            if t < b.t0_ns:
                continue
            k = int((t - b.t0_ns) // sym_ns)
            if k not in keyed:
                keyed[k] = bits.random() < 0.5
            if keyed[k]:
                add[n] = add.get(n, 0j) + amp * cmath.exp(2j * math.pi * b.freq_hz * t * 1e-9)
    return add


class FrontEnd:
    """Deterministic per-sample analog front end + ADC for one capture."""

    def __init__(self, hw: HardwareModel, rng: random.Random):
        self.hw = hw
        g = 10 ** (hw.iq_gain_db / 20)
        phi = math.radians(hw.iq_phase_deg)
        self.mu = (1 + g * cmath.exp(-1j * phi)) / 2
        self.nu = (1 - g * cmath.exp(1j * phi)) / 2
        self.dc_phase = rng.uniform(-math.pi, math.pi)
        self.dc = (FULL_SCALE * 10 ** (hw.dc_offset_dbfs / 20) * cmath.exp(1j * self.dc_phase)
                   if hw.dc_offset_dbfs is not None else 0j)
        self.sat = FULL_SCALE * 10 ** (hw.sat_level_dbfs / 20) if hw.sat_level_dbfs is not None else None

    def _adc(self, x):
        hw = self.hw
        if hw.adc_bits == 16:
            return max(-32768, min(32767, int(round(x))))
        half = 2 ** (hw.adc_bits - 1)
        code = max(-half, min(half - 1, math.floor(x / hw.step)))
        return int((code + 0.5) * hw.step)

    def __call__(self, v: complex) -> Tuple[int, int]:
        if self.sat is not None:
            a = abs(v)
            if a:
                p2 = 2 * self.hw.rapp_p
                v *= 1 / (1 + (a / self.sat) ** p2) ** (1 / p2)
        v = self.mu * v + self.nu * v.conjugate() + self.dc
        return self._adc(v.real), self._adc(v.imag)

    def summary(self):
        return {"irr_db": 10 * math.log10(abs(self.mu) ** 2 / abs(self.nu) ** 2) if abs(self.nu) else None,
                "dc_phase_rad": self.dc_phase}
