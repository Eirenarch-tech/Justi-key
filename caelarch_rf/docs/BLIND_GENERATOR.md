# Sprint 1a: blind disturbance generator

**Status:** implemented, synthetic only. Package `caelarch_rf/sim/` is
**offline only**: it knows the truth. A test checks that no online module
(`gate04/contract`, `ingest`, `records`, `states`, `observability`,
`validate`) imports it or `sealed_truth`.

```bash
python3 -m caelarch_rf.sim.batch --captures OUT/caps --truth OUT/truth --n 40
python3 -m caelarch_rf.gate04.validate OUT/caps/<capture_id>
```

## What it produces

For each `Scenario` and seed, the generator writes two things:

* a `caelarch.capture/0.4` directory: raw `sc16` IQ, `events.jsonl`,
  `witness.jsonl` and `manifest.json`. Every generated capture is checked by
  the same loader an online method uses;
* a sealed-truth file in a **different** directory. It holds every
  disturbance (scheduled, fault-induced and background), the clock model, the
  switch schedule with true settling times, acquisition-fault notes and the
  seed. The manifest carries only the salted commitment.

## Signal and timing model

One SDR channel at `fs` (default 1 MS/s, 20 ms), complex baseband, **ideal
point sampling**:

| Component | Phase terms | Notes |
|---|---|---|
| scene / reference | `X + G + A` + `H` or `R` (by selector port) | Time-multiplexed. Each switch cross-fades over a true settling time |
| pilot | `S + J + G + A + FG + FA` | −20 dB below the scene by default. Only C2 has one |
| witness | `R + K` on its own timebase `wit0` | Records about every 1 ms with jitter. C1 and C2 only |

* **Every graph term except `M`** can be disturbed, in these shapes: `step`,
  `ramp`, `pulse`, `sine`, `random_walk`, `delay_step`, `intersample_pulse`.
  `M` is only seen by the analog monitor, which arrives in Sprint 1c.
* **Frequency-dependent impairments:** `FG`/`FA` steps, and `delay_step` on G
  (evaluated at RF frequency) or A (evaluated at baseband frequency). A
  delay step moves the scene and the pilot by different amounts.
* **Background:** random-walk phase noise on A (LO) and S (unlocked pilot
  source), recorded in truth.
* **Timing:** sample-clock error (±2 ppm), random device-time origin, witness
  timebase offset, and a declared alignment error inside the declared
  uncertainty. Events are scheduled in physical time and stamped through the
  device clock model, so their placement matches the samples.

### Acquisition faults (`AcqFault`)

| Kind | Physical effect | What the online side sees |
|---|---|---|
| `retune` | LO frequency change and random A phase reset over the settling time | `LO_RETUNE` (locked), sometimes `SETTLED` |
| `retune_unlocked` | Unlocked LO with a fast A random walk, then a relock 2 ms later | `LO_RETUNE` (unlocked, then locked) |
| `gain_change` | Amplitude step with a ramp, plus a small hidden A phase step | `GAIN_CHANGE` |
| `ref_unlock` | Fast A random walk. **The lock report lags the physical unlock** | `REF_LOCK` false/true, both late |
| `overflow_known` / `overflow_unknown` | Samples dropped | `OVERFLOW` with or without the drop count |
| `time_discontinuity` | Device timestamps jump; sampling stays continuous | `TIME_DISCONTINUITY` |
| `overload` | Strong interferer burst, saturating int16 | Nothing (detected from clipped samples) |
| `short_guard` | True switch settling is 2–3× the declared guard; no `SETTLED` reports | Nothing. **Unsettled samples are accepted** |
| `pilot_off` | Pilot gated off, with latency | `PILOT_STATE` |
| `witness_invalid` | Witness records flagged invalid, with garbage values | `valid: false` |

`short_guard` and the `ref_unlock` report latency are deliberate hazards. The
contract accepts samples that are physically bad, and the scorer must count
them. The tests show an honest guard accepts no unsettled sample, while a
short guard does.

