# Provenance

## Input package

* `Eirenarch_Caelarch_RF_Coding_Team_Handoff_2026-11-07.zip`
  sha256 `2c2bd854fa77c9731045de8cbb804b852e7fffb81ea4feaf77a1792a3b2dfc9c`
* All 148 entries of its `SHA256SUMS_HANDOFF.txt` verified on 2026-11-07.
* `scripts/run_all_tests.sh` reproduced: **96/96 tests pass** (Python 3.13,
  numpy 2.5.3). That is software logic only, not hardware evidence.

## Referenced (not copied) source files

| File in handoff | sha256 | Used for |
|---|---|---|
| `source/CAE_RF_004_Gate0_v03/src/online_interval_gate.py` | `7b819111b179b7152f39a00e3b584aea2bec40cb9c41208908b4fae7725ecbc7` | Status names mapped in `gate04/states.py` |
| `next_0044/pilot_observability.py` | `87270e9004a25031c5c5b87a40020d8cb2dfdbd814c4d68ecc2f8a730589efa0` | Scaffold status names; `MonitorQualification` fields informed `records.py` |

The historical releases (CAE-RF-002 to 004.4 starter) are **not** imported
here. Backlog P0.1 asks for a dedicated repository with tags and archive
hashes, and that repository should be the place for them. This directory holds
only new Gate 0.4 code.
