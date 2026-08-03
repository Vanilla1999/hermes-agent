"""Declared verification dispatch tests for bounded-engineering."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from contract import Verification  # noqa: E402
from verification import VerificationDispatchError, dispatch_declared_verification, persist_evidence, select_evidence_for_snapshot  # noqa: E402


class FakeContext:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def dispatch_tool(self, tool_name: str, args: dict) -> str:
        self.calls.append((tool_name, args))
        return self.response


def test_dispatch_binds_declared_command_result_to_exact_snapshot() -> None:
    declaration = Verification("focused", "test", ("python3", "-m", "pytest", "-q"), 60, "pytest", 1, True)
    ctx = FakeContext(json.dumps({"exit_code": 0, "output": "1 passed in 0.01s", "error": None}))

    evidence = dispatch_declared_verification(ctx, declaration, snapshot_digest="a" * 64)

    assert ctx.calls == [("terminal", {"command": "python3 -m pytest -q", "timeout": 60})]
    assert evidence.verification_id == "focused"
    assert evidence.snapshot_digest == "a" * 64
    assert evidence.exit_code == 0
    assert evidence.passed is True


def test_dispatch_supports_only_declared_structured_parsers() -> None:
    exit_zero = Verification("lint", "lint", ("python3", "-m", "ruff", "check", "."), 60, "exit_zero", None, True)
    evidence = dispatch_declared_verification(
        FakeContext(json.dumps({"exit_code": 0, "output": "All checks passed"})),
        exit_zero,
        snapshot_digest="a" * 64,
    )

    assert evidence.passed is True

    unsupported = Verification("unknown", "test", ("python3", "-m", "pytest", "-q"), 60, "human_output", None, True)
    with pytest.raises(VerificationDispatchError, match="unsupported verification parser"):
        dispatch_declared_verification(
            FakeContext(json.dumps({"exit_code": 0, "output": "pass"})),
            unsupported,
            snapshot_digest="a" * 64,
        )


def test_dispatch_rejects_missing_or_ambiguous_exit_code() -> None:
    declaration = Verification("focused", "test", ("python3", "-m", "pytest", "-q"), 60, "pytest", 1, True)

    with pytest.raises(VerificationDispatchError, match="exit_code"):
        dispatch_declared_verification(FakeContext(json.dumps({"output": "passed"})), declaration, snapshot_digest="a" * 64)


def test_dispatch_rejects_zero_collected_pytest_result() -> None:
    declaration = Verification("focused", "test", ("python3", "-m", "pytest", "-q"), 60, "pytest", 1, True)

    with pytest.raises(VerificationDispatchError, match="collected"):
        dispatch_declared_verification(
            FakeContext(json.dumps({"exit_code": 0, "output": "no tests ran in 0.01s", "error": None})),
            declaration,
            snapshot_digest="a" * 64,
        )


def test_dispatch_rejects_timeout_without_evidence() -> None:
    class TimeoutContext:
        def dispatch_tool(self, tool_name: str, args: dict) -> str:
            raise TimeoutError("tool timeout")

    declaration = Verification("focused", "test", ("python3", "-m", "pytest", "-q"), 60, "pytest", 1, True)
    with pytest.raises(VerificationDispatchError, match="verification dispatch failed: focused"):
        dispatch_declared_verification(TimeoutContext(), declaration, snapshot_digest="a" * 64)


def test_dispatch_reports_terminal_backend_error_before_parser_error() -> None:
    declaration = Verification("focused", "test", ("python3", "-m", "pytest", "-q"), 60, "pytest", 1, True)
    response = {"exit_code": -1, "output": "", "error": "docker unavailable"}

    with pytest.raises(VerificationDispatchError, match="terminal execution failed: docker unavailable"):
        dispatch_declared_verification(
            FakeContext(json.dumps(response)), declaration, snapshot_digest="a" * 64,
        )


def test_dispatch_counts_all_pytest_summary_outcomes() -> None:
    declaration = Verification("focused", "test", ("python3", "-m", "pytest", "-q"), 60, "pytest", 5, True)
    response = {"exit_code": 1, "output": "1 failed, 3 passed, 1 skipped in 0.10s", "error": None}

    evidence = dispatch_declared_verification(
        FakeContext(json.dumps(response)), declaration, snapshot_digest="a" * 64,
    )

    assert evidence.passed is False


def test_dispatch_redacts_credential_like_output_before_evidence() -> None:
    declaration = Verification("focused", "test", ("python3", "-m", "pytest", "-q"), 60, "pytest", 1, True)
    ctx = FakeContext(json.dumps({"exit_code": 1, "output": "token=secret-value\n1 failed in 0.01s", "error": None}))

    evidence = dispatch_declared_verification(ctx, declaration, snapshot_digest="a" * 64)

    assert evidence.passed is False
    assert evidence.output == "token=[REDACTED]\n1 failed in 0.01s"


def test_persist_evidence_stores_canonical_redacted_payload(tmp_path: Path) -> None:
    evidence = dispatch_declared_verification(
        FakeContext(json.dumps({"exit_code": 0, "output": "token=secret\n1 passed in 0.01s", "error": None})),
        Verification("focused", "test", ("python3", "-m", "pytest", "-q"), 60, "pytest", 1, True),
        snapshot_digest="a" * 64,
    )

    path = persist_evidence(tmp_path, evidence)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "exit_code": 0,
        "output": "token=[REDACTED]\n1 passed in 0.01s",
        "passed": True,
        "snapshot_digest": "a" * 64,
        "verification_id": "focused",
    }


def test_select_evidence_rejects_required_evidence_from_a_stale_snapshot() -> None:
    focused = dispatch_declared_verification(
        FakeContext(json.dumps({"exit_code": 0, "output": "1 passed", "error": None})),
        Verification("focused", "test", ("python3", "-m", "pytest"), 60, "pytest", 1, True),
        snapshot_digest="a" * 64,
    )
    stale = dispatch_declared_verification(
        FakeContext(json.dumps({"exit_code": 0, "output": "1 passed", "error": None})),
        Verification("secondary", "test", ("python3", "-m", "pytest"), 60, "pytest", 1, True),
        snapshot_digest="b" * 64,
    )

    with pytest.raises(VerificationDispatchError, match="missing passed evidence: secondary"):
        select_evidence_for_snapshot((focused, stale), snapshot_digest="a" * 64, required_ids=("focused", "secondary"))
