"""Gate 0.4 observation graph and impairment observability matrix (P0.2).

A *structural*, linearised phase model of the proposed 004.4 bench. Each
impairment is an additive phase term; each sensor observes the sum of the
terms on its physical path (see ../docs/GATE0_4_ARCHITECTURE.md for planes).
For a sensor set we compute, exactly (rational arithmetic):

* detectable   - some sensor's observation moves when the impairment moves;
* attributable - the impairment is estimable (its unit vector lies in the row
                 space of the sensor matrix), i.e. it can be separated from
                 every other impairment by the online data alone;
* conditional  - estimable only after adding *qualified bound* rows (e.g. a
                 qualified witness transfer K) that are assumptions, not data;
* confounded   - otherwise; we list its *minimal confounding sets* (circuits).

Limits, stated so the table is not over-read: static phase-only model; no
noise, amplitude, time multiplexing, sampling or bandwidth (those are in the
temporal-coverage table, qualitatively); two terms carried by the same sensor
set are treated as distinct parameters even if hardware makes them collinear.
Passing this analysis is necessary, not sufficient, for a physical claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
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
}
ORDER = tuple(IMPAIRMENTS)


@dataclass(frozen=True)
class Sensor:
    name: str
    terms: FrozenSet[str]
    plane: str
    temporal: str  # qualitative coverage note
    hypothetical: bool = False


SENSORS: Dict[str, Sensor] = {s.name: s for s in [
    Sensor("scene", frozenset("XHGA"), "ADC (selector on H)",
           "Sampled at fs inside accepted intervals; time-multiplexed with reference"),
    Sensor("reference", frozenset("XRGA"), "ADC (selector on R)",
           "Sampled at fs only while selector is on R; brackets cannot see interior AG excursions"),
    Sensor("witness", frozenset("RK"), "P0/P1 (external instrument)",
           "Instrument rate; needs a declared timebase alignment to be fresh against SDR time"),
    Sensor("pilot_sampled", frozenset({"S", "J", "G", "A", "FG", "FA"}), "ADC (digital extraction)",
           "Sampled at fs, continuous across selector states, minus gaps/clipping/settling; "
           "blind between samples and above extraction bandwidth"),
    Sensor("analog_monitor_P4", frozenset({"S", "J", "G", "FG"}), "P4 (before SDR conversion)",
           "Continuous-time but band-limited by detector; cannot see A (downstream of tap)",
           hypothetical=True),
    Sensor("pilot_path_monitor", frozenset("SJ"), "PS -> J (independent coupler/detector)",
           "Independent observation of source+injection; does not exist yet",
           hypothetical=True),
]}

# Qualified physical bounds act as pseudo-observations that pin a term.
# They are assumptions from a signed qualification record, never online data.
# Each assumption contributes one or more rows; a row pins the SUM of its terms
# (e.g. the 004.4 scaffold bounds source+injection drift jointly).
ASSUMPTIONS: Dict[str, Tuple[FrozenSet[str], ...]] = {
    "K_qualified": (frozenset({"K"}),),
    "SJ_bounded": (frozenset({"S", "J"}),),
    "F_bounded": (frozenset({"FG"}), frozenset({"FA"})),
}

CONFIGS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...], str]] = {
    "C0_bracket": (("scene", "reference"), (), "Scene + switched reference only"),
    "C1_witness": (("scene", "reference", "witness"), ("K_qualified",),
                   "Gate 0.3 baseline: + independent R witness"),
    "C2_sampled_pilot": (("scene", "reference", "witness", "pilot_sampled"),
                         ("K_qualified", "SJ_bounded", "F_bounded"),
                         "004.4 option A: + sampled SDR pilot"),
    "C3_analog_P4": (("scene", "reference", "witness", "analog_monitor_P4"),
                     ("K_qualified", "SJ_bounded", "F_bounded"),
                     "004.4 option B: + analog quadrature monitor at P4"),
    "C4_pilot_plus_path": (("scene", "reference", "witness", "pilot_sampled", "pilot_path_monitor"),
                           ("K_qualified", "F_bounded"),
                           "Option A + independent pilot-path monitor"),
    "C5_all": (("scene", "reference", "witness", "pilot_sampled", "analog_monitor_P4",
                "pilot_path_monitor"), ("K_qualified",),
               "Every candidate sensor"),
}


def _row(terms):
    return [Fraction(1) if k in terms else Fraction(0) for k in ORDER]


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


FUNCTIONALS: Dict[str, Dict[str, int]] = {
    # The receiver phase U = A*G (additive in phase) is what the pilot targets.
    "U=AG": {"G": 1, "A": 1},
}


@dataclass(frozen=True)
class Cell:
    detectable: bool
    status: str               # ATTRIBUTABLE | CONDITIONAL | CONFOUNDED | UNDETECTABLE
    circuits: Tuple[FrozenSet[str], ...]  # minimal confounding sets containing it


def circuits(sensors) -> List[FrozenSet[str]]:
    """Minimal sets of impairments whose sensor columns are linearly dependent.

    A circuit {a, b, c} means some non-zero combination of changes to exactly
    a, b and c leaves every sensor unchanged: those changes are
    indistinguishable online. Basis-independent, unlike a null-space basis.
    An impairment is attributable iff it lies in no circuit.
    """
    from itertools import combinations
    cols = {k: [Fraction(int(k in SENSORS[s].terms)) for s in sensors] for k in ORDER}
    found: List[FrozenSet[str]] = []
    for size in range(1, len(ORDER) + 1):
        for T in combinations(ORDER, size):
            fs = frozenset(T)
            if any(c <= fs for c in found):
                continue
            if rank([cols[k] for k in T]) < size:
                found.append(fs)
    return found


def _status(vec, rows, arows, detectable):
    if not detectable:
        return "UNDETECTABLE"
    if in_rowspace(vec, rows):
        return "ATTRIBUTABLE"
    if in_rowspace(vec, arows):
        return "CONDITIONAL"
    return "CONFOUNDED"


def analyse(sensors, assumptions=()) -> Dict[str, Cell]:
    rows = [_row(SENSORS[s].terms) for s in sensors]
    arows = rows + [_row(t) for a in assumptions for t in ASSUMPTIONS[a]]
    circ = circuits(sensors)
    out = {}
    for i, k in enumerate(ORDER):
        e = [Fraction(int(j == i)) for j in range(len(ORDER))]
        detectable = any(k in SENSORS[s].terms for s in sensors)
        mine = tuple(sorted((c for c in circ if k in c and len(c) > 1),
                            key=lambda c: (len(c), sorted(ORDER.index(x) for x in c))))
        out[k] = Cell(detectable, _status(e, rows, arows, detectable), mine)
    for name, coef in FUNCTIONALS.items():
        v = [Fraction(coef.get(k, 0)) for k in ORDER]
        detectable = any(set(coef) & SENSORS[s].terms for s in sensors)
        out[name] = Cell(detectable, _status(v, rows, arows, detectable), ())
    return out


def analyse_config(name):
    sensors, assumptions, _ = CONFIGS[name]
    return analyse(sensors, assumptions)


def _abbrev(cell: Cell):
    return {"ATTRIBUTABLE": "**attrib**", "CONDITIONAL": "cond",
            "CONFOUNDED": "conf", "UNDETECTABLE": "-"}[cell.status]


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
        L.append(f"| `{k}` | Combined receiver phase " + " + ".join(coef) + " (functional, not a separate term) |")
    L.append("\n## Sensor x impairment (does the sensor's phase move?)\n")
    L.append("| Sensor | Plane | " + " | ".join(ORDER) + " | Exists? |")
    L.append("|---|---|" + "---|" * len(ORDER) + "---|")
    for s in SENSORS.values():
        cells = " | ".join("x" if k in s.terms else "." for k in ORDER)
        L.append(f"| `{s.name}` | {s.plane} | {cells} | "
                 f"{'hypothetical' if s.hypothetical else 'planned (no hardware yet)'} |")
    L.append("\n## Configuration x impairment (online attribution)\n")
    L.append("`**attrib**` estimable from online data alone; `cond` estimable only with the "
             "listed qualified-bound assumptions (state at best CONDITIONAL until a signed "
             "record exists); `conf` detectable but confounded; `-` undetectable.\n")
    cols = list(ORDER) + list(FUNCTIONALS)
    L.append("| Config | Sensors | Assumptions | " + " | ".join(cols) + " |")
    L.append("|---|---|---|" + "---|" * len(cols))
    for name, (sensors, assumptions, _) in CONFIGS.items():
        res = analyse(sensors, assumptions)
        L.append(f"| `{name}` | {', '.join(sensors)} | {', '.join(assumptions) or 'none'} | "
                 + " | ".join(_abbrev(res[k]) for k in cols) + " |")
    L.append("\n## Minimal confounding sets (online data only)\n")
    L.append("Each set is a *circuit*: some non-zero combination of changes to exactly those "
             "impairments leaves every sensor in the configuration unchanged, so the online "
             "data cannot tell them apart. A pair such as `{G,A}` means the two have "
             "identical sensor signatures.\n")
    for name, (sensors, assumptions, desc) in CONFIGS.items():
        sets = ["{" + ",".join(sorted(c, key=ORDER.index)) + "}"
                for c in sorted((c for c in circuits(sensors) if len(c) > 1),
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
