# Sprint 1e blind evaluation: results (synthetic)

Reproduce (deterministic, about 40 s):

```bash
python3 -m caelarch_rf.sim.study --out OUT --n 102 \
  --seed-hex c0ffee0ddba11c0ffee0ddba11c0ffee0ddba11c0ffee0ddba11c0ffee0ddba1
```

The seed is disclosed **after** scoring, for reproducibility only. A blind
batch must use a fresh CSPRNG seed (the default when `--seed-hex` is
omitted). Full machine-readable report: [`study/sprint1e_report.json`](study/sprint1e_report.json).
Method: [`GUARD_AND_SCORER.md`](GUARD_AND_SCORER.md).

## Interpretation

Synthetic, illustrative envelope; **not a hardware, performance or novelty
claim.**

1. **Continuous monitoring detects more of the observable disturbances than
   the switched-reference baseline under matched captures.** The sampled
   pilot minus the bracket is +0.32 in Pd over all events [0.21, 0.44]. The
   analog monitor minus the bracket is +0.21 [0.10, 0.32]. Latency is about
   0.15 ms against about 3.3 ms. This is a positive result *for detection
   only*, within this envelope.
2. **The bracket's cost and blind spots are structural.** It consumes about 19%
   of scene time as reference dwell. It misses pulses between dwells
   (model-unobservable) and can never certify an interior, so the REF guard
   is never CONDITIONAL.
3. **The continuous monitors have their own cost and blind spots:**
   * a pilot source and coupler; the analog monitor also needs a detector,
     reference and logger;
   * a not-ready fraction after each requalification (about 8%) and logger
     dead time (about 4%);
   * **gradual drift.** Frequency tracking absorbs slow receiver ramps by
     construction: P(CONDITIONAL | gradual U change) ≈ 0.98 for PILOT. This
     is the most important negative finding. A monitor that detects steps
     does not bound drift;
   * intersample glitches (in this ideal-sampling model).
4. **Abrupt receiver changes of 0.4 rad or more were never passed as
   CONDITIONAL by the pilot guard** (0/16). Between 0.15 and 0.4 rad, about a
   third were, consistent with floors of about 0.2–0.27 rad.
5. **Attribution integrity held.** No unique attribution and no
   VALID_BOUNDED were issued. The consistent set held a true cause in 100% of
   detected windows, after the non-detection fix. Matched pairs, including
   leakage-broken ones, gave identical online outputs.
6. **False-alarm evidence is weak.** Zero false alarms in about 0.03 monitored
   minutes bounds the rate only at about 110/min. This needs far longer
   exposure.

## Generated report

Captures: 102 (C6_pilot_analog, 20 ms each). Frozen online outputs sha256 `bc23e0afc2c9b089…`; match policy `match-policy/0.1` sha256 `a220948351e88869…`. All detectors scored on the same captures (paired, identical acquisition budget). **Synthetic, illustrative envelope; not a hardware or novelty claim.**

## Detection (one credit per event; nothing removed from denominators)

| Detector | Events | Detected | Missed | Below floor | Unmonitored | Unobs. (structural) | Unobs. (model) | Pd all [95% CI, clustered] | Pd observable | Pd observable+monitored |
|---|---|---|---|---|---|---|---|---|---|---|
| `analog_quadrature` | 99 | 32 | 15 | 22 | 6 | 24 | 0 | 0.323 [0.235, 0.414] | 0.427 [0.316, 0.539] | 0.464 [0.349, 0.583] |
| `reference_bracket` | 99 | 11 | 4 | 35 | 15 | 30 | 4 | 0.111 [0.051, 0.176] | 0.169 [0.083, 0.266] | 0.220 [0.109, 0.341] |
| `sampled_digital` | 99 | 43 | 15 | 13 | 4 | 21 | 3 | 0.434 [0.337, 0.536] | 0.573 [0.459, 0.680] | 0.606 [0.492, 0.721] |

## Latency and false alarms

| Detector | Latency mean (us) | p95 (us) | jitter (us) | Duplicates | False alarms | Monitored exposure (min) | FA/min | FA/min 95% upper bound |
|---|---|---|---|---|---|---|---|---|
| `analog_quadrature` | 163 | 217 | 50 | 7 | 0 | 0.026 | 0.000 | 115.905 |
| `reference_bracket` | 3276 | 6240 | 1410 | 0 | 0 | 0.012 | 0.000 | 242.798 |
| `sampled_digital` | 144 | 185 | 30 | 10 | 0 | 0.027 | 0.000 | 110.389 |

Zero observed false alarms is not a zero rate: the last column is the exact one-sided Poisson upper bound on the monitored exposure outside event windows.

## Paired detector comparison (same events)

| Pair | Pd difference | 95% CI (clustered) | Events |
|---|---|---|---|
| analog_quadrature - reference_bracket | 0.212 | [0.102, 0.317] | 99 |
| analog_quadrature - sampled_digital | -0.111 | [-0.196, -0.030] | 99 |
| reference_bracket - sampled_digital | -0.323 | [-0.436, -0.212] | 99 |

## Online guard (1 ms windows, decided 300 us after window end)

