"""Focused deterministic creation, writer exclusion, and lock tests."""
from __future__ import annotations

import argparse
import builtins
import importlib
import json
import multiprocessing
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))
import create_cli  # noqa: E402


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "tracked").write_text("base")
    subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    policy = repo / ".hermes"
    policy.mkdir()
    (policy / "engineering.toml").write_text('''schema_version = 1
project_id = "sample"
profile = "bounded-engineer"
tenant = "bounded-engineering/v1"
completion_gate = "bounded-engineering/v1"
[workspace]
kind = "worktree"
require_clean_source = false
allow_commits = false
[context]
target_bytes = 6144
hard_bytes = 12288
[sandbox]
network = false
forward_env = []
[execution]
default_max_runtime_seconds = 900
default_max_retries = 2
max_active_writer_tasks = 1
[policy]
forbidden_tools = []
forbidden_git_subcommands = []
forbidden_command_tokens = []
[[verification]]
id = "focused"
kind = "test"
argv = ["python3", "-m", "pytest"]
timeout_seconds = 600
parser = "pytest"
minimum_collected = 1
required = true
''')
    spec = tmp_path / "task.json"
    spec.write_text(json.dumps({"schema_version": 1, "title": "Do The Thing", "objective": "Change one thing.",
        "acceptance": [{"id": "A1", "text": "It works.", "verification_ids": ["focused"]}],
        "allowed_paths": {"roots": ["src"], "files": []}, "required_verification_ids": ["focused"],
        "risk": "local_behavior", "max_runtime_seconds": 600, "max_retries": 1}))
    subprocess.run(["git", "-C", str(repo), "add", ".hermes/engineering.toml"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "policy"], check=True)
    return repo, spec


class FakeKB:
    def __init__(self):
        self.tasks = []
        self.calls = []

    @contextmanager
    def connect_closing(self, *, board):
        yield object()

    def list_tasks(self, conn, **kwargs):
        return list(self.tasks)

    def create_task(self, conn, **kwargs):
        self.calls.append(kwargs)
        task = SimpleNamespace(id="t_exact", status="running", project_id=kwargs["project_id"],
                               tenant=kwargs["tenant"], idempotency_key=kwargs["idempotency_key"])
        self.tasks.append(task)
        return task.id

    def get_task(self, conn, task_id):
        return next(t for t in self.tasks if t.id == task_id)


def _install_fake(monkeypatch, fake):
    from hermes_cli import kanban_db
    monkeypatch.setattr(kanban_db, "connect_closing", fake.connect_closing)
    monkeypatch.setattr(kanban_db, "list_tasks", fake.list_tasks)
    monkeypatch.setattr(kanban_db, "create_task", fake.create_task)
    monkeypatch.setattr(kanban_db, "get_task", fake.get_task)


def test_exact_fields_and_sequential_idempotency_before_storage(tmp_path, monkeypatch):
    repo, spec = _repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    fake = FakeKB()
    _install_fake(monkeypatch, fake)
    args = argparse.Namespace(repo=str(repo), spec=str(spec), board="dedicated")

    first = create_cli.create(args)
    specs = list((home / "engineering" / "specs").iterdir())
    contracts = list((home / "engineering" / "contracts").iterdir())
    second = create_cli.create(args)

    assert first["task_id"] == second["task_id"] == "t_exact"
    assert second["reused"] is True
    assert len(fake.calls) == 1
    assert list((home / "engineering" / "specs").iterdir()) == specs
    assert len(contracts) == 1
    assert contracts[0].stem == first["contract_sha256"]
    assert list((home / "engineering" / "contracts").iterdir()) == contracts
    call = fake.calls[0]
    assert call == {
        "title": "Do The Thing", "body": call["body"], "assignee": "bounded-engineer",
        "created_by": "hermes engineering create", "workspace_kind": "worktree",
        "workspace_path": str(home / "engineering/worktrees/sample" / first["spec_sha256"]),
        "branch_name": f"be/do-the-thing-{first['spec_sha256'][:8]}",
        "tenant": "bounded-engineering/v1",
        "idempotency_key": f"bounded-engineering:v1:sample:{first['baseline_head']}:{first['spec_sha256']}",
        "max_runtime_seconds": 600, "skills": ["bounded-engineering:bounded-engineering-worker"],
        "max_retries": 1, "goal_mode": False, "initial_status": "running",
        "completion_gate": "bounded-engineering/v1", "board": "dedicated", "project_id": "sample",
    }


