# Public raw-I/Q recordings through the Gate 0.4 loader

**What this is:** 36 real over-the-air recordings from the public [`merbanan/rtl_433_tests`](https://github.com/merbanan/rtl_433_tests) repository (commit `0914403a28db`). They were converted by `adapters/external_raw.py`, validated, and loaded with the same fail-closed loader used for synthetic captures. **What it is not:** Caelarch hardware, or a capture with a pilot, reference path, witness or known disturbance truth. Nothing here measures monitor performance or supports any Gate 0 claim. The repository has no license file, so **no recording is committed**: only the file list ([`study/public_iq_files.txt`](study/public_iq_files.txt)), the SHA-256 of each source, and the results ([`study/public_iq_report.json`](study/public_iq_report.json)).

Reproduce:

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/merbanan/rtl_433_tests.git
cd rtl_433_tests && sed 's#^#/#' ../public_iq_files.txt | git sparse-checkout set --no-cone --stdin && cd ..
python3 -m caelarch_rf.tools.public_iq_check --src rtl_433_tests --list public_iq_files.txt --out OUT
```

## Loader result

* **35/36 loaded**: 7,132,840 samples in 1.5 s total. Formats: RTL-SDR `cu8`, HackRF-style `cs8`, 16-bit `cs16`. Rates: 250 kS/s to 2.56 MS/s, including non-integer rates (2359.3 kS/s).
* **Refused (correctly):** `tests/m-bus/02/g001_0M_1600k.cu8`: manifest.streams[0].center_freq_hz: number out of range. The file name records the centre frequency as `0M` (unknown), and the contract will not accept an invented one.
* The only rejection reason seen was `CLIPPED_OR_NONFINITE`. 22/35 recordings had clipped blocks, and accepted fractions ran from 8% to 100%.

## Real-hardware artefacts the synthetic generator does not model

| Quantity | Min | Median | Max | Synthetic generator |
|---|---|---|---|---|
| Samples at ADC rails (clipping) | 0.0% | 5.0% | 68.7% | 0 (steady tones, 16-bit) |
| DC offset relative to RMS (dB) | -60.4 | -48.0 | -17.7 | none |
| DC spike over median spectral bin (dB) | -2.0 | 10.1 | 34.1 | none |
| IQ gain imbalance (dB) | -0.81 | -0.00 | 0.08 | 0 |
| IQ phase imbalance (deg) | -1.89 | 0.00 | 0.30 | 0 |
| Image rejection (dB) | 26.7 | 62.1 | 80.8 | infinite |
| Noise floor, 10th-percentile block (dBFS) | -56.7 | -29.4 | -7.7 | steady: scene tone -12 dBFS, noise -52 dBFS |
| Burst dynamic range p95/p10 (dB) | 2.0 | 27.6 | 44.3 | ~0 (continuous) |

The 8-bit recordings use at most 256 levels per rail. The generator renders 16-bit values with Gaussian noise and no DC, imbalance or saturation.

## Findings and what changed

1. **Monitor fix: no excursion without an established track.** The sampled extractor was run on each recording's DC component as a stand-in pilot (robustness only; these recordings have no pilot). Monitors that had not yet reached TRACKING raised 11,837 'ambiguous 2π' excursions on noise across the set. A monitor without a track has no basis to claim a change. An ambiguous jump before readiness now silently restarts acquisition. After the fix: **17 excursions in total**, all from TRACKING monitors. This is covered by `test_no_excursion_without_an_established_track`. The committed Sprint 1e study is byte-identical after the fix.
2. **The monitor did not manufacture confidence on real noise.** Of 9,713 one-millisecond windows, 27 (0.3%) were CONDITIONAL, 2,206 UNRESOLVED and 7,480 INVALID (mostly clipped blocks or lost 'pilot'). The CONDITIONAL windows occur only in recordings with a strong, stable DC component, which really is a steady signal.
3. **Clipping is common and the 4096-sample clip granularity is coarse.** Median 5.0% of samples sit on the rails (max 69%). Because one clipped sample rejects a whole 4096-sample block, accepted fractions fell as low as 8%. That is safe but wasteful for bursty signals. Proposed: finer clip blocks and bench-measured ADC rails.
4. **DC offset/LO leakage and IQ imbalance are real and should enter the generator.** DC is 18–60 dB below RMS, with a DC spike up to 34 dB above the median bin. Image rejection is 27–81 dB. A pilot or reference tone placed near DC, or near its image, would be biased. Proposed for the generator: DC offset, IQ gain/phase imbalance, 8-bit quantisation, front-end saturation, and bursty interference.
5. **Contract changes forced by real metadata gaps:** a new provenance kind `external_recording`; `rx_gain_db` may be `null` (unknown, never invented); an `internal` reference must be declared locked (there is no external lock to lose). Files without frequency/rate metadata are refused, not guessed.

## Per-recording results

| Recording | Fmt | kS/s | ms | Accepted | Clipped | DC dB | IRR dB | NF dBFS | DR dB | Excursions | 1 ms windows C/U/I |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `124-spider/01_FR_1_433.92M_250k.cu8` | cu8 | 250 | 524 | 84% | 5.8% | -46.4 | 68.4 | -26.4 | 25.7 | 0 | 0/306/218 |
| `6sc2/01/g001_315.1M_250k.cu8` | cu8 | 250 | 1311 | 74% | 9.3% | -49.4 | 54.1 | -11.4 | 10.2 | 0 | 0/705/605 |
| `BurnhardBBQ/g001_433.92M_250k.cu8` | cu8 | 250 | 1311 | 51% | 19.7% | -51.6 | 41.2 | -43.1 | 42.5 | 3 | 0/467/843 |
| `Byron-BY101/01/g001_433.92M_250k.cu8` | cu8 | 250 | 1835 | 8% | 31.5% | -57.4 | 73.9 | -7.7 | 6.6 | 0 | 0/109/1726 |
| `DeltaDore-X3D/g004_868.95M_1000k.cu8` | cu8 | 1000 | 983 | 70% | 18.1% | -55.1 | 67.0 | -27.8 | 29.3 | 0 | 0/205/778 |
| `EcoWitt-WN20/g025_915M_1000k.cu8` | cu8 | 1000 | 197 | 100% | 0.0% | -42.1 | 70.6 | -27.9 | 20.3 | 0 | 0/58/138 |
| `Elsner-Solexa/01/g001_868.25M_1000k.cu8` | cu8 | 1000 | 197 | 100% | 0.0% | -43.7 | 49.4 | -33.4 | 27.4 | 0 | 0/76/120 |
| `Neptune-R900/01/g018_912.6M_1000k.cu8` | cu8 | 1000 | 66 | 81% | 0.9% | -43.7 | 48.1 | -26.9 | 24.5 | 0 | 0/24/41 |
| `ESIC-EMT7110_power_meter/g003_868.28M_1024k.cu8` | cu8 | 1024 | 128 | 72% | 21.6% | -52.0 | 60.9 | -31.6 | 33.0 | 0 | 0/30/97 |
| `Neptune-R900/01/g001_912M_1024k.cu8` | cu8 | 1024 | 64 | 81% | 6.1% | -46.7 | 80.8 | -28.6 | 27.6 | 0 | 0/9/54 |
| `SQMLtdFan/01/g018_303.8M_1024k.cu8` | cu8 | 1024 | 26 | 100% | 0.0% | -32.6 | 35.3 | -40.5 | 29.7 | 3 | 9/15/2 |
| `silver_spring_mesh/g007_916.45M_1600k.cu8` | cu8 | 1600 | 41 | 81% | 7.0% | -48.5 | 80.7 | -31.3 | 32.1 | 0 | 0/5/35 |
| `bresser_garden/01/base_ack_0x8a_433.6M_1200k.cu8` | cu8 | 1200 | 164 | 58% | 38.3% | -58.1 | 73.2 | -15.5 | 17.0 | 0 | 0/29/134 |
| `m-bus/01/g002_868.95M_1200k.cu8` | cu8 | 1200 | 55 | 81% | 10.9% | -40.5 | 53.6 | -27.8 | 29.3 | 0 | 0/9/45 |
| `olympia/9571/keyfob_arm_home_only_19e755_868.5M_1200k.cu8` | cu8 | 1200 | 109 | 53% | 43.2% | -60.4 | 74.1 | -29.7 | 31.1 | 0 | 0/6/103 |
| `TFA-KlimaLogg-30.3180.IT/01/g001_868.25M_1536k.cu8` | cu8 | 1536 | 128 | 92% | 3.6% | -44.8 | 53.2 | -32.4 | 30.7 | 0 | 0/15/112 |
| `Neptune-R900/01/g001_912M_2048k.cu8` | cu8 | 2048 | 64 | 88% | 5.4% | -46.5 | 71.3 | -25.5 | 24.1 | 0 | 0/3/60 |
| `elero/01/g003_869.4M_2048k.cu8` | cu8 | 2048 | 32 | 100% | 0.0% | -41.9 | 68.8 | -45.1 | 39.7 | 0 | 14/3/14 |
| `elster/01/elster_t2_cleartext_nbrtable_len189_03_910M_2048k.cu8` | cu8 | 2048 | 64 | 44% | 54.1% | -52.8 | 76.2 | -29.2 | 30.8 | 0 | 0/3/60 |
| `olympia/9571/door_open_4ddf3b_868.4M_2048k.cu8` | cu8 | 2048 | 128 | 38% | 59.7% | -59.7 | 59.2 | -29.4 | 30.9 | 0 | 0/5/122 |
| `ecowitt/WH41/g001_915M_2400k.cu8` | cu8 | 2400 | 55 | 31% | 68.7% | -59.7 | 60.1 | -30.6 | 32.2 | 0 | 0/1/53 |
| `ert/scm/01/g001_912.6M_2400k.cu8` | cu8 | 2400 | 9 | 100% | 0.0% | -39.5 | 50.5 | -16.7 | 5.2 | 0 | 0/2/6 |
| `lacrosse_ltv/BreezePro_LTV-WSDTH01/g001_914.938M_2400k.cu8` | cu8 | 2400 | 55 | 47% | 52.1% | -56.7 | 64.8 | -42.9 | 44.3 | 4 | 0/1/53 |
| `chrysler_car_remote/01/lock_315.1M_920k.cu8` | cu8 | 920 | 1496 | 8% | 4.1% | -54.5 | 64.8 | -8.4 | 5.6 | 0 | 0/31/1464 |
| `idm/IDM/g002_912.6M_2359.3k.cu8` | cu8 | 2359.3 | 63 | 94% | 0.0% | -17.7 | 62.3 | -15.1 | 7.0 | 4 | 4/4/55 |
| `scmplus/01/g002_912.6M_2359.3k.cu8` | cu8 | 2359.3 | 20 | 100% | 0.0% | -47.8 | 73.3 | -34.7 | 30.2 | 0 | 0/4/16 |
| `BMW_G4_TPMS/g001_433.92M_2500k.cs16` | cs16 | 2500 | 13 | 100% | 0.0% | -51.4 | 51.6 | -56.6 | 42.9 | 1 | 0/1/12 |
| `BMW_G5_TPMS/2/g080_433.92M_2500k.cs16` | cs16 | 2500 | 13 | 100% | 0.0% | -48.0 | 64.4 | -50.0 | 31.5 | 0 | 0/7/6 |
| `funkbus/remote/01/g001_433.92M_2000k.cu8` | cu8 | 2000 | 131 | 100% | 0.0% | -46.8 | 72.9 | -30.9 | 27.4 | 0 | 0/8/123 |
| `typhur_sync_gold/01/g002_915M_1000k.cs16` | cs16 | 1000 | 33 | 100% | 0.0% | -38.8 | 26.7 | -37.1 | 20.8 | 0 | 0/9/23 |
| `tyreguard_400/01/g001_433.92M_1000k.cs16` | cs16 | 1000 | 66 | 100% | 0.0% | -56.2 | 37.6 | -56.7 | 26.2 | 2 | 0/20/45 |
| `Schrader_MRXBC5A4_TPMS/01/g001_433.92M_2048k.cs8` | cs8 | 2048 | 19 | 100% | 0.0% | -49.6 | 54.0 | -16.7 | 2.9 | 0 | 0/1/17 |
| `IBIS_beacon/g001_433.92M_2560k.cu8` | cu8 | 2560 | 192 | 13% | 5.0% | -36.0 | 55.1 | -13.9 | 12.4 | 0 | 0/1/190 |
| `Schrader_MRXBC5A4_TPMS/01/g004_433.92M_2048k.cs16` | cs16 | 2048 | 18 | 100% | 0.0% | -29.4 | 57.0 | -23.9 | 2.0 | 0 | 0/6/11 |
| `ESIC-EMT7110_power_meter/g007_868.28M_1024k.cu8` | cu8 | 1024 | 128 | 72% | 21.5% | -50.4 | 62.1 | -32.0 | 33.5 | 0 | 0/28/99 |

## Limits

* Third-party hardware, settings unknown (gain, AGC, antennas). Device classes are inferred from the file format only. No ground truth, so nothing can be scored.
* The `cs16` byte order is assumed little-endian. The plausible noise floors and image rejection are consistent with that, but it is not verified.
* The DC component is not a designed pilot. The extractor run tests robustness to real noise, quantisation and clipping, not pilot-tracking performance.
* Other public datasets (IQEngine, SigID Wiki, Zenodo) are blocked by this environment's network policy and were not used.
