"""Sprint 1a blind disturbance generator. OFFLINE ONLY: it knows the truth.

Produces a contract-conformant capture directory (what an online method may
see) and a separately stored sealed-truth file (what only the scorer may
open) from a :class:`Scenario` and a high-entropy seed.

Signal model (complex baseband, one SDR channel ``rx0``, ideal point sampling):

    scene/ref = a_s * exp(j(tone_s + X + G + A)) * path(port)     path: H, R, TERM=0
    pilot     = a_p * exp(j(tone_p + S + J + G + A + FG + FA))   (configs with a pilot)
    witness   = |w| * exp(j(w0 + R + K)) + noise                 (configs with a witness)

The scene and reference are time-multiplexed by a selector whose switches
cross-fade over a true settling time. These are modelled: sample-clock error,
device-time start offset, witness timebase offset and declared-alignment
error, LO retunes with phase reset, gain steps, reference unlock with a
delayed lock report, overflows (known/unknown drop count), timestamp
discontinuities, overload clipping, pilot gating, invalid witness records and
switch guards shorter than the true settling time. The disturbance model
covers every graph term except M, which only the analog monitor sees
(Sprint 1c).

Blindness rules (refinement 3; see docs/BLIND_GENERATOR.md):
  * seeds must carry >= 128 bits; integers are refused (enumerable);
  * independent sub-streams: the operating schedule never reads the
    disturbance schedule; noise draws never depend on either;
  * the capture holds no seed, case name, disturbance parameter or truth-
    derived id; capture_id is a keyed hash; created_utc and file mtimes are
    fixed constants; truth is written outside the capture directory.
"""
from __future__ import annotations

import array
import cmath
import hashlib
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from caelarch_rf.gate04 import contract as C
from caelarch_rf.gate04 import sealed_truth
from caelarch_rf.gate04.observability import CONFIGS

from .disturbances import Disturbance

GENERATOR_VERSION = "caelarch-sim 0.1"
MIN_SEED_BYTES = 16
FIXED_CREATED = "2026-01-01T00:00:00Z"
FIXED_MTIME = 1767225600  # 2026-01-01T00:00:00Z
PHYS = "phys"             # truth timebase: physical ns since capture start

# Configs the waveform renderer can emit (analog monitors arrive in Sprint 1c).
EMITTED = {"C0_bracket": (False, False), "C1_witness": (False, True),
           "C2_sampled_pilot": (True, True)}  # (pilot, witness)
FAULT_KINDS = ("retune", "retune_unlocked", "gain_change", "ref_unlock", "overflow_known",
               "overflow_unknown", "time_discontinuity", "overload", "short_guard",
               "pilot_off", "witness_invalid")

CENTER0_HZ = 915e6
SCENE_RF_HZ = CENTER0_HZ + 25e3
PILOT_RF_HZ = CENTER0_HZ + 100e3
SWITCH_GUARD, RETUNE_GUARD, GAIN_GUARD = 50_000, 200_000, 20_000
SCENE_AMP = 8000.0
CLIP_LEVEL = 32000
BASE_GAIN_DB = 30.0
WITNESS_MAG, WITNESS_PHASE0, WITNESS_NOISE = 0.5, 0.2, 0.002
BACKGROUND = {"A": 2.0, "S": 5.0}  # random-walk phase noise, rad/sqrt(s)


# --------------------------------------------------------------------------- seeds

def normalize_seed(seed) -> bytes:
    """>= 16 bytes, given as bytes or hex. Integers are refused: a small seed
    space lets an online method regenerate candidate captures and match noise."""
    if isinstance(seed, (bytes, bytearray)):
        b = bytes(seed)
    elif isinstance(seed, str):
        b = bytes.fromhex(seed)
    else:
        raise TypeError("seed must be bytes or a hex string with >= 128 bits; integers are refused")
    if len(b) < MIN_SEED_BYTES:
        raise ValueError(f"seed must carry at least {MIN_SEED_BYTES} bytes of entropy")
    return b


def derive_bytes(master: bytes, label: str) -> bytes:
    return hashlib.sha256(master + b"/" + label.encode()).digest()


def stream(master: bytes, label: str) -> random.Random:
    return random.Random(int.from_bytes(derive_bytes(master, label), "big"))


def capture_id_for(master: bytes) -> str:
    return "cap-" + derive_bytes(master, "capture_id").hex()[:20]


