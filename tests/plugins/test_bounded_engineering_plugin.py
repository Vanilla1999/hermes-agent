"""Bounded-engineering plugin surface tests."""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))


def test_real_plugin_discovery_loads_qualified_worker_skill(monkeypatch, tmp_path: Path) -> None:
    """Exercise discovery and skill_view, rather than seeding registry internals."""
    from hermes_cli import plugins as plugins_module
    from hermes_cli.plugins import PluginManager
    from tools import skills_tool

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    empty_skills = tmp_path / "skills"
    empty_skills.mkdir()
    manager = PluginManager()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(PLUGIN_DIR.parent))
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    monkeypatch.setattr(plugins_module, "_plugin_manager", manager)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", empty_skills)

    manager.discover_and_load()

    qualified = "bounded-engineering:bounded-engineering-worker"
    skill_path = manager.find_plugin_skill(qualified)
    assert skill_path == PLUGIN_DIR / "skills" / "bounded-engineering-worker" / "SKILL.md"
    assert skill_path is not None
    expected = skill_path.read_text()
    result = json.loads(skills_tool.skill_view(qualified))
    assert result["success"] is True
    assert result["name"] == qualified
    assert expected in result["content"]
    assert "Complete only through `engineering_complete`" in result["content"]
    assert "Do not commit or push." in result["content"]
    assert "unless the trusted task contract" not in result["content"]


def test_real_plugin_manager_dispatch_resolves_package_siblings(monkeypatch, tmp_path: Path) -> None:
    """Directory plugins must not depend on their directory being importable at top level."""
    from hermes_cli.plugins import PluginManager, PluginManifest
    from tools.registry import registry

    sibling_names = {
        "audit", "body", "completion", "context", "contract", "create_cli",
        "engineering_cli", "operator_cli", "plan_cli", "planner_runtime", "record", "snapshot", "spec",
        "status", "storage", "telemetry", "verification",
    }
    saved_siblings = {
        name: sys.modules.pop(name)
        for name in sibling_names
        if name in sys.modules
    }
    for name in tuple(sys.modules):
        if name == "hermes_plugins.bounded_engineering" or name.startswith("hermes_plugins.bounded_engineering."):
            sys.modules.pop(name)
    monkeypatch.setattr(
        sys, "path",
        [entry for entry in sys.path if Path(entry or ".").resolve() != PLUGIN_DIR.resolve()],
    )
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(PLUGIN_DIR.parent))

    try:
        manager = PluginManager()
        manager._load_plugin(PluginManifest(
            name="bounded-engineering", key="bounded-engineering",
            kind="backend", source="bundled", path=str(PLUGIN_DIR),
        ))

        loaded = manager._plugins["bounded-engineering"]
        assert loaded.enabled is True, loaded.error
        result = json.loads(registry.dispatch("engineering_verify", {}))
        assert "HERMES_KANBAN_TASK is required" in result["error"]
        assert not sibling_names.intersection(sys.modules)
    finally:
        sys.modules.update(saved_siblings)


def test_real_plugin_discovery_missing_worker_skill_fails_closed(monkeypatch, tmp_path: Path) -> None:
    from hermes_cli import plugins as plugins_module
    from hermes_cli.plugins import PluginManager

    real_exists = Path.exists
    skill_path = PLUGIN_DIR / "skills" / "bounded-engineering-worker" / "SKILL.md"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(PLUGIN_DIR.parent))
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: False if self == skill_path else real_exists(self),
    )
    manager = PluginManager()
    monkeypatch.setattr(plugins_module, "_plugin_manager", manager)
    manager.discover_and_load()
    assert manager.find_plugin_skill("bounded-engineering:bounded-engineering-worker") is None
    loaded = manager._plugins["bounded-engineering"]
    assert loaded.enabled is False
    assert loaded.error and "SKILL.md not found" in loaded.error


