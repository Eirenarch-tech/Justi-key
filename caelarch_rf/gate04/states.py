"""Four public runtime states and conservative adapters (P0.8).

``VALID_BOUNDED`` is defined but **unreachable** in this package: promoting a
window to it requires an authenticated physical qualification record and a
verifier for that record, neither of which exists yet. Every adapter here
fails closed: an unknown upstream status maps to INVALID, and no historical
``UNRESOLVED_*`` or ``*_BOUNDED_MODEL_ONLY`` status maps to VALID_BOUNDED.
"""
from __future__ import annotations

from enum import Enum


class PublicState(str, Enum):
    VALID_BOUNDED = "VALID_BOUNDED"
    CONDITIONAL = "CONDITIONAL"
    UNRESOLVED = "UNRESOLVED"
    INVALID = "INVALID"


S = PublicState

# CAE-RF-004.3 (Gate 0.3) ``assess_online_window`` statuses, from
# source/CAE_RF_004_Gate0_v03/src/online_interval_gate.py (handoff sha256
# 7b819111b179b7152f39a00e3b584aea2bec40cb9c41208908b4fae7725ecbc7).
GATE0_V03 = {
    "UNRESOLVED_NO_INDEPENDENT_WITNESS": (S.UNRESOLVED, "no independent R witness"),
    # A retune makes earlier qualification stale: the brief classes that INVALID.
    "UNRESOLVED_RETUNE_REQUALIFICATION": (S.INVALID, "stale qualification after retune"),
    "UNRESOLVED_TIMEBASE": (S.INVALID, "timebase fault observed"),
    # Endpoint disagreement is a detection, not a localised fault.
    "UNRESOLVED_ENDPOINT_BRACKET": (S.UNRESOLVED, "endpoint bracket invalid; cause not attributable"),
    "UNRESOLVED_NO_QUALIFIED_AG_ENVELOPE": (S.UNRESOLVED, "no qualified AG envelope"),
    "UNRESOLVED_SPAN_EXCEEDS_ENVELOPE": (S.INVALID, "operation beyond qualified span"),
    "UNRESOLVED_INVALID_OPERATING_MODE": (S.INVALID, "invalid operating mode"),
    "UNRESOLVED_LIVE_MONITOR_MISSING": (S.INVALID, "required live monitor absent"),
    "UNRESOLVED_LIVE_MONITOR_NOT_QUALIFIED": (S.UNRESOLVED, "live monitor present but not qualified"),
    "UNRESOLVED_LIVE_AG_MONITOR_ALARM": (S.UNRESOLVED, "monitor excursion detected; not attributed"),
    "SIMULATED_MONITORED_CONDITIONAL": (S.CONDITIONAL, "simulation-basis envelope with monitor"),
    "MONITORED_CONDITIONAL": (S.CONDITIONAL, "monitor clean; no authenticated record verifier"),
    "SIMULATED_BOUNDED_MODEL_ONLY": (S.CONDITIONAL, "simulation-basis envelope, model only"),
    "BOUNDED_MODEL_ONLY": (S.CONDITIONAL, "bounded by model assumption only"),
}

# CAE-RF-004.4 starter ``assess_window`` already emits public names; it never
# emits VALID_BOUNDED and neither may the adapter.
SCAFFOLD_0044 = {"CONDITIONAL": S.CONDITIONAL, "UNRESOLVED": S.UNRESOLVED, "INVALID": S.INVALID}


def from_gate0_v03(status: str):
    """Map a Gate 0.3 status to (PublicState, reason). Unknown -> INVALID."""
    if status in GATE0_V03:
        return GATE0_V03[status]
    return S.INVALID, f"unrecognised upstream status {status!r}; failing closed"


def from_scaffold_0044(result: dict):
    status = result.get("status") if isinstance(result, dict) else None
    if status in SCAFFOLD_0044:
        return SCAFFOLD_0044[status], result.get("reason", "")
    return S.INVALID, f"unrecognised scaffold status {status!r}; failing closed"


def combine(*states: PublicState) -> PublicState:
    """Most conservative of several per-evidence states (INVALID dominates)."""
    if not states:
        return S.UNRESOLVED
    rank = [S.INVALID, S.UNRESOLVED, S.CONDITIONAL, S.VALID_BOUNDED]
    return min(states, key=rank.index)


class QualificationVerifierMissing(RuntimeError):
    pass


def promote_to_valid_bounded(state: PublicState, qualification_ref):
    """The only route to VALID_BOUNDED. Deliberately unimplemented.

    A future implementation must authenticate ``qualification_ref`` (signed
    physical qualification record, see manifest ``qualification_ref``) against
    an independent registry and check that the window operated inside every
    qualified bound. Until then this raises rather than returning a state.
    """
    raise QualificationVerifierMissing(
        "No authenticated physical qualification verifier exists (Gate 0A-0D NOT PASSED); "
        "VALID_BOUNDED cannot be issued.")
