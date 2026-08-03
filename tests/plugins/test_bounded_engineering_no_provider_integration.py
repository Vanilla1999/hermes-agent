"""No-provider integration coverage over native Kanban and bounded operator modules.

Every test uses a private HERMES_HOME, generated Git repository, and dedicated
native board.  No worker, provider, session, network, or historical board is
started or opened.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))
import create_cli  # noqa: E402
import engineering_cli  # noqa: E402
import operator_cli  # noqa: E402
from hermes_cli import kanban_db as kb  # noqa: E402


def _plugin_module():
    spec = importlib.util.spec_from_file_location("task8_bounded_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTerminal:
    """Structured, bounded terminal tool; never starts a process or provider."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def dispatch_tool(self, name, args):
        assert name == "terminal"
        self.calls.append((name, args))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return json.dumps(reply, sort_keys=True)


def _running(created, board, monkeypatch, repo):
    workspace = Path(created["workspace_path"])
    # ``engineering create`` owns materialization; integration helpers must use
    # the exact persisted worktree rather than manufacturing a replacement.
    assert workspace.is_dir()
    assert subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip() == created["baseline_head"]
    assert subprocess.run(["git", "-C", str(workspace), "branch", "--show-current"], check=True,
                          capture_output=True, text=True).stdout.strip() == created["branch"]
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", created["task_id"])
    with kb.connect_closing(board=board) as conn:
        assert kb.claim_task(conn, created["task_id"])
        task = kb.get_task(conn, created["task_id"])
        run_id = task.current_run_id
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return Path(created["workspace_path"]), run_id


