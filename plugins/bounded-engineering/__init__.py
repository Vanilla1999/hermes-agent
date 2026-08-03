"""Bounded-engineering plugin entry point."""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

_DIRECT_LIFECYCLE_TOOLS = frozenset({
    "kanban_block", "kanban_complete", "kanban_create", "kanban_link", "kanban_unblock",
})
_BOUNDED_ALLOWED_TOOLS = frozenset({
    "engineering_block", "engineering_complete", "engineering_status", "engineering_verify",
    "patch", "read_file", "search_files", "terminal", "write_file",
})
_DOCATLAS_TOOL = "mcp_docatlas_benchmark_get_docs_context"
_ALWAYS_FORBIDDEN_GIT_SUBCOMMANDS = frozenset({"commit", "push"})


_INJECTED_LLM_CONTEXTS: set[tuple[str, int, str]] = set()
_PREVERIFY_NUDGED: set[tuple[str, int, str]] = set()


def _terminal_block_message(args, policy) -> str | None:
    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip():
        return "terminal command is required for bounded worker"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "terminal command could not be parsed for bounded worker"
    forbidden_tokens = policy.get("forbidden_command_tokens", ())
    if not isinstance(forbidden_tokens, (list, tuple)) or not all(isinstance(token, str) and token for token in forbidden_tokens):
        return "bounded contract has invalid forbidden_command_tokens"
    if any(token in forbidden_tokens for token in tokens):
        return "terminal command contains a forbidden contract token"
    forbidden_git = policy.get("forbidden_git_subcommands", ())
    if not isinstance(forbidden_git, (list, tuple)) or not all(isinstance(token, str) and token for token in forbidden_git):
        return "bounded contract has invalid forbidden_git_subcommands"
    forbidden_git = frozenset(forbidden_git) | _ALWAYS_FORBIDDEN_GIT_SUBCOMMANDS
    if any(tokens[index] == "git" and index + 1 < len(tokens) and tokens[index + 1] in forbidden_git for index in range(len(tokens))):
        return "terminal command contains a forbidden git subcommand"
    return None


def _on_pre_tool_call_impl(*, tool_name: str, args=None, **kwargs):
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .context import load_worker_task_context
        else:  # Direct-module tests and scripts.
            from context import load_worker_task_context
        trusted = load_worker_task_context()
    except Exception:
        return {"action": "block", "message": "bounded worker trusted context is unavailable"}
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .telemetry import record_tool_attempt
        else:
            from telemetry import record_tool_attempt
        record_tool_attempt(trusted, tool_name=tool_name)
    except Exception:
        pass
    if tool_name in _DIRECT_LIFECYCLE_TOOLS:
        return {"action": "block", "message": "bounded worker must use the engineering protocol"}
    trusted_spec = getattr(trusted, "spec", None)
    benchmark = trusted_spec.get("benchmark") if isinstance(trusted_spec, dict) else None
    lane = benchmark.get("lane") if isinstance(benchmark, dict) else None
    if tool_name == _DOCATLAS_TOOL:
        if lane != "docatlas_once":
            return {"action": "block", "message": "DocAtlas is unavailable in this benchmark lane"}
        run = getattr(trusted, "run", None)
        metadata = run.metadata if run is not None and isinstance(run.metadata, dict) else {}
        docatlas = metadata.get("docatlas") if isinstance(metadata.get("docatlas"), dict) else {}
        if metadata.get("first_edit_at_ms") is not None:
            return {"action": "block", "message": "DocAtlas must be called before the first edit"}
        try:
            if __package__ and __import__("sys").modules.get(__package__) is not None:
                from .snapshot import capture_snapshot
            else:
                from snapshot import capture_snapshot
            if capture_snapshot(trusted.workspace_path).entries:
                return {"action": "block", "message": "DocAtlas must be called before workspace changes"}
        except Exception:
            return {"action": "block", "message": "workspace state could not be verified before DocAtlas"}
        if docatlas.get("call_count", 0) >= 1:
            return {"action": "block", "message": "docatlas_once permits exactly one call"}
    forbidden = trusted.contract.policy.get("forbidden_tools", ())
    if tool_name in forbidden:
        return {"action": "block", "message": f"tool is forbidden by bounded contract: {tool_name}"}
    if tool_name == "delegate_task" and not trusted.contract.execution.get("allow_delegate_task", False):
        return {"action": "block", "message": "delegate_task is disabled by bounded contract"}
    if tool_name not in _BOUNDED_ALLOWED_TOOLS and tool_name != _DOCATLAS_TOOL:
        return {"action": "block", "message": f"tool is not in the bounded worker allowlist: {tool_name}"}
    if tool_name == "terminal":
        message = _terminal_block_message(args, trusted.contract.policy)
        if message:
            return {"action": "block", "message": message}
    return None


