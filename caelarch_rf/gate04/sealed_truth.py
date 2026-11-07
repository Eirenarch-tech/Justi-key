"""OFFLINE ONLY: sealed disturbance truth and its commitment.

Nothing in the online path (``contract``, ``ingest``, ``records``, ``states``,
any monitor or policy) may import this module; a unit test enforces it.

Commitment scheme ``sha256-salted-v1``: the manifest's
``truth_commitment.digest`` is the SHA-256 of the exact bytes of the truth
file, and the truth file must contain a random salt of at least 32 bytes.
The salt matters: a bare hash of a low-entropy schedule ("G step of 0.3 rad at
120 ms") could be recovered by an online method that enumerates candidate
schedules and compares digests. The digest lets the scorer prove the truth was
fixed before the run; the salt keeps the digest from being an oracle.

The truth file is stored *outside* the capture directory (ingestion refuses
any undeclared file), and should be encrypted at rest by the controller host.
"""
from __future__ import annotations

import hashlib
import json
import secrets

from . import contract as C
from .observability import ORDER

MIN_SALT_BYTES = 32
TARGETS = ORDER  # impairment ids of the observation graph


def _canonical(doc):
    return (json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def write_truth(path, capture_id, controller_timebase, events, salt=None, extra=None):
    """Write a truth file; return the manifest ``truth_commitment`` block.

    ``extra`` (optional dict) is stored under ``"generator"``: seeds, clock
    model and acquisition-fault notes the scorer needs to reconstruct truth.
    """
    salt = salt if salt is not None else secrets.token_bytes(MIN_SALT_BYTES)
    if len(salt) < MIN_SALT_BYTES:
        raise ValueError("salt too short")
    doc = {"schema": C.SCHEMA_TRUTH, "capture_id": capture_id, "salt_hex": salt.hex(),
           "timebase": controller_timebase, "events": list(events)}
    if extra is not None:
        doc["generator"] = extra
    _check(doc)
    data = _canonical(doc)
    with open(path, "wb") as f:
        f.write(data)
    return {"scheme": "sha256-salted-v1", "digest": hashlib.sha256(data).hexdigest()}


def _check(doc):
    if doc.get("schema") != C.SCHEMA_TRUTH:
        raise C.ContractError("not a sealed-truth file")
    if len(bytes.fromhex(doc.get("salt_hex", ""))) < MIN_SALT_BYTES:
        raise C.ContractError("truth salt missing or shorter than 32 bytes")
    for i, ev in enumerate(doc["events"]):
        if ev.get("target") not in TARGETS or not isinstance(ev.get("t_ns"), int):
            raise C.ContractError(f"truth event {i}: needs target in {TARGETS} and integer t_ns")


def open_for_scoring(manifest, truth_path):
    """Verify the commitment, then return the truth document. Scorer use only.

    Call this only after all online decisions for the capture are frozen.
    """
    commit = manifest.get("truth_commitment")
    if not commit:
        raise C.ContractError("capture carries no truth commitment; cannot score blind")
    with open(truth_path, "rb") as f:
        data = f.read()
    if hashlib.sha256(data).hexdigest() != commit["digest"]:
        raise C.ContractError("truth file does not match the committed digest")
    doc = json.loads(data)
    _check(doc)
    if doc["capture_id"] != manifest["capture_id"]:
        raise C.ContractError("truth file belongs to a different capture")
    return doc