def _forbid_provider_imports(monkeypatch):
    original = __import__
    calls = []

    def guarded(name, *args, **kwargs):
        if name == "run_agent" or "provider" in name or "session" in name:
            calls.append(name)
            raise AssertionError(f"provider/session import forbidden: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded)
    return calls

BOARD_DESCRIPTION = "isolated Task 8 no-provider fixture"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, project: str = "task8"):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "task8@example.invalid")
    _git(repo, "config", "user.name", "Task 8")
    (repo / "src").mkdir()
    (repo / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    policy = repo / ".hermes"
    policy.mkdir()
    (policy / "engineering.toml").write_text(f'''schema_version = 1
project_id = "{project}"
profile = "bounded-engineer"
tenant = "bounded-engineering/v1"
completion_gate = "bounded-engineering/v1"
[workspace]
kind = "worktree"
require_clean_source = true
allow_commits = false
[context]
target_bytes = 6144
hard_bytes = 12288
[sandbox]
network = false
forward_env = []
[execution]
default_max_runtime_seconds = 90
default_max_retries = 1
max_active_writer_tasks = 1
[policy]
forbidden_tools = ["web_search", "browser", "delegate_task"]
forbidden_git_subcommands = ["push", "fetch", "reset"]
forbidden_command_tokens = ["curl", "wget", "ssh"]
[[verification]]
id = "focused"
kind = "test"
argv = ["python3", "-m", "pytest", "-q"]
timeout_seconds = 60
parser = "pytest"
minimum_collected = 1
required = true
''', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    spec = tmp_path / "task.json"
    spec.write_text(json.dumps({
        "schema_version": 1,
        "title": "Change Value",
        "objective": "Change the fixture value.",
        "acceptance": [{"id": "A1", "text": "Value changes.", "verification_ids": ["focused"]}],
        "allowed_paths": {"roots": ["src"], "files": []},
        "required_verification_ids": ["focused"],
        "risk": "local_behavior",
        "max_runtime_seconds": 60,
        "max_retries": 1,
    }, sort_keys=True), encoding="utf-8")
    board = f"task8-{tmp_path.name.lower()}"
    kb.create_board(board, name="Task 8 fixture", description=BOARD_DESCRIPTION)
    return home, repo, spec, board


def _args(repo: Path, spec: Path, board: str):
    return argparse.Namespace(repo=str(repo), spec=str(spec), board=board)


def _deny_network(monkeypatch: pytest.MonkeyPatch):
    calls: list[object] = []

    def denied(*args, **kwargs):
        calls.append(args)
        raise AssertionError("network access is forbidden in no-provider integration tests")

    monkeypatch.setattr(socket, "create_connection", denied)
    return calls


def test_native_create_exact_fields_immutable_records_idempotency_and_read_only_views(tmp_path, monkeypatch):
    home, repo, spec, board = _fixture(tmp_path, monkeypatch)
    network_calls = _deny_network(monkeypatch)
    source_fingerprint = hashlib.sha256((repo / "src/value.py").read_bytes()).hexdigest()

    first = create_cli.create(_args(repo, spec, board))
    spec_path = home / "engineering/specs" / f"{first['spec_sha256']}.json"
    contract_path = home / "engineering/contracts" / f"{first['contract_sha256']}.json"
    records_before = {
        spec_path: (spec_path.read_bytes(), spec_path.stat().st_mode & 0o777),
        contract_path: (contract_path.read_bytes(), contract_path.stat().st_mode & 0o777),
    }
    second = create_cli.create(_args(repo, spec, board))

    assert first["reused"] is False and second["reused"] is True
    assert first["task_id"] == second["task_id"]
    with kb.connect_closing(board=board) as conn:
        tasks = kb.list_tasks(conn, include_archived=True)
        assert len(tasks) == 1
        task = tasks[0]
        assert {
            "title": task.title, "assignee": task.assignee, "tenant": task.tenant,
            "project_id": task.project_id, "workspace_kind": task.workspace_kind,
            "workspace_path": task.workspace_path, "branch_name": task.branch_name,
            "completion_gate": task.completion_gate, "skills": task.skills,
            "max_runtime_seconds": task.max_runtime_seconds, "max_retries": task.max_retries,
            "goal_mode": task.goal_mode, "status": task.status,
        } == {
            "title": "Change Value", "assignee": "bounded-engineer",
            # The fixture project_id is a repository policy identity, not a
            # registered Hermes Projects DB foreign key; native Kanban correctly
            # leaves the optional project anchor empty.
            "tenant": "bounded-engineering/v1", "project_id": None,
            "workspace_kind": "worktree", "workspace_path": first["workspace_path"],
            "branch_name": first["branch"], "completion_gate": "bounded-engineering/v1",
            "skills": ["bounded-engineering:bounded-engineering-worker"],
            "max_runtime_seconds": 60, "max_retries": 1, "goal_mode": False,
            "status": "ready",
        }
        with pytest.raises(kb.CompletionGateRequiredError):
            kb.complete_task(conn, task.id)

    status = operator_cli.build_status(first["task_id"], board)
    proof = operator_cli.validate_proof(first["task_id"], board)
    report = operator_cli.build_report(first["task_id"], board)
    assert status["task"]["completion_gate"] == "bounded-engineering/v1"
    assert proof["valid"] is False
    assert report["model"]["api_requests"] is None
    assert any(key.startswith("model.") for key in report["missing_metrics_reasons"])
    assert records_before == {p: (p.read_bytes(), p.stat().st_mode & 0o777) for p in records_before}
    assert all(mode == 0o600 for _, mode in records_before.values())
    assert hashlib.sha256((repo / "src/value.py").read_bytes()).hexdigest() == source_fingerprint
    assert network_calls == []


def test_two_real_create_processes_produce_one_native_task(tmp_path, monkeypatch):
    home, repo, spec, board = _fixture(tmp_path, monkeypatch, project="concurrent")
    script = """
import argparse, json, sys
sys.path.insert(0, sys.argv[1])
import create_cli
args=argparse.Namespace(repo=sys.argv[2], spec=sys.argv[3], board=sys.argv[4])
print(json.dumps(create_cli.create(args), sort_keys=True))
"""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    command = [sys.executable, "-c", script, str(PLUGIN_DIR), str(repo), str(spec), board]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                  cwd=Path(__file__).resolve().parents[2]) for _ in range(2)]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert results[0]["task_id"] == results[1]["task_id"]
    assert sorted(item["reused"] for item in results) == [False, True]
    with kb.connect_closing(board=board) as conn:
        assert len(kb.list_tasks(conn, tenant=create_cli.TENANT, include_archived=True)) == 1


def test_native_active_writer_override_and_pilot_historical_safety(tmp_path, monkeypatch):
    home, repo, spec, board = _fixture(tmp_path, monkeypatch, project="safety")
    created = create_cli.create(_args(repo, spec, board))
    other_spec = tmp_path / "other.json"
    payload = json.loads(spec.read_text())
    payload["title"] = "Other Change"
    other_spec.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(create_cli.CreationError, match="active_writer_exists"):
        create_cli.create(_args(repo, other_spec, board))

    with kb.connect_closing(board=board) as conn:
        assert kb.block_task(conn, created["task_id"], reason="external fixture restored", kind="capability")
    recovered = operator_cli.override_block(
        created["task_id"], board, "external fixture restored", "task8-test", confirmed=True
    )
    assert recovered["from_status"] == "blocked"
    with kb.connect_closing(board=board) as conn:
        task = kb.get_task(conn, created["task_id"])
        assert task.completion_gate == "bounded-engineering/v1"
        assert task.status in {"ready", "todo"}
        assert any("bounded_engineering_override_block" in comment.body for comment in kb.list_comments(conn, task.id))

    pilot = f"{board}-pilot"
    assert operator_cli.pilot_prepare(pilot, apply=False)["state"] == "would_create"
    assert not kb.board_exists(pilot)
    assert operator_cli.pilot_prepare(pilot, apply=True)["state"] == "created"
    assert operator_cli.pilot_prepare(pilot, apply=True)["state"] == "already_prepared"
    with pytest.raises(ValueError, match="historical"):
        operator_cli.pilot_prepare(board, apply=True)
    assert kb.read_board_metadata(board)["description"] == BOARD_DESCRIPTION


