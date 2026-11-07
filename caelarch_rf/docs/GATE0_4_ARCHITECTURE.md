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
the future disturbance generator): `X H R K J S G A FG FA`. `FG`/`FA` are the
pilot-vs-scene frequency mismatch of the G and A transfers. `U=AG` is the
combined receiver phase the pilot targets.

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
5. **New finding: known-source drift X mimics receiver drift in bracket-only
   data** (`{X,G}` and `{X,A}` are circuits in C0/C1). The W0/W1 ratio cancels
   X, so the witness doesn't help here. The handoff treats the RF source as
   "known". That assumption needs its own bound or monitor, or Gate 0.3
   bracket conclusions about AG are conditional on it.

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
| R1 | Known RF source phase X is unmonitored | Confounded with G and A in bracket data (§2.5) | Add an X bound to the qualification plan, or a source-referenced monitor. Re-read Gate 0.3 claims as conditional on X |
| R2 | Pilot source S and injection J drift | Indistinguishable from U=AG with one pilot | Specify an independent PS→J stability test, or a second, physically distinct observable. Without it, no G attribution |
| R3 | Frequency-selective transfer (FG, FA) | A single offset pilot only validates U at its own frequency | Measure transfer equivalence across the scene band. Bound FG and FA separately |
| R4 | Timestamp-aligned 2-channel RX on the B220mini is unconfirmed | Contract alignment and overflow semantics depend on it | Vendor evidence (P0.4). If overflows report no drop count, every overflow untimes the rest of the stream (by design) |
| R5 | Analog monitor location | It can't sit inside the SDR, so it never sees A | Treat option B as a G-only monitor. Decide option A vs. A+B with the cost audit (P1.4) |
| R6 | Witness-to-SDR timebase alignment | A witness is useless for freshness without a measured alignment | Bench method (`shared_pps_trigger` or `loopback_xcorr`) with an uncertainty budget |
| R7 | Pilot leakage, dynamic range and spectral occupancy | The pilot is not free. It can mask scene changes or desensitise the receiver | Score in P1.4. Contract records `PILOT_STATE` so off-pilot baselines are possible |
| R8 | Inter-sample and out-of-band pulses | Sampled monitors cannot see them. The bracket cannot either | Report them as missed events with exposure-time denominators. Never invent an online alarm |
| R9 | No qualification-record authority | VALID_BOUNDED has no issuer | Decide who signs physical records, the key custody and the registry before Gate 0 signoff |
| R10 | Code lives in the JustiKey repo | No dedicated Caelarch repository was available in this session | Move `caelarch_rf/` to its own repository with history (`git subtree split`) |

## 5. Preliminary sprint breakdown

| Sprint | Items | Exit criterion | Blocked by vendor? |
|---|---|---|---|
| **0 (this change)** | P0.2 observation graph + matrix. P0.3 contract, validator, ingestion, truth isolation. P0.8 four-state adapter. Monitor evidence record | Team review of planes, contract and risks | No |
| 0 (open) | P0.1 import historical releases into a dedicated repo with tags and archive hashes (see `PROVENANCE.md`). P0.4 vendor gate | Historical tests pass unmodified in CI | P0.4 only |
| 1a | Blind disturbance generator over the impairment ids. It emits a contract-conformant capture plus a separate sealed truth with commitment | All brief cases (H-only … retune/clip faults) generated. The online loader can't see truth | No |
| 1b | P0.5 sampled pilot extractor: raw IQ → `PilotObservation` with latency, unwrap continuity and gap handling | Known synthetic steps recovered inside the stated coverage. Leakage and clipping tests | No |
| 1c | P0.6 analog quadrature model: detector bandwidth, impulse response, dead time, tap at P4 | Shows the detector misses A-side events and out-of-band glitches | No |
| 1d | P0.7 attribution guard + window policy on `OnlineCapture` + records → public state | Matched-observable negative controls give the same online state | No |
| 1e | Offline scorer: detection vs. attribution, false alarms per exposure minute, latency, missed events, trial-cluster bootstrap CIs | Uses `open_for_scoring()` only after decisions are frozen | No |
| 2 | P1.1–P1.4 instrumentation, conducted wiring review, blind controller, cost audit. B220mini adapter writing this contract | Gate 0 evidence package | **Yes**, plus procurement signoff |

Sprints 0 and 1 need nothing from CelonixLab. The hardware adapter in Sprint 2
is the first component that does. Everything upstream reads only the
device-agnostic contract.
