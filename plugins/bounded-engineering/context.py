"""Fail-closed loading of task/run/workspace/gate context from trusted sources."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

if __package__ and __import__("sys").modules.get(__package__) is not None:
    from .contract import Contract, load_contract
else:  # Direct-module tests and scripts.
    from contract import Contract, load_contract
from hermes_cli.kanban_db import connect, get_run, get_task


class TrustedContextError(ValueError):
    """Raised when any worker-supplied identity disagrees with durable sources."""


@dataclass(frozen=True)
class TrustedTaskContext:
    task_id: str
    run_id: int
    workspace_path: Path
    completion_gate: str
    task: Any
    run: Any
    contract: Contract
    spec: dict[str, Any]


def _load_immutable_spec(task: Any) -> dict[str, Any]:
    body = task.body if isinstance(getattr(task, "body", None), str) else ""
    match = re.search(r"(?m)^spec_sha256: ([0-9a-f]{64})$", body)
    if match is None:
        raise TrustedContextError("task is missing canonical spec digest")
    digest = match.group(1)
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    path = home / "engineering" / "specs" / f"{digest}.json"
    try:
        payload = path.read_bytes()
        spec = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise TrustedContextError("immutable task spec could not be loaded") from exc
    if hashlib.sha256(payload).hexdigest() != digest or not isinstance(spec, dict):
        raise TrustedContextError("immutable task spec digest mismatch")
    return spec


def _canonical_path(value: str | Path | None, name: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise TrustedContextError(f"{name} must be a non-empty workspace path")
    return Path(value).resolve()


def load_trusted_task_context(
    conn: Any, *, task_id: str, run_id: int, workspace_path: str | Path, completion_gate: str,
    allow_blocked: bool = False,
) -> TrustedTaskContext:
    """Load exact durable identity and reject every mismatch before tool dispatch."""
    task = get_task(conn, task_id)
    if task is None:
        raise TrustedContextError("unknown task")
    run = get_run(conn, run_id)
    if run is None or run.task_id != task.id:
        raise TrustedContextError("run does not belong to task")
    if task.current_run_id != run.id:
        # A gated completion atomically closes the run and clears the task's
        # current_run_id.  Permit only that exact terminal pair so a retried
        # engineering_complete can return its already-recorded evidence.
        completed = (
            getattr(task, "status", None) == "done"
            and getattr(run, "status", None) in {"done", "completed"}
            and getattr(run, "outcome", None) == "completed"
            and getattr(run, "ended_at", None) is not None
        )
        blocked = (
            allow_blocked
            and getattr(task, "status", None) == "blocked"
            and getattr(run, "status", None) == "blocked"
            and getattr(run, "outcome", None) == "blocked"
            and getattr(run, "ended_at", None) is not None
        )
        if not (completed or blocked):
            raise TrustedContextError("run is not task current_run_id")
    canonical_workspace = _canonical_path(workspace_path, "workspace_path")
    if _canonical_path(task.workspace_path, "task.workspace_path") != canonical_workspace:
        raise TrustedContextError("workspace path mismatch")
    if task.completion_gate != completion_gate:
        raise TrustedContextError("completion gate mismatch")
    try:
        contract = load_contract(canonical_workspace)
    except Exception as exc:
        raise TrustedContextError("workspace contract could not be loaded") from exc
    if contract.completion_gate != task.completion_gate:
        raise TrustedContextError("contract completion gate mismatch")
    return TrustedTaskContext(
        task.id, run.id, canonical_workspace, completion_gate, task, run, contract,
        _load_immutable_spec(task),
    )


def load_worker_task_context(*, allow_blocked: bool = False) -> TrustedTaskContext:
    """Load identity exclusively from dispatcher environment and durable board state."""
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not task_id:
        raise TrustedContextError("HERMES_KANBAN_TASK is required")
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "")
    try:
        run_id = int(raw_run_id)
    except (TypeError, ValueError) as exc:
        raise TrustedContextError("HERMES_KANBAN_RUN_ID must be a positive integer") from exc
    if run_id <= 0:
        raise TrustedContextError("HERMES_KANBAN_RUN_ID must be a positive integer")
    conn = connect()
    try:
        task = get_task(conn, task_id)
        if task is None:
            raise TrustedContextError("unknown task")
        if task.workspace_path is None or task.completion_gate is None:
            raise TrustedContextError("task is missing trusted workspace or completion gate")
        return load_trusted_task_context(
            conn,
            task_id=task_id,
            run_id=run_id,
            workspace_path=task.workspace_path,
            completion_gate=task.completion_gate,
            allow_blocked=allow_blocked,
        )
    finally:
        conn.close()