def test_doctor_fail_closed_and_green_local_fixture_without_running_docker(tmp_path, monkeypatch, capsys):
    home, repo, _spec, board = _fixture(tmp_path, monkeypatch, project="doctor")
    profile = home / "profiles/bounded-engineer"
    profile.mkdir(parents=True)
    profile_config = profile / "config.yaml"
    profile_config.write_text("terminal:\n  backend: local\nplugins:\n  enabled: []\n", encoding="utf-8")
    args = argparse.Namespace(repo=str(repo), board=board, profile="bounded-engineer", json_output=True,
                              engineering_action="doctor")
    assert engineering_cli.run(args) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["ready"] is False
    assert not next(c for c in failed["checks"] if c["name"] == "network_isolation")["ok"]

    profile_config.write_text('''terminal:
  backend: docker
  docker_image: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  docker_forward_env: []
  docker_mount_cwd_to_workspace: true
  docker_run_as_host_user: true
  docker_network: false
  docker_extra_args: []
  docker_volumes: []
  docker_env: {}
  docker_persist_across_processes: false
  docker_mount_host_data: false
plugins:
  enabled: [bounded-engineering]
kanban:
  auto_decompose: false
  max_in_progress: 1
  max_in_progress_per_profile: 1
''', encoding="utf-8")
    # Task 8 must not depend on a host Docker daemon.  Stub only the bounded
    # container boundary; all repo/profile/board/gate checks above remain real.
    monkeypatch.setattr(engineering_cli, "_container_probe_checks", lambda *_: [
        engineering_cli._check("docker_image_local", True, "isolated fixture"),
        engineering_cli._check("container_probe", True, "network=none; no secrets/mounts"),
    ])
    assert engineering_cli.run(args) == 0
    green = json.loads(capsys.readouterr().out)
    assert green["ready"] is True
    assert all(item["ok"] for item in green["checks"])
    policy = engineering_cli._profile_policy_checks(profile)
    assert next(c for c in policy if c["name"] == "network_isolation")["ok"]
    assert "docker_forward_env: []" in profile_config.read_text()
    assert "docker_volumes: []" in profile_config.read_text()


def test_direct_verify_complete_and_atomic_retry_without_provider(tmp_path, monkeypatch):
    _home, repo, spec, board = _fixture(tmp_path, monkeypatch, project="lifecycle")
    created = create_cli.create(_args(repo, spec, board))
    workspace, run_id = _running(created, board, monkeypatch, repo)
    (workspace / "src/value.py").write_text("VALUE = 2\n", encoding="utf-8")
    calls = _forbid_provider_imports(monkeypatch)
    module = _plugin_module()
    module._on_session_start(session_id="bounded-session")
    module._on_post_api_request(
        session_id="bounded-session", provider="openai-codex", model="gpt-5.6-sol",
        response_model="gpt-5.6-sol", usage={
            "input_tokens": 321, "output_tokens": 54, "cache_read_tokens": 123,
            "cache_write_tokens": 0,
        },
    )
    terminal = FakeTerminal([{"exit_code": 0, "output": "1 passed", "collected": 1}] * 3)
    verified = json.loads(module._engineering_verify(terminal))

    import hermes_cli.kanban_db as native_db
    real = native_db.complete_task_with_gate
    monkeypatch.setattr(native_db, "complete_task_with_gate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected DB failure")))
    with pytest.raises(RuntimeError, match="injected DB failure"):
        module._engineering_complete(terminal)
    assert list((Path(os.environ["HERMES_HOME"]) / "engineering/completion-evidence").glob("*.json"))
    with kb.connect_closing(board=board) as conn:
        assert kb.get_task(conn, created["task_id"]).current_run_id == run_id
    monkeypatch.setattr(native_db, "complete_task_with_gate", real)
    first = json.loads(module._engineering_complete(terminal))
    second = json.loads(module._engineering_complete(FakeTerminal([])))
    assert first == second and first["status"] == "done"
    assert first["completion_evidence_sha256"] == verified["completion_evidence_sha256"]
    with kb.connect_closing(board=board) as conn:
        assert kb.get_task(conn, created["task_id"]).status == "done"
        completed_run = kb.get_run(conn, run_id)
        assert completed_run.outcome == "completed"
        assert completed_run.metadata["model"] == {
            "api_requests": 1, "provider": "openai-codex", "model": "gpt-5.6-sol",
            "sessions": 1, "input_tokens": 321, "output_tokens": 54,
            "cache_read_tokens": 123, "cache_write_tokens": 0,
            "reasoning_tokens": None,
        }
        assert completed_run.metadata["verification"]["passed"] == 1
        assert completed_run.metadata["workspace"]["changed_files"] == 1
    report = operator_cli.build_report(created["task_id"], board)
    assert report["model"]["input_tokens"] == 321
    assert report["verification"]["passed"] == 1
    assert report["workspace"]["changed_files"] == 1
    assert calls == []