def _on_pre_tool_call(*, tool_name: str, args=None, **kwargs):
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    try:
        return _on_pre_tool_call_impl(tool_name=tool_name, args=args, **kwargs)
    except Exception:
        return {"action": "block", "message": "bounded worker policy guard failed"}


def _on_pre_llm_call(*, session_id: str, is_first_turn: bool = False, **kwargs):
    if not os.environ.get("HERMES_KANBAN_TASK") or not is_first_turn or not isinstance(session_id, str):
        return None
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .context import load_worker_task_context
        else:  # Direct-module tests and scripts.
            from context import load_worker_task_context
        trusted = load_worker_task_context()
        benchmark = trusted.spec.get("benchmark") if isinstance(trusted.spec, dict) else None
        if isinstance(benchmark, dict) and benchmark.get("lane") == "docatlas_once":
            import hashlib
            index = Path(os.environ.get(
                "HERMES_DOCATLAS_INDEX",
                "/srv/hermes-lab/operator/docatlas-baseline-index/docmancer.db",
            ))
            if index.is_symlink() or not index.is_file():
                return {"action": "block", "message": "DocAtlas benchmark index is unavailable"}
            if hashlib.sha256(index.read_bytes()).hexdigest() != benchmark.get("docatlas_index_revision"):
                return {"action": "block", "message": "DocAtlas benchmark index revision mismatch"}
        key = (trusted.task_id, trusted.run_id, session_id)
        if key in _INJECTED_LLM_CONTEXTS:
            return None
        required = ", ".join(item.id for item in trusted.contract.verification if item.required) or "none"
        spec_digest = str(getattr(trusted.task, "spec_sha256", "unknown"))[:12]
        network_access = trusted.spec.get("network_access") if isinstance(trusted.spec, dict) else None
        if isinstance(network_access, dict) and network_access.get("mode") == "restricted_proxy":
            hosts = ", ".join(network_access.get("allowed_hosts", []))
            network_rule = (
                f"Network access is permitted only through the configured restricted proxy to: {hosts}. "
                "Direct network access remains prohibited."
            )
        else:
            network_rule = "Do not use network."
        docatlas_rule = (
            " Before inspecting broadly or editing, call "
            "mcp_docatlas_benchmark_get_docs_context exactly once."
            if isinstance(benchmark, dict) and benchmark.get("lane") == "docatlas_once"
            else ""
        )
        diagnostic_rule = (
            " If a test node is added, removed, or renamed, update the corresponding "
            "tests/diagnostic_labels.json module_node_hashes entry before verification."
            if isinstance(trusted.spec, dict)
            and "tests/diagnostic_labels.json" in trusted.spec.get("allowed_paths", {}).get("files", ())
            else ""
        )
        text = (
            f"Bounded task {trusted.task_id}; run {trusted.run_id}; spec {spec_digest}.\n"
            f"Required verification: {required}.\n"
            "Use terminal for shell commands and the file tools for repository inspection. "
            "Use engineering_status, engineering_verify, engineering_complete, or engineering_block. "
            f"{network_rule}{docatlas_rule}{diagnostic_rule} Do not use direct Kanban lifecycle tools, history rewrite, or child agents."
        )
        if len(text.encode()) > 1024:
            raise RuntimeError("bounded first-run context exceeds 1 KiB")
        _INJECTED_LLM_CONTEXTS.add(key)
        return {"context": text}
    except Exception:
        return {"context": "Bounded worker context unavailable: use engineering_block."}


