# Sprints 1d–1e: online guard and blind offline scorer

**Status:** implemented, synthetic only. Code: `gate04/guard.py` and
`gate04/evaluate.py` (online; covered by the isolation test), and
`sim/scorer.py` and `sim/study.py` (offline). Results:
[`SPRINT1E_STUDY.md`](SPRINT1E_STUDY.md).

## Review corrections integrated first

| Finding | Change | Test |
|---|---|---|
| P1: future evidence | `window_state(res, t_start, t_end, t_decision)`: observations and excursions count only if `available_ns ≤ t_decision`. The guard maps the window into each monitor's timebase through a valid alignment epoch, widens it by the alignment uncertainty, and pulls the decision time **earlier** by that uncertainty | `TestCausalDecisions`, `test_decision_before_alarm_availability_does_not_see_it` |
| P1: readiness | The monitor states are `ACQUIRING → WARMING → TRACKING`, plus `REACQUIRING` after a benign gap and `UNMONITORED`. Any gap other than a known-count overflow boundary (retune, unlock, gain change, clipping, clock jump, pilot off/lost, untimed data, or anything unrecognised) forces **full requalification**: frequency, noise and amplitude estimates are discarded. Only TRACKING blocks can support CONDITIONAL | `test_retune_forces_requalification_before_conditional`, `test_benign_known_overflow_costs_one_block_not_a_requalification` |
| P1: causal floor | Each TRACKING record carries the `detection_floor_rad` known at its availability time. `MonitorResult.envelope` is labelled `retrospective` and is never used online | `test_online_floor_is_causal_not_the_retrospective_envelope` |
| P2: leakage | Three levels are kept distinct: structural identifiability, detectability under finite noise and bandwidth, and estimator performance. Leakage earns **no attribution credit**: the guard's candidate sets use the structural graph without leakage channels | Matrix text; `test_leakage_broken_pairs_still_indistinguishable_to_current_monitors` |

## 1d: online guard (`assess`)

Inputs: an `OnlineCapture`, the monitor results, an active configuration
(`REF`, `PILOT`, `ANALOG`, `PILOT_ANALOG`), a window and a decision time on a
decision timebase.

* **INVALID**: any active evidence shows a fault or an absent monitor.
* **UNRESOLVED** in any of these cases:
  * evidence is missing, stale, unmappable or not ready;
  * an excursion was detected;
  * no continuous monitor is active (the reference bracket is supplementary:
    its excursions and faults count, but it can never certify an interior).
* **CONDITIONAL**: every active continuous monitor covered the window while
  TRACKING with nothing above its causal floor, **and** the witness is fresh
  in the decision's alignment epoch. This is conditional on unproven
  requirements (QR-*).
* **VALID_BOUNDED** is never produced.

**Attribution integrity.** The guard narrows the causes but never attributes:

* It lists the single causes consistent with which sensors moved and which
  stayed still.
* *Non-detection is not zero change.* A still sensor excludes a cause only if,
  under that single-cause hypothesis, the moved sensor's observed change
  (scaled by the coefficient ratio) would have cleared the still sensor's
  causal floor with a √2 margin. Without this rule, 11% of detected windows
  in the study excluded the true cause. With it, the true cause is retained
  in 100%.
* `unique_attribution` stays `None` unless a cause is structurally
  attributable from the active sensors alone **and** an authenticated
  qualification exists. That never happens in this package.

## 1e: blind scorer

**Freeze before reveal.**
* `gate04.evaluate.freeze()` writes every online output and returns its
  SHA-256. It has no truth parameter and imports nothing that can open truth.
* `score(frozen, truth_root, expected_sha256)` re-hashes the file and refuses
  any mismatch **before** touching truth. Truth is then opened per capture
  through the salted commitments carried in the frozen file.
* Freezing and scoring are deterministic and reproducible.

**Predeclared matching policy** (`MatchPolicy`, digest recorded in every
report):
* An event is the set of scheduled disturbances sharing an onset. Its match
  window is [onset, end + max latency]. Max latency is 1 ms for the
  continuous monitors and 8 ms for the bracket.
* The earliest unclaimed alarm in the window earns the **one** credit. Other
  alarms in the window are duplicates, counted separately: no credit, and not
  false alarms.
* An alarm outside every event window and outside announced acquisition
  changes is a false alarm.

**Categories (none removed from denominators):**

| Category | Meaning |
|---|---|
| DETECTED | Credited detection |
| MISSED | Observable and monitored, above the causal floor, but no alarm |
| MISSED_BELOW_FLOOR | The projection (wrapped) is below the causal floor at onset |
| UNMONITORED | The onset fell outside TRACKING coverage |
| UNOBSERVABLE_STRUCTURAL | Zero wrapped projection on the detector's sensor (including cancellations) |
| UNOBSERVABLE_MODEL | Intersample pulse for point-sampled detectors, or a pulse entirely between reference dwells for the bracket |

Pd is reported three ways: over all events, over observable events, and over
observable and monitored events.

**Statistics:**
* Capture-clustered percentile bootstrap (seeded) and Wilson intervals.
* Paired detector differences on identical events, with clustered bootstrap.
* False alarms per monitored minute, with an **exact one-sided Poisson upper
  bound**. Zero observed is never reported as zero rate.

**Guard metrics:**
* P(CONDITIONAL | U changed in window), stratified as abrupt ≥ 0.4 rad, abrupt
  0.15–0.4 rad, abrupt < 0.15 rad, gradual, and intersample;
* P(CONDITIONAL | quiet);
* counts of VALID_BOUNDED and of unique attributions;
* the rate at which the consistent set holds a true cause (announced
  acquisition-induced changes count as true causes).

**Matched-pair controls.** Online outputs (excursions, guard states,
candidate sets) must be identical for matched pairs, including
leakage-broken ones.

**Resource accounting:**
* usable exposure, not-ready fraction and logger dead time per detector;
* reference dwell as a fraction of scene time;
* pilot level and occupancy, and leakage RMS;
* extra hardware per detector.

**Matched measurement budget.** All detectors and guard configurations are
scored on the **same** captures, so disturbances, duration, reference
schedule and noise are all identical. The continuous monitors' extra cost is
reported, not hidden.

## Limits of this evaluation

* **Exposure is small.** 102 captures × 20 ms gives about 0.03 monitored
  minutes, so even zero false alarms bounds the rate only at about 110/min.
  A bound of 1/min needs at least 3 clean monitored minutes (about 9,000
  such captures). Longer captures are a Sprint 2 item.
* The noise, disturbance and amplitude envelope is illustrative. Pd depends
  on it directly: library steps are 0.1–0.5 rad, against floors of about
  0.2–0.27 rad.
* The pilot is present in every capture, including when the bracket is
  scored. Its interference with the scene was quantified (RMS about 8×10⁻⁵
  rad) and is negligible here.
* All detectors use k = 6. No threshold was tuned on the evaluation batch.
