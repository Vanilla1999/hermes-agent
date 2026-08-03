"""Canonical aggregate evidence required for gated bounded completion."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

if __package__ and __import__("sys").modules.get(__package__) is not None:
    from .verification import VerificationEvidence, select_evidence_for_snapshot
else:  # Direct-module tests and scripts.
    from verification import VerificationEvidence, select_evidence_for_snapshot


@dataclass(frozen=True)
class CompletionEvidence:
    sha256: str
    payload: bytes


def build_completion_evidence(
    evidence: tuple[VerificationEvidence, ...], *, snapshot_digest: str, required_ids: tuple[str, ...]
) -> CompletionEvidence:
    """Canonically bind every required passed verification to one snapshot."""
    selected = select_evidence_for_snapshot(
        evidence, snapshot_digest=snapshot_digest, required_ids=required_ids
    )
    payload = json.dumps(
        {
            "required_verification_ids": list(required_ids),
            "snapshot_digest": snapshot_digest,
            "verifications": [
                {
                    "exit_code": item.exit_code,
                    "output": item.output,
                    "verification_id": item.verification_id,
                }
                for item in selected
            ],
        },
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return CompletionEvidence(hashlib.sha256(payload).hexdigest(), payload)
