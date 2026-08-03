"""Fail-closed trusted bounded-engineering task context tests."""
from __future__ import annotations

import sys
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

import context as bounded_context  # noqa: E402


def _install_valid_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[SimpleNamespace, SimpleNamespace]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = b'{"acceptance":[],"allowed_paths":{"files":[],"roots":["src"]},"objective":"fix it"}'
    digest = hashlib.sha256(payload).hexdigest()
    spec_dir = tmp_path / "hermes/engineering/specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / f"{digest}.json").write_bytes(payload)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    task = SimpleNamespace(id="task-1", current_run_id=7, workspace_path=str(workspace), completion_gate="bounded-engineering/v1", body=f"spec_sha256: {digest}")
    run = SimpleNamespace(id=7, task_id="task-1")
    contract = SimpleNamespace(completion_gate="bounded-engineering/v1")
    monkeypatch.setattr(bounded_context, "get_task", lambda conn, task_id: task if task_id == "task-1" else None)
    monkeypatch.setattr(bounded_context, "get_run", lambda conn, run_id: run if run_id == 7 else None)
    monkeypatch.setattr(bounded_context, "load_contract", lambda repo: contract)
    return task, run


def test_load_trusted_task_context_accepts_exact_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task, run = _install_valid_sources(monkeypatch, tmp_path)

    context = bounded_context.load_trusted_task_context(None, task_id="task-1", run_id=7, workspace_path=task.workspace_path, completion_gate="bounded-engineering/v1")

    assert context.task is task
    assert context.run is run
    assert context.workspace_path == Path(task.workspace_path).resolve()
    assert context.spec["objective"] == "fix it"


@pytest.mark.parametrize("field,value", [("current_run_id", 8), ("workspace_path", "/spoofed"), ("completion_gate", "spoofed/v1")])
def test_load_trusted_task_context_rejects_spoofed_task_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: object) -> None:
    task, _ = _install_valid_sources(monkeypatch, tmp_path)
    setattr(task, field, value)

    with pytest.raises(bounded_context.TrustedContextError):
        bounded_context.load_trusted_task_context(None, task_id="task-1", run_id=7, workspace_path=str(tmp_path / "workspace"), completion_gate="bounded-engineering/v1")


def test_load_trusted_task_context_rejects_run_owned_by_another_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task, run = _install_valid_sources(monkeypatch, tmp_path)
    run.task_id = "task-2"

    with pytest.raises(bounded_context.TrustedContextError, match="run does not belong"):
        bounded_context.load_trusted_task_context(None, task_id=task.id, run_id=run.id, workspace_path=task.workspace_path, completion_gate=task.completion_gate)


def test_closed_blocked_run_is_available_only_to_lifecycle_observers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task, run = _install_valid_sources(monkeypatch, tmp_path)
    task.current_run_id = None
    task.status = "blocked"
    run.status = "blocked"
    run.outcome = "blocked"
    run.ended_at = 1

    kwargs = dict(task_id=task.id, run_id=run.id, workspace_path=task.workspace_path,
                  completion_gate=task.completion_gate)
    with pytest.raises(bounded_context.TrustedContextError, match="current_run_id"):
        bounded_context.load_trusted_task_context(None, **kwargs)
    assert bounded_context.load_trusted_task_context(
        None, allow_blocked=True, **kwargs,
    ).run is run


def test_load_trusted_task_context_rejects_contract_gate_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task, _ = _install_valid_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(bounded_context, "load_contract", lambda repo: SimpleNamespace(completion_gate="other/v1"))

    with pytest.raises(bounded_context.TrustedContextError, match="contract completion gate mismatch"):
        bounded_context.load_trusted_task_context(None, task_id=task.id, run_id=7, workspace_path=task.workspace_path, completion_gate=task.completion_gate)


def test_load_worker_task_context_uses_only_dispatcher_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task, _ = _install_valid_sources(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")

    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    conn = Connection()
    monkeypatch.setattr(bounded_context, "connect", lambda: conn)

    context = bounded_context.load_worker_task_context()

    assert context.task_id == task.id
    assert conn.closed is True


@pytest.mark.parametrize("run_id", [None, "", "0", "not-an-int"])
def test_load_worker_task_context_rejects_missing_or_invalid_run_id(monkeypatch: pytest.MonkeyPatch, run_id: str | None) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)

    with pytest.raises(bounded_context.TrustedContextError, match="HERMES_KANBAN_RUN_ID"):
        bounded_context.load_worker_task_context()
