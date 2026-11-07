"""Gate 0.4 observation graph and impairment observability matrix (P0.2).

A *structural*, linearised phase model of the proposed 004.4 bench. Each
impairment is an additive phase term; each sensor observes a signed sum of
the terms on its physical path (a quadrature detector reports signal phase
*minus* its own reference phase). See ../docs/GATE0_4_ARCHITECTURE.md.

For a sensor set we compute, exactly (rational arithmetic):

* detectable   - some sensor's observation moves when the impairment moves;
* attributable - the impairment is estimable (its unit vector lies in the row
                 space of the sensor matrix) from online data alone;
* conditional  - estimable only after adding *qualified bound* rows, which are
                 assumptions backed by a physical qualification requirement
                 (QR-*), never online data;
* confounded   - otherwise; we list its *minimal confounding sets* (circuits).

Limits, stated so the table is not over-read: static phase-only model; no
noise, amplitude, time multiplexing, sampling or bandwidth (those are in the
temporal-coverage table, qualitatively, and in the Sprint 1a waveform
generator); two terms carried by the same sensor set are treated as distinct
parameters even if hardware makes them collinear. Passing this analysis is
necessary, not sufficient, for a physical claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from typing import Dict, FrozenSet, List, Tuple

IMPAIRMENTS: Dict[str, str] = {
    "X": "Known RF source phase at P0 (common to H and R)",
    "H": "Scene channel, P0 -> selector H port",
    "R": "Reference path, P0 (W0) -> P1 (W1) -> selector R port",
    "K": "Witness differential transfer W1/W0 (instrument, cables)",
    "J": "Pilot injection path, PS -> coupler -> P3",
    "S": "Pilot source phase at PS",
    "G": "External receive chain, P3 -> P4 (SDR RF connector)",
    "A": "SDR internal conversion, P4 -> ADC (LO, IF filters, sample clock)",
    "FG": "Pilot-vs-scene frequency mismatch of the G transfer",
    "FA": "Pilot-vs-scene frequency mismatch of the A transfer",
    "M": "Analog detector's own reference and electronics phase (monitor-internal)",
}
ORDER = tuple(IMPAIRMENTS)


@dataclass(frozen=True)
class Sensor:
    name: str
    coef: Tuple[Tuple[str, int], ...]   # signed contribution of each term
    plane: str
    temporal: str  # qualitative coverage note
    hypothetical: bool = False

    @property
    def terms(self) -> FrozenSet[str]:
        return frozenset(k for k, c in self.coef if c)

    def c(self, k) -> int:
        return dict(self.coef).get(k, 0)


def _s(name, coef, plane, temporal, hypothetical=False):
    return Sensor(name, tuple(sorted(coef.items(), key=lambda kv: ORDER.index(kv[0]))),
                  plane, temporal, hypothetical)


SENSORS: Dict[str, Sensor] = {s.name: s for s in [
    _s("scene", {"X": 1, "H": 1, "G": 1, "A": 1}, "ADC (selector on H)",
       "Sampled at fs inside accepted intervals; time-multiplexed with reference"),
    _s("reference", {"X": 1, "R": 1, "G": 1, "A": 1}, "ADC (selector on R)",
       "Sampled at fs only while selector is on R; brackets cannot see interior AG excursions"),
    _s("witness", {"R": 1, "K": 1}, "P0/P1 (external instrument)",
       "Instrument rate; needs a declared timebase alignment to be fresh against SDR time"),
    # The digital extractor's NCO is clocked by the SDR sample clock, which is
    # part of A; it adds no independent reference term.
    _s("pilot_sampled", {"S": 1, "J": 1, "G": 1, "A": 1, "FG": 1, "FA": 1},
       "ADC (digital extraction)",
       "Sampled at fs, continuous across selector states, minus gaps/clipping/settling; "
       "blind between samples and above extraction bandwidth"),
    # Analog quadrature detector at P4 with an independent reference oscillator:
    # output = (S+J+G+FG) - M. Cannot see A (downstream of the tap).
    _s("analog_monitor_P4", {"S": 1, "J": 1, "G": 1, "FG": 1, "M": -1},
       "P4, independent detector reference",
       "Continuous-time but band-limited by detector; cannot see A; adds its own drift M",
       hypothetical=True),
    # Same detector referenced to the pilot source tapped at PS (before J):
    # output = (S+J+G+FG) - (S+M) = J+G+FG-M. S cancels; J and M do not.
    _s("analog_monitor_P4_srcref", {"J": 1, "G": 1, "FG": 1, "M": -1},
       "P4, reference tapped from pilot source at PS",
       "As analog_monitor_P4, but source phase S cancels; reference-path drift enters as M",
       hypothetical=True),
    _s("pilot_path_monitor", {"S": 1, "J": 1}, "PS -> J (independent coupler/detector)",
       "Independent observation of source+injection; does not exist yet",
       hypothetical=True),
]}

# Qualified physical bounds act as pseudo-observations that pin a term (or a
# sum of terms). Each maps to a qualification requirement a signed physical
# record must satisfy before any state that relies on it can exceed
# CONDITIONAL.
ASSUMPTIONS: Dict[str, Tuple[FrozenSet[str], ...]] = {
    "K_qualified": (frozenset({"K"}),),
    "X_bounded": (frozenset({"X"}),),
    "SJ_bounded": (frozenset({"S", "J"}),),
    "J_bounded": (frozenset({"J"}),),
    "F_bounded": (frozenset({"FG"}), frozenset({"FA"})),
    "M_bounded": (frozenset({"M"}),),
}
REQUIREMENTS: Dict[str, Tuple[str, str]] = {
    "K_qualified": ("QR-K", "Witness differential transfer K characterised at the P0/P1 planes, "
                            "with drift bound over the capture"),
    "X_bounded": ("QR-X", "Known RF source phase X bounded (or independently monitored) over every "
                          "bracket span; required by finding R1"),
    "SJ_bounded": ("QR-SJ", "Joint pilot source + injection path drift S+J bounded"),
    "J_bounded": ("QR-J", "Injection path J drift bounded on its own (source-referenced detector)"),
    "F_bounded": ("QR-F", "Pilot-vs-scene transfer equivalence: FG and FA each bounded across the "
                          "scene band"),
    "M_bounded": ("QR-M", "Analog detector reference and electronics drift M bounded"),
}

CONFIGS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...], str]] = {
    "C0_bracket": (("scene", "reference"), ("X_bounded",), "Scene + switched reference only"),
    "C1_witness": (("scene", "reference", "witness"), ("K_qualified", "X_bounded"),
                   "Gate 0.3 baseline: + independent R witness"),
    "C2_sampled_pilot": (("scene", "reference", "witness", "pilot_sampled"),
                         ("K_qualified", "X_bounded", "SJ_bounded", "F_bounded"),
                         "004.4 option A: + sampled SDR pilot"),
    "C3_analog_P4": (("scene", "reference", "witness", "analog_monitor_P4"),
                     ("K_qualified", "X_bounded", "SJ_bounded", "F_bounded", "M_bounded"),
                     "004.4 option B: + analog detector at P4, independent reference"),
    "C3s_analog_P4_srcref": (("scene", "reference", "witness", "analog_monitor_P4_srcref"),
                             ("K_qualified", "X_bounded", "J_bounded", "F_bounded", "M_bounded"),
                             "Option B with the detector referenced to the pilot source"),
    "C4_pilot_plus_path": (("scene", "reference", "witness", "pilot_sampled", "pilot_path_monitor"),
                           ("K_qualified", "X_bounded", "F_bounded"),
                           "Option A + independent pilot-path monitor"),
    "C5_all": (("scene", "reference", "witness", "pilot_sampled", "analog_monitor_P4",
                "pilot_path_monitor"), ("K_qualified", "X_bounded", "F_bounded", "M_bounded"),
               "Every candidate sensor (independent-reference detector)"),
}

FUNCTIONALS: Dict[str, Dict[str, int]] = {
    # The receiver phase U = A*G (additive in phase) is what the pilot targets.
    "U=AG": {"G": 1, "A": 1},
}


def _row(sensor_or_terms):
    if isinstance(sensor_or_terms, Sensor):
        return [Fraction(sensor_or_terms.c(k)) for k in ORDER]
    return [Fraction(int(k in sensor_or_terms)) for k in ORDER]


def _rref(rows):
    m = [list(r) for r in rows]
    pivots, r = [], 0
    for c in range(len(m[0]) if m else 0):
        p = next((i for i in range(r, len(m)) if m[i][c] != 0), None)
        if p is None:
            continue
        m[r], m[p] = m[p], m[r]
        pv = m[r][c]
        m[r] = [x / pv for x in m[r]]
        for i in range(len(m)):
            if i != r and m[i][c] != 0:
                f = m[i][c]
                m[i] = [a - f * b for a, b in zip(m[i], m[r])]
        pivots.append(c)
        r += 1
        if r == len(m):
            break
    return m[:r], pivots


def rank(rows):
    return len(_rref(rows)[1]) if rows else 0


def in_rowspace(vec, rows):
    return bool(rows) and rank(rows + [vec]) == rank(rows)


def sensor_rows(sensors):
    return [_row(SENSORS[s]) for s in sensors]


def assumption_rows(assumptions):
    return [_row(t) for a in assumptions for t in ASSUMPTIONS[a]]


def observe(sensors, delta: Dict[str, float]) -> Dict[str, float]:
    """Linear sensor response to a phase perturbation ``delta`` (term -> rad)."""
    return {s: sum(SENSORS[s].c(k) * v for k, v in delta.items()) for s in sensors}


@dataclass(frozen=True)
class Cell:
    detectable: bool
    status: str               # ATTRIBUTABLE | CONDITIONAL | CONFOUNDED | UNDETECTABLE
    circuits: Tuple[FrozenSet[str], ...]  # minimal confounding sets containing it


def _columns(sensors):
    return {k: [Fraction(SENSORS[s].c(k)) for s in sensors] for k in ORDER}


def circuits(sensors) -> List[FrozenSet[str]]:
    """Minimal sets of impairments whose sensor columns are linearly dependent.

    A circuit {a, b, c} means some non-zero combination of changes to exactly
    a, b and c leaves every sensor unchanged: those changes are
    indistinguishable online. Basis-independent, unlike a null-space basis.
    An impairment is attributable iff it lies in no circuit.
    """
    cols = _columns(sensors)
    found: List[FrozenSet[str]] = []
    for size in range(1, len(ORDER) + 1):
        for T in combinations(ORDER, size):
            fs = frozenset(T)
            if any(c <= fs for c in found):
                continue
            if rank([cols[k] for k in T]) < size:
                found.append(fs)
    return found


def circuit_vector(sensors, circuit) -> Dict[str, Fraction]:
    """The (unique up to scale) null vector supported exactly on ``circuit``.

    Normalised so the first term in ORDER has coefficient +1. Applying
    ``delta * v`` as a disturbance leaves every sensor in ``sensors`` unchanged.
    """
    terms = sorted(circuit, key=ORDER.index)
    cols = _columns(sensors)
    # Solve sum_k v_k col_k = 0 with v_{terms[0]} = 1 via rref of [cols of rest | -col0].
    rest = terms[1:]
    if not rest:
        if any(cols[terms[0]]):
            raise ValueError("not a circuit")
        return {terms[0]: Fraction(1)}
    aug = [[cols[k][r] for k in rest] + [-cols[terms[0]][r]] for r in range(len(sensors))]
    red, piv = _rref(aug)
    if len(piv) != len(rest) or len(rest) in piv:
        raise ValueError(f"{set(circuit)} is not a circuit of {sensors}")
    v = {terms[0]: Fraction(1)}
    for r, pc in enumerate(piv):
        v[rest[pc]] = red[r][-1]
    if any(x == 0 for x in v.values()):
        raise ValueError("not minimal")
    if any(abs(x) > 1e-12 for x in observe(sensors, v).values()):
        raise AssertionError("circuit vector does not annihilate the sensors")
    return v


def _status(vec, rows, arows, detectable):
    if not detectable:
        return "UNDETECTABLE"
    if in_rowspace(vec, rows):
        return "ATTRIBUTABLE"
    if in_rowspace(vec, arows):
        return "CONDITIONAL"
    return "CONFOUNDED"


def _vec(key):
    if key in FUNCTIONALS:
        return [Fraction(FUNCTIONALS[key].get(k, 0)) for k in ORDER]
    return [Fraction(int(k == key)) for k in ORDER]


def analyse(sensors, assumptions=()) -> Dict[str, Cell]:
    rows = sensor_rows(sensors)
    arows = rows + assumption_rows(assumptions)
    circ = circuits(sensors)
    out = {}
    for k in list(ORDER) + list(FUNCTIONALS):
        involved = set(FUNCTIONALS.get(k, {k: 1}))
        detectable = any(involved & SENSORS[s].terms for s in sensors)
        mine = () if k in FUNCTIONALS else tuple(sorted(
            (c for c in circ if k in c and len(c) > 1),
            key=lambda c: (len(c), sorted(ORDER.index(x) for x in c))))
        out[k] = Cell(detectable, _status(_vec(k), rows, arows, detectable), mine)
    return out


def analyse_config(name):
    sensors, assumptions, _ = CONFIGS[name]
    return analyse(sensors, assumptions)


def minimal_requirements(sensors, assumptions, key) -> List[FrozenSet[str]]:
    """Minimal subsets of ``assumptions`` that make ``key`` estimable."""
    rows = sensor_rows(sensors)
    v = _vec(key)
    if in_rowspace(v, rows):
        return [frozenset()]
    found: List[FrozenSet[str]] = []
    for size in range(1, len(assumptions) + 1):
        for sub in combinations(assumptions, size):
            fs = frozenset(sub)
            if any(f <= fs for f in found):
                continue
            if in_rowspace(v, rows + assumption_rows(sub)):
                found.append(fs)
    return found


def _abbrev(cell: Cell):
    return {"ATTRIBUTABLE": "**attrib**", "CONDITIONAL": "cond",
            "CONFOUNDED": "conf", "UNDETECTABLE": "-"}[cell.status]


def _fmt_set(c):
    return "{" + ",".join(sorted(c, key=ORDER.index)) + "}"


def render_markdown() -> str:
    """Deterministic Markdown; docs/OBSERVABILITY_MATRIX.md is generated from this."""
    L: List[str] = []
    L.append("# Gate 0.4 impairment observability matrix (generated)\n")
    L.append("Generated by `python -m caelarch_rf.gate04.observability`. Do not edit by hand; "
             "`test_observability` fails if this file is stale.\n")
    L.append("Structural, linearised phase model. **Necessary, not sufficient**: a cell that "
             "is attributable here can still fail on hardware (noise, sampling, bandwidth, "
             "time multiplexing, non-linear or amplitude effects are not in this table).\n")
    L.append("## Impairments\n")
    L.append("| Id | Meaning |\n|---|---|")
    for k, v in IMPAIRMENTS.items():
        L.append(f"| `{k}` | {v} |")
    for k, coef in FUNCTIONALS.items():
        L.append(f"| `{k}` | Combined receiver phase " + " + ".join(coef)
                 + " (functional, not a separate term) |")
    L.append("\n## Sensor x impairment (signed phase coefficient)\n")
    L.append("`+1`/`-1`: the sensor's phase moves with/against the term; `.`: does not move. "
             "A quadrature detector reports signal phase minus its own reference phase.\n")
    L.append("| Sensor | Plane | " + " | ".join(ORDER) + " | Exists? |")
    L.append("|---|---|" + "---|" * len(ORDER) + "---|")
    for s in SENSORS.values():
        cells = " | ".join(f"{s.c(k):+d}" if s.c(k) else "." for k in ORDER)
        L.append(f"| `{s.name}` | {s.plane} | {cells} | "
                 f"{'hypothetical' if s.hypothetical else 'planned (no hardware yet)'} |")
    L.append("\n## Configuration x impairment (online attribution)\n")
    L.append("`**attrib**` estimable from online data alone; `cond` estimable only with the "
             "listed qualified-bound assumptions (state at best CONDITIONAL until a signed "
             "record exists); `conf` detectable but confounded; `-` undetectable.\n")
    cols = list(ORDER) + list(FUNCTIONALS)
    L.append("| Config | Sensors | Candidate assumptions | " + " | ".join(cols) + " |")
    L.append("|---|---|---|" + "---|" * len(cols))
    for name, (sensors, assumptions, _) in CONFIGS.items():
        res = analyse(sensors, assumptions)
        L.append(f"| `{name}` | {', '.join(sensors)} | {', '.join(assumptions) or 'none'} | "
                 + " | ".join(_abbrev(res[k]) for k in cols) + " |")
    L.append("\n## Qualification requirements\n")
    L.append("| Id | Assumption | Requirement |\n|---|---|---|")
    for a, (qid, text) in REQUIREMENTS.items():
        L.append(f"| **{qid}** | `{a}` | {text} |")
    L.append("\nMinimal requirement sets per configuration: each alternative (separated by "
             "*or*) is a smallest set of QRs that makes the quantity estimable. A quantity "
             "absent from a row stays confounded under every candidate assumption.\n")
    for name, (sensors, assumptions, desc) in CONFIGS.items():
        res = analyse(sensors, assumptions)
        parts = []
        for k in cols:
            if res[k].status != "CONDITIONAL":
                continue
            alts = minimal_requirements(sensors, assumptions, k)
            parts.append(f"`{k}`: " + " *or* ".join(
                "{" + ", ".join(REQUIREMENTS[a][0] for a in sorted(alt, key=list(ASSUMPTIONS).index)) + "}"
                for alt in alts))
        L.append(f"- **{name}**: " + ("; ".join(parts) if parts else "none"))
    L.append("\n## Minimal confounding sets (online data only)\n")
    L.append("Each set is a *circuit*: some non-zero combination of changes to exactly those "
             "impairments leaves every sensor in the configuration unchanged, so the online "
             "data cannot tell them apart. A pair such as `{G,A}` means the two have "
             "identical sensor signatures. Circuits are per configuration: `{X,G}` is a "
             "circuit of C0/C1 but not of C2, where the pilot separates them (larger "
             "circuits such as `{X,J,G}` remain). The Sprint 1a generator builds matched "
             "disturbance pairs from these sets.\n")
    for name, (sensors, assumptions, desc) in CONFIGS.items():
        sets = [_fmt_set(c) for c in sorted(
            (c for c in circuits(sensors) if len(c) > 1),
            key=lambda c: (len(c), sorted(ORDER.index(x) for x in c)))]
        L.append(f"- **{name}** ({desc}): " + (", ".join(f"`{x}`" for x in sets) or "none"))
    L.append("\n## Temporal coverage (qualitative; not in the rank analysis)\n")
    L.append("| Sensor | Coverage |\n|---|---|")
    for s in SENSORS.values():
        L.append(f"| `{s.name}` | {s.temporal} |")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":  # pragma: no cover
    import os
    import sys
    out = os.path.join(os.path.dirname(__file__), "..", "docs", "OBSERVABILITY_MATRIX.md")
    if "--check" in sys.argv:
        with open(out, encoding="utf-8") as f:
            sys.exit(0 if f.read() == render_markdown() else 1)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_markdown())
    print(f"wrote {os.path.normpath(out)}")