# ----------------------------------------------------------------------- scenario

@dataclass(frozen=True)
class AcqFault:
    kind: str
    at_ns: Optional[int] = None  # physical time; None = drawn from the ops stream

    def __post_init__(self):
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault {self.kind!r}")


@dataclass(frozen=True)
class Scenario:
    disturbances: Tuple[Disturbance, ...] = ()
    faults: Tuple[AcqFault, ...] = ()
    config: str = "C2_sampled_pilot"
    duration_ns: int = 20_000_000
    fs_hz: int = 1_000_000
    snr_db: float = 40.0          # scene tone SNR per sample
    pilot_rel_db: float = -20.0   # weak pilot relative to scene
    background: bool = True       # A/S phase-noise random walks
    case: str = "custom"          # truth-only label

    def __post_init__(self):
        if self.config not in EMITTED:
            raise ValueError(f"config {self.config!r} cannot be rendered yet; emittable: {sorted(EMITTED)}")
        if self.fs_hz <= 0 or self.duration_ns * self.fs_hz < 10 ** 12:
            raise ValueError("need at least 1000 samples")
        pilot, witness = EMITTED[self.config]
        for d in self.disturbances:
            if d.target == "M":
                raise ValueError("M is only observable by the analog monitor (Sprint 1c)")
        kinds = [f.kind for f in self.faults]
        if sum(k in ("overflow_known", "overflow_unknown") for k in kinds) > 1 or kinds.count("time_discontinuity") > 1:
            raise ValueError("at most one drop and one timestamp discontinuity per capture")
        if not pilot and "pilot_off" in kinds:
            raise ValueError("pilot_off needs a configuration with a pilot")
        if not witness and "witness_invalid" in kinds:
            raise ValueError("witness_invalid needs a configuration with a witness")


# ----------------------------------------------------------------- clock / ops model

class _Clock:
    def __init__(self, fs, eps, t0_dev, n0):
        self.fs, self.eps, self.t0_dev, self.n0 = fs, eps, t0_dev, n0
        self.T = 1e9 / fs
        self.jump_index: Optional[int] = None   # original sample index
        self.jump_ns = 0
        self.drop: Optional[Tuple[int, int, bool]] = None  # (orig index, count, known)

    def tp(self, n):
        """Physical time (ns since start) of original sample n."""
        return n * self.T * (1 + self.eps)

    def n_of(self, t_phys):
        return t_phys / (self.T * (1 + self.eps))

    def dev_exact(self, n):
        """Device timestamp of original sample n (nominal rate, plus any jump)."""
        j = self.jump_ns if self.jump_index is not None and n >= self.jump_index else 0
        return self.t0_dev + n * self.T + j

    def dev(self, t_phys) -> int:
        return int(round(self.dev_exact(self.n_of(t_phys))))

    def file_index(self, n) -> Optional[int]:
        if self.drop is None:
            return n
        i, k, _ = self.drop
        if n < i:
            return n
        return None if n < i + k else n - k


def _ops_plan(master, sc: Scenario):
    """Operating schedule. Reads ONLY the ops stream and the scenario geometry."""
    ops = stream(master, "ops")
    plan = {"t0_dev": ops.randrange(10 ** 9, 10 ** 12), "eps": ops.uniform(-2e-6, 2e-6)}
    t, port, sw = 200_000 + ops.randrange(0, 100_000), "H", []
    while t < sc.duration_ns - 100_000:
        sw.append({"t": t, "port": port, "settle": int(ops.uniform(0.4, 0.8) * SWITCH_GUARD),
                   "report": ops.random() < 0.5, "report_lat": ops.randrange(1_000, 5_000)})
        t += int(ops.uniform(2.5e6, 4.5e6) if port == "H" else ops.uniform(0.6e6, 1.2e6))
        port = "R" if port == "H" else "H"
    plan["switches"] = sw
    plan["wit_offset"] = ops.randrange(-10 ** 9, 10 ** 9)
    plan["wit_align_unc"] = 1_000
    plan["wit_align_err"] = ops.randrange(-300, 301)
    wt, t = [], 500_000
    while t < sc.duration_ns:
        wt.append(t + ops.randrange(-50_000, 50_001))
        t += 1_000_000
    plan["witness_t"] = [x for x in wt if 0 <= x < sc.duration_ns]
    return plan


