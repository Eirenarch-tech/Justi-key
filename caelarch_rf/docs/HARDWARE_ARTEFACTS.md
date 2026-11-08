# Receiver hardware artefacts in the generator

`sim/hardware.py` adds a receiver model to the Sprint 1a generator, calibrated on the public RTL-SDR recordings in [`PUBLIC_IQ_CHECK.md`](PUBLIC_IQ_CHECK.md). It is **opt-in** (`Scenario(hardware=...)`, `study --hardware <preset>`). With no hardware model, captures and truth are bit-identical to before: the Sprint 1e study reproduces its frozen digest `bc23e0af…` exactly.

## Signal chain (SDR stream)

```
signal + noise ──► + interference ──► front-end compression ──► IQ imbalance ──► + DC/LO leakage ──► N-bit ADC (rails)
                   (OOK bursts,         (Rapp, above ADC        (μv + νv*)        (fixed offset,        (midrise; 8-bit gives the
                    Poisson)             full scale)                                random phase)         exact cu8 lattice)
```

* Each artefact draws from its own derived random stream (`hardware`, `interference`). The disturbance schedule, operating events, noise and truth of an ideal capture are untouched, and matched pairs stay equivalent through the front end (tested).
* Interference is physics only. It never appears as an online event. Its schedule is recorded in truth (`generator.hardware.interference`), along with the model and the realised DC phase and image rejection.
* The manifest describes only what an operator would know (`simulated 8-bit receiver`, ADC rail as `clip_level`). The preset name stays in truth.

## Presets and calibration

Targets are the median and 90th percentile of the 29 RTL-SDR `cu8` recordings. The generated values are the median of 8 generated captures, measured with the **same estimator** used on the real files.

| Preset | ADC | DC target → generated (dBFS) | Image rejection target → generated (dB) | IQ gain / phase (generated) | Bursts | Clipping (generated, median / max) |
|---|---|---|---|---|---|---|
| `rtl_sdr_typical` | 8-bit | −57.5 → -57.2 | 64.8 (median) → 64.0 | -0.003 dB / -0.07° | 15.6 /s | 0.0% / 0.0% |
| `rtl_sdr_adverse` | 8-bit | −51.2 → -51.1 | 49.1 (10th pct) → 48.0 | -0.034 dB / -0.32° | 270 /s, −15…+6 dBFS | 0.8% / 5.1% |
| `iq_worst_observed` | 16-bit | none → -83.0 | 26.7 (worst real file) → 26.1 | -0.810 dB / -1.90° | none | 0.0% / 0.0% |

The 16-bit `iq_worst_observed` preset's 26.1 dB comes from the standard mixer model for 0.81 dB / 1.89°. The analysis estimator gave 26.7 dB for the real file. **Caveats:** rtl_433 saves a file *because* a burst was present, so burst rate and clipping are biased high. A shielded, conducted Caelarch bench should be far cleaner, so treat the interference settings as a stress model until bench measurements exist. Front-end compression is set 6 dB above ADC full scale. An earlier draft set it below full scale, which hid all rail clipping; the real data showed rails clip first.

## Same blind study through each receiver (102 captures, same seed, C6)

Same disturbances, operating schedule and noise. Only the receiver differs.