def _on_pre_verify(*, session_id: str, changed_paths=None, **kwargs):
    if not os.environ.get("HERMES_KANBAN_TASK") or not isinstance(session_id, str) or not changed_paths:
        return None
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .context import load_worker_task_context
        else:  # Direct-module tests and scripts.
            from context import load_worker_task_context
        trusted = load_worker_task_context()
        if trusted.task.status != "running":
            return None
        key = (trusted.task_id, trusted.run_id, session_id)
        if key in _PREVERIFY_NUDGED:
            return None
        _PREVERIFY_NUDGED.add(key)
        return {"continuation": "Use engineering_verify; if it succeeds, use engineering_complete. If work cannot proceed, use engineering_block."}
    except Exception:
        return None


def _on_post_tool_call(*, tool_name: str = "", args=None, result=None, session_id: str = "", **kwargs):
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .audit import append_audit
        else:  # Direct-module tests and scripts.
            from audit import append_audit
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .context import load_worker_task_context
        else:  # Direct-module tests and scripts.
            from context import load_worker_task_context
        trusted = load_worker_task_context(allow_blocked=True)
        append_audit(Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser(), task_id=trusted.task_id, run_id=trusted.run_id, event="tool", fields={"args": args, "result": result, "session_id": session_id, "tool_name": tool_name})
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .telemetry import record_tool_result
        else:
            from telemetry import record_tool_result
        record_tool_result(trusted, result=result, status=kwargs.get("status"))
        if tool_name == "terminal":
            if __package__ and __import__("sys").modules.get(__package__) is not None:
                from .snapshot import capture_snapshot
                from .telemetry import record_workspace_edit
            else:
                from snapshot import capture_snapshot
                from telemetry import record_workspace_edit
            if capture_snapshot(trusted.workspace_path).entries:
                record_workspace_edit(trusted)
        if tool_name == _DOCATLAS_TOOL:
            if __package__ and __import__("sys").modules.get(__package__) is not None:
                from .telemetry import record_docatlas_call
            else:
                from telemetry import record_docatlas_call
            record_docatlas_call(trusted, args=args, result=result)
    except Exception:
        pass
    return None


def _on_metric(event: str, **kwargs):
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .audit import append_audit
        else:  # Direct-module tests and scripts.
            from audit import append_audit
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .context import load_worker_task_context
        else:  # Direct-module tests and scripts.
            from context import load_worker_task_context
        trusted = load_worker_task_context(allow_blocked=True)
        safe = {name: value for name, value in kwargs.items() if name not in {"messages", "conversation_history", "response", "user_message", "result"}}
        append_audit(Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser(), task_id=trusted.task_id, run_id=trusted.run_id, event=event, fields=safe)
    except Exception:
        pass
    return None


def _on_session_start(**kwargs):
    if os.environ.get("HERMES_KANBAN_TASK"):
        try:
            if __package__ and __import__("sys").modules.get(__package__) is not None:
                from .context import load_worker_task_context
                from .telemetry import record_session_start
            else:
                from context import load_worker_task_context
                from telemetry import record_session_start
            record_session_start(load_worker_task_context(allow_blocked=True), **kwargs)
        except Exception:
            pass
    return _on_metric("session_start", **kwargs)


def _on_session_end(**kwargs):
    return _on_metric("session_end", **kwargs)


def _on_session_finalize(**kwargs):
    return _on_metric("session_finalize", **kwargs)


def _on_post_llm_call(**kwargs):
    return _on_metric("llm", **kwargs)


def _on_post_api_request(**kwargs):
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .context import load_worker_task_context
            from .telemetry import record_api_request
        else:
            from context import load_worker_task_context
            from telemetry import record_api_request
        record_api_request(load_worker_task_context(allow_blocked=True), **kwargs)
    except Exception:
        pass
    return _on_metric("api", **kwargs)


def _on_pre_api_request(**kwargs):
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .context import load_worker_task_context
            from .telemetry import record_api_request_start
        else:
            from context import load_worker_task_context
            from telemetry import record_api_request_start
        record_api_request_start(load_worker_task_context(allow_blocked=True), **kwargs)
    except Exception:
        pass
    return _on_metric("api_start", **kwargs)


def _engineering_status(*args, **kwargs) -> str:
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .context import load_worker_task_context
    else:  # Direct-module tests and scripts.
        from context import load_worker_task_context
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .status import build_engineering_status, status_payload
    else:  # Direct-module tests and scripts.
        from status import build_engineering_status, status_payload

    return json.dumps(status_payload(build_engineering_status(load_worker_task_context())), sort_keys=True)