class _Effects:
    """Physical consequences of acquisition faults, plus their online events."""

    def __init__(self):
        self.hidden: List[Disturbance] = []
        self.events: List[Tuple[float, dict]] = []      # (t_phys, event without seq/t_ns)
        self.lo: List[Tuple[float, float]] = []         # (t_phys, new center)
        self.gain: List[Tuple[float, float, float]] = []  # (t_phys, factor, settle)
        self.pilot_off: List[Tuple[float, float]] = []
        self.bursts: List[Tuple[float, float, float, float]] = []  # t, dur, amp, freq
        self.wit_invalid: List[Tuple[float, float]] = []
        self.short_guard = False
        self.notes: List[dict] = []


def _faults(master, sc: Scenario, clock: _Clock, plan) -> _Effects:
    fx = _Effects()
    center = CENTER0_HZ
    for idx, f in enumerate(sc.faults):
        ops = stream(master, f"ops_fault/{idx}/{f.kind}")
        hid = stream(master, f"hidden_fault/{idx}/{f.kind}")
        at = f.at_ns if f.at_ns is not None else int(ops.uniform(0.25, 0.75) * sc.duration_ns)
        note = {"kind": f.kind, "t_ns": at}
        if f.kind in ("retune", "retune_unlocked"):
            hops = [(at, f.kind == "retune")]
            if f.kind == "retune_unlocked":
                relock = at + 2_000_000
                hops.append((relock, True))
                sigma = hid.uniform(30.0, 60.0)
                fx.hidden.append(Disturbance("A", "random_walk", at, sigma, 2_000_000,
                                             seed=hid.getrandbits(64), cause="lo_unlocked"))
                note.update(relock_t_ns=relock, walk_rad_per_rts=sigma)
            for t, locked in hops:
                center = center + ops.choice([0.0, 200e3, -200e3])
                settle = int(ops.uniform(0.5, 0.9) * RETUNE_GUARD)
                phi = hid.uniform(-math.pi, math.pi)
                fx.lo.append((t, center))
                fx.hidden.append(Disturbance("A", "ramp", t, phi, settle, cause="retune"))
                fx.events.append((t, {"type": "LO_RETUNE", "stream_id": "rx0",
                                      "new_center_freq_hz": center, "lo_locked": locked}))
                if locked and ops.random() < 0.5:
                    fx.events.append((t + settle + ops.randrange(1_000, 5_000),
                                      {"type": "SETTLED", "stream_id": "rx0", "cause": "retune"}))
            note.update(final_center_hz=center)
        elif f.kind == "gain_change":
            delta = ops.choice([-6.0, 6.0, 10.0])
            settle = ops.uniform(0.3, 0.8) * GAIN_GUARD
            fx.gain.append((at, 10 ** (delta / 20), settle))
            phi = hid.gauss(0.0, 0.05)
            fx.hidden.append(Disturbance("A", "step", at, phi, cause="gain_change"))
            fx.events.append((at, {"type": "GAIN_CHANGE", "stream_id": "rx0",
                                   "new_rx_gain_db": BASE_GAIN_DB + delta}))
            note.update(delta_db=delta, phase_step_rad=phi)
        elif f.kind == "ref_unlock":
            lat1, lat2 = ops.randrange(5_000, 40_000), ops.randrange(5_000, 40_000)
            dur = ops.randrange(1_000_000, 3_000_000)
            sigma = hid.uniform(20.0, 50.0)
            fx.hidden.append(Disturbance("A", "random_walk", at, sigma, dur,
                                         seed=hid.getrandbits(64), cause="ref_unlock"))
            fx.events.append((at + lat1, {"type": "REF_LOCK", "locked": False}))
            fx.events.append((at + dur + lat2, {"type": "REF_LOCK", "locked": True}))
            # Samples in [at, at+lat1) are physically unlocked but precede the report.
            note.update(duration_ns=dur, report_latency_ns=lat1, relock_report_latency_ns=lat2,
                        unreported_unlocked_ns=lat1)
        elif f.kind in ("overflow_known", "overflow_unknown"):
            i = int(clock.n_of(at))
            k = ops.randrange(50, 500)
            clock.drop = (i, k, f.kind == "overflow_known")
            note.update(orig_index=i, dropped=k)
        elif f.kind == "time_discontinuity":
            clock.jump_index = int(clock.n_of(at))
            clock.jump_ns = ops.randrange(1_000, 100_000)
            # A declared timebase alignment measured before the jump is now wrong.
            note.update(orig_index=clock.jump_index, jump_ns=clock.jump_ns)
        elif f.kind == "overload":
            dur, freq = ops.randrange(100_000, 400_000), ops.uniform(-400e3, 400e3)
            fx.bursts.append((at, dur, 3.0 * 32767, freq))
            note.update(duration_ns=dur)
        elif f.kind == "short_guard":
            fx.short_guard = True
            note.update(declared_guard_ns=SWITCH_GUARD, true_settle_range_ns=[2 * SWITCH_GUARD, 3 * SWITCH_GUARD])
        elif f.kind == "pilot_off":
            dur, lat = ops.randrange(1_000_000, 3_000_000), ops.randrange(0, 2_000)
            fx.pilot_off.append((at + lat, at + dur + lat))
            fx.events.append((at, {"type": "PILOT_STATE", "enabled": False}))
            fx.events.append((at + dur, {"type": "PILOT_STATE", "enabled": True}))
            note.update(duration_ns=dur, latency_ns=lat)
        elif f.kind == "witness_invalid":
            fx.wit_invalid.append((at, at + 3_000_000))
        fx.notes.append(note)
    if fx.short_guard:
        sg = stream(master, "ops_fault/short_guard_settle")
        for s in plan["switches"]:
            s["settle"] = int(SWITCH_GUARD * sg.uniform(2.0, 3.0))
            s["report"] = False
    return fx