def test_existing_exact_task_is_reused_before_stale_revision_rejection(tmp_path, monkeypatch):
    repo, spec = _repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    fake = FakeKB()
    _install_fake(monkeypatch, fake)
    first = create_cli.create(argparse.Namespace(repo=str(repo), spec=str(spec), board="dedicated"))

    second = create_cli.create(argparse.Namespace(
        repo=str(repo), spec=str(spec), board="dedicated",
        expected_board_revision="deliberately-stale",
    ))

    assert second["task_id"] == first["task_id"]
    assert second["reused"] is True
    assert len(fake.calls) == 1


def test_closed_blocked_task_does_not_hold_writer_slot(tmp_path, monkeypatch):
    repo, spec = _repo(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    fake = FakeKB()
    fake.tasks.append(SimpleNamespace(id="t_other", status="blocked", project_id="sample",
                                      tenant=create_cli.TENANT, idempotency_key="different"))
    _install_fake(monkeypatch, fake)
    created = create_cli.create(argparse.Namespace(repo=str(repo), spec=str(spec), board="dedicated"))
    assert created["task_id"] != "t_other"
    assert len(fake.calls) == 1
    home = tmp_path / "home"
    assert (home / "engineering/specs").exists()
    assert (home / "engineering/worktrees").exists()
    branches = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname)",
                               "refs/heads/be/"], check=True, capture_output=True, text=True).stdout
    assert branches != ""