def _engineering_block(reason=None, **kwargs) -> str:
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .context import load_worker_task_context
    else:  # Direct-module tests and scripts.
        from context import load_worker_task_context
    from hermes_cli.kanban_db import block_task, connect

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("engineering_block requires a non-empty reason")
    if len(reason) > 2000:
        raise ValueError("engineering_block reason exceeds 2000 characters")
    trusted = load_worker_task_context()
    conn = connect()
    try:
        if not block_task(conn, trusted.task_id, reason=reason.strip(), kind="needs_input", expected_run_id=trusted.run_id):
            raise RuntimeError("engineering_block rejected stale or unavailable trusted run")
    finally:
        conn.close()
    return json.dumps({"run_id": trusted.run_id, "status": "blocked", "task_id": trusted.task_id}, sort_keys=True)


def _engineering_block_from_args(args, **kwargs) -> str:
    if not isinstance(args, dict):
        raise ValueError("engineering_block arguments must be an object")
    return _engineering_block(args.get("reason"))


def _engineering_complete(ctx, *args, **kwargs) -> str:
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .context import load_worker_task_context
    else:  # Direct-module tests and scripts.
        from context import load_worker_task_context
    from hermes_cli.kanban_db import complete_task_with_gate, connect

    trusted = load_worker_task_context()
    if trusted.task.status == "done":
        metadata = trusted.run.metadata if isinstance(trusted.run.metadata, dict) else {}
        evidence_sha256 = metadata.get("completion_evidence_sha256")
        if not isinstance(evidence_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
            raise RuntimeError("completed trusted run is missing canonical completion evidence")
        return json.dumps({"completion_evidence_sha256": evidence_sha256, "run_id": trusted.run_id, "status": "done", "task_id": trusted.task_id}, sort_keys=True)
    verification = json.loads(_engineering_verify(ctx))
    evidence_sha256 = verification.get("completion_evidence_sha256")
    if not isinstance(evidence_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise RuntimeError("engineering_verify did not return canonical completion evidence")
    conn = connect()
    try:
        metadata = dict(trusted.run.metadata) if isinstance(trusted.run.metadata, dict) else {}
        metadata.update({
            "completion_evidence_ref": verification.get("completion_evidence_ref"),
            "verification": verification.get("verification_metrics"),
            "workspace": verification.get("workspace_metrics"),
        })
        if not complete_task_with_gate(
            conn, trusted.task_id, gate_name=trusted.completion_gate,
            evidence_sha256=evidence_sha256, expected_run_id=trusted.run_id,
            summary="bounded engineering completion", metadata=metadata,
        ):
            raise RuntimeError("engineering_complete rejected stale run or gate mismatch")
    finally:
        conn.close()
    return json.dumps({"completion_evidence_sha256": evidence_sha256, "run_id": trusted.run_id, "status": "done", "task_id": trusted.task_id}, sort_keys=True)


def _engineering_verify(ctx, *args, **kwargs) -> str:
    import hashlib
    import time
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .completion import build_completion_evidence
        from .context import load_worker_task_context
        from .snapshot import capture_snapshot, enforce_limits, enforce_scope
        from .storage import store_immutable_completion_evidence
        from .verification import VerificationDispatchError, dispatch_declared_verification, persist_evidence
    else:  # Direct-module tests and scripts.
        from completion import build_completion_evidence
        from context import load_worker_task_context
        from snapshot import capture_snapshot, enforce_limits, enforce_scope
        from storage import store_immutable_completion_evidence
        from verification import VerificationDispatchError, dispatch_declared_verification, persist_evidence

    trusted = load_worker_task_context()
    snapshot = capture_snapshot(trusted.workspace_path)
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    body = trusted.task.body if isinstance(trusted.task.body, str) else ""
    match = re.search(r"(?m)^spec_sha256: ([0-9a-f]{64})$", body)
    if match is None:
        raise RuntimeError("trusted task is missing canonical spec digest")
    spec_sha256 = match.group(1)
    spec_path = hermes_home / "engineering" / "specs" / f"{spec_sha256}.json"
    spec_payload = spec_path.read_bytes()
    if hashlib.sha256(spec_payload).hexdigest() != spec_sha256:
        raise RuntimeError("immutable task spec digest mismatch")
    spec_record = json.loads(spec_payload)
    allowed = spec_record.get("allowed_paths")
    if not isinstance(allowed, dict):
        raise RuntimeError("immutable task spec has invalid allowed_paths")
    enforce_scope(
        snapshot,
        allowed_roots=tuple(allowed.get("roots", ())),
        allowed_files=tuple(allowed.get("files", ())),
    )
    enforce_limits(
        snapshot,
        max_changed_files=trusted.contract.workspace.get("max_changed_files", 100),
        max_total_changed_bytes=trusted.contract.workspace.get("max_total_changed_bytes", 2 * 1024 * 1024),
    )
    evidence = []
    refs = []
    verification_started = time.monotonic()
    for declaration in trusted.contract.verification:
        item = dispatch_declared_verification(ctx, declaration, snapshot_digest=snapshot.digest)
        evidence.append(item)
        refs.append(str(persist_evidence(hermes_home, item)))
        if not item.passed:
            raise VerificationDispatchError(
                f"declared verification failed: {item.verification_id} (exit code {item.exit_code})"
            )
    aggregate = build_completion_evidence(
        tuple(evidence), snapshot_digest=snapshot.digest,
        required_ids=tuple(item.id for item in trusted.contract.verification if item.required),
    )
    aggregate_ref = store_immutable_completion_evidence(hermes_home, aggregate.payload)
    verification_metrics = {
        "required": sum(1 for item in trusted.contract.verification if item.required),
        "passed": sum(1 for item in evidence if item.exit_code == 0),
        "failed": sum(1 for item in evidence if item.exit_code != 0),
        "elapsed_ms": max(0, int((time.monotonic() - verification_started) * 1000)),
    }
    workspace_metrics = {
        "changed_files": len(snapshot.entries),
        "changed_bytes": len(snapshot.patch),
        "final_snapshot_id": snapshot.digest,
        "artifacts": [
            {"path": entry.path, "sha256": entry.sha256, "mode": entry.mode}
            for entry in snapshot.entries
            if entry.path in set(allowed.get("files", ())) and entry.status != "deleted"
        ],
    }
    try:
        if __package__ and __import__("sys").modules.get(__package__) is not None:
            from .telemetry import persist_run_telemetry
        else:
            from telemetry import persist_run_telemetry
        persist_run_telemetry(trusted, {
            "verification": verification_metrics, "workspace": workspace_metrics,
        })
    except Exception:
        pass
    return json.dumps({
        "completion_evidence_ref": str(aggregate_ref),
        "completion_evidence_sha256": aggregate.sha256,
        "evidence_refs": refs,
        "snapshot_digest": snapshot.digest,
        "verification_ids": [item.verification_id for item in evidence],
        "verification_metrics": verification_metrics,
        "workspace_metrics": workspace_metrics,
    }, sort_keys=True)


def register(ctx) -> None:
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .engineering_cli import entrypoint as run_engineering_cli, setup_parser as setup_engineering_parser
    else:  # Direct-module tests and scripts.
        from engineering_cli import entrypoint as run_engineering_cli, setup_parser as setup_engineering_parser

    ctx.register_skill(
        "bounded-engineering-worker",
        Path(__file__).resolve().parent / "skills" / "bounded-engineering-worker" / "SKILL.md",
        description="Bounded autonomous engineering worker protocol",
    )
    ctx.register_cli_command(
        "engineering",
        "Deterministic bounded-engineering controls",
        setup_engineering_parser,
        run_engineering_cli,
    )
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_verify", _on_pre_verify)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_tool(
        name="engineering_status",
        toolset="bounded-engineering",
        schema={"description": "Return trusted bounded task status and contract scope.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        handler=_engineering_status,
    )
    ctx.register_tool(
        name="engineering_block",
        toolset="bounded-engineering",
        schema={"description": "Block the current bounded run with a non-empty reason.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}},
        handler=_engineering_block_from_args,
    )
    ctx.register_tool(
        name="engineering_complete",
        toolset="bounded-engineering",
        schema={"description": "Verify and complete the current bounded task.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        handler=lambda *args, **kwargs: _engineering_complete(ctx, *args, **kwargs),
    )
    ctx.register_tool(
        name="engineering_verify",
        toolset="bounded-engineering",
        schema={"description": "Run contract-declared verification and persist evidence.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        handler=lambda *args, **kwargs: _engineering_verify(ctx, *args, **kwargs),
    )