# ------------------------------------------------------------------------ rendering

class Simulation:
    """Noise-free and noisy rendering state. Tests may inspect it; the online
    side only ever receives the written capture directory."""

    def __init__(self, sc: Scenario, master: bytes):
        self.sc, self.master = sc, master
        self.pilot_on_cfg, self.witness_on = EMITTED[sc.config]
        plan = _ops_plan(master, sc)
        n0 = sc.duration_ns * sc.fs_hz // 10 ** 9
        self.clock = _Clock(sc.fs_hz, plan["eps"], plan["t0_dev"], n0)
        self.plan = plan
        self.fx = _faults(master, sc, self.clock, plan)
        bg = stream(master, "background")
        self.background = [Disturbance(k, "random_walk", 0, s, sc.duration_ns,
                                       seed=bg.getrandbits(64), cause="background")
                           for k, s in BACKGROUND.items()] if sc.background else []
        self.disturbances = self._resolve(list(sc.disturbances)) + self.fx.hidden + self.background
        self._render()

    def _resolve(self, ds):
        out = []
        for d in ds:
            if d.kind == "intersample_pulse":
                # Snap strictly between two sample instants: width T/2 starting T/4
                # after a sample, so no sample instant falls inside the pulse.
                n = max(1, int(self.clock.n_of(d.t0_ns)))
                T = self.clock.T * (1 + self.clock.eps)
                t0 = math.ceil(self.clock.tp(n) + T / 4)
                d = Disturbance(d.target, "pulse", t0, d.value, max(1, int(T / 2) - 1), cause=d.cause)
            out.append(d)
        return out

    def _render(self):
        sc, ck, fx = self.sc, self.clock, self.fx
        n0 = ck.n0
        tp = [ck.tp(n) for n in range(n0)]
        self.tp = tp
        # LO and tone phases (accumulated baseband phase; independent of disturbances).
        lo = sorted(fx.lo)
        f_lo, li = [], 0
        cur = CENTER0_HZ
        for t in tp:
            while li < len(lo) and lo[li][0] <= t:
                cur = lo[li][1]
                li += 1
            f_lo.append(cur)
        tone_s, tone_p = [0.0] * n0, [0.0] * n0
        for n in range(1, n0):
            dt = (tp[n] - tp[n - 1]) * 1e-9
            tone_s[n] = tone_s[n - 1] + 2 * math.pi * (SCENE_RF_HZ - f_lo[n - 1]) * dt
            tone_p[n] = tone_p[n - 1] + 2 * math.pi * (PILOT_RF_HZ - f_lo[n - 1]) * dt
        # Term phases per component (only delay_step differs between components).
        terms = ("X", "H", "R", "J", "S", "G", "A", "FG", "FA")
        ph_s = {k: [0.0] * n0 for k in terms}
        ph_p = {k: [0.0] * n0 for k in terms}
        for d in self.disturbances:
            if d.target not in terms:
                continue  # K only reaches the witness
            if d.kind == "delay_step":
                for n in range(n0):
                    if d.target == "G":   # RF stage: RF frequency
                        fs_, fp_ = SCENE_RF_HZ, PILOT_RF_HZ
                    else:                 # baseband stage: baseband frequency
                        fs_, fp_ = SCENE_RF_HZ - f_lo[n], PILOT_RF_HZ - f_lo[n]
                    ph_s[d.target][n] += d.phase(tp[n], fs_)
                    ph_p[d.target][n] += d.phase(tp[n], fp_)
            else:
                for n in range(n0):
                    v = d.phase(tp[n])
                    if v:
                        ph_s[d.target][n] += v
                        ph_p[d.target][n] += v
        self.ph_s, self.ph_p = ph_s, ph_p
        # Selector: physical port and cross-fade weight per sample.
        sw = self.plan["switches"]
        a_s = SCENE_AMP
        a_p = SCENE_AMP * 10 ** (sc.pilot_rel_db / 20) if self.pilot_on_cfg else 0.0
        sigma = SCENE_AMP / 10 ** (sc.snr_db / 20) / math.sqrt(2)
        noise = stream(self.master, "noise")
        gains = sorted(fx.gain)
        iq: List[complex] = []
        self.port_phys: List[str] = []
        si, prev_port, port = 0, "TERM", "TERM"
        for n in range(n0):
            t = tp[n]
            while si < len(sw) and sw[si]["t"] <= t:
                prev_port, port = port, sw[si]["port"]
                si += 1
            w = 1.0
            if si:
                s = sw[si - 1]
                if t < s["t"] + s["settle"]:
                    w = (t - s["t"]) / s["settle"]
            self.port_phys.append(port if w >= 1.0 else "SETTLING")

            def path(p):
                if p == "H":
                    return cmath.exp(1j * ph_s["H"][n])
                if p == "R":
                    return cmath.exp(1j * ph_s["R"][n])
                return 0j
            common = tone_s[n] + ph_s["X"][n] + ph_s["G"][n] + ph_s["A"][n]
            v = a_s * cmath.exp(1j * common) * ((1 - w) * path(prev_port) + w * path(port))
            if a_p and not any(a <= t < b for a, b in fx.pilot_off):
                pp = (tone_p[n] + ph_p["S"][n] + ph_p["J"][n] + ph_p["G"][n] + ph_p["A"][n]
                      + ph_p["FG"][n] + ph_p["FA"][n])
                v += a_p * cmath.exp(1j * pp)
            v += complex(noise.gauss(0.0, sigma), noise.gauss(0.0, sigma))
            g = 1.0
            for tg, fac, st in gains:
                if t >= tg:
                    g *= 1 + (fac - 1) * min(1.0, (t - tg) / st)
            v *= g
            for tb, dur, amp, fb in fx.bursts:
                if tb <= t < tb + dur:
                    v += amp * cmath.exp(2j * math.pi * fb * t * 1e-9)
            iq.append(v)
        self.iq_float = iq
        # Witness (instrument timebase), evaluated at its own physical times.
        self.witness: List[dict] = []
        if self.witness_on:
            wn = stream(self.master, "witness_noise")
            wh = stream(self.master, "witness_invalid_values")
            for seq, t in enumerate(self.plan["witness_t"]):
                ph = WITNESS_PHASE0 + sum(d.phase(t) for d in self.disturbances if d.target in ("R", "K"))
                z = WITNESS_MAG * cmath.exp(1j * ph) + complex(wn.gauss(0, WITNESS_NOISE), wn.gauss(0, WITNESS_NOISE))
                valid = not any(a <= t < b for a, b in fx.wit_invalid)
                if not valid:
                    z = complex(wh.uniform(-1, 1), wh.uniform(-1, 1))
                self.witness.append({
                    "seq": seq, "timebase": "wit0", "t_ns": int(round(t + self.plan["wit_offset"])),
                    "instrument": "simulated witness", "plane_cal_id": "sim-p0p1",
                    "ratio_re": round(z.real, 12), "ratio_im": round(z.imag, 12),
                    "phase_std_rad": round(WITNESS_NOISE / WITNESS_MAG, 12), "valid": valid})

    # ---------------------------------------------------------------- outputs

    def quantized(self) -> array.array:
        """Interleaved int16 I/Q after dropping lost samples; saturating."""
        a = array.array("h")
        for n, v in enumerate(self.iq_float):
            if self.clock.file_index(n) is None:
                continue
            a.append(max(-32768, min(32767, int(round(v.real)))))
            a.append(max(-32768, min(32767, int(round(v.imag)))))
        return a

    def events(self) -> List[dict]:
        ck, fx = self.clock, self.fx
        evs: List[Tuple[int, int, dict]] = []
        order = 0
        for s in self.plan["switches"]:
            evs.append((ck.dev(s["t"]), order, {"type": "SWITCH_COMMAND", "port": s["port"]}))
            order += 1
            if s["report"]:
                evs.append((ck.dev(s["t"] + s["settle"] + s["report_lat"]), order,
                            {"type": "SETTLED", "stream_id": "rx0", "cause": "switch"}))
                order += 1
        for t, e in fx.events:
            evs.append((ck.dev(t), order, dict(e)))
            order += 1
        if ck.drop is not None:
            i, k, known = ck.drop
            evs.append((int(round(ck.dev_exact(i + k))), order,
                        {"type": "OVERFLOW", "stream_id": "rx0", "sample_index": i,
                         "dropped_samples": k if known else None}))
            order += 1
        if ck.jump_index is not None:
            fi = ck.file_index(ck.jump_index)
            if fi is not None and fi > 0:
                evs.append((int(round(ck.dev_exact(ck.jump_index))), order,
                            {"type": "TIME_DISCONTINUITY", "stream_id": "rx0",
                             "sample_index": fi, "jump_ns": ck.jump_ns}))
                order += 1
        evs.sort(key=lambda x: (x[0], x[1]))
        out = []
        for seq, (t, _, e) in enumerate(evs):
            out.append({"seq": seq, "type": e.pop("type"), "timebase": "sdr0", "t_ns": t, **e})
        return out

    def sample_count(self):
        return self.clock.n0 - (self.clock.drop[1] if self.clock.drop else 0)

    def truth_extra(self) -> dict:
        p = self.plan
        return {
            "generator": GENERATOR_VERSION, "case": self.sc.case, "config": self.sc.config,
            "seed_hex": self.master.hex(),
            "scenario": {"duration_ns": self.sc.duration_ns, "fs_hz": self.sc.fs_hz,
                         "snr_db": self.sc.snr_db, "pilot_rel_db": self.sc.pilot_rel_db,
                         "background": self.sc.background},
            "clock": {"eps": p["eps"], "t0_dev_ns": p["t0_dev"],
                      "phys_to_device": "dev = t0_dev + t_phys/(1+eps) [+ jump after jump sample]",
                      "witness_true_offset_ns": p["wit_offset"],
                      "declared_alignment_error_ns": p["wit_align_err"]},
            "tones_hz": {"scene_rf": SCENE_RF_HZ, "pilot_rf": PILOT_RF_HZ, "center0": CENTER0_HZ},
            "switches": p["switches"],
            "acquisition_faults": self.fx.notes,
        }


