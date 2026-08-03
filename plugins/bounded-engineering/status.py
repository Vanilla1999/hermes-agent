"""Bounded, read-only engineering status output."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

if __package__ and __import__("sys").modules.get(__package__) is not None:
    from .context import TrustedTaskContext
else:  # Direct-module tests and scripts.
    from context import TrustedTaskContext


@dataclass(frozen=True)
class EngineeringStatus:
    task_id: str
    run_id: int
    completion_gate: str
    status: str
    required_verification_ids: tuple[str, ...]
    evidence_count: int
    objective: str
    acceptance: tuple[dict[str, Any], ...]
    allowed_paths: dict[str, Any]


def build_engineering_status(context: TrustedTaskContext, *, evidence_refs: tuple[str, ...] = ()) -> EngineeringStatus:
    """Produce a stable status payload without exposing workspace or mutable state."""
    required = tuple(item.id for item in context.contract.verification if item.required)
    return EngineeringStatus(
        task_id=context.task_id,
        run_id=context.run_id,
        completion_gate=context.completion_gate,
        status=str(context.task.status),
        required_verification_ids=required,
        evidence_count=len(evidence_refs),
        objective=str(context.spec["objective"]),
        acceptance=tuple(context.spec["acceptance"]),
        allowed_paths=dict(context.spec["allowed_paths"]),
    )


def status_payload(status: EngineeringStatus) -> dict[str, Any]:
    return {
        "task_id": status.task_id,
        "run_id": status.run_id,
        "completion_gate": status.completion_gate,
        "status": status.status,
        "required_verification_ids": list(status.required_verification_ids),
        "evidence_count": status.evidence_count,
        "objective": status.objective,
        "acceptance": list(status.acceptance),
        "allowed_paths": status.allowed_paths,
    }
