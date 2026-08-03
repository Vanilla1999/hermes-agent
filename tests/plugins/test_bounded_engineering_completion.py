"""Canonical completion-evidence aggregation tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from completion import build_completion_evidence
from verification import VerificationDispatchError, VerificationEvidence


def _evidence(identifier: str, *, snapshot: str = "a" * 64, passed: bool = True) -> VerificationEvidence:
    return VerificationEvidence(identifier, snapshot, 0 if passed else 1, passed, f"{identifier} output")


def test_completion_evidence_canonically_binds_required_passed_evidence() -> None:
    aggregate = build_completion_evidence(
        (_evidence("unit"), _evidence("lint")),
        snapshot_digest="a" * 64,
        required_ids=("unit", "lint"),
    )

    assert len(aggregate.sha256) == 64
    assert b'"required_verification_ids":["unit","lint"]' in aggregate.payload
    assert b'"snapshot_digest":"' + b"a" * 64 in aggregate.payload


def test_completion_evidence_rejects_missing_or_ambiguous_required_evidence() -> None:
    with pytest.raises(VerificationDispatchError, match="missing passed evidence"):
        build_completion_evidence((_evidence("unit"),), snapshot_digest="a" * 64, required_ids=("unit", "lint"))
    with pytest.raises(VerificationDispatchError, match="missing passed evidence"):
        build_completion_evidence((_evidence("unit"), _evidence("unit")), snapshot_digest="a" * 64, required_ids=("unit",))
