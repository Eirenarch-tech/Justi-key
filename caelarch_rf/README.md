# Caelarch RF: Gate 0.4 architecture and measurement interface

**Eirenarch Technologies, CAE-RF-004.4 research. Simulation and software
only. Hardware Gate 0 HOLD.**

> This directory is **unrelated to JustiKey** and shares no code with it. It
> sits here only because no dedicated Caelarch repository was available. Move
> it out with `git subtree split --prefix=caelarch_rf` once one exists.

Gate 0.4 work that doesn't depend on hardware, done while CelonixLab's
technical confirmations are pending. Sprint 0 defines how captures are
recorded and what an online method may see. Sprint 1a adds a blind
disturbance generator that produces adversarial, contract-conformant
captures. Sprints 1b–1c add a causal sampled-pilot extractor and an analog
quadrature-monitor model, which detect but never attribute. No SDR has been
chosen. The attribution guard and blind scorer are Sprints 1d–1e.

| Path | Backlog | What it is |
|---|---|---|
| `docs/GATE0_4_ARCHITECTURE.md` | P0.2 | Planes P0–P4/PS/ADC, structural findings, **risk register**, sprint plan |
| `docs/OBSERVABILITY_MATRIX.md` | P0.2 | Generated sensor × impairment and configuration × impairment tables |
| `docs/MEASUREMENT_CONTRACT.md` | P0.3 | Raw-IQ capture contract: timing, events, witness, sealed truth |
| `gate04/contract.py`, `gate04/ingest.py` | P0.3 | Allowlist validator. Loader that derives accepted samples and fails closed |
| `gate04/sealed_truth.py` | P0.3 | **Offline only.** Salted truth commitment and verification |
| `gate04/records.py` | P0.5/P0.6 interface | `PilotObservation` with a causality (`available_ns`) rule |
| `gate04/states.py` | P0.8 | Four public states, conservative Gate 0.3 adapter. VALID_BOUNDED unreachable |
| `gate04/observability.py` | P0.2 | Exact rank and circuit analysis, minimal qualification-requirement sets (QR-*) |
| `docs/BLIND_GENERATOR.md` | 1a | Generator model, faults, matched pairs, blindness rules, limitations |
| `sim/generator.py`, `sim/disturbances.py` | 1a | **Offline only.** Complex-IQ capture + sealed truth from a scenario and a ≥16-byte CSPRNG seed |
| `sim/matched.py` | 1a | Per-configuration matched-observation pairs and an online-view comparator |
| `sim/batch.py` | 1a | Blind batches over the brief's case library (CLI) |
| `docs/MONITORS.md` | 1b–1c | Extractor pipeline, analog model, acceptance mapping, envelope, leakage, findings |
| `gate04/monitors.py` | 1b–1c | **Online.** Sampled-pilot and analog-monitor tracking, coverage, unattributed excursions, window state |
| `sim/leakage.py` | 1b | Pilot ↔ scene interference by counterfactual renders |

Stdlib only (enforced by a test), so the validator runs on a bench host and
the generator's random streams don't depend on a numpy version.

```bash
python3 -m unittest discover -s caelarch_rf/tests -t .      # 109 tests, ~35 s
python3 -m caelarch_rf.sim.batch --captures OUT/caps --truth OUT/truth --n 40
python3 -m caelarch_rf.gate04.validate <capture_dir>        # contract check
python3 -m caelarch_rf.gate04.observability                 # regenerate matrix doc
```

## What this does not establish

Passing these tests says that synthetic captures are parsed, guarded and
mapped as specified. It is **not** evidence of phase transfer, monitor
coverage, attribution performance, novelty or published-method superiority.
Every hard stop in the handoff (`TEAM_BRIEF.md`, `TEAM_BACKLOG_AND_GATES.md`)
still applies.
