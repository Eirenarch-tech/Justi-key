# Sprints 1b–1c: sampled-pilot extractor and analog-monitor model

**Status:** implemented and tested on synthetic captures only. No detection
threshold here is a performance claim. Each capture reports its own envelope,
and thresholds belong to the Sprint 1e scorer under a declared noise and
disturbance envelope.

Online code: `gate04/monitors.py`. It takes an `OnlineCapture` and nothing
else; the isolation test covers it. Offline study code: `sim/leakage.py`.

## Three capabilities, kept separate

| Capability | What these monitors do |
|---|---|
| **Detection** (a measurement changed) | Yes, within the envelope each capture reports |
| **Attribution** (which cause) | **No.** Every excursion is `UNATTRIBUTED` and lists every graph term that moves the sensor |
| **Correction** (estimate the error well enough to compensate) | Not attempted. The observations carry phase and uncertainty for a later sprint |

## Sampled-pilot extractor (1b): `extract_sampled_pilot`

The pipeline runs per block of 100 accepted, timed samples (100 µs at 1 MS/s):

1. **NCO mix-down driven by device timestamps.** The frequency follows
   `LO_RETUNE` (pilot RF minus the current LO). Phase advances by elapsed
   time, so known drops keep the nominal phase.
2. **Frequency-offset compensation.** First an acquisition phase: a
   full-gain running mean over 4 innovations, which never seeds σ or raises
   an alarm. Then tracking: gain 0.2, **lagged one block**, so the first
   half of a step that straddles a block boundary doesn't train the tracker.
3. **Estimate.** A Hann-weighted block mean, which rejects the scene tone 75
   kHz away. The difference between the two half-block estimates gives the
   noise variance.
4. **Track.** Causal prediction, a wrapped innovation, and unwrapping only
   within a continuity segment.
5. **Detect.** |innovation| > kσ, *or* the sum of two consecutive
   innovations > kσ√2 (a straddling step). σ comes from an EMA winsorised at
   3σ, so sub-threshold parts of an excursion don't raise the floor.

Each `PilotObservation` reports `available_ns = window_end + latency`, its
`continuity_id`, the tracked `freq_offset_hz` and its `innovation_rad`. The
pilot is injected downstream of the selector, so selector settling is
**ignored** for pilot samples. That rests on the planned plane ordering and
must be confirmed on the bench (risk R14).

## Analog-monitor model (1c)

**Generator (`sim/generator.py`, configs C6 and C6s).** The quadrature
detector sits at P4. Its output is (signal at P4) × conj(reference):

| Reference | Pilot term | Not seen |
|---|---|---|
| Independent oscillator (C6) | `S + J + G + FG − M` | A, FA |
| Tapped from the pilot source at PS (C6s) | `J + G + FG − M` (S cancels) | A, FA, S |

The rest of the model:
* two first-order sections set to an effective −3 dB bandwidth (default
  10 kHz);
* complex detector noise, saturation at ±1.0, and scene leakage (see below);
* `M` drift (random walk: 2 rad/√s independent, 0.5 rad/√s source-referenced);
* sub-sample pulses enter through their exact interval overlap, so the
  detector sees them attenuated by its bandwidth, while the SDR point
  samples miss them;
* the detector is logged by its own ADC at 200 kS/s on its own clock
  (`mon0`), with periodic dead time recorded as known-count `OVERFLOW`
  events.

**Online (`extract_analog_monitor`).** The same tracker runs on 20-sample
(100 µs) blocks with no NCO. Records carry the declared detector bandwidth.

## Acceptance table to tests (`tests/test_monitors.py`)

