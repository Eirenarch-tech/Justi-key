# CAE-RF Gate 0.4: observation architecture, risks and sprint plan

**Status:** draft for review. Software and simulation only. Gate 0A–0D are
**NOT PASSED** and Stage A is **NOT AUTHORIZED**. No hardware has been
selected: the B220mini is still an evaluation candidate, pending CelonixLab's
written technical confirmations.

This document is the "architecture diagram, preliminary sprint breakdown,
measurement-data contract and unresolved technical risks" that the handoff
cover note asks for before code integration or hardware orders. The contract
itself is in [`MEASUREMENT_CONTRACT.md`](MEASUREMENT_CONTRACT.md). The
generated identifiability tables are in
[`OBSERVABILITY_MATRIX.md`](OBSERVABILITY_MATRIX.md).

## 1. Observation graph (graph version `gate04-graph/0.1`)

```
            P0                                   P2          P3                P4                 ADC
 Known  ----+---[ H: scene channel ]------------+                                                   
 RF src  X  |                                    [selector]---[coupler]--[ G: ext RX chain ]--[ A: SDR conv ]--> sampled IQ
            +---[ R: reference path ]--+--------+    ^            ^                   |                   |
            |                          P1            |            |  J                 |                   +--> pilot extractor (option A)
           W0 ------- witness K ------ W1         SWITCH_      PS: pilot source S      +--> analog quadrature
                     (instrument)                 COMMAND                               monitor tap at P4 (option B)
                                                                                         sees S+J+G, NOT A
```

| Plane | Definition |
|---|---|
| **P0** | Known RF source output after the splitter. The H and R inputs, and W0, are referred here |
| **P1** | Reference path output, at the selector R port. W1 is referred here; the witness measures W1/W0 ≈ R·K |
| **P2** | Selector common output, at the coupler through-port input |
| **P3** | Coupler output: scene/reference plus injected pilot. G starts here |
| **P4** | SDR RF connector. G ends here and A (internal LO, filters, sample clock) starts |
| **PS** | Pilot source output. J is the path PS → coupler → P3 |
| **ADC** | Sampled IQ. The digital pilot extractor and scene processing act here |

Impairment ids (shared by the observability model, the sealed-truth schema and
the Sprint 1a generator): `X H R K J S G A FG FA M`. `FG`/`FA` are the
pilot-vs-scene frequency mismatch of the G and A transfers. `M` is the analog
detector's own reference and electronics phase: a quadrature detector reports
signal phase *minus* its reference phase. `U=AG` is the combined receiver
phase the pilot targets.

## 2. What the structural model says

From `gate04/observability.py`. These are exact rank results on a linearised
phase model, so they are necessary conditions for a claim, not sufficient
ones. Each is a regression test.

1. **The pilot cannot see R** (option A or B). Reference-path drift still
   needs the independent witness. Without it the result is UNRESOLVED.
2. **An analog monitor at P4 cannot see A.** Internal SDR LO and conversion
   events are invisible to any tap ahead of the SDR.
3. **Detection is not attribution.** With the sampled pilot, `{J,S}` and
   `{G,A}` are minimal confounding sets. `U=AG` becomes estimable only after
   adding *both* a source+injection drift bound and a frequency-mismatch bound.
   Either bound alone leaves it confounded. Its best public state is
   CONDITIONAL until a signed record backs those bounds.
4. **No configuration attributes G alone from online data**, including the
   one with every candidate sensor.
5. **Known-source drift X is an identifiability problem (R1, now QR-X).**
   `{X,G}` and `{X,A}` are circuits in C0/C1, and the W0/W1 ratio cancels X.
   In the Gate 0.3 baseline (C1), `U=AG` is estimable only under
   **{QR-K, QR-X}**, and **only at reference observation instants**. A
   stable source does not make receiver excursions *between* references
   observable: the Gate 0.3 inter-reference gap stays open. Only a
   continuously observing sensor (sampled pilot or analog detector) sees
   between references, and only inside its coverage envelope. The ambiguity is configuration-specific: with a sampled
   pilot (C2), `{X,G}` is no longer a circuit, although `{X,J,G}` and
   `{X,S,G}` are. C2 offers two independent routes to `U`: {QR-K, QR-X}
   *or* {QR-SJ, QR-F}.
6. **The analog monitor's own drift weakens option B.** With an
   independent-reference detector (C3), attributing G now needs
   {QR-SJ, QR-F, **QR-M**}. Without QR-M, G is confounded. Referencing the
   detector to the pilot source (C3s) cancels S but not J or M, so G needs
   {QR-J, QR-F, QR-M}.
7. **Qualification requirements are computed, not hand-listed.**
   `OBSERVABILITY_MATRIX.md` lists, for every configuration and every
   conditionally estimable quantity, the minimal sets of QR-K, QR-X, QR-SJ,
   QR-J, QR-F and QR-M that a signed record must satisfy.
