"""Focused no-provider contracts for deterministic operator commands."""
from __future__ import annotations

import argparse
import builtins
import hashlib
import importlib
import json
import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_retry_telemetry_is_aggregated() -> None:
    first = _run({
        "model": {"api_requests": 1, "input_tokens": 10},
        "tool_calls_attempted": 2,
        "docatlas": {
            "call_count": 1, "response_bytes": 20,
            "audit_artifact_path": "/audit/abc.json", "audit_artifact_sha256": "a" * 64,
        },
    })
    second = _run({
        "model": {"api_requests": 2, "input_tokens": 30},
        "tool_calls_attempted": 3,
        "docatlas": {"call_count": 0, "response_bytes": 0},
    })

    result = operator_cli._aggregate_run_metadata((first, second))

    assert result["model"]["api_requests"] == 3
    assert result["model"]["input_tokens"] == 40
    assert result["tool_calls_attempted"] == 5
    assert result["docatlas"]["call_count"] == 1
    assert result["docatlas"]["audit_artifact_path"] == "/audit/abc.json"
    assert result["docatlas"]["audit_artifact_sha256"] == "a" * 64

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))
operator_cli = importlib.import_module("operator_cli")
from engineering_cli import run, setup_parser  # noqa: E402


def _task(status: str = "done"):
    return SimpleNamespace(
        id="task-1", title="bounded", body=(
            "[bounded-engineering/v1]\n"
            f"spec_sha256: {'1' * 64}\ncontract_sha256: {'2' * 64}\n"
            "baseline_head: abcdef\nrisk: local_behavior\n"
        ), assignee="bounded-engineer", status=status, project_id="project-1",
        completion_gate="bounded-engineering/v1", consecutive_failures=0,
        current_run_id=7 if status == "running" else None, created_at=10,
        started_at=11, completed_at=20 if status == "done" else None,
    )


def _run(metadata=None, *, status="done", pid=None):
    return SimpleNamespace(
        id=7, profile="bounded-engineer", status=status, worker_pid=pid,
        last_heartbeat_at=12, started_at=11,
        ended_at=24 if status == "blocked" else (20 if status == "done" else None),
        outcome="blocked" if status == "blocked" else ("completed" if status == "done" else None), error=None,
        metadata=metadata,
    )


