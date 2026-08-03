"""Bounded, non-authoritative rendering of a Kanban task body."""
from __future__ import annotations

import json
from typing import Iterable


TARGET_BYTES = 6 * 1024
HARD_BYTES = 12 * 1024


class BodyRenderError(ValueError):
    """Raised when a compact task body cannot fit the mandatory hard cap."""


def render_compact_body(
    *,
    spec_sha256: str,
    contract_sha256: str,
    baseline_head: str,
    repository: str,
    objective: str,
    acceptance: Iterable[tuple[str, str]],
    allowed_paths: Iterable[str],
    verification_ids: Iterable[str],
    risk: str,
    diagnostic_context: dict | None = None,
    benchmark: dict | None = None,
) -> str:
    """Render only stable task context; never logs, commands, or evidence bodies."""
    acceptance_lines = "\n".join(f"  {identifier}: {text}" for identifier, text in acceptance) or "  (none)"
    allowed = ", ".join(allowed_paths) or "(none)"
    verification = ", ".join(verification_ids) or "(none)"
    lines = [
        "[bounded-engineering/v1]",
        f"spec_sha256: {spec_sha256}",
        f"contract_sha256: {contract_sha256}",
        f"baseline_head: {baseline_head}",
        f"repository: {repository}",
        f"objective: {objective}",
        "acceptance:",
        acceptance_lines,
        f"allowed paths: {allowed}",
        f"required verification: {verification}",
        f"risk: {risk}",
        "forbidden: push/deploy/history rewrite/direct kanban completion/subagents",
        "finish: engineering_verify(...) then engineering_complete(...)",
        "block: engineering_block(...)",
    ]
    if diagnostic_context is not None:
        lines.extend((
            "prior diagnostic context (read-only):",
            json.dumps(diagnostic_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
    if benchmark is not None:
        lines.extend((
            f"benchmark_experiment: {benchmark['experiment_id']}",
            f"benchmark_lane: {benchmark['lane']}",
            f"docatlas_index_revision: {benchmark['docatlas_index_revision']}",
        ))
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > HARD_BYTES:
        raise BodyRenderError(f"compact body exceeds hard cap of {HARD_BYTES} bytes")
    return body