8. **Scene leakage gives the P4 detector an unintended channel** (Sprint 1c).
   The detector weakly observes `X+H/R+G−M` (and `−S` when
   source-referenced). This breaks 10 of the 27 C6 and 9 of the 28 C6s
   circuits *structurally*. That establishes neither detectability under
   finite noise nor a usable estimator, so leakage earns no attribution
   credit. See `MONITORS.md`.
9. **Step monitors do not bound drift** (Sprint 1e). Frequency tracking
   absorbs slow receiver ramps by construction. In the blind study,
   P(CONDITIONAL | gradual U change) ≈ 0.98 for the pilot guard, against 0/16
   for abrupt changes of 0.4 rad or more.
10. **Non-detection is not zero change** (Sprint 1d). A still sensor may
    exclude a cause only if it was sensitive enough to have seen that cause's
    predicted change. Without this rule, 11% of detected windows excluded the
    true cause.

What the model leaves out: noise, amplitude, time multiplexing of scene and
reference, sampling, detector bandwidth, non-linearity. Those belong to the
Sprint 1 waveform simulations and the blind scorer.

## 3. Public states

`gate04/states.py` defines `VALID_BOUNDED / CONDITIONAL / UNRESOLVED / INVALID`
and maps every Gate 0.3 `assess_online_window` status conservatively. An
unknown status maps to INVALID. Retune or stale qualification, a missing
required monitor, or a span beyond the envelope maps to INVALID. Monitor
alarms and missing witnesses map to UNRESOLVED. The `*_BOUNDED_MODEL_ONLY`
statuses map to CONDITIONAL. **VALID_BOUNDED is unreachable.**
`promote_to_valid_bounded()` raises until an authenticated
qualification-record verifier exists.

## 4. Unresolved technical risks

| # | Risk | Why it matters | Mitigation / owner action |
|---|---|---|---|
| R1 | Known RF source phase X is unmonitored | Confounded with G and A in bracket data (§2.5) | **QR-X** is now an explicit qualification requirement. Gate 0.3 AG conclusions are conditional on it |
| R2 | Pilot source S and injection J drift | Indistinguishable from U=AG with one pilot | Specify an independent PS→J stability test, or a second, physically distinct observable. Without it, no G attribution |
| R3 | Frequency-selective transfer (FG, FA) | A single offset pilot only validates U at its own frequency | Measure transfer equivalence across the scene band. Bound FG and FA separately |
| R4 | Timestamp-aligned 2-channel RX on the B220mini is unconfirmed | Contract alignment and overflow semantics depend on it | Vendor evidence (P0.4). If overflows report no drop count, every overflow untimes the rest of the stream (by design) |
| R5 | Analog monitor location and own drift | It can't sit inside the SDR, so it never sees A. Its reference adds M | Treat option B as a G-only monitor that needs QR-M. Consider a source-referenced detector (cancels S). Decide with the cost audit (P1.4) |
| R6 | Witness-to-SDR timebase alignment | A witness is useless for freshness without a measured alignment | Bench method (`shared_pps_trigger` or `loopback_xcorr`) with an uncertainty budget |
| R7 | Pilot leakage, dynamic range and spectral occupancy | The pilot is not free. It can mask scene changes or desensitise the receiver | Score in P1.4. Contract records `PILOT_STATE` so off-pilot baselines are possible |
| R8 | Inter-sample and out-of-band pulses | Sampled monitors cannot see them. The bracket cannot either | Report them as missed events with exposure-time denominators. Never invent an online alarm |
| R9 | No qualification-record authority | VALID_BOUNDED has no issuer | Decide who signs physical records, the key custody and the registry before Gate 0 signoff |
| R10 | Code lives in the JustiKey repo | No dedicated Caelarch repository was available in this session | Move `caelarch_rf/` to its own repository with history (`git subtree split`), without interrupting current work |
| R11 | Ideal point sampling in the generator | "Intersample glitches are invisible" is a model artifact. A real anti-alias filter leaks them | Sprint 1c front-end model. No unobservability claims from 1a captures |
| R12 | Lock-detect and settling reports lag physics | Unlocked or unsettled samples before the report, or beyond a short guard, are accepted online | Generator hazards `ref_unlock` and `short_guard`. Measure real latencies and guards on the bench (P1.x). The scorer counts accepted-but-bad samples |
| R13 | Clock discontinuities invalidate timebase alignments | A witness mapped across a jump is misplaced by the jump | Fixed with alignment **epochs**: after a jump, no cross-clock witness is fresh, whatever `max_age`, until a requalified epoch (`valid_from_ns`) is declared. A bench re-alignment procedure is still needed |
| R14 | Pilot extractor ignores selector settling | Valid only if selector transients don't couple into the pilot path (pilot injected downstream at P3) | Bench check of the selector → pilot coupling. Synthetic cross-fade leakage is about 0.013 rad max, 20× below the floor |
| R15 | Scene transients splatter into the pilot band | A scene step 20 dB above the pilot distorts the pilot estimate of that block | Quantified in `sim/leakage.py`. Consider pilot level, block length and window choice in Sprint 1e |
| R16 | Unintended leakage channels | The detector's scene leakage breaks some structural circuits | Any estimator that uses it must declare it. `leakage_breaks()` lists the affected circuits |
| R17 | Slow drift invisible to step monitors | CONDITIONAL during gradual receiver drift | Proposed Sprint 1f. Until then, a CONDITIONAL from continuous monitors bounds steps, not drift |
| R19 | Generator lacked real-hardware artefacts (**now modelled**, opt-in presets; see `HARDWARE_ARTEFACTS.md`) | Public recordings show DC/LO leakage up to 34 dB over the median bin, image rejection down to 27 dB, frequent clipping (median 5% of samples), 8-bit quantisation and bursts (`PUBLIC_IQ_CHECK.md`) | Add DC, IQ imbalance, quantisation, saturation and bursts to the generator; finer clip granularity |
| R20 | Leakage reaches guard narrowing through a noise-floor estimate | For a leakage-broken matched pair, the still analog monitor's causal floor differed enough to flip one exclusion decision. No attribution, but leakage information influenced a decision (P2) | Option: compute exclusion floors from a leakage-rejecting statistic, or add hysteresis and report borderline exclusions as non-exclusions. Until then, narrowing involving the analog monitor is informational only |
| R21 | No interference rejection in the extractor | Under the adverse receiver preset: 95 false alarms, all during bursts that were mostly off the pilot frequency but about 24 dB above it; P(CONDITIONAL \| quiet) 0.83 → 0.47 (`HARDWARE_ARTEFACTS.md`) | Block-level interference detection, pilot isolation and frequency planning, a separate scoring category. Measure the real conducted-bench interference environment |
| R18 | False-alarm evidence limited by exposure | Zero false alarms in about 0.03 min bounds the rate only at about 110/min | Longer synthetic captures now. Long bench runs later |