def simulate(scenario: Scenario, seed) -> Simulation:
    return Simulation(scenario, normalize_seed(seed))


def _inside(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return os.path.commonpath([child, parent]) == parent


def generate(scenario: Scenario, seed, capture_dir, truth_path) -> dict:
    """Write one capture directory and its sealed truth file. Returns the manifest."""
    master = normalize_seed(seed)
    if _inside(truth_path, capture_dir):
        raise ValueError("truth file must be stored outside the capture directory")
    if os.path.exists(capture_dir) and os.listdir(capture_dir):
        raise FileExistsError(f"capture directory {capture_dir!r} is not empty")
    if os.path.exists(truth_path):
        raise FileExistsError(f"truth file {truth_path!r} already exists")
    sim = Simulation(scenario, master)
    pilot, witness = EMITTED[scenario.config]
    cid = capture_id_for(master)
    os.makedirs(capture_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(truth_path)), exist_ok=True)

    iq = sim.quantized()
    if sys.byteorder != "little":
        iq.byteswap()
    files = {"rx0.sc16": iq.tobytes(),
             "events.jsonl": "".join(json.dumps(e) + "\n" for e in sim.events()).encode()}
    if witness:
        files["witness.jsonl"] = "".join(json.dumps(w) + "\n" for w in sim.witness).encode()
    for name, data in files.items():
        with open(os.path.join(capture_dir, name), "wb") as f:
            f.write(data)
    sha = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}

    commitment = sealed_truth.write_truth(
        truth_path, cid, PHYS, [d.to_truth() for d in sim.disturbances],
        salt=derive_bytes(master, "truth_salt"), extra=sim.truth_extra())

    p = sim.plan
    timebases = [{"id": "sdr0", "kind": "sdr_device"}]
    aligns = []
    if witness:
        timebases.append({"id": "wit0", "kind": "instrument"})
        aligns.append({"from": "wit0", "to": "sdr0",
                       "offset_ns": p["t0_dev"] - p["wit_offset"] + p["wit_align_err"],
                       "uncertainty_ns": p["wit_align_unc"], "method": "shared_pps_trigger"})
    manifest = {
        "schema": C.SCHEMA_MANIFEST, "capture_id": cid, "created_utc": FIXED_CREATED,
        "graph_version": C.GRAPH_VERSION,
        "provenance": {"kind": "synthetic", "operator": "blind generator", "software": GENERATOR_VERSION},
        "device": {"model": "simulated single channel", "serial": None, "driver": "none",
                   "fpga_image_sha256": None},
        "clock": {"ref_source": "external_10mhz", "pps_source": "external", "ref_locked_at_start": True},
        "timebases": timebases, "timebase_alignments": aligns,
        "settling": {"switch_guard_ns": SWITCH_GUARD, "retune_guard_ns": RETUNE_GUARD,
                     "gain_guard_ns": GAIN_GUARD},
        "streams": [{
            "stream_id": "rx0", "channel": 0, "file": "rx0.sc16", "sha256": sha["rx0.sc16"],
            "format": "sc16_le", "sample_count": sim.sample_count(), "sample_rate_hz": scenario.fs_hz,
            "center_freq_hz": CENTER0_HZ, "rx_gain_db": BASE_GAIN_DB, "timebase": "sdr0",
            "t0_ns": p["t0_dev"], "clip_level": CLIP_LEVEL,
            "carries": ["scene", "reference"] + (["pilot"] if pilot else []),
            "pilot": {"offset_hz": PILOT_RF_HZ - CENTER0_HZ, "waveform_id": "cw"} if pilot else None,
        }],
        "events_file": "events.jsonl", "events_sha256": sha["events.jsonl"],
        "witness_file": "witness.jsonl" if witness else None,
        "witness_sha256": sha.get("witness.jsonl"),
        "qualification_ref": None,
        "truth_commitment": commitment,
    }
    C.validate_manifest(manifest)
    mpath = os.path.join(capture_dir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")
    for name in list(files) + ["manifest.json"]:
        os.utime(os.path.join(capture_dir, name), (FIXED_MTIME, FIXED_MTIME))
    os.utime(capture_dir, (FIXED_MTIME, FIXED_MTIME))
    return manifest


def sensor_phases(sim: Simulation) -> Dict[str, List[Tuple[float, float]]]:
    """Noise-free term phases per emitted sensor (tone phase excluded).

    Used to check the renderer against the observation graph. scene/reference
    are listed only for samples where the selector is settled on H/R.
    """
    out = {"scene": [], "reference": [], "pilot_sampled": [], "witness": []}
    for n, t in enumerate(sim.tp):
        port = sim.port_phys[n]
        base = sim.ph_s["X"][n] + sim.ph_s["G"][n] + sim.ph_s["A"][n]
        if port == "H":
            out["scene"].append((t, base + sim.ph_s["H"][n]))
        elif port == "R":
            out["reference"].append((t, base + sim.ph_s["R"][n]))
        if sim.pilot_on_cfg:
            out["pilot_sampled"].append((t, sim.ph_p["S"][n] + sim.ph_p["J"][n] + sim.ph_p["G"][n]
                                         + sim.ph_p["A"][n] + sim.ph_p["FG"][n] + sim.ph_p["FA"][n]))
    if sim.witness_on:
        for t in sim.plan["witness_t"]:
            out["witness"].append((t, sum(d.phase(t) for d in sim.disturbances if d.target in ("R", "K"))))
    return out


def configured_sensors(config):
    return CONFIGS[config][0]