| Active | Windows | CONDITIONAL / UNRESOLVED / INVALID | P(CONDITIONAL \| U changed) [CI] | P(CONDITIONAL \| quiet) [CI] | VALID_BOUNDED | Unique attributions | Consistent set holds a true cause | Mean set size |
|---|---|---|---|---|---|---|---|---|
| `ANALOG` | 1915 | 1168 / 719 / 28 | 0.522 [0.373, 0.618] | 0.613 [0.587, 0.636] | 0 | 0 | 1.000 | 5.000 |
| `PILOT` | 1915 | 1544 / 329 / 42 | 0.670 [0.434, 0.805] | 0.827 [0.801, 0.851] | 0 | 0 | 1.000 | 6.000 |
| `PILOT_ANALOG` | 1915 | 1113 / 740 / 62 | 0.478 [0.310, 0.586] | 0.597 [0.569, 0.620] | 0 | 0 | 1.000 | 4.660 |
| `REF` | 1915 | 0 / 1915 / 0 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0 | 0 | n/a | n/a |

P(CONDITIONAL | U changed), stratified by the size and kind of the change inside the window:

| Active | abrupt >= 0.4 rad | abrupt 0.15-0.4 rad | abrupt < 0.15 rad | gradual (any size) | intersample (model-unobservable) |
|---|---|---|---|---|---|
| `ANALOG` | 0.062 (1/16) [0.000, 0.200] | 0.345 (10/29) [0.185, 0.524] | 0.500 (5/10) [0.167, 0.833] | 0.719 (41/57) [0.618, 0.800] | 1.000 (3/3) [1.000, 1.000] |
| `PILOT` | 0.000 (0/16) [0.000, 0.000] | 0.345 (10/29) [0.174, 0.524] | 0.800 (8/10) [0.500, 1.000] | 0.982 (56/57) [0.903, 1.000] | 1.000 (3/3) [1.000, 1.000] |
| `PILOT_ANALOG` | 0.000 (0/16) [0.000, 0.000] | 0.207 (6/29) [0.067, 0.368] | 0.500 (5/10) [0.167, 0.833] | 0.719 (41/57) [0.618, 0.800] | 1.000 (3/3) [1.000, 1.000] |
| `REF` | 0.000 (0/16) [0.000, 0.000] | 0.000 (0/29) [0.000, 0.000] | 0.000 (0/10) [0.000, 0.000] | 0.000 (0/57) [0.000, 0.000] | 0.000 (0/3) [0.000, 0.000] |

## Matched-pair controls

| Circuit | Leakage-broken | Online outputs identical | Detected |
|---|---|---|---|
| `{J,S}` | False | True | True |
| `{FG,J}` | False | True | True |
| `{FG,S}` | False | True | True |
| `{A,FA,X}` | True | False | True |
| `{FA,J,M}` | True | True | True |
| `{FA,M,S}` | True | True | True |

## Resource accounting

| Detector | Usable exposure | Not ready | Logger dead time | Extra hardware |
|---|---|---|---|---|
| `reference_bracket` | 0.764 | 0.000 | 0.000 | RF selector + reference path (already required); independent R witness |
| `sampled_digital` | 0.903 | 0.079 | 0.000 | pilot source; injection coupler at P3; pilot spectrum within SDR band |
| `analog_quadrature` | 0.871 | 0.085 | 0.039 | pilot source; injection coupler; P4 tap + quadrature detector; detector reference (oscillator or pilot-source tap); logging ADC + clock alignment |

Reference dwell consumes 18.7% of scene time. Pilot at -20 dB re scene, main-lobe occupancy ~20 kHz. Leakage RMS: pilot->scene 8.36e-05 rad, scene->pilot 7.11e-03 rad, scene->analog 3.84e-03 rad.

## Per-case detection (DETECTED / events)

| Case | `analog_quadrature` | `reference_bracket` | `sampled_digital` |
|---|---|---|---|
| A_delay_selective | 0/3 | 0/3 | 0/3 |
| A_step | 0/3 | 0/3 | 2/3 |
| FA_step | 0/3 | 0/3 | 3/3 |
| FG_step | 2/3 | 0/3 | 2/3 |
| G_delay_selective | 2/3 | 2/3 | 2/3 |
| G_glitch_inband | 3/3 | 0/3 | 3/3 |
| G_glitch_intersample | 0/3 | 0/3 | 0/3 |
| G_plus_J_cancel | 0/3 | 0/3 | 0/3 |
| G_ramp | 0/3 | 1/3 | 0/3 |
| G_sine | 0/3 | 0/3 | 0/3 |
| G_step | 3/3 | 0/3 | 3/3 |
| H_plus_G | 0/3 | 1/3 | 2/3 |
| H_plus_J | 2/3 | 0/3 | 3/3 |
| H_step | 0/3 | 0/3 | 0/3 |
| J_step | 3/3 | 0/3 | 3/3 |
| K_step | 0/3 | 0/3 | 0/3 |
| M_random_walk | 0/3 | 0/3 | 0/3 |
| M_step | 1/3 | 0/3 | 0/3 |
| R_step | 0/3 | 1/3 | 0/3 |
| S_random_walk | 0/3 | 0/3 | 1/3 |
| S_step | 2/3 | 0/3 | 1/3 |
| X_step | 0/3 | 2/3 | 0/3 |
| fault_gain_change | 1/3 | 2/3 | 2/3 |
| fault_overflow_known | 2/3 | 0/3 | 3/3 |
| fault_overflow_unknown | 1/3 | 0/3 | 1/3 |
| fault_overload | 0/3 | 0/3 | 0/3 |
| fault_pilot_off | 1/3 | 1/3 | 1/3 |
| fault_ref_unlock | 3/3 | 0/3 | 2/3 |
| fault_retune | 0/3 | 0/3 | 2/3 |
| fault_retune_unlocked | 0/3 | 0/3 | 1/3 |
| fault_short_guard | 2/3 | 1/3 | 2/3 |
| fault_time_discontinuity | 1/3 | 0/3 | 1/3 |
| fault_witness_invalid | 3/3 | 0/3 | 3/3 |
