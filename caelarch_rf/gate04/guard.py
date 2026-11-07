"""Sprint 1d online guard: causal, timebase-aware window assessment.

Combines, for a decision window [t_start, t_end] on a decision timebase
(normally ``sdr0``) decided at ``t_decision``:

* every ACTIVE monitor's causal ``window_state`` (sampled pilot, analog
  detector, reference bracket), each evaluated on its own timebase after
  mapping the window through a valid alignment epoch (widened by the
  alignment uncertainty; the decision time is pulled EARLIER by it, so no
  evidence can arrive "from the future" through clock error);
* the independent R witness (fresh, same alignment epoch).

State policy (VALID_BOUNDED is never produced here):
  INVALID     any active evidence reports a fault / absent monitor;
  UNRESOLVED  any evidence missing, stale, unmappable, not ready, bracket-only,
              or an excursion was detected (detection is not attribution);
  CONDITIONAL every active continuous monitor covered the window while
              TRACKING with no excursion, the witness is fresh, and at least
              one continuous monitor is active. CONDITIONAL is conditional on
              the qualification requirements (QR-*) that remain unproven.

Attribution integrity: when monitors move, the guard lists the single causes
CONSISTENT with which active sensors moved and which stayed still (still =
covered while TRACKING with no excursion). Non-detection is not zero change:
a still sensor excludes a cause only if, under that single-cause hypothesis,
the moved sensor's observed change (scaled by the coefficient ratio) would
have cleared the still sensor's causal floor with a sqrt(2) straddle margin. It never claims a unique cause
unless the term is structurally attributable from the active sensors alone
AND an authenticated qualification exists, which in this package it never
does. Candidate sets come from the structural graph only: unintended leakage
channels earn no attribution credit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Optional, Tuple

from .monitors import EXCURSION_PREFIX, MonitorResult, window_state
from .observability import ASSUMPTIONS, ORDER, SENSORS, analyse, circuits, minimal_requirements
from .states import PublicState, combine

CONTINUOUS = ("sampled_digital", "analog_quadrature")


@lru_cache(maxsize=64)
def _structure(sensors: Tuple[str, ...]):
    return circuits(sensors), analyse(sensors)


@lru_cache(maxsize=512)
def _requires(sensors: Tuple[str, ...], term: str):
    return tuple(tuple(sorted(a)) for a in minimal_requirements(sensors, tuple(ASSUMPTIONS), term))

# Named active configurations (which monitor results the guard may use).
ACTIVE = {
    "REF": ("reference_bracket",),
    "PILOT": ("reference_bracket", "sampled_digital"),
    "ANALOG": ("reference_bracket", "analog_quadrature"),
    "PILOT_ANALOG": ("reference_bracket", "sampled_digital", "analog_quadrature"),
}


@dataclass(frozen=True)
class GuardDecision:
    t_start_ns: int
    t_end_ns: int
    t_decision_ns: int
    timebase: str
    active: str
    state: PublicState
    reasons: Tuple[str, ...]
    detected: bool
    moved: Tuple[str, ...]
    still: Tuple[str, ...]
    consistent_single_causes: Tuple[str, ...]
    multi_cause_explanations: bool
    unique_attribution: Optional[str]
    requires_for_attribution: Dict[str, Tuple[Tuple[str, ...], ...]] = field(default_factory=dict)
    evidence: Dict[str, Tuple[str, str]] = field(default_factory=dict)


def _map_window(cap, t0, t1, td, src, dst):
    """Map (window, decision) src -> dst conservatively; None if any point is unmappable."""
    a, b, c = cap.map_time(t0, src, dst), cap.map_time(t1, src, dst), cap.map_time(td, src, dst)
    if a is None or b is None or c is None:
        return None
    u = max(a[1], b[1], c[1])
    return a[0] - u, b[0] + u, c[0] - u


def assess(cap, results: Dict[str, MonitorResult], active: str, t_start_ns: int, t_end_ns: int,
           t_decision_ns: int, timebase: str = "sdr0", witness_max_age_ns: int = 5_000_000) -> GuardDecision:
    kinds = ACTIVE[active]
    states, evidence, reasons = [], {}, []
    moved, still, sensors_used = [], [], []
    moved_mag: Dict[str, float] = {}       # largest |innovation| per moved sensor
    still_floor: Dict[str, float] = {}     # largest causal floor per still sensor
    for kind in kinds:
        res = results.get(kind)
        if res is None:
            states.append(PublicState.UNRESOLVED)
            evidence[kind] = ("UNRESOLVED", "active monitor has no output in this capture")
            continue
        sensors_used.append(res.sensor)
        m = _map_window(cap, t_start_ns, t_end_ns, t_decision_ns, timebase, res.timebase)
        if m is None:
            states.append(PublicState.UNRESOLVED)
            evidence[kind] = ("UNRESOLVED", f"no valid alignment epoch {timebase}->{res.timebase}")
            continue
        st, why = window_state(res, *m)
        if kind == "reference_bracket" and why.startswith("bracket-only"):
            # Supplementary: the bracket's inability to certify the interior is
            # neutral here; certification must come from a continuous monitor
            # (enforced below). Its excursions and faults still count.
            evidence[kind] = ("SUPPLEMENTARY", why)
            continue
        states.append(st)
        evidence[kind] = (st.value, why)
        m0, m1, md = m
        if why.startswith(EXCURSION_PREFIX):
            moved.append(res.sensor)
            moved_mag[res.sensor] = max(abs(e.innovation_rad) for e in res.excursions
                                        if e.available_ns <= md and e.window_start_ns <= m1
                                        and e.window_end_ns >= m0)
        elif st == PublicState.CONDITIONAL and kind in CONTINUOUS:
            floors = [o.detection_floor_rad for o in res.observations
                      if o.detection_floor_rad is not None and o.available_ns <= md
                      and o.window_start_ns <= m1 and o.window_end_ns >= m0]
            if floors:
                still.append(res.sensor)
                still_floor[res.sensor] = max(floors)
    w = cap.latest_witness(t_decision_ns, timebase, witness_max_age_ns)
    if w.reason == "OK":
        evidence["witness"] = ("CONDITIONAL", "fresh witness in the decision's alignment epoch")
        sensors_used.append("witness")
    else:
        states.append(PublicState.UNRESOLVED)
        evidence["witness"] = ("UNRESOLVED", w.reason)
    if not any(k in CONTINUOUS for k in kinds):
        states.append(PublicState.UNRESOLVED)
        reasons.append("no continuous monitor active: brackets cannot certify the interior")
    state = combine(*states)
    reasons += [f"{k}: {why}" for k, (st, why) in evidence.items() if st not in ("CONDITIONAL", "SUPPLEMENTARY")]
    detected = bool(moved)
    consistent: Tuple[str, ...] = ()
    multi = False
    requires = {}
    unique = None
    if detected:
        def excluded_by_still(k):
            for s in still:
                cs = SENSORS[s].c(k)
                if not cs:
                    continue
                # Single-cause prediction of the still sensor's change from the
                # weakest moved observation; exclude only if it would have shown.
                pred = min(abs(cs / SENSORS[m].c(k)) * moved_mag[m] for m in moved)
                if pred >= 2 ** 0.5 * still_floor[s]:
                    return True
            return False
        consistent = tuple(k for k in ORDER
                           if all(SENSORS[s].c(k) for s in moved) and not excluded_by_still(k))
        used = tuple(dict.fromkeys(sensors_used))
        circ, structural = _structure(used)
        multi = any(any(SENSORS[s].c(k) for s in moved for k in c) for c in circ if len(c) > 1)
        for k in consistent:
            requires[k] = _requires(used, k)
        # A unique claim needs: one consistent cause, structurally attributable
        # from the active sensors alone, and an authenticated qualification
        # record. The last never exists in this package, so unique stays None.
        if len(consistent) == 1 and structural[consistent[0]].status == "ATTRIBUTABLE":
            reasons.append(f"{consistent[0]} structurally attributable but no authenticated qualification")
    return GuardDecision(t_start_ns, t_end_ns, t_decision_ns, timebase, active, state, tuple(reasons),
                         detected, tuple(moved), tuple(still), consistent, multi, unique, requires, evidence)
