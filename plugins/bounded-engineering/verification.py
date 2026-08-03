"""Fail-closed dispatch of contract-declared verification commands."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, cast

if __package__ and __import__("sys").modules.get(__package__) is not None:
    from .contract import Verification
    from .storage import store_immutable_evidence
else:  # Direct-module tests and scripts.
    from contract import Verification
    from storage import store_immutable_evidence


class VerificationDispatchError(RuntimeError):
    """Raised when declared verification cannot yield trustworthy evidence."""


@dataclass(frozen=True)
class VerificationEvidence:
    verification_id: str
    snapshot_digest: str
    exit_code: int
    passed: bool
    output: str


def persist_evidence(hermes_home: Any, evidence: VerificationEvidence):
    """Persist redacted evidence as exact canonical bytes in immutable storage."""
    payload = json.dumps(
        {
            "exit_code": evidence.exit_code,
            "output": evidence.output,
            "passed": evidence.passed,
            "snapshot_digest": evidence.snapshot_digest,
            "verification_id": evidence.verification_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return store_immutable_evidence(hermes_home, payload)


def select_evidence_for_snapshot(
    evidence: tuple[VerificationEvidence, ...], *, snapshot_digest: str, required_ids: tuple[str, ...]
) -> tuple[VerificationEvidence, ...]:
    """Select only unambiguous passed evidence for the exact completion snapshot."""
    selected: list[VerificationEvidence] = []
    for verification_id in required_ids:
        matches = [
            item for item in evidence
            if item.verification_id == verification_id
            and item.snapshot_digest == snapshot_digest
            and item.passed
        ]
        if len(matches) != 1:
            _fail(f"missing passed evidence: {verification_id}")
        selected.append(matches[0])
    return tuple(selected)


def _fail(message: str) -> None:
    raise VerificationDispatchError(message)


def _redact_output(output: str) -> str:
    return re.sub(
        r"(?i)\b(token|password|secret|api[_-]?key)=\S+",
        r"\1=[REDACTED]",
        output,
    )


def _pytest_collected(output: str) -> int | None:
    explicit = re.search(r"\bcollected\s+(\d+)\s+items?\b", output)
    if explicit:
        return int(explicit.group(1))
    if re.search(r"\bno tests ran\b", output):
        return 0
    for line in reversed(output.splitlines()):
        counts = re.findall(
            r"\b(\d+)\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b",
            line,
        )
        if counts:
            return sum(int(value) for value in counts)
    return None


def dispatch_declared_verification(ctx: Any, declaration: Verification, *, snapshot_digest: str) -> VerificationEvidence:
    """Run one immutable declaration; callers supply no command or execution knobs."""
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_digest):
        _fail("snapshot_digest must be a SHA-256 hex digest")
    if declaration.parser not in {"exit_zero", "pytest"}:
        _fail(f"unsupported verification parser: {declaration.parser}")
    try:
        raw = ctx.dispatch_tool(
            "terminal",
            {"command": shlex.join(declaration.argv), "timeout": declaration.timeout_seconds},
        )
    except Exception as exc:
        raise VerificationDispatchError(f"verification dispatch failed: {declaration.id}") from exc
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VerificationDispatchError("terminal result must be structured JSON") from exc
    if not isinstance(result, dict):
        _fail("terminal result must be an object")
    error = result.get("error")
    if error is not None:
        if not isinstance(error, str):
            _fail("terminal result error must be a string or null")
        if error.strip():
            _fail(f"terminal execution failed: {_redact_output(error.strip())}")
    exit_code = result.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        _fail("terminal result requires an unambiguous integer exit_code")
    exit_code = cast(int, exit_code)
    output = result.get("output", "")
    if not isinstance(output, str):
        _fail("terminal result output must be a string")
    if declaration.parser == "pytest" and declaration.minimum_collected is not None:
        collected = _pytest_collected(output)
        if collected is None:
            _fail("pytest output does not contain an unambiguous collected count")
        if collected < declaration.minimum_collected:
            _fail("pytest result collected fewer tests than declared minimum")
    return VerificationEvidence(
        declaration.id, snapshot_digest, exit_code, exit_code == 0, _redact_output(output)
    )