def test_plugin_unavailable_and_invalid_spec_contract_fail_closed(tmp_path, monkeypatch):
    _home, repo, spec, board = _fixture(tmp_path, monkeypatch, project="invalid")
    payload = json.loads(spec.read_text())
    payload["required_verification_ids"] = ["missing"]
    spec.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception):
        create_cli.create(_args(repo, spec, board))
    payload["required_verification_ids"] = ["focused"]
    spec.write_text(json.dumps(payload), encoding="utf-8")
    created = create_cli.create(_args(repo, spec, board))
    workspace, _ = _running(created, board, monkeypatch, repo)
    (workspace / ".hermes/engineering.toml").write_text("schema_version = 999\n", encoding="utf-8")
    with pytest.raises(Exception):
        _plugin_module()._engineering_verify(FakeTerminal([]))
    with kb.connect_closing(board=board) as conn:
        with pytest.raises(kb.CompletionGateRequiredError):
            kb.complete_task(conn, created["task_id"])
        assert kb.get_task(conn, created["task_id"]).status == "running"


@pytest.mark.parametrize("reply", [
    {"exit_code": 1, "output": "1 failed", "collected": 1},
    {"exit_code": 0, "output": "no tests ran", "collected": 0},
    TimeoutError("bounded timeout"),
    {"output": "ambiguous", "collected": 1},
])
def test_failed_zero_timeout_ambiguous_then_fix_and_valid_rerun(tmp_path, monkeypatch, reply):
    _home, repo, spec, board = _fixture(tmp_path, monkeypatch, project="rerun")
    created = create_cli.create(_args(repo, spec, board))
    workspace, _ = _running(created, board, monkeypatch, repo)
    module = _plugin_module()
    with pytest.raises(Exception):
        module._engineering_verify(FakeTerminal([reply]))
    (workspace / "src/value.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert json.loads(module._engineering_verify(FakeTerminal([
        {"exit_code": 0, "output": "1 passed", "collected": 1}
    ])))["verification_ids"] == ["focused"]


def test_scope_violation_then_correction_at_direct_verify_boundary(tmp_path, monkeypatch):
    _home, repo, spec, board = _fixture(tmp_path, monkeypatch, project="scope")
    created = create_cli.create(_args(repo, spec, board))
    workspace, _ = _running(created, board, monkeypatch, repo)
    outside = workspace / "outside.txt"
    outside.write_text("forbidden\n", encoding="utf-8")
    with pytest.raises(Exception, match="outside allowed scope"):
        _plugin_module()._engineering_verify(FakeTerminal([
            {"exit_code": 0, "output": "1 passed", "collected": 1}
        ]))
    outside.unlink()


def test_native_crash_reclaim_preserves_workspace_evidence_and_reports_dead_pid(tmp_path, monkeypatch):
    home, repo, spec, board = _fixture(tmp_path, monkeypatch, project="reclaim")
    created = create_cli.create(_args(repo, spec, board))
    workspace, run_id = _running(created, board, monkeypatch, repo)
    changed = workspace / "src/value.py"
    changed.write_text("VALUE = 2\n", encoding="utf-8")
    evidence = home / "engineering/completion-evidence" / ("e" * 64 + ".json")
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"preserved":true}\n', encoding="utf-8")

    dead_pid = 2_147_483_647
    with kb.connect_closing(board=board) as conn:
        conn.execute("UPDATE tasks SET worker_pid = ?, started_at = 0 WHERE id = ?", (dead_pid, created["task_id"]))
        conn.execute("UPDATE task_runs SET worker_pid = ? WHERE id = ?", (dead_pid, run_id))
        conn.commit()
        before_reclaim = operator_cli.build_status(created["task_id"], board)
        assert before_reclaim["runs"][-1]["worker_pid"] == dead_pid
        assert before_reclaim["runs"][-1]["pid_alive"] is False
        assert kb.detect_crashed_workers(conn) == [created["task_id"]]
        reclaimed = kb.get_task(conn, created["task_id"])
        assert reclaimed is not None and reclaimed.status == "blocked"

    status = operator_cli.build_status(created["task_id"], board)
    assert status["runs"][-1]["worker_pid"] is None
    assert status["runs"][-1]["pid_alive"] is None
    assert changed.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert evidence.read_text(encoding="utf-8") == '{"preserved":true}\n'