def test_register_exposes_only_engineering_status() -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    registered: list[dict] = []
    hooks: list[tuple[str, object]] = []
    cli_commands: list[tuple] = []
    skills: list[tuple[tuple, dict]] = []

    class Context:
        def register_tool(self, **kwargs) -> None:
            registered.append(kwargs)

        def register_hook(self, name: str, handler: object) -> None:
            hooks.append((name, handler))

        def register_cli_command(self, *args) -> None:
            cli_commands.append(args)

        def register_skill(self, *args, **kwargs) -> None:
            skills.append((args, kwargs))

    module.register(Context())

    assert hooks == [
        ("pre_tool_call", module._on_pre_tool_call), ("pre_llm_call", module._on_pre_llm_call),
        ("pre_verify", module._on_pre_verify), ("post_tool_call", module._on_post_tool_call),
        ("on_session_start", module._on_session_start), ("on_session_end", module._on_session_end),
        ("on_session_finalize", module._on_session_finalize), ("post_llm_call", module._on_post_llm_call),
        ("pre_api_request", module._on_pre_api_request),
        ("post_api_request", module._on_post_api_request),
    ]
    assert [item["name"] for item in registered] == ["engineering_status", "engineering_block", "engineering_complete", "engineering_verify"]
    assert cli_commands[0][0] == "engineering"
    assert skills == [(
        ("bounded-engineering-worker", PLUGIN_DIR / "skills" / "bounded-engineering-worker" / "SKILL.md"),
        {"description": "Bounded autonomous engineering worker protocol"},
    )]
    assert registered[0]["toolset"] == "bounded-engineering"
    assert registered[0]["schema"]["parameters"] == {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    block = next(item for item in registered if item["name"] == "engineering_block")
    assert block["schema"]["parameters"]["required"] == ["reason"]


def test_pre_llm_context_is_first_turn_only_deduplicated_and_capped(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    trusted = SimpleNamespace(
        task_id="task-1", run_id=7, task=SimpleNamespace(spec_sha256="a" * 64),
        contract=SimpleNamespace(verification=(SimpleNamespace(id="unit", required=True),)),
        spec={"network_access": {
            "mode": "restricted_proxy",
            "allowed_hosts": ["github.com", "raw.githubusercontent.com"],
        }},
    )
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    assert module._on_pre_llm_call(session_id="s1", is_first_turn=False) is None
    result = module._on_pre_llm_call(session_id="s1", is_first_turn=True)
    assert result and len(result["context"].encode()) <= 1024
    assert "Required verification: unit" in result["context"]
    assert "Network access is permitted only through the configured restricted proxy" in result["context"]
    assert module._on_pre_llm_call(session_id="s1", is_first_turn=True) is None


def test_pre_llm_blocks_docatlas_index_drift(monkeypatch, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context

    index = tmp_path / "docmancer.db"
    index.write_bytes(b"wrong revision")
    trusted = SimpleNamespace(
        task_id="task-1", run_id=7, task=SimpleNamespace(spec_sha256="a" * 64),
        contract=SimpleNamespace(verification=()),
        spec={"benchmark": {"lane": "docatlas_once", "docatlas_index_revision": "0" * 64}},
    )
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_DOCATLAS_INDEX", str(index))

    assert module._on_pre_llm_call(session_id="drift", is_first_turn=True) == {
        "action": "block", "message": "DocAtlas benchmark index revision mismatch",
    }


def test_pre_llm_instructs_docatlas_lane_to_call_once_before_edit(monkeypatch, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context

    index = tmp_path / "docmancer.db"
    index.write_bytes(b"pinned")
    import hashlib
    trusted = SimpleNamespace(
        task_id="task-1", run_id=7, task=SimpleNamespace(spec_sha256="a" * 64),
        contract=SimpleNamespace(verification=()),
        spec={
            "benchmark": {
                "lane": "docatlas_once",
                "docatlas_index_revision": hashlib.sha256(b"pinned").hexdigest(),
            },
            "allowed_paths": {"files": ["tests/diagnostic_labels.json"]},
        },
    )
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_DOCATLAS_INDEX", str(index))

    result = module._on_pre_llm_call(session_id="docatlas", is_first_turn=True)

    assert "mcp_docatlas_benchmark_get_docs_context exactly once" in result["context"]
    assert "module_node_hashes" in result["context"]


def test_pre_verify_nudges_once_only_for_changed_running_bounded_task(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setattr(context, "load_worker_task_context", lambda: SimpleNamespace(task_id="task-1", run_id=7, task=SimpleNamespace(status="running")))
    result = module._on_pre_verify(session_id="s1", changed_paths=["src/a.py"])
    assert result and "engineering_verify" in result["continuation"]
    assert module._on_pre_verify(session_id="s1", changed_paths=["src/a.py"]) is None
    assert module._on_pre_verify(session_id="s2", changed_paths=[]) is None


def test_engineering_status_uses_trusted_worker_context(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    import context
    import status

    trusted = SimpleNamespace(
        task_id="task-1", run_id=7, completion_gate="bounded-engineering/v1",
        task=SimpleNamespace(status="running"),
        contract=SimpleNamespace(verification=()),
        spec={"objective": "fix", "acceptance": [], "allowed_paths": {"roots": ["src"], "files": []}},
    )
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)

    assert json.loads(module._engineering_status()) == status.status_payload(status.build_engineering_status(trusted))


def test_engineering_verify_dispatches_only_contract_declarations_and_persists(monkeypatch, tmp_path: Path) -> None:
    import time
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import completion
    import context
    import snapshot
    import storage
    import telemetry
    import verification
    declaration = SimpleNamespace(id="focused", required=True)
    spec_payload = b'{"allowed_paths":{"files":[],"roots":["src"]}}'
    spec_sha256 = hashlib.sha256(spec_payload).hexdigest()
    spec_dir = tmp_path / "hermes/engineering/specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / f"{spec_sha256}.json").write_bytes(spec_payload)
    trusted = SimpleNamespace(
        workspace_path=str(tmp_path), task=SimpleNamespace(body=f"spec_sha256: {spec_sha256}"),
        contract=SimpleNamespace(verification=(declaration,), workspace={}),
    )
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setattr(snapshot, "capture_snapshot", lambda path: SimpleNamespace(
        digest="a" * 64, entries=(), patch=b""))
    monkeypatch.setattr(snapshot, "enforce_scope", lambda *args, **kwargs: None)
    monkeypatch.setattr(snapshot, "enforce_limits", lambda *args, **kwargs: None)
    evidence = SimpleNamespace(verification_id="focused", exit_code=0, passed=True)
    monkeypatch.setattr(verification, "dispatch_declared_verification", lambda ctx, item, **kwargs: evidence)
    monkeypatch.setattr(verification, "persist_evidence", lambda home, item: tmp_path / "evidence.json")
    aggregate = SimpleNamespace(sha256="b" * 64, payload=b"aggregate")
    monkeypatch.setattr(completion, "build_completion_evidence", lambda *args, **kwargs: aggregate)
    monkeypatch.setattr(storage, "store_immutable_completion_evidence", lambda home, payload: tmp_path / "completion.json")
    monkeypatch.setattr(time, "monotonic", lambda: 1.0)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    monkeypatch.setattr(telemetry, "persist_run_telemetry", lambda *args, **kwargs: {})
    assert json.loads(module._engineering_verify(SimpleNamespace(dispatch_tool=lambda *_: "{}"))) == {
        "completion_evidence_ref": str(tmp_path / "completion.json"), "completion_evidence_sha256": "b" * 64,
        "evidence_refs": [str(tmp_path / "evidence.json")], "snapshot_digest": "a" * 64,
        "verification_ids": ["focused"],
        "verification_metrics": {"required": 1, "passed": 1, "failed": 0,
                                 "elapsed_ms": 0},
        "workspace_metrics": {"changed_files": 0, "changed_bytes": 0,
                              "final_snapshot_id": "a" * 64, "artifacts": []},
    }


def test_engineering_block_uses_trusted_expected_run(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    from hermes_cli import kanban_db
    trusted = SimpleNamespace(task_id="task-1", run_id=7)
    calls = []
    conn = SimpleNamespace(close=lambda: calls.append("close"))
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setattr(kanban_db, "connect", lambda: conn)
    monkeypatch.setattr(kanban_db, "block_task", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    monkeypatch.setattr(kanban_db, "VALID_BLOCK_KINDS", {"external"})

    assert json.loads(module._engineering_block("need a human")) == {"run_id": 7, "status": "blocked", "task_id": "task-1"}
    assert calls[0][1]["expected_run_id"] == 7
    assert calls[0][1]["kind"] == "needs_input"
    assert calls[-1] == "close"


def test_registered_engineering_block_unpacks_registry_args(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registered = []

    class Context:
        def register_tool(self, **kwargs):
            registered.append(kwargs)

        def register_hook(self, *args):
            pass

        def register_cli_command(self, *args):
            pass

        def register_skill(self, *args, **kwargs):
            pass

    def block(reason):
        if not reason:
            raise ValueError("engineering_block requires a non-empty reason")
        return reason
    monkeypatch.setattr(module, "_engineering_block", block)
    module.register(Context())
    handler = next(item["handler"] for item in registered if item["name"] == "engineering_block")

    assert handler({"reason": "blocked"}) == "blocked"
    with pytest.raises(ValueError, match="non-empty reason"):
        handler({})


def test_engineering_complete_uses_fresh_canonical_evidence_and_trusted_gate(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    from hermes_cli import kanban_db
    trusted = SimpleNamespace(task_id="task-1", run_id=7, completion_gate="bounded", task=SimpleNamespace(status="running"), run=SimpleNamespace(metadata={}))
    calls = []
    conn = SimpleNamespace(close=lambda: calls.append("close"))
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setattr(module, "_engineering_verify", lambda ctx: json.dumps({"completion_evidence_sha256": "c" * 64}))
    monkeypatch.setattr(kanban_db, "connect", lambda: conn)
    monkeypatch.setattr(kanban_db, "complete_task_with_gate", lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    assert json.loads(module._engineering_complete(SimpleNamespace())) == {"completion_evidence_sha256": "c" * 64, "run_id": 7, "status": "done", "task_id": "task-1"}
    assert calls[0][1]["gate_name"] == "bounded"
    assert calls[0][1]["expected_run_id"] == 7
    assert calls[-1] == "close"


def test_engineering_complete_rejects_invalid_evidence_or_stale_run(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    from hermes_cli import kanban_db
    trusted = SimpleNamespace(task_id="task-1", run_id=7, completion_gate="bounded", task=SimpleNamespace(status="running"), run=SimpleNamespace(metadata={}))
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setattr(module, "_engineering_verify", lambda ctx: json.dumps({"completion_evidence_sha256": "invalid"}))
    with pytest.raises(RuntimeError, match="canonical completion evidence"):
        module._engineering_complete(SimpleNamespace())

    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(module, "_engineering_verify", lambda ctx: json.dumps({"completion_evidence_sha256": "c" * 64}))
    monkeypatch.setattr(kanban_db, "connect", lambda: conn)
    monkeypatch.setattr(kanban_db, "complete_task_with_gate", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="stale run or gate mismatch"):
        module._engineering_complete(SimpleNamespace())


def test_engineering_complete_idempotently_returns_gate_recorded_done_run(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    trusted = SimpleNamespace(
        task_id="task-1", run_id=7, completion_gate="bounded",
        task=SimpleNamespace(status="done"), run=SimpleNamespace(metadata={"completion_evidence_sha256": "d" * 64}),
    )
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setattr(module, "_engineering_verify", lambda ctx: pytest.fail("done retry must not verify"))

    assert json.loads(module._engineering_complete(SimpleNamespace())) == {"completion_evidence_sha256": "d" * 64, "run_id": 7, "status": "done", "task_id": "task-1"}


def test_non_allowlisted_tools_are_blocked_only_for_bounded_worker(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    import context
    monkeypatch.setattr(context, "load_worker_task_context", lambda: SimpleNamespace(
        contract=SimpleNamespace(policy={"forbidden_tools": ()}, execution={"allow_delegate_task": False})
    ))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    assert module._on_pre_tool_call(tool_name="kanban_complete") == {
        "action": "block", "message": "bounded worker must use the engineering protocol"
    }
    assert module._on_pre_tool_call(tool_name="kanban_show") == {
        "action": "block", "message": "tool is not in the bounded worker allowlist: kanban_show"
    }
    assert module._on_pre_tool_call(tool_name="process") == {
        "action": "block", "message": "tool is not in the bounded worker allowlist: process"
    }
    assert module._on_pre_tool_call(tool_name="read_file") is None
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert module._on_pre_tool_call(tool_name="kanban_complete") is None


def test_bounded_hook_exception_fails_closed_but_ordinary_session_is_unaffected(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_on_pre_tool_call_impl", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    assert module._on_pre_tool_call(tool_name="read_file") == {"action": "block", "message": "bounded worker policy guard failed"}
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert module._on_pre_tool_call(tool_name="read_file") is None


def test_contract_forbidden_tool_and_missing_context_fail_closed(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setattr(context, "load_worker_task_context", lambda: SimpleNamespace(
        contract=SimpleNamespace(policy={"forbidden_tools": ("terminal",)}, execution={"allow_delegate_task": False})
    ))
    assert module._on_pre_tool_call(tool_name="terminal") == {
        "action": "block", "message": "tool is forbidden by bounded contract: terminal"
    }
    assert module._on_pre_tool_call(tool_name="delegate_task") == {
        "action": "block", "message": "delegate_task is disabled by bounded contract"
    }
    monkeypatch.setattr(context, "load_worker_task_context", lambda: (_ for _ in ()).throw(RuntimeError("broken")))
    assert module._on_pre_tool_call(tool_name="read_file") == {
        "action": "block", "message": "bounded worker trusted context is unavailable"
    }


def test_terminal_policy_rejects_forbidden_tokens_and_git_subcommands(monkeypatch) -> None:
    for name in tuple(sys.modules):
        if name == "bounded_engineering_plugin" or name.startswith("bounded_engineering_plugin."):
            sys.modules.pop(name)
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setattr(context, "load_worker_task_context", lambda: SimpleNamespace(
        contract=SimpleNamespace(
            policy={"forbidden_tools": (), "forbidden_command_tokens": ["curl"], "forbidden_git_subcommands": ["reset"]},
            execution={"allow_delegate_task": False},
        )
    ))

    assert module._on_pre_tool_call(tool_name="terminal", args={"command": "curl https://example.test"})["message"] == "terminal command contains a forbidden contract token"
    assert module._on_pre_tool_call(tool_name="terminal", args={"command": "git reset --hard"})["message"] == "terminal command contains a forbidden git subcommand"
    assert module._on_pre_tool_call(tool_name="terminal", args={"command": "git commit -m bounded"})["message"] == "terminal command contains a forbidden git subcommand"
    assert module._on_pre_tool_call(tool_name="terminal", args={"command": "git push origin HEAD"})["message"] == "terminal command contains a forbidden git subcommand"
    assert module._on_pre_tool_call(tool_name="terminal", args={"command": "git status --short"}) is None


def test_docatlas_uses_prefixed_name_and_lane_guard(monkeypatch, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    import snapshot
    import telemetry

    trusted = SimpleNamespace(
        spec={"benchmark": {"lane": "docatlas_once"}},
        run=SimpleNamespace(metadata={}), workspace_path=tmp_path,
        contract=SimpleNamespace(policy={"forbidden_tools": ()}, execution={"allow_delegate_task": False}),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setattr(telemetry, "record_tool_attempt", lambda *args, **kwargs: {})
    monkeypatch.setattr(snapshot, "capture_snapshot", lambda path: SimpleNamespace(entries=()))

    assert module._on_pre_tool_call(
        tool_name="mcp_docatlas_benchmark_get_docs_context",
    ) is None
    assert module._on_pre_tool_call(tool_name="get_docs_context")["action"] == "block"
    trusted.spec["benchmark"]["lane"] = "repo_only"
    assert module._on_pre_tool_call(
        tool_name="mcp_docatlas_benchmark_get_docs_context",
    )["message"] == "DocAtlas is unavailable in this benchmark lane"


def test_docatlas_blocks_dirty_workspace(monkeypatch, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import context
    import snapshot
    import telemetry

    trusted = SimpleNamespace(
        spec={"benchmark": {"lane": "docatlas_once"}},
        run=SimpleNamespace(metadata={}), workspace_path=tmp_path,
        contract=SimpleNamespace(policy={"forbidden_tools": ()}, execution={"allow_delegate_task": False}),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setattr(context, "load_worker_task_context", lambda: trusted)
    monkeypatch.setattr(telemetry, "record_tool_attempt", lambda *args, **kwargs: {})
    monkeypatch.setattr(snapshot, "capture_snapshot", lambda path: SimpleNamespace(entries=("changed",)))

    assert module._on_pre_tool_call(
        tool_name="mcp_docatlas_benchmark_get_docs_context",
    )["message"] == "DocAtlas must be called before workspace changes"
