"""Compact non-authoritative Kanban body tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from body import BodyRenderError, render_compact_body  # noqa: E402


def test_render_compact_body_includes_digests_and_protocol() -> None:
    body = render_compact_body(
        spec_sha256="a" * 64,
        contract_sha256="b" * 64,
        baseline_head="c" * 40,
        repository="/repo",
        objective="Make the bounded task deterministic.",
        acceptance=(("A1", "Tests pass."),),
        allowed_paths=("src/example", "tests/test_example.py"),
        verification_ids=("focused",),
        risk="local_behavior",
    )

    assert "[bounded-engineering/v1]" in body
    assert "finish: engineering_verify(...) then engineering_complete(...)" in body
    assert len(body.encode("utf-8")) < 6 * 1024


def test_render_compact_body_rejects_hard_cap_overflow() -> None:
    with pytest.raises(BodyRenderError, match="hard cap"):
        render_compact_body(
            spec_sha256="a" * 64,
            contract_sha256="b" * 64,
            baseline_head="c" * 40,
            repository="/repo",
            objective="x" * (13 * 1024),
            acceptance=(),
            allowed_paths=(),
            verification_ids=(),
            risk="local_behavior",
        )