def test_rejects_other_active_writer(tmp_path, monkeypatch):
    repo, spec = _repo(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    fake = FakeKB()
    fake.tasks.append(SimpleNamespace(id="t_other", status="running", project_id="sample",
                                      tenant=create_cli.TENANT, idempotency_key="different"))
    _install_fake(monkeypatch, fake)
    with pytest.raises(create_cli.CreationError, match="active_writer_exists: t_other"):
        create_cli.create(argparse.Namespace(repo=str(repo), spec=str(spec), board="dedicated"))
    assert fake.calls == []


def test_dirty_source_fails_closed_when_contract_requires_it(tmp_path, monkeypatch):
    repo, spec = _repo(tmp_path)
    policy = repo / ".hermes/engineering.toml"
    policy.write_text(policy.read_text().replace("require_clean_source = false", "require_clean_source = true"))
    (repo / "dirty").write_text("x")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    fake = FakeKB()
    _install_fake(monkeypatch, fake)
    with pytest.raises(create_cli.CreationError, match="source repository is dirty"):
        create_cli.create(argparse.Namespace(repo=str(repo), spec=str(spec), board="dedicated"))
    assert fake.calls == []


def _hold_lock(home: str, ready, elapsed):
    with create_cli.creation_lock(Path(home), "board", "project", timeout=3):
        ready.set()
        time.sleep(0.35)
    elapsed.value = time.monotonic()


def _wait_lock(home: str, ready, waited):
    ready.wait(2)
    start = time.monotonic()
    with create_cli.creation_lock(Path(home), "board", "project", timeout=3):
        waited.value = time.monotonic() - start


def test_cross_process_creation_lock_serializes_same_board_tenant_project(tmp_path):
    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    elapsed = ctx.Value("d", 0.0)
    waited = ctx.Value("d", 0.0)
    first = ctx.Process(target=_hold_lock, args=(str(tmp_path), ready, elapsed))
    second = ctx.Process(target=_wait_lock, args=(str(tmp_path), ready, waited))
    first.start(); second.start()
    first.join(5); second.join(5)
    assert first.exitcode == second.exitcode == 0
    assert waited.value >= 0.25


def test_creation_lock_windows_branch_never_imports_fcntl_and_locks_one_byte(tmp_path, monkeypatch):
    calls = []
    fake_msvcrt = SimpleNamespace(
        LK_NBLCK=1, LK_UNLCK=2,
        locking=lambda fd, mode, size: calls.append(
            (fd, mode, size, os.lseek(fd, 0, os.SEEK_CUR))),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(create_cli.os, "name", "nt")
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "fcntl":
            raise AssertionError("Windows lock path imported fcntl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with create_cli.creation_lock(tmp_path, "board", "project", timeout=0.01):
        pass

    assert [(mode, size, offset) for _, mode, size, offset in calls] == [(1, 1, 0), (2, 1, 0)]
    assert next((tmp_path / "engineering" / "locks").iterdir()).stat().st_size == 1


def test_creation_lock_contention_times_out_closes_and_never_enters(tmp_path, monkeypatch):
    closed = []
    entered = False
    real_close = create_cli.os.close
    monkeypatch.setattr(create_cli.os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    import fcntl
    monkeypatch.setattr(fcntl, "flock", lambda *a: (_ for _ in ()).throw(BlockingIOError()))

    with pytest.raises(create_cli.CreationError, match="creation_lock_timeout"):
        with create_cli.creation_lock(tmp_path, "board", "project", timeout=0.001):
            entered = True
    assert entered is False
    assert len(closed) == 1


def test_creation_lock_unlock_failure_is_typed_and_handle_is_closed(tmp_path, monkeypatch):
    closed = []
    real_close = create_cli.os.close
    monkeypatch.setattr(create_cli.os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    import fcntl
    real_flock = fcntl.flock

    def fail_unlock(fd, operation):
        if operation == fcntl.LOCK_UN:
            raise OSError("unlock failed")
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", fail_unlock)
    with pytest.raises(create_cli.CreationError,
                       match="creation_lock_cleanup_failed: unlock failed"):
        with create_cli.creation_lock(tmp_path, "board", "project", timeout=0.01):
            pass
    assert len(closed) == 1


def _native_create(repo: str, spec: str, board: str, home: str, output):
    os.environ["HERMES_HOME"] = home
    try:
        output.put(("ok", create_cli.create(argparse.Namespace(repo=repo, spec=spec, board=board))))
    except Exception as exc:
        output.put(("error", repr(exc)))


def test_native_create_materializes_exact_clean_worktree_before_card_and_reuses_without_mutation(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    repo, spec = _repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.create_board("dedicated")
    args = argparse.Namespace(repo=str(repo), spec=str(spec), board="dedicated")

    first = create_cli.create(args)
    workspace = Path(first["workspace_path"])
    assert workspace.is_dir()
    assert subprocess.run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"], check=True,
                          capture_output=True, text=True).stdout.strip() == str(workspace)
    assert subprocess.run(["git", "-C", str(workspace), "branch", "--show-current"], check=True,
                          capture_output=True, text=True).stdout.strip() == first["branch"]
    assert subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip() == first["baseline_head"]
    assert subprocess.run(["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"],
                          check=True, capture_output=True, text=True).stdout == ""
    marker = workspace / "sequential-reuse-must-not-touch"
    marker.write_text("keep")

    second = create_cli.create(args)
    assert second["reused"] is True
    assert marker.read_text() == "keep"
    with kb.connect_closing(board="dedicated") as conn:
        assert len(kb.list_tasks(conn, tenant=create_cli.TENANT, include_archived=True)) == 1


def test_native_concurrent_create_produces_one_task_and_one_valid_worktree(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    repo, spec = _repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.create_board("dedicated")
    ctx = multiprocessing.get_context("fork")
    output = ctx.Queue()
    processes = [ctx.Process(target=_native_create,
                             args=(str(repo), str(spec), "dedicated", str(home), output)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
    results = [output.get(timeout=2) for _ in processes]
    assert [process.exitcode for process in processes] == [0, 0]
    assert all(kind == "ok" for kind, _ in results), results
    payloads = [payload for _, payload in results]
    assert {payload["task_id"] for payload in payloads} == {payloads[0]["task_id"]}
    assert sorted(payload["reused"] for payload in payloads) == [False, True]
    workspace = Path(payloads[0]["workspace_path"])
    assert subprocess.run(["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"],
                          check=True, capture_output=True, text=True).stdout == ""
    assert subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip() == payloads[0]["baseline_head"]
    with kb.connect_closing(board="dedicated") as conn:
        assert len(kb.list_tasks(conn, tenant=create_cli.TENANT, include_archived=True)) == 1


def test_card_creation_failure_preserves_uncertain_worktree_and_branch(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    repo, spec = _repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.create_board("dedicated")
    monkeypatch.setattr(kb, "create_task", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("card failed")))

    with pytest.raises(create_cli.CreationError, match="card creation failed"):
        create_cli.create(argparse.Namespace(repo=str(repo), spec=str(spec), board="dedicated"))

    worktrees = home / "engineering" / "worktrees" / "sample"
    assert worktrees.is_dir() and len(list(worktrees.iterdir())) == 1
    branches = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)", "refs/heads/be/"],
                              check=True, capture_output=True, text=True).stdout
    assert branches.startswith("be/do-the-thing-")  # uncertain ref is preserved


def test_verification_failure_after_successful_add_preserves_uncertain_residue(tmp_path, monkeypatch):
    repo, _ = _repo(tmp_path)
    baseline = create_cli._git(repo, "rev-parse", "HEAD")
    workspace = tmp_path / "worktree"
    monkeypatch.setattr(create_cli, "_verify_worktree",
                        lambda *a, **k: (_ for _ in ()).throw(create_cli.CreationError("verify failed")))
    with pytest.raises(create_cli.CreationError, match="verify failed"):
        create_cli._materialize_worktree(repo, workspace, "be/verify-fail", baseline)
    assert workspace.exists()
    assert create_cli._branch_oid(repo, "be/verify-fail") == baseline


def test_partial_worktree_add_preserves_uncertain_path_and_baseline_ref(tmp_path, monkeypatch):
    repo, _ = _repo(tmp_path)
    baseline = create_cli._git(repo, "rev-parse", "HEAD")
    workspace = tmp_path / "partial"
    real_git = create_cli._git
    def partial(root, *args):
        if args[:2] == ("worktree", "add"):
            workspace.mkdir()
            real_git(repo, "branch", "be/partial", baseline)
            raise create_cli.CreationError("partial add")
        return real_git(root, *args)
    monkeypatch.setattr(create_cli, "_git", partial)
    with pytest.raises(create_cli.CreationError, match="partial add"):
        create_cli._materialize_worktree(repo, workspace, "be/partial", baseline)
    assert workspace.exists()
    assert create_cli._branch_oid(repo, "be/partial") == baseline


def test_preservation_helper_leaves_unreceipted_branch_residue(tmp_path):
    repo, _ = _repo(tmp_path)
    baseline = create_cli._git(repo, "rev-parse", "HEAD")
    create_cli._git(repo, "branch", "be/preserved", baseline)

    create_cli._preserve_worktree_unless_rollback_is_provably_safe(
        repo, tmp_path / "absent", "be/preserved", None
    )

    assert create_cli._branch_oid(repo, "be/preserved") == baseline


def test_preservation_helper_preserves_replaced_foreign_path_and_same_oid_recreated_ref(tmp_path):
    repo, _ = _repo(tmp_path)
    baseline = create_cli._git(repo, "rev-parse", "HEAD")
    workspace = tmp_path / "owned"
    create_cli._git(repo, "worktree", "add", "-b", "be/takeover", str(workspace), baseline)
    receipt = create_cli._worktree_receipt(workspace)
    create_cli._git(repo, "worktree", "remove", "--force", str(workspace))
    create_cli._git(repo, "branch", "-D", "be/takeover")
    create_cli._git(repo, "branch", "be/takeover", baseline)
    workspace.mkdir()
    foreign = workspace / "foreign"
    foreign.write_text("survive")
    create_cli._preserve_worktree_unless_rollback_is_provably_safe(repo, workspace, "be/takeover", receipt)
    assert foreign.read_text() == "survive"
    assert create_cli._branch_oid(repo, "be/takeover") == baseline


def test_create_task_commit_then_raise_preserves_visible_card_and_workspace(tmp_path, monkeypatch):
    repo, spec = _repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    fake = FakeKB()
    def committed_then_raised(conn, **kwargs):
        FakeKB.create_task(fake, conn, **kwargs)
        raise RuntimeError("after commit")
    fake.create_task = committed_then_raised
    _install_fake(monkeypatch, fake)
    result = create_cli.create(argparse.Namespace(repo=str(repo), spec=str(spec), board="dedicated"))
    assert result["task_id"] == "t_exact"
    assert Path(result["workspace_path"]).is_dir()
    assert create_cli._branch_oid(repo, result["branch"]) == result["baseline_head"]