| Receiver | Sampled Pd all [CI] | Sampled FA (exposure min) | Sampled usable exposure | Bracket Pd all [CI] | Bracket usable exposure | Analog Pd all | PILOT guard P(COND \| abrupt ≥0.4) | P(COND \| gradual) | P(COND \| quiet) | Unique attributions / VALID_BOUNDED |
|---|---|---|---|---|---|---|---|---|---|---|
| `ideal` | 0.434 [0.34, 0.54] | 0 (0.0271) | 0.90 | 0.111 [0.05, 0.18] | 0.76 | 0.323 | 0/16 | 0.982 | 0.827 | 0 / 0 |
| `rtl_sdr_typical` | 0.364 [0.27, 0.46] | 4 (0.0268) | 0.89 | 0.111 [0.05, 0.18] | 0.75 | 0.323 | 0/16 | 0.982 | 0.813 | 0 / 0 |
| `rtl_sdr_adverse` | 0.182 [0.11, 0.26] | 95 (0.0172) | 0.57 | 0.040 [0.01, 0.08] | 0.38 | 0.323 | 0/16 | 0.491 | 0.465 | 0 / 0 |
| `iq_worst_observed` | 0.444 [0.35, 0.55] | 0 (0.0271) | 0.90 | 0.111 [0.05, 0.18] | 0.76 | 0.323 | 0/16 | 0.982 | 0.827 | 0 / 0 |

Full reports: `study/hardware_<preset>_report.json` (ideal: `study/sprint1e_report.json`). Reproduce with `python3 -m caelarch_rf.sim.study --out OUT --n 102 --hardware <preset> --seed-hex c0ffee0ddba11c0ffee0ddba11c0ffee0ddba11c0ffee0ddba11c0ffee0ddba1`.

## Findings

1. **The sampled extractor has no interference rejection.** Under `rtl_sdr_adverse` it raised 95 false alarms, against 0 on the ideal receiver. **All 95** overlap an interference burst, and none occur outside one. The bursts were mostly *not* on the pilot frequency (median 73 kHz away; 10th–90th percentile 20–276 kHz), but strong: the strongest overlapping burst had a median level of −8 dBFS, about 24 dB above the pilot. On/off keying spreads that energy through the Hann window's sidelobes. The guard stayed safe: alarms are UNRESOLVED, abrupt ≥ 0.4 rad changes were never CONDITIONAL, and there were no unique attributions and no VALID_BOUNDED. The cost is availability: P(CONDITIONAL | quiet) fell from 0.83 to 0.47. **Proposed:** block-level interference detection (out-of-pilot energy or residual tests, marking blocks unusable), better pilot isolation (window choice, pilot level, frequency planning), and scoring interference-induced alarms in their own category.
2. **8-bit quantisation costs sensitivity.** Under `rtl_sdr_typical`, sampled detections fell from 43 to 36, with below-floor misses up from 13 to 18. The pilot at −32 dBFS spans only a few 8-bit LSBs, so quantisation noise (step/√12 ≈ 74 per rail) dominates thermal noise (56). All four false alarms overlap an interference burst (two captures, two alarms each).
3. **Clipping hits the bracket hardest.** Under the adverse preset the bracket's usable exposure fell from 0.76 to 0.38, because clipped 4096-sample blocks take out entire reference dwells. Its Pd fell to 0.04.
4. **Realistic IQ imbalance and DC are harmless here.** With 26 dB image rejection the results were unchanged within noise. The planned frequencies (pilot +100 kHz, scene +25 kHz) avoid DC and each other's images. That is a frequency-planning constraint to keep on the bench.
5. **Limitation: the analog monitor is unaffected by construction.** Interference and front-end artefacts are applied to the SDR stream only (identical analog results across presets). A real P4 detector would see interference too.

## Correction to the Sprint 1e report

While running these comparisons I found that the matched-pair control `{A,FA,X}` (a leakage-broken circuit) was **not** identical in the committed Sprint 1e study either. The earlier summary and `SPRINT1E_STUDY.md` stated that all matched pairs gave identical online outputs. That came from the 12-capture preview and was wrong for the 102-capture run. The detector excursions are identical. The difference is one guard window's *cause narrowing*: the still analog monitor excluded G, J, S and FG in one pair member and not the other, because scene leakage (which carries X) nudged the analog's causal noise floor across the exclusion threshold. Both sets contain the true cause and neither is an attribution. Still, leakage information reached a guard decision through a second-order statistic, which is relevant to review finding P2. It is logged as risk R20. All non-leakage pairs were identical, under all four receivers.