## Matched-observation negative controls (refinement 1)

`sim/matched.py` builds pairs **per sensor configuration**. A circuit of that
configuration's graph has a null vector `v`. Rendering `a·v⁺` and `a·v⁻` (the
same trajectory shape on disjoint physical causes) with the same seed must
give captures that match within 1 LSB for an online method. The tests check:

* every circuit of C0 (6), C1 (6) and C2 (24) gives equivalent captures, for
  step, ramp and pulse shapes;
* each pair built for a sparser configuration becomes **distinguishable**
  when rendered in a richer configuration where it is no longer a circuit.
  For example, `{X,G}` is equivalent in C0 and C1 and separates in C2. In C2,
  X still hides in `{X,J,G}`;
* a broadband G group delay matches a G phase step plus an FG mismatch step
  (single-pilot frequency ambiguity);
* an intersample glitch is invisible, while an in-band glitch is not.

The analog-monitor configurations (C3, C3s, C5) are covered at the structural
level only, until Sprint 1c renders the monitor and its own drift `M`.

## Blindness beyond import isolation (refinement 3)

| Channel | Rule | Test |
|---|---|---|
| Seeds | ≥128-bit bytes or hex. Integers refused, because a small seed space lets an online method regenerate noise and match | `test_enumerable_seeds_refused` |
| Seed in capture | Never. The seed is stored only in truth | Batch scan for the seed hex |
| Sub-streams | `ops`, `ops_fault/i`, `hidden_fault/i`, `background`, `noise`, `witness_noise` and `disturbance` are derived independently with SHA-256. The operating schedule never reads disturbances | Events, witness timing and manifest are byte-identical across different disturbance schedules |
| Names and parameters | Capture ids are keyed hashes. File names are fixed. The contract allowlist admits no free text | Batch scan for case names, kinds and timebase names |
| Timing patterns | Case templates receive only `(rng, duration, fs)`. Onsets are uniform over the capture, drawn independently of switch times | Template signature test |
| Metadata | `created_utc` and all file mtimes are fixed constants | Batch mtime and created-time check |
| Filesystem | Truth must be outside the capture directory. Batch roots must be disjoint. Captures hold only declared files | Refusal tests and the loader |
| Commitment | Salted with a seed-derived 32-byte salt | `open_for_scoring` round trip |

What an online method can still infer, by design, is what a real bench
would also reveal: the physics in the samples, and the operating events.

## Contract fixes found by the generator

1. **Untimed-tail placement bug (fixed).** An event after an unknown-drop
   overflow resolved to sample 0, so the whole stream was rejected.
   `index_at` now resolves gaps to their boundaries and the untimed tail to
   its start.
2. **Alignment after a clock jump (fixed).** A declared timebase alignment
   stayed in force after `TIME_DISCONTINUITY`, although the generator's truth
   showed it was then wrong by the jump. Alignments are now void from the
   first discontinuity on either timebase (`ALIGNMENT_VOID_AFTER_DISCONTINUITY`).

## Limitations (do not over-read the captures)

* **Ideal point sampling, no anti-alias filter.** "Intersample glitches are
  invisible" is a property of this model. A real front end will smear a
  sub-sample glitch into neighbouring samples, attenuated. Sprint 1c's
  front-end/analog model must revisit this before anyone claims a glitch was
  "unobservable".
* Phase impairments only, apart from the gain step. No IQ imbalance,
  multipath, amplitude drift, non-linearity or DC offset.
* One channel. A two-channel B220mini-class adapter waits on vendor answers.
* The noise and disturbance envelope is illustrative. No detection
  threshold is implied. Thresholds belong to Sprint 1b/1e and must be tied
  to a stated envelope.
* Pure-Python rendering is about 0.1 s per 20 ms capture at 1 MS/s. Fine for
  test batches, slow for large studies.