def _forbid_provider_and_session_imports(monkeypatch):
    original = builtins.__import__
    called = []

    def guarded(name, *args, **kwargs):
        if any(word in name.lower() for word in ("provider", "session", "conversation")):
            called.append(name)
            raise AssertionError(f"operator imported forbidden runtime module: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    return called


def test_prepare_task_push_exports_only_snapshot_bound_exact_file(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    publish = tmp_path / "publish"
    subprocess.run(["git", "init", "-q", str(publish)], check=True)
    subprocess.run(["git", "-C", str(publish), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(publish), "config", "user.name", "Test"], check=True)
    artifact = Path("eval/kotlin_smoke/result.json")
    (publish / artifact.parent).mkdir(parents=True)
    (publish / artifact.parent / ".keep").write_text("keep\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(publish), "add", "."], check=True)
    subprocess.run(["git", "-C", str(publish), "commit", "-qm", "base"], check=True)
    baseline = subprocess.run(
        ["git", "-C", str(publish), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    spec_payload = json.dumps({
        "allowed_paths": {"files": [str(artifact)], "roots": []},
    }, sort_keys=True, separators=(",", ":")).encode()
    spec_digest = hashlib.sha256(spec_payload).hexdigest()
    spec_dir = home / "engineering/specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / f"{spec_digest}.json").write_bytes(spec_payload)
    workspace = home / "engineering/worktrees/project" / spec_digest
    workspace.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(publish), "worktree", "add", "-q", str(workspace), baseline], check=True)
    (workspace / artifact).write_bytes(b'{"accepted":true}\n')

    import snapshot
    accepted = snapshot.capture_snapshot(workspace)
    task = SimpleNamespace(
        id="task-1", status="done", completion_gate="bounded-engineering/v1",
        workspace_path=str(workspace), body=(
            f"spec_sha256: {spec_digest}\n"
            f"baseline_head: {baseline}\n"
            f"repository: {publish}\n"
        ),
    )
    completed = _run({
        "workspace": {
            "final_snapshot_id": accepted.digest,
            "artifacts": [{
                "path": accepted.entries[0].path,
                "sha256": accepted.entries[0].sha256,
                "mode": accepted.entries[0].mode,
            }],
        },
    })
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(operator_cli, "_load", lambda *_: (task, (completed,)))
    monkeypatch.setattr(operator_cli, "validate_proof", lambda *_: {"valid": True, "run_id": 7})

    result = operator_cli.prepare_task_push("task-1", "board", str(publish))

    assert result["prepared"] is True
    assert result["artifacts"] == [{
        "path": str(artifact), "sha256": accepted.entries[0].sha256,
        "mode": accepted.entries[0].mode,
    }]
    assert (publish / artifact).read_bytes() == b'{"accepted":true}\n'


def test_safe_relative_file_rejects_parent_symlink(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="contains symlink"):
        operator_cli._safe_relative_file(tmp_path, "linked/artifact.json", must_exist=False)


def test_status_uses_task_runs_and_native_pid_without_provider(monkeypatch):
    called = _forbid_provider_and_session_imports(monkeypatch)
    monkeypatch.setattr(operator_cli, "_load", lambda task_id, board: (_task("running"), (_run(status="running", pid=123),)))
    monkeypatch.setattr(operator_cli, "_pid_liveness", lambda pid: (True, None))

    payload = operator_cli.build_status("task-1", "engineering")

    assert payload["task"]["status"] == "running"
    assert payload["runs"] == [{
        "ended_at": None, "error": None, "id": 7, "last_heartbeat_at": 12,
        "outcome": None, "pid_alive": True, "pid_liveness_reason": None,
        "profile": "bounded-engineer", "started_at": 11, "status": "running",
        "worker_pid": 123,
    }]
    assert called == []


def test_proof_validates_digest_and_canonical_path_without_mutation(tmp_path, monkeypatch):
    called = _forbid_provider_and_session_imports(monkeypatch)
    payload = b'{"immutable":true}'
    digest = hashlib.sha256(payload).hexdigest()
    path = tmp_path / "engineering" / "completion-evidence" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    before = (path.stat().st_mtime_ns, path.read_bytes())
    run = _run({"completion_evidence_sha256": digest, "completion_evidence_ref": str(path)})
    monkeypatch.setattr(operator_cli, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(operator_cli, "_load", lambda task_id, board: (_task(), (run,)))

    proof = operator_cli.validate_proof("task-1", "engineering")

    assert proof["valid"] is True
    assert proof["status"] == "valid"
    assert proof["evidence_path"] == str(path)
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before
    assert called == []


def test_proof_fails_on_digest_mismatch(tmp_path, monkeypatch):
    digest = "a" * 64
    path = tmp_path / "engineering" / "completion-evidence" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"tampered")
    monkeypatch.setattr(operator_cli, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(operator_cli, "_load", lambda task_id, board: (
        _task(), (_run({"completion_evidence_sha256": digest}),),
    ))

    proof = operator_cli.validate_proof("task-1", "engineering")

    assert proof["valid"] is False
    assert proof["status"] == "invalid"
    assert proof["errors"] == ["completion_evidence_digest_mismatch"]


def test_blocked_run_proof_is_not_applicable(monkeypatch):
    monkeypatch.setattr(operator_cli, "_load", lambda task_id, board: (
        _task("blocked"), (_run(status="blocked"),),
    ))

    proof = operator_cli.validate_proof("task-1", "engineering")

    assert proof["status"] == "not_applicable"
    assert proof["valid"] is None
    assert proof["errors"] == []
    assert proof["schema_version"] == 2


def test_blocked_report_uses_closed_run_elapsed_time(monkeypatch):
    monkeypatch.setattr(operator_cli, "_load", lambda task_id, board: (
        _task("blocked"), (_run(status="blocked"),),
    ))

    report = operator_cli.build_report("task-1", "engineering")

    assert report["lifecycle"]["elapsed_ms"] == 13_000
    assert report["lifecycle"]["completed_at"] is None
    assert "lifecycle.elapsed_ms" not in report["missing_metrics_reasons"]
    assert report["proof"]["status"] == "not_applicable"
    assert report["verdict"] == "blocked"
    assert report["schema_version"] == 2


def test_report_writes_only_explicit_artifacts_and_never_estimates(tmp_path, monkeypatch):
    called = _forbid_provider_and_session_imports(monkeypatch)
    report = operator_cli.build_report
    monkeypatch.setattr(operator_cli, "_load", lambda task_id, board: (_task(), (_run({}),)))
    monkeypatch.setattr(operator_cli, "validate_proof", lambda task_id, board: {
        "valid": False, "status": "invalid", "errors": ["missing"], "evidence_path": None,
        "evidence_sha256": None, "run_id": 7, "task_id": task_id,
        "board": board, "schema_version": 2,
    })
    output = tmp_path / "explicit"

    paths = operator_cli.write_report("task-1", "engineering", str(output))
    data = json.loads((output / "report.json").read_text())

    assert sorted(item.name for item in output.iterdir()) == ["report.json", "report.md"]
    assert paths == {"report_json": str(output / "report.json"), "report_markdown": str(output / "report.md"), "schema_version": 2}
    assert data["model"]["input_tokens"] is None
    assert data["model"]["api_requests"] is None
    assert data["verification"]["elapsed_ms"] is None
    assert data["workspace"]["changed_files"] is None
    assert any(key.startswith("model.") for key in data["missing_metrics_reasons"])
    assert called == []


def test_report_surfaces_persisted_exact_run_telemetry(monkeypatch):
    metadata = {
        "model": {"provider": "openai-codex", "model": "gpt-5.6-sol", "sessions": 1,
                  "api_requests": 3, "input_tokens": 1200, "output_tokens": 240,
                  "cache_read_tokens": 700, "cache_write_tokens": 0,
                  "provider_tool_count": 2, "provider_tools": ["terminal", "read_file"]},
        "context_bytes": 1495, "tool_calls": 12, "tool_calls_attempted": 12,
        "tool_calls_succeeded": 10, "tool_calls_failed": 2, "tool_calls_incomplete": 0,
        "tool_errors": ["Tool execution failed: RuntimeError: unavailable"],
        "verification": {"required": 1, "passed": 1, "failed": 0},
        "workspace": {"final_snapshot_id": "a" * 64, "changed_files": 2,
                      "changed_bytes": 900},
    }
    monkeypatch.setattr(operator_cli, "_load", lambda task_id, board: (
        _task(), (_run(metadata),),
    ))
    monkeypatch.setattr(operator_cli, "validate_proof", lambda task_id, board: {
        "valid": True, "status": "valid", "errors": [], "evidence_path": "evidence.json",
        "evidence_sha256": "b" * 64, "run_id": 7, "task_id": task_id,
        "board": board, "schema_version": 2,
    })

    report = operator_cli.build_report("task-1", "engineering")

    assert report["model"]["api_requests"] == 3
    assert report["model"]["input_tokens"] == 1200
    assert report["model"]["context_bytes"] == 1495
    assert report["model"]["tool_calls"] == 12
    assert report["model"]["tool_calls_attempted"] == 12
    assert report["model"]["tool_calls_succeeded"] == 10
    assert report["model"]["tool_calls_failed"] == 2
    assert report["model"]["tool_calls_incomplete"] == 0
    assert report["model"]["provider_tools"] == ["terminal", "read_file"]
    assert report["tool_errors"] == ["Tool execution failed: RuntimeError: unavailable"]
    assert report["verification"]["passed"] == 1
    assert report["workspace"]["changed_files"] == 2
    assert "model.input_tokens" not in report["missing_metrics_reasons"]


def test_cli_requires_operator_scope_and_explicit_report_output(tmp_path, monkeypatch, capsys):
    parser = argparse.ArgumentParser()
    setup_parser(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["status", "--board", "engineering"])
    with pytest.raises(SystemExit):
        parser.parse_args(["report", "--task", "task-1", "--board", "engineering"])

    monkeypatch.setattr(operator_cli, "build_status", lambda task, board: {"schema_version": 1, "task_id": task, "board": board})
    args = parser.parse_args(["status", "--task", "task-1", "--board", "engineering", "--json"])
    assert run(args) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "task-1"


class _PilotKB:
    class BoardAlreadyExistsError(FileExistsError):
        pass

    def __init__(self):
        self.boards = {}
        self.create_calls = 0

    def board_exists(self, board):
        return board in self.boards

    def read_board_metadata(self, board):
        return self.boards[board]

    def create_board(self, board, **metadata):
        self.create_calls += 1
        self.boards[board] = metadata

    def create_board_exclusive(self, board, **metadata):
        if board in self.boards:
            raise self.BoardAlreadyExistsError(board)
        return self.create_board(board, **metadata)

    def write_board_metadata(self, board, **metadata):
        self.boards[board].update(metadata)


def test_pilot_prepare_is_dry_run_nonmutating_and_apply_is_idempotent(monkeypatch):
    called = _forbid_provider_and_session_imports(monkeypatch)
    kb = _PilotKB()
    monkeypatch.setattr(operator_cli, "_kanban", lambda: kb)
    dry = operator_cli.pilot_prepare("new-pilot", apply=False)
    assert dry["state"] == "would_create"
    assert kb.boards == {}
    first = operator_cli.pilot_prepare("new-pilot", apply=True)
    second = operator_cli.pilot_prepare("new-pilot", apply=True)
    assert first["state"] == "created"
    assert second["state"] == "already_prepared"
    assert kb.create_calls == 1
    assert first["profile_mutated"] is False
    assert called == []


def test_pilot_prepare_refuses_historical_board(monkeypatch):
    kb = _PilotKB()
    kb.boards["history"] = {"description": "existing user board"}
    monkeypatch.setattr(operator_cli, "_kanban", lambda: kb)
    with pytest.raises(ValueError, match="historical"):
        operator_cli.pilot_prepare("history", apply=True)
    assert kb.create_calls == 0


def test_pilot_create_failure_is_fail_closed_and_retryable(tmp_path, monkeypatch):
    kb = _PilotKB()
    attempts = 0
    real_create = kb.create_board_exclusive
    def flaky(board, **metadata):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("marker create failed")
        return real_create(board, **metadata)
    kb.create_board_exclusive = flaky
    monkeypatch.setattr(operator_cli, "_kanban", lambda: kb)
    monkeypatch.setattr(operator_cli, "_hermes_home", lambda: tmp_path)
    with pytest.raises(OSError, match="marker create failed"):
        operator_cli.pilot_prepare("retry", apply=True)
    assert kb.boards == {}
    assert operator_cli.pilot_prepare("retry", apply=True)["state"] == "created"
    assert kb.boards["retry"]["description"] == operator_cli._PILOT_BOARD_DESCRIPTION


def test_pilot_race_boundary_noncanonical_board_is_never_converted(tmp_path, monkeypatch):
    kb = _PilotKB()
    def raced_create(board, **metadata):
        kb.create_calls += 1
        kb.boards[board] = {"description": "historical winner"}
        raise kb.BoardAlreadyExistsError(board)
    kb.create_board_exclusive = raced_create
    monkeypatch.setattr(operator_cli, "_kanban", lambda: kb)
    monkeypatch.setattr(operator_cli, "_hermes_home", lambda: tmp_path)
    with pytest.raises(ValueError, match="historical"):
        operator_cli.pilot_prepare("race", apply=True)
    assert kb.boards["race"]["description"] == "historical winner"


def test_concurrent_pilot_prepares_are_idempotent(tmp_path, monkeypatch):
    kb = _PilotKB()
    monkeypatch.setattr(operator_cli, "_kanban", lambda: kb)
    monkeypatch.setattr(operator_cli, "_hermes_home", lambda: tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: operator_cli.pilot_prepare("parallel", apply=True), range(2)))
    assert sorted(result["state"] for result in results) == ["already_prepared", "created"]
    assert kb.create_calls == 1
    assert kb.boards["parallel"]["description"] == operator_cli._PILOT_BOARD_DESCRIPTION


def test_native_pilot_apply_persists_marker_report_accepts_only_that_board_and_is_idempotent(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    first = operator_cli.pilot_prepare("new-pilot", apply=True)
    metadata_before = kb.read_board_metadata("new-pilot")
    second = operator_cli.pilot_prepare("new-pilot", apply=True)

    assert first["state"] == "created"
    assert second["state"] == "already_prepared"
    assert kb.read_board_metadata("new-pilot") == metadata_before
    assert operator_cli.pilot_report("new-pilot")["board"] == "new-pilot"

    kb.create_board("historical", description="ordinary board")
    with pytest.raises(ValueError, match="not a dedicated"):
        operator_cli.pilot_report("historical")


def _board_racer(kind, home, profile, start, output):
    os.environ["HERMES_KANBAN_HOME"] = home
    os.environ["HERMES_HOME"] = f"{home}/profiles/{profile}"
    start.wait(5)
    try:
        if kind == "pilot":
            result = operator_cli.pilot_prepare("shared-race", apply=True)["state"]
        else:
            from hermes_cli import kanban_db as kb
            kb.create_board("shared-race", description="ordinary noncanonical")
            result = "ordinary"
        output.put((kind, "ok", result))
    except Exception as exc:
        output.put((kind, "error", type(exc).__name__))


def test_native_cross_profile_same_kanban_home_pilot_vs_ordinary_never_converts(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    home = tmp_path / "shared"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    ctx = multiprocessing.get_context("fork")
    start, output = ctx.Event(), ctx.Queue()
    processes = [
        ctx.Process(target=_board_racer, args=("pilot", str(home), "pilot-profile", start, output)),
        ctx.Process(target=_board_racer, args=("ordinary", str(home), "ordinary-profile", start, output)),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
    results = [output.get(timeout=2) for _ in processes]
    assert all(process.exitcode == 0 for process in processes), results
    description = kb.read_board_metadata("shared-race")["description"]
    assert description in {operator_cli._PILOT_BOARD_DESCRIPTION, "ordinary noncanonical"}
    if description == "ordinary noncanonical":
        assert ("pilot", "error", "ValueError") in results
    else:
        assert any(item[:2] == ("pilot", "ok") for item in results)


def test_override_block_rejects_invalid_transition_without_mutation(monkeypatch):
    task = _task("done")
    mutations = []
    conn = SimpleNamespace(close=lambda: None)
    kb = SimpleNamespace(
        connect=lambda board: conn, get_task=lambda connection, task_id: task,
        add_comment=lambda *args, **kwargs: mutations.append("comment"),
        unblock_task=lambda *args, **kwargs: mutations.append("unblock"),
    )
    monkeypatch.setattr(operator_cli, "_kanban", lambda: kb)
    with pytest.raises(ValueError, match="invalid recovery transition"):
        operator_cli.override_block("task-1", "pilot", "external evidence restored", "alice", confirmed=True)
    assert mutations == []


def test_override_block_rejects_recovery_while_another_writer_is_active(monkeypatch, tmp_path):
    import contextlib
    import contract
    import create_cli

    task = _task("blocked")
    task.body += f"repository: {tmp_path}\nproject_id: project-1\n"
    active = _task("running")
    active.id = "task-2"
    mutations = []
    conn = SimpleNamespace(close=lambda: None)
    kb = SimpleNamespace(
        connect=lambda board: conn,
        get_task=lambda connection, task_id: task,
        list_tasks=lambda *args, **kwargs: [task, active],
        add_comment=lambda *args, **kwargs: mutations.append("comment"),
        unblock_task=lambda *args, **kwargs: mutations.append("unblock"),
    )
    monkeypatch.setattr(operator_cli, "_kanban", lambda: kb)
    monkeypatch.setattr(operator_cli, "_record_matches", lambda *args: True)
    monkeypatch.setattr(contract, "load_contract", lambda path: SimpleNamespace(
        sha256="2" * 64, project_id="project-1",
    ))
    monkeypatch.setattr(create_cli, "creation_lock", lambda *args: contextlib.nullcontext())

    with pytest.raises(ValueError, match="active_writer_exists: task-2"):
        operator_cli.override_block("task-1", "pilot", "retry", "alice", confirmed=True)
    assert mutations == []


def test_new_cli_shape_requires_explicit_override_fields():
    parser = argparse.ArgumentParser()
    setup_parser(parser)
    args = parser.parse_args(["pilot", "prepare", "--board", "pilot", "--json"])
    assert args.pilot_action == "prepare" and args.apply is False
    with pytest.raises(SystemExit):
        parser.parse_args(["override-block", "--task", "task-1", "--board", "pilot"])


def test_pilot_report_is_read_only_and_keeps_missing_metrics_typed(monkeypatch):
    called = _forbid_provider_and_session_imports(monkeypatch)
    mutations = []
    conn = SimpleNamespace(close=lambda: None)
    task = SimpleNamespace(id="task-1")
    kb = SimpleNamespace(
        board_exists=lambda board: True,
        read_board_metadata=lambda board: {"description": operator_cli._PILOT_BOARD_DESCRIPTION},
        connect=lambda board: conn,
        list_tasks=lambda connection, include_archived=False: [task],
        create_board=lambda *args, **kwargs: mutations.append("create"),
    )
    report = {
        "task": {"id": "task-1"},
        "lifecycle": {"top_level_invocations": 1},
        "missing_metrics_reasons": {"model.api_requests": "not_recorded"},
        "verdict": "accepted",
    }
    monkeypatch.setattr(operator_cli, "_kanban", lambda: kb)
    monkeypatch.setattr(operator_cli, "build_report", lambda task_id, board: report)
    payload = operator_cli.pilot_report("pilot")
    assert payload["top_level_invocations"] == 1
    assert payload["verdict"] == "insufficient_measurement"
    assert payload["missing_metrics"] == [{
        "metric": "model.api_requests", "reason": "not_recorded", "task_id": "task-1",
    }]
    assert mutations == []
    assert called == []
