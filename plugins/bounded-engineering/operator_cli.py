"""Read-only, deterministic operator views for bounded engineering.

This module deliberately depends only on Kanban, filesystem, hashing, and native
process state.  It must never import conversation/session/provider code.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"[0-9a-f]{64}")
_PILOT_BOARD_DESCRIPTION = "bounded-engineering pilot board v1 (dedicated; do not reuse)"


def setup_operator_parsers(sub) -> None:
    status = sub.add_parser("status", help="Show persisted bounded task/run state")
    status.add_argument("--task", required=True, help="Kanban task id")
    status.add_argument("--board", required=True, help="Kanban board slug")
    status.add_argument("--json", action="store_true", dest="json_output", help="Emit typed JSON")
    proof = sub.add_parser("proof", help="Validate stored completion evidence")
    proof.add_argument("--task", required=True, help="Kanban task id")
    proof.add_argument("--board", required=True, help="Kanban board slug")
    proof.add_argument("--json", action="store_true", dest="json_output", help="Emit typed JSON")
    report = sub.add_parser("report", help="Write deterministic task report artifacts")
    report.add_argument("--task", required=True, help="Kanban task id")
    report.add_argument("--board", required=True, help="Kanban board slug")
    report.add_argument("--output-dir", required=True, help="Explicit report destination")
    report.add_argument("--json", action="store_true", dest="json_output", help="Emit typed JSON")
    prepare_push = sub.add_parser("prepare-task-push", help="Export accepted task artifacts to its publish checkout")
    prepare_push.add_argument("--task", required=True, help="Kanban task id")
    prepare_push.add_argument("--board", required=True, help="Kanban board slug")
    prepare_push.add_argument("--repository", required=True, help="Publish checkout")
    prepare_push.add_argument("--json", action="store_true", dest="json_output", help="Emit typed JSON")
    override = sub.add_parser("override-block", help="Audited recovery of a bounded blocked task")
    override.add_argument("--task", required=True)
    override.add_argument("--board", required=True)
    override.add_argument("--reason", required=True)
    override.add_argument("--operator", required=True)
    override.add_argument("--yes", action="store_true", help="Explicitly authorize mutation")
    override.add_argument("--json", action="store_true", dest="json_output")
    pilot = sub.add_parser("pilot", help="Dedicated bounded pilot operations")
    pilot_sub = pilot.add_subparsers(dest="pilot_action", required=True)
    prepare = pilot_sub.add_parser("prepare", help="Dry-run preparation by default")
    prepare.add_argument("--board", required=True)
    prepare.add_argument("--apply", action="store_true", help="Create only a new dedicated board")
    prepare.add_argument("--json", action="store_true", dest="json_output")
    pilot_report = pilot_sub.add_parser("report", help="Read-only dedicated-board aggregation")
    pilot_report.add_argument("--board", required=True)
    pilot_report.add_argument("--json", action="store_true", dest="json_output")


def _kanban():
    from hermes_cli import kanban_db
    return kanban_db


def _load(task_id: str, board: str):
    kb = _kanban()
    conn = kb.connect(board=board)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise ValueError(f"unknown task on board {board}: {task_id}")
        runs = tuple(kb.list_runs(conn, task_id))
    finally:
        conn.close()
    return task, runs


def _pid_liveness(pid: int | None) -> tuple[bool | None, str | None]:
    if pid is None:
        return None, "worker_pid_not_recorded"
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None, "worker_pid_invalid"
    try:
        from gateway.status import _pid_exists
        return bool(_pid_exists(pid)), None
    except (ImportError, OSError, RuntimeError):
        return None, "native_pid_liveness_unavailable"


def _run_payload(run) -> dict[str, Any]:
    alive, reason = _pid_liveness(run.worker_pid)
    return {
        "ended_at": run.ended_at,
        "error": run.error,
        "id": run.id,
        "last_heartbeat_at": run.last_heartbeat_at,
        "outcome": run.outcome,
        "pid_alive": alive,
        "pid_liveness_reason": reason,
        "profile": run.profile,
        "started_at": run.started_at,
        "status": run.status,
        "worker_pid": run.worker_pid,
    }


def build_status(task_id: str, board: str) -> dict[str, Any]:
    """Build status exclusively from native persisted task/run state."""
    task, runs = _load(task_id, board)
    return {
        "board": board,
        "current_run_id": task.current_run_id,
        "runs": [_run_payload(run) for run in runs],
        "schema_version": 1,
        "task": {
            "completed_at": task.completed_at,
            "completion_gate": task.completion_gate,
            "consecutive_failures": task.consecutive_failures,
            "created_at": task.created_at,
            "id": task.id,
            "project_id": task.project_id,
            "started_at": task.started_at,
            "status": task.status,
            "title": task.title,
        },
    }


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def validate_proof(task_id: str, board: str) -> dict[str, Any]:
    """Validate run metadata against immutable evidence bytes without writing."""
    task, runs = _load(task_id, board)
    run = runs[-1] if runs else None
    if (
        run is not None
        and run.outcome == "blocked"
        and run.ended_at is not None
    ):
        return {
            "board": board,
            "errors": [],
            "evidence_path": None,
            "evidence_sha256": None,
            "run_id": run.id,
            "schema_version": 2,
            "status": "not_applicable",
            "task_id": task.id,
            "valid": None,
        }
    metadata = run.metadata if run is not None and isinstance(run.metadata, dict) else {}
    digest = metadata.get("completion_evidence_sha256")
    metadata_path = metadata.get("completion_evidence_ref") or metadata.get("completion_evidence_path")
    errors: list[str] = []
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        errors.append("run_metadata_missing_completion_evidence_sha256")
        path = None
    else:
        canonical_path = _hermes_home() / "engineering" / "completion-evidence" / f"{digest}.json"
        path = Path(metadata_path).expanduser() if isinstance(metadata_path, str) and metadata_path else canonical_path
        if path != canonical_path:
            errors.append("run_metadata_evidence_path_not_canonical")
        if not path.is_file():
            errors.append("completion_evidence_file_missing")
        else:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != digest:
                errors.append("completion_evidence_digest_mismatch")
    return {
        "board": board,
        "errors": errors,
        "evidence_path": str(path) if path is not None else None,
        "evidence_sha256": digest if isinstance(digest, str) else None,
        "run_id": run.id if run is not None else None,
        "schema_version": 2,
        "status": "valid" if not errors else "invalid",
        "task_id": task.id,
        "valid": not errors,
    }


def _body_field(body: str | None, name: str) -> str | None:
    prefix = f"{name}: "
    for line in (body or "").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
    return None


def _record_matches(namespace: str, digest: str | None) -> bool:
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        return False
    path = _hermes_home() / "engineering" / namespace / f"{digest}.json"
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest


def _git_output(repository: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args], check=True, capture_output=True,
            text=True, timeout=30,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"Git repository check failed: {args!r}") from exc
    return result.stdout.strip()


def _safe_relative_file(root: Path, relative: str, *, must_exist: bool) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", "..", ".git"} for part in path.parts):
        raise ValueError(f"unsafe artifact path: {relative!r}")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"artifact path contains symlink: {relative}")
    if must_exist and not current.is_file():
        raise ValueError(f"artifact is not a regular file: {relative}")
    if not must_exist and current.exists() and not current.is_file():
        raise ValueError(f"artifact destination is not a regular file: {relative}")
    return current


def prepare_task_push(task_id: str, board: str, repository: str) -> dict[str, Any]:
    """Copy only snapshot-bound exact-file artifacts from one accepted task."""
    task, runs = _load(task_id, board)
    run = runs[-1] if runs else None
    if (
        task.status != "done"
        or task.completion_gate != "bounded-engineering/v1"
        or run is None
        or run.status not in {"done", "completed"}
        or run.outcome != "completed"
        or run.ended_at is None
    ):
        raise ValueError("task does not have an accepted completed run")
    proof = validate_proof(task_id, board)
    if proof.get("valid") is not True or proof.get("run_id") != run.id:
        raise ValueError("accepted run completion evidence is invalid")

    body = task.body if isinstance(task.body, str) else ""
    spec_digest = _body_field(body, "spec_sha256")
    baseline = _body_field(body, "baseline_head")
    recorded_repository = _body_field(body, "repository")
    if not _record_matches("specs", spec_digest):
        raise ValueError("immutable task spec digest does not match")
    spec_path = _hermes_home() / "engineering" / "specs" / f"{spec_digest}.json"
    spec = json.loads(spec_path.read_bytes())
    allowed = spec.get("allowed_paths") if isinstance(spec, dict) else None
    declared = set(allowed.get("files", ())) if isinstance(allowed, dict) else set()
    if not declared or not all(isinstance(path, str) for path in declared):
        raise ValueError("task has no exact-file artifacts declared")

    publish = Path(repository).expanduser().resolve()
    if recorded_repository is None or publish != Path(recorded_repository).expanduser().resolve():
        raise ValueError("publish checkout does not match task repository")
    if not publish.is_dir() or not (publish / ".git").exists():
        raise ValueError("publish checkout is not a Git repository")
    if _git_output(publish, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("publish checkout must be clean")
    if baseline is None or _git_output(publish, "rev-parse", "HEAD") != baseline:
        raise ValueError("publish checkout HEAD does not match task baseline")

    workspace = Path(task.workspace_path).expanduser().resolve()
    worktree_root = (_hermes_home() / "engineering" / "worktrees").resolve()
    if not workspace.is_relative_to(worktree_root) or workspace.name != spec_digest:
        raise ValueError("task worktree is not the immutable spec-bound workspace")
    if not workspace.is_dir() or not (workspace / ".git").exists():
        raise ValueError("task worktree is unavailable")
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .snapshot import capture_snapshot
    else:
        from snapshot import capture_snapshot
    snapshot = capture_snapshot(workspace)
    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    workspace_metadata = metadata.get("workspace") if isinstance(metadata.get("workspace"), dict) else {}
    if snapshot.digest != workspace_metadata.get("final_snapshot_id") or snapshot.head != baseline:
        raise ValueError("task worktree no longer matches the accepted snapshot")
    entries = tuple(entry for entry in snapshot.entries if entry.path in declared)
    if not entries or any(entry.status == "deleted" for entry in entries):
        raise ValueError("accepted snapshot contains no publishable declared artifacts")

    recorded_manifest = workspace_metadata.get("artifacts")
    if recorded_manifest is not None:
        expected = sorted(
            (item.get("path"), item.get("sha256"), item.get("mode"))
            for item in recorded_manifest if isinstance(item, dict)
        ) if isinstance(recorded_manifest, list) else []
        actual = sorted((entry.path, entry.sha256, entry.mode) for entry in entries)
        if expected != actual:
            raise ValueError("artifact manifest does not match the accepted snapshot")

    prepared: list[tuple[Path, Path, str]] = []
    try:
        for entry in entries:
            source = _safe_relative_file(workspace, entry.path, must_exist=True)
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != entry.sha256:
                raise ValueError(f"artifact digest mismatch: {entry.path}")
            destination = _safe_relative_file(publish, entry.path, must_exist=False)
            if not destination.parent.is_dir():
                raise ValueError(f"artifact parent directory is unavailable: {entry.path}")
            descriptor, temporary_name = tempfile.mkstemp(prefix=".hermes-artifact-", dir=destination.parent)
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, entry.mode)
            prepared.append((temporary, destination, digest))
        for temporary, destination, _digest in prepared:
            os.replace(temporary, destination)
        for _temporary, destination, digest in prepared:
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError(f"published artifact digest mismatch: {destination.relative_to(publish)}")
    finally:
        for temporary, _destination, _digest in prepared:
            temporary.unlink(missing_ok=True)

    return {
        "schema_version": 1,
        "task_id": task.id,
        "run_id": run.id,
        "repository": str(publish),
        "baseline_head": baseline,
        "snapshot_digest": snapshot.digest,
        "artifacts": [
            {"path": entry.path, "sha256": entry.sha256, "mode": entry.mode}
            for entry in entries
        ],
        "prepared": True,
        "next_action": "Review the repository diff, then call hermes_prepare_push.",
    }


def override_block(task_id: str, board: str, reason: str, operator: str, *, confirmed: bool) -> dict[str, Any]:
    """Fail closed unless this is an explicit bounded ``blocked -> ready/todo`` recovery."""
    reason, operator = reason.strip(), operator.strip()
    if not reason or not operator:
        raise ValueError("--reason and --operator must be non-empty")
    if not confirmed:
        raise ValueError("override requires explicit --yes")
    kb = _kanban()
    conn = kb.connect(board=board)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise ValueError(f"unknown task on board {board}: {task_id}")
        if task.status != "blocked":
            raise ValueError(f"invalid recovery transition: {task.status} -> ready/todo")
        if task.completion_gate != "bounded-engineering/v1":
            raise ValueError("task is not governed by bounded-engineering/v1")
        if task.current_run_id is not None:
            raise ValueError("task has a current run; use native reclaim first")
        spec_digest = _body_field(task.body, "spec_sha256")
        contract_digest = _body_field(task.body, "contract_sha256")
        if not _record_matches("specs", spec_digest):
            raise ValueError("current immutable spec digest does not match")
        repository = _body_field(task.body, "repository")
        if not repository:
            raise ValueError("task repository is missing")
        try:
            if __package__ and __import__("sys").modules.get(__package__) is not None:
                from .contract import load_contract
            else:  # Direct-module tests and scripts.
                from contract import load_contract
            current_contract = load_contract(Path(repository))
        except (OSError, ValueError) as exc:
            raise ValueError(f"current contract cannot be validated: {exc}") from exc
        if current_contract.sha256 != contract_digest:
            raise ValueError("current contract digest does not match")
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .create_cli import _WRITER_STATUSES, creation_lock
        else:
            from create_cli import _WRITER_STATUSES, creation_lock
        project = task.project_id or _body_field(task.body, "project_id") or current_contract.project_id
        with creation_lock(_hermes_home(), board, project):
            task = kb.get_task(conn, task_id)
            if task is None or task.status != "blocked" or task.current_run_id is not None:
                raise ValueError("blocked task changed while recovery was waiting")
            active = next((
                other for other in kb.list_tasks(conn, tenant="bounded-engineering/v1", include_archived=True)
                if other.id != task_id and other.status in _WRITER_STATUSES and (
                    other.project_id == project
                    or _body_field(getattr(other, "body", None), "repository") == repository
                )
            ), None)
            if active is not None:
                raise ValueError(f"active_writer_exists: {active.id}")
            audit = json.dumps({"action": "bounded_engineering_override_block", "operator": operator, "reason": reason}, sort_keys=True)
            kb.add_comment(conn, task_id, operator, audit)
            if not kb.unblock_task(conn, task_id):
                raise ValueError("native unblock rejected the transition")
            updated = kb.get_task(conn, task_id)
            if updated is None:
                raise ValueError("task disappeared after native unblock")
        return {"board": board, "from_status": "blocked", "operator": operator, "reason": reason,
                "schema_version": 1, "task_id": task_id, "to_status": updated.status}
    finally:
        conn.close()


def pilot_prepare(board: str, *, apply: bool) -> dict[str, Any]:
    """Dry-run by default; apply creates only a previously absent dedicated board."""
    kb = _kanban()
    if not apply:
        if kb.board_exists(board):
            if kb.read_board_metadata(board).get("description") != _PILOT_BOARD_DESCRIPTION:
                raise ValueError("refusing to reuse or mutate a non-pilot/historical board")
            state = "already_prepared"
        else:
            state = "would_create"
    else:
        try:
            kb.create_board_exclusive(
                board, name=f"Bounded Engineering Pilot ({board})",
                description=_PILOT_BOARD_DESCRIPTION,
            )
            state = "created"
        except kb.BoardAlreadyExistsError:
            if kb.read_board_metadata(board).get("description") != _PILOT_BOARD_DESCRIPTION:
                raise ValueError("refusing to reuse or mutate a non-pilot/historical board")
            state = "already_prepared"
    return {"applied": bool(apply), "board": board, "dedicated": True, "profile_mutated": False,
            "schema_version": 1, "state": state}


def pilot_report(board: str) -> dict[str, Any]:
    """Read-only aggregation from a marked dedicated board and its tasks."""
    kb = _kanban()
    if not kb.board_exists(board) or kb.read_board_metadata(board).get("description") != _PILOT_BOARD_DESCRIPTION:
        raise ValueError("board is not a dedicated bounded-engineering pilot board")
    conn = kb.connect(board=board)
    try:
        tasks = tuple(kb.list_tasks(conn, include_archived=False))
    finally:
        conn.close()
    reports = [build_report(task.id, board) for task in sorted(tasks, key=lambda item: item.id)]
    invocations = sum(item["lifecycle"]["top_level_invocations"] for item in reports)
    missing = [{"metric": metric, "reason": reason, "task_id": item["task"]["id"]}
               for item in reports for metric, reason in sorted(item["missing_metrics_reasons"].items())
               if reason is not None]
    if not reports:
        missing.append({"metric": "pilot.tasks", "reason": "dedicated_pilot_board_has_no_tasks", "task_id": None})
    verdict = "reject" if invocations > 3 else (
        "insufficient_measurement" if missing else (
            "reject" if any(item["verdict"] != "accepted" for item in reports) else "proceed_to_real_phase"
        )
    )
    return {"board": board, "missing_metrics": missing, "schema_version": 2, "task_count": len(reports),
            "tasks": reports, "top_level_invocations": invocations, "verdict": verdict}


def _aggregate_run_metadata(runs) -> dict[str, Any]:
    aggregate: dict[str, Any] = {"model": {}, "docatlas": {}}
    additive_model = {
        "sessions", "api_requests", "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "reasoning_tokens",
    }
    additive_top = {
        "context_bytes", "tool_calls", "tool_calls_attempted", "tool_calls_succeeded",
        "tool_calls_failed", "tool_calls_incomplete",
    }
    additive_docatlas = {"call_count", "request_bytes", "response_bytes", "estimated_visible_tokens"}
    errors: list[str] = []
    missing: dict[str, Any] = {}
    for run in runs:
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        model = metadata.get("model") if isinstance(metadata.get("model"), dict) else {}
        for name in additive_model:
            value = model.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                aggregate["model"][name] = aggregate["model"].get(name, 0) + value
        for name in ("provider", "model", "reasoning", "provider_tool_count", "provider_tools",
                     "tool_catalog_bytes", "mcp_tool_catalog_bytes"):
            if model.get(name) is not None:
                aggregate["model"][name] = model[name]
        for name in additive_top:
            value = metadata.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                aggregate[name] = aggregate.get(name, 0) + value
        tool_counts = metadata.get("tool_call_counts") \
            if isinstance(metadata.get("tool_call_counts"), dict) else {}
        aggregate_counts = aggregate.setdefault("tool_call_counts", {})
        for name, value in tool_counts.items():
            if isinstance(name, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                aggregate_counts[name] = aggregate_counts.get(name, 0) + value
        docatlas = metadata.get("docatlas") if isinstance(metadata.get("docatlas"), dict) else {}
        for name in additive_docatlas:
            value = docatlas.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                aggregate["docatlas"][name] = aggregate["docatlas"].get(name, 0) + value
        for name in (
            "packet_hash", "source_ids", "response_status", "docs_status", "latency_ms",
            "audit_artifact_path", "audit_artifact_sha256",
        ):
            if docatlas.get(name) is not None:
                aggregate["docatlas"][name] = docatlas[name]
        if isinstance(metadata.get("tool_errors"), list):
            errors.extend(item for item in metadata["tool_errors"] if isinstance(item, str))
        if isinstance(metadata.get("missing_metrics_reasons"), dict):
            missing.update(metadata["missing_metrics_reasons"])
        for name in ("first_edit_at_ms", "session_started_at_ms"):
            value = metadata.get(name)
            if isinstance(value, int) and (name not in aggregate or value < aggregate[name]):
                aggregate[name] = value
    aggregate["tool_errors"] = errors[-10:]
    aggregate["missing_metrics_reasons"] = missing
    return aggregate


def build_report(task_id: str, board: str) -> dict[str, Any]:
    task, runs = _load(task_id, board)
    proof = validate_proof(task_id, board)
    completed = [run for run in runs if run.ended_at is not None]
    lifecycle_elapsed = None
    lifecycle_reason = None
    if task.created_at is not None and task.completed_at is not None:
        lifecycle_elapsed = max(0, task.completed_at - task.created_at) * 1000
    elif (
        runs
        and runs[-1].outcome == "blocked"
        and runs[-1].started_at is not None
        and runs[-1].ended_at is not None
    ):
        lifecycle_elapsed = max(0, runs[-1].ended_at - runs[-1].started_at) * 1000
    else:
        lifecycle_reason = "task_not_completed_or_timestamps_missing"
    latest_metadata = runs[-1].metadata if runs and isinstance(runs[-1].metadata, dict) else {}
    aggregate_metadata = _aggregate_run_metadata(runs)
    stored_model = aggregate_metadata.get("model")
    stored_verification = latest_metadata.get("verification") \
        if isinstance(latest_metadata.get("verification"), dict) else {}
    stored_workspace = latest_metadata.get("workspace") \
        if isinstance(latest_metadata.get("workspace"), dict) else {}
    spec_digest = _body_field(task.body, "spec_sha256")
    spec = {}
    if _record_matches("specs", spec_digest):
        spec = json.loads((_hermes_home() / "engineering" / "specs" / f"{spec_digest}.json").read_bytes())
    benchmark = spec.get("benchmark") if isinstance(spec, dict) and isinstance(spec.get("benchmark"), dict) else None
    docatlas = aggregate_metadata.get("docatlas")
    lane = benchmark.get("lane") if benchmark else None
    missing_reasons = dict(aggregate_metadata.get("missing_metrics_reasons", {}))
    if lifecycle_reason is None:
        missing_reasons.pop("lifecycle.elapsed_ms", None)
    else:
        missing_reasons["lifecycle.elapsed_ms"] = lifecycle_reason
    missing_reasons["lifecycle.manual_interventions"] = "manual_intervention_telemetry_unavailable"
    for field in ("provider", "model", "reasoning", "sessions", "api_requests", "input_tokens",
                  "output_tokens", "cache_read_tokens", "cache_write_tokens",
                  "provider_tool_count", "provider_tools"):
        if stored_model.get(field) is None:
            missing_reasons.setdefault(f"model.{field}", "run_metric_not_recorded")
    for field in ("context_bytes", "tool_calls_attempted", "tool_calls_succeeded",
                  "tool_calls_failed", "tool_calls_incomplete"):
        if aggregate_metadata.get(field) is None:
            missing_reasons.setdefault(f"model.{field}", "run_metric_not_recorded")
    for field in ("required", "passed", "failed", "elapsed_ms"):
        if stored_verification.get(field) is None:
            missing_reasons.setdefault(f"verification.{field}", "run_metric_not_recorded")
    for field in ("final_snapshot_id", "changed_files", "changed_bytes"):
        if stored_workspace.get(field) is None:
            missing_reasons.setdefault(f"workspace.{field}", "run_metric_not_recorded")
    benchmark_violations: list[str] = []
    if benchmark is not None:
        expected_calls = benchmark.get("max_docatlas_calls")
        actual_calls = docatlas.get("call_count", 0)
        visible_tokens = docatlas.get("estimated_visible_tokens", 0)
        if actual_calls != expected_calls:
            benchmark_violations.append("docatlas_call_count_mismatch")
        if not isinstance(visible_tokens, int) or visible_tokens > benchmark.get("max_visible_docatlas_tokens", -1):
            benchmark_violations.append("docatlas_visible_token_limit_exceeded")
        catalog_bytes = stored_model.get("mcp_tool_catalog_bytes")
        if lane == "repo_only" and catalog_bytes not in {0, None}:
            benchmark_violations.append("docatlas_tool_exposed_in_repo_only")
        if lane == "docatlas_once" and (not isinstance(catalog_bytes, int) or catalog_bytes <= 0):
            benchmark_violations.append("docatlas_tool_missing_from_docatlas_once")
        if lane == "docatlas_once" and docatlas.get("response_status") != "ok":
            benchmark_violations.append("docatlas_response_not_ok")
        if lane == "docatlas_once":
            audit_digest = docatlas.get("audit_artifact_sha256")
            audit_path = docatlas.get("audit_artifact_path")
            canonical_audit_path = (
                _hermes_home() / "engineering" / "docatlas-audit" / f"{audit_digest}.json"
                if isinstance(audit_digest, str) and _SHA256.fullmatch(audit_digest) else None
            )
            if (
                canonical_audit_path is None
                or audit_path != str(canonical_audit_path)
                or canonical_audit_path.is_symlink()
                or not canonical_audit_path.is_file()
                or hashlib.sha256(canonical_audit_path.read_bytes()).hexdigest() != audit_digest
            ):
                benchmark_violations.append("docatlas_audit_artifact_invalid")
        if benchmark.get("baseline_head") != _body_field(task.body, "baseline_head"):
            benchmark_violations.append("benchmark_baseline_mismatch")
        index = Path("/srv/hermes-lab/operator/docatlas-baseline-index/docmancer.db")
        if index.is_symlink() or not index.is_file() or \
                hashlib.sha256(index.read_bytes()).hexdigest() != benchmark.get("docatlas_index_revision"):
            benchmark_violations.append("docatlas_index_revision_mismatch")
        attempted = aggregate_metadata.get("tool_calls_attempted")
        resolved = sum(aggregate_metadata.get(name, 0) for name in (
            "tool_calls_succeeded", "tool_calls_failed", "tool_calls_incomplete",
        ))
        if not isinstance(attempted, int) or attempted != resolved:
            benchmark_violations.append("tool_call_counters_inconsistent")
        required_missing = {
            name: reason for name, reason in missing_reasons.items()
            if reason is not None and name != "lifecycle.manual_interventions"
        }
        if required_missing:
            benchmark_violations.append("required_benchmark_metrics_missing")
    accepted = task.status == "done" and proof["status"] == "valid" and not benchmark_violations
    return {
        "schema_version": 2,
        "task": {
            "id": task.id, "board": board, "project_id": task.project_id,
            "spec_sha256": _body_field(task.body, "spec_sha256"),
            "contract_sha256": _body_field(task.body, "contract_sha256"),
            "risk": _body_field(task.body, "risk"),
            "lane": lane,
        },
        "lifecycle": {
            "created_at": task.created_at, "started_at": task.started_at,
            "completed_at": task.completed_at, "elapsed_ms": lifecycle_elapsed,
            "top_level_invocations": len(runs),
            "technical_retries": max(0, len(runs) - 1),
            "manual_interventions": None,
        },
        "model": {
            "profile": runs[-1].profile if runs else task.assignee,
            "provider": stored_model.get("provider"), "model": stored_model.get("model"),
            "reasoning": stored_model.get("reasoning"), "sessions": stored_model.get("sessions"),
            "api_requests": stored_model.get("api_requests"),
            "provider_requests": stored_model.get("api_requests"),
            "input_tokens": stored_model.get("input_tokens"),
            "uncached_input_tokens": (
                stored_model.get("input_tokens")
                if isinstance(stored_model.get("input_tokens"), int) else None
            ),
            "output_tokens": stored_model.get("output_tokens"),
            "cache_read_tokens": stored_model.get("cache_read_tokens"),
            "cache_write_tokens": stored_model.get("cache_write_tokens"),
            "reasoning_tokens": stored_model.get("reasoning_tokens"),
            "total_provider_tokens": (
                sum(stored_model.get(name, 0) for name in (
                    "input_tokens", "cache_read_tokens", "output_tokens",
                ))
                if all(isinstance(stored_model.get(name), int) for name in (
                    "input_tokens", "cache_read_tokens", "output_tokens",
                )) else None
            ),
            "provider_tool_count": stored_model.get("provider_tool_count"),
            "provider_tools": stored_model.get("provider_tools"),
            "tool_catalog_bytes": stored_model.get("tool_catalog_bytes", 0 if lane == "repo_only" else None),
            "mcp_tool_catalog_bytes": stored_model.get("mcp_tool_catalog_bytes", 0 if lane == "repo_only" else None),
            "context_bytes": aggregate_metadata.get("context_bytes"),
            "tool_calls": aggregate_metadata.get("tool_calls"),
            "tool_calls_attempted": aggregate_metadata.get("tool_calls_attempted"),
            "tool_calls_succeeded": aggregate_metadata.get("tool_calls_succeeded"),
            "tool_calls_failed": aggregate_metadata.get("tool_calls_failed"),
            "tool_calls_incomplete": aggregate_metadata.get("tool_calls_incomplete"),
            "tool_call_counts": aggregate_metadata.get("tool_call_counts"),
        },
        "tool_errors": aggregate_metadata.get("tool_errors", []),
        "benchmark": benchmark,
        "benchmark_violations": benchmark_violations,
        "docatlas": {name: docatlas.get(name) for name in (
            "call_count", "request_bytes", "response_bytes", "estimated_visible_tokens",
            "packet_hash", "source_ids", "response_status", "docs_status", "latency_ms",
            "audit_artifact_path", "audit_artifact_sha256",
        )},
        "timing": {
            "time_to_first_edit_ms": (
                aggregate_metadata.get("first_edit_at_ms") - aggregate_metadata.get("session_started_at_ms")
                if isinstance(aggregate_metadata.get("first_edit_at_ms"), int)
                and isinstance(aggregate_metadata.get("session_started_at_ms"), int) else None
            ),
            "total_elapsed_ms": lifecycle_elapsed,
        },
        "verification": {name: stored_verification.get(name) for name in
                         ("required", "passed", "failed", "elapsed_ms")},
        "workspace": {
            "baseline_head": _body_field(task.body, "baseline_head"),
            "final_snapshot_id": stored_workspace.get("final_snapshot_id"),
            "changed_files": stored_workspace.get("changed_files"),
            "changed_bytes": stored_workspace.get("changed_bytes"),
            "scope_violations": None, "original_checkout_unchanged": None,
        },
        "proof": proof,
        "missing_metrics_reasons": missing_reasons,
        "verdict": "accepted" if accepted else (
            "insufficient_measurement" if task.status == "done" else task.status
        ),
        "closed_run_count": len(completed),
    }


def _markdown(report: dict[str, Any]) -> str:
    task = report["task"]
    life = report["lifecycle"]
    proof = report["proof"]
    return "\n".join((
        "# Bounded Engineering Report", "", f"- Task: `{task['id']}`",
        f"- Board: `{task['board']}`", f"- Verdict: `{report['verdict']}`",
        f"- Runs: {life['top_level_invocations']}",
        f"- Completion evidence: `{proof['status']}`", "",
        "Missing metrics remain `null`; reasons are recorded in `report.json`.", "",
    ))


def write_report(task_id: str, board: str, output_dir: str) -> dict[str, Any]:
    """Write exactly report.json and report.md under the explicit destination."""
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ValueError("--output-dir is required")
    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise ValueError("--output-dir must be a directory")
    report = build_report(task_id, board)
    json_path = destination / "report.json"
    md_path = destination / "report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    return {"report_json": str(json_path), "report_markdown": str(md_path), "schema_version": 2}


def run_operator(args) -> tuple[int, dict[str, Any]]:
    try:
        if args.engineering_action == "status":
            return 0, build_status(args.task, args.board)
        if args.engineering_action == "proof":
            payload = validate_proof(args.task, args.board)
            return (0 if payload["status"] in {"valid", "not_applicable"} else 1), payload
        if args.engineering_action == "report":
            return 0, write_report(args.task, args.board, args.output_dir)
        if args.engineering_action == "prepare-task-push":
            return 0, prepare_task_push(args.task, args.board, args.repository)
        if args.engineering_action == "override-block":
            return 0, override_block(args.task, args.board, args.reason, args.operator, confirmed=args.yes)
        if args.engineering_action == "pilot" and args.pilot_action == "prepare":
            return 0, pilot_prepare(args.board, apply=args.apply)
        if args.engineering_action == "pilot" and args.pilot_action == "report":
            return 0, pilot_report(args.board)
    except (OSError, ValueError) as exc:
        return 1, {"error": "operator_command_failed", "detail": str(exc), "schema_version": 1}
    raise ValueError(f"unsupported operator action: {args.engineering_action}")
