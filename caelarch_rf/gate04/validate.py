"""Validate a capture directory against the Gate 0.4 contract.

    python -m caelarch_rf.gate04.validate <capture_dir>

Exit 0 = contract-conformant (format only; NOT a qualification); 1 = refused.
"""
from __future__ import annotations

import sys

from .contract import ContractError
from .ingest import load_capture


def report(cap) -> str:
    lines = [f"capture {cap.manifest['capture_id']}  provenance={cap.manifest['provenance']['kind']}"]
    if cap.synthetic:
        lines.append("  NOTE: synthetic capture - cannot support any hardware claim")
    lines.append(f"  events={len(cap.events)} witness_records={len(cap.witness)} "
                 f"truth_commitment={'yes' if cap.manifest.get('truth_commitment') else 'NO (cannot score blind)'}")
    for sv in cap.streams.values():
        acc = sum(b - a for a, b in sv.accepted_ranges())
        lines.append(f"  stream {sv.stream_id}: n={sv.n} fs={float(sv.fs):g} Hz "
                     f"accepted={acc}/{sv.n} ({100.0 * acc / sv.n:.1f}%) "
                     f"retunes={len(sv.retune_indices)} untimed_from={sv.untimed_from}")
        reasons = {}
        for r in sv.rejections:
            reasons[r.reason] = reasons.get(r.reason, 0) + (r.stop - r.start)
        for k in sorted(reasons):
            lines.append(f"    rejected {k}: {reasons[k]} samples (pre-merge)")
    lines.append("  Contract conformance is not instrument, phase-transfer or monitor qualification.")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        cap = load_capture(argv[0])
    except (ContractError, OSError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    print(report(cap))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