## 5. Preliminary sprint breakdown

| Sprint | Items | Exit criterion | Blocked by vendor? |
|---|---|---|---|
| **0 (this change)** | P0.2 observation graph + matrix. P0.3 contract, validator, ingestion, truth isolation. P0.8 four-state adapter. Monitor evidence record | Team review of planes, contract and risks | No |
| 0 (open) | P0.1 import historical releases into a dedicated repo with tags and archive hashes (see `PROVENANCE.md`). P0.4 vendor gate | Historical tests pass unmodified in CI | P0.4 only |
| **1a (done)** | Blind disturbance generator, matched-observation pairs per configuration, blindness controls ([`BLIND_GENERATOR.md`](BLIND_GENERATOR.md)) | Reproducible, contract-valid, causal captures. Negative controls show physically different disturbances are observationally equivalent | No |
| **1b (done)** | Sampled pilot extractor: raw IQ → `PilotObservation` with frequency compensation, latency, unwrap continuity and gap handling ([`MONITORS.md`](MONITORS.md)) | Every row of the 1b–1c acceptance table has a test | No |
| **1c (done)** | Analog quadrature model: bandwidth, two-section response, detector noise, saturation, logger dead time, tap at P4, own drift M, independent vs source reference (rendered as C6/C6s) | Misses A-side events, attenuates sub-µs glitches, preserves M ambiguity | No |
| 1c (open) | Front-end anti-alias model (R11) | Revisit the intersample-glitch results | No |
| **1d (done)** | Causal readiness-aware monitors; reference-bracket baseline; online guard combining bracket, pilot, analog and witness across timebases; attribution integrity ([`GUARD_AND_SCORER.md`](GUARD_AND_SCORER.md)) | No future evidence, no CONDITIONAL before requalification, consistent sets always hold the true cause, no unique attribution | No |
| **1e (done)** | Freeze-before-reveal blind scorer; predeclared matching; clustered CIs; Poisson FA bounds; matched-pair controls; resource accounting; 102-capture study ([`SPRINT1E_STUDY.md`](SPRINT1E_STUDY.md)) | Reproducible, statistically defensible evaluation (no detection advantage predetermined) | No |
| 1f (proposed) | Drift monitoring (finding 9): bound slow U drift, e.g. by comparing pilot frequency to a qualified reference, or by bracket-anchored drift estimation; longer captures for FA bounds | P(CONDITIONAL \| gradual) reduced, with an exposure-based FA bound | No |
| 1d | P0.7 attribution guard + window policy on `OnlineCapture` + records → public state | Matched-observable negative controls give the same online state | No |
| 1e | Offline scorer: detection vs. attribution, false alarms per exposure minute, latency, missed events, trial-cluster bootstrap CIs | Uses `open_for_scoring()` only after decisions are frozen | No |
| 2 | P1.1–P1.4 instrumentation, conducted wiring review, blind controller, cost audit. B220mini adapter writing this contract | Gate 0 evidence package | **Yes**, plus procurement signoff |

Sprints 0 and 1 need nothing from CelonixLab. The hardware adapter in Sprint 2
is the first component that does. Everything upstream reads only the
device-agnostic contract.