| Case | Behaviour shown | Test |
|---|---|---|
| Receiver phase step | G and A steps of 0.5 rad are detected inside the capture's reported floor and latency bound. A 0.05 rad step (below the floor) is missed, not invented. No excursion before onset | `TestReceiverStep` |
| Pilot-source/injection drift | J and S steps are detected but `UNATTRIBUTED`, with candidates = every pilot term. The window state is UNRESOLVED. Slow S drift and the pilot frequency offset are tracked without alarms | `TestNoFalseReceiverAttribution` |
| Matched-observation controls | Excursion lists and continuity segments are identical for `{J,S}`, `{G,A}` and `{X,J,G}` (C2), `{G,A,M}` and `{J,FA,M}` (C6), and `{G,A,M}` (C6s). The change is detected in each case, never attributed | `TestMatchedControlsSurviveTheMonitors` |
| Unknown sample loss | Coverage ends with `UNTIMED` (open-ended). No observation after it. Window state INVALID | `TestUnknownSampleLoss` |
| Pilot interruption | A `PILOT_OFF` interval is reported. Continuity ids after it are strictly greater. INVALID inside it | `TestPilotInterruption` |
| Phase wrapping | A fast ramp gives ambiguous excursions and new segments, never an unwrap step beyond the limit. A slow 12 rad ramp is unwrapped to within 0.3 rad in one segment. A known gap restarts unwrapping | `TestPhaseWrapping` |
| Intersample glitch | Sampled: no detection, with a limitation stating point sampling "IN THIS MODEL". Analog: a sub-µs pulse is attenuated below the floor and a 150 µs pulse is detected. A 60 µs pulse is missed honestly without inflating the floor | `TestIntersampleGlitch` |
| Analog detector drift M | M moves only the analog monitor and stays unattributed. An A step (downstream of P4) is invisible to it. An S step cancels in C6s; J does not | `TestAnalogDetectorDriftM` |
| Reference clock jump | `mon0`↔`sdr0` and witness alignments are void after the jump. Sampled-pilot continuity segments before and after are disjoint | `TestReferenceClockJump` |
| Pilot leakage into scene | Quantified in both directions and into the analog monitor (below) | `TestPilotLeakage` |
| False alarms | No excursions in 6 baseline captures (more than 1000 monitored blocks). A wider ad-hoc run gave 0 in about 11,700 blocks | `TestFalseAlarms` |

## Reported envelope (per capture, not universal)

At the default illustrative noise (scene SNR 40 dB, pilot −20 dB, A walk 1
rad/√s, S walk 2 rad/√s): σ_innovation ≈ 0.033 rad per 100 µs block
(sampled). That gives a single-block floor of 6σ ≈ 0.20 rad and a worst-case
step floor (straddling a block boundary) of 6σ√2 ≈ 0.27–0.28 rad. Steps above
π/2 within one block are ambiguous by 2π. Latency is at most 2 blocks plus
the declared processing latency. The independent-reference analog monitor
gives a floor of about 0.15–0.24 rad; the source-referenced one about 0.04 rad
(S and its frequency offset cancel).

## Pilot ↔ scene interference (`sim/leakage.py`)

Computed by counterfactual renders with the same seed:

| Direction | RMS | Max | Steady-state Hann bound |
|---|---|---|---|
| Pilot → scene phase | ~6×10⁻⁵ rad | ~1.5×10⁻⁴ rad | 7.7×10⁻⁵ rad |
| Scene → sampled pilot | ~0.006 rad | ~0.013 rad | 0.0077 rad |
| Scene → analog monitor | ~0.003 rad | ~0.007 rad | n/a |

The RMS stays within the steady-state bound. The maximum exceeds it by about
2×, because of selector cross-fade transients, which reach the pilot because
the extractor ignores switch settling. All of these are at least 20× below
the detection floor at this pilot level. **Scene phase steps** splatter more:
a 0.3 rad scene step at 20 dB above the pilot visibly distorts the pilot
estimate of the block containing it. This is real physics, not a detection
mechanism.

## Finding: scene leakage gives the P4 detector an unintended channel

Leakage carries `X + H/R + G − M` (and `−S` for C6s) into the detector
stream. As a result, 10 of the 27 C6 circuits and 9 of the 28 C6s circuits are
**not** information-theoretically indistinguishable. The raw `mon0` streams
differ, and with the scene removed they match again. The current monitors
can't exploit the difference (excursions identical, phases within 0.05 rad).
`OBSERVABILITY_MATRIX.md` lists the affected circuits, computed by
`leakage_breaks()`. Any future estimator that uses leakage must declare it as
an observation channel.

## Limitations

* Ideal point sampling in the SDR path. There is still no anti-alias
  front-end model (R11).
* The analog "continuous time" runs on the 1 µs SDR grid, except for the
  exact overlap of short pulses. No detector non-linearity beyond
  saturation, and no comparator.
* One pilot frequency, so FG and FA are not separable from the pilot alone
  (QR-F).
* A changed parameter set changes the envelope. Detection probability,
  false-alarm rate per exposure time and confidence intervals belong to the
  Sprint 1e scorer.
