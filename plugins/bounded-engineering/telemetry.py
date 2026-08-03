"""Bounded, provider-neutral telemetry persisted on the active Kanban run."""
from __future__ import annotations

import json
import hashlib
import os
import time
from pathlib import Path
from typing import Any, Callable


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _text(value: Any) -> str | None:
    return value[:200] if isinstance(value, str) and value.strip() else None


def _is_docatlas_tool_name(value: Any) -> bool:
    return value == "mcp_docatlas_benchmark_get_docs_context"


def _provider_tool_names(event: dict[str, Any]) -> list[str] | None:
    request = event.get("request")
    body = request.get("body") if isinstance(request, dict) else None
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list):
        return None
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name and len(name) <= 200:
            names.append(name)
    return names[:100]


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if key == "missing_metrics_reasons" and isinstance(value, dict):
            merged[key] = dict(value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _update_run_telemetry(
    trusted, update: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Atomically update telemetry for the durable trusted run identity."""
    from hermes_cli.kanban_db import connect, write_txn

    conn = connect()
    try:
        with write_txn(conn):
            row = conn.execute(
                "SELECT metadata FROM task_runs WHERE id = ? AND task_id = ?",
                (trusted.run_id, trusted.task_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("trusted Kanban run is unavailable")
            try:
                current = json.loads(row["metadata"]) if row["metadata"] else {}
            except (TypeError, json.JSONDecodeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            merged = _merge(current, update(current))
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ? AND task_id = ?",
                (json.dumps(merged, ensure_ascii=False, sort_keys=True),
                 trusted.run_id, trusted.task_id),
            )
            return merged
    finally:
        conn.close()


def persist_run_telemetry(trusted, patch: dict[str, Any]) -> dict[str, Any]:
    return _update_run_telemetry(trusted, lambda current: patch)


def record_api_request_start(trusted, **event: Any) -> dict[str, Any]:
    """Persist request identity before provider execution can fail or be interrupted."""
    def update(current: dict[str, Any]) -> dict[str, Any]:
        prior = current.get("model") if isinstance(current.get("model"), dict) else {}
        model = {
            "api_requests": (_count(prior.get("api_requests")) or 0) + 1,
            "provider": _text(event.get("provider")),
            "model": _text(event.get("model")),
            "reasoning": _text(event.get("reasoning_effort")) or _text(event.get("reasoning"))
            or _text(os.environ.get("HERMES_BOUNDED_REASONING_EFFORT")),
        }
        provider_tools = _provider_tool_names(event)
        if provider_tools is not None:
            model["provider_tool_count"] = len(provider_tools)
            model["provider_tools"] = provider_tools
            request = event.get("request")
            body = request.get("body") if isinstance(request, dict) else None
            tools = body.get("tools") if isinstance(body, dict) else None
            model["tool_catalog_bytes"] = len(json.dumps(tools, sort_keys=True, separators=(",", ":")).encode())
            docatlas_tools = [tool for tool in tools if isinstance(tool, dict) and (
                _is_docatlas_tool_name(tool.get("name"))
                or isinstance(tool.get("function"), dict)
                and _is_docatlas_tool_name(tool["function"].get("name"))
            )]
            model["mcp_tool_catalog_bytes"] = (
                len(json.dumps(docatlas_tools, sort_keys=True, separators=(",", ":")).encode())
                if docatlas_tools else 0
            )
        return {"model": model}
    return _update_run_telemetry(trusted, update)


def record_api_request(trusted, **event: Any) -> dict[str, Any]:
    """Persist exact provider usage from one pinned-Hermes post_api_request hook."""
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "cache_read_tokens": ("cache_read_tokens",),
        "cache_write_tokens": ("cache_write_tokens",),
        "reasoning_tokens": ("reasoning_tokens",),
    }
    exact: dict[str, int | None] = {}
    missing_updates: dict[str, str | None] = {}
    for target, names in aliases.items():
        value = next((_count(usage.get(name)) for name in names if _count(usage.get(name)) is not None), None)
        exact[target] = value
        if value is None:
            missing_updates[f"model.{target}"] = "provider_usage_metric_unavailable"
        else:
            missing_updates[f"model.{target}"] = None

    def update(current: dict[str, Any]) -> dict[str, Any]:
        prior = current.get("model") if isinstance(current.get("model"), dict) else {}
        missing = dict(current.get("missing_metrics_reasons", {})) \
            if isinstance(current.get("missing_metrics_reasons"), dict) else {}
        for name, value in missing_updates.items():
            if value is None:
                missing.pop(name, None)
            else:
                missing[name] = value
        model: dict[str, Any] = {
            "api_requests": _count(prior.get("api_requests")) or 1,
            "provider": _text(event.get("provider")),
            "model": _text(event.get("response_model")) or _text(event.get("model")),
        }
        for name, value in exact.items():
            previous = _count(prior.get(name)) or 0
            model[name] = previous + value if value is not None else prior.get(name)
        return {"model": model, "missing_metrics_reasons": missing}
    return _update_run_telemetry(trusted, update)


def record_tool_attempt(trusted, *, tool_name: str | None = None) -> dict[str, Any]:
    def update(current: dict[str, Any]) -> dict[str, Any]:
        attempted = (_count(current.get("tool_calls_attempted")) or 0) + 1
        succeeded = _count(current.get("tool_calls_succeeded")) or 0
        failed = _count(current.get("tool_calls_failed")) or 0
        patch = {
            "tool_calls": attempted,
            "tool_calls_attempted": attempted,
            "tool_calls_incomplete": max(0, attempted - succeeded - failed),
        }
        counts = current.get("tool_call_counts") if isinstance(current.get("tool_call_counts"), dict) else {}
        patch["tool_call_counts"] = {
            **counts,
            str(tool_name or "unknown"): (_count(counts.get(str(tool_name or "unknown"))) or 0) + 1,
        }
        if _is_docatlas_tool_name(tool_name):
            patch["docatlas_started_at_ms"] = int(time.time() * 1000)
        if tool_name in {"patch", "write_file"} and current.get("first_edit_at_ms") is None:
            patch["first_edit_at_ms"] = int(time.time() * 1000)
        return patch
    return _update_run_telemetry(trusted, update)


def record_workspace_edit(trusted) -> dict[str, Any]:
    """Record the first observed workspace change after a terminal call."""
    def update(current: dict[str, Any]) -> dict[str, Any]:
        if current.get("first_edit_at_ms") is not None:
            return {}
        return {"first_edit_at_ms": int(time.time() * 1000)}
    return _update_run_telemetry(trusted, update)


def record_tool_result(trusted, *, result: Any = None, status: Any = None) -> dict[str, Any]:
    payload = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    failed_result = (
        isinstance(error, str) and bool(error.strip())
    ) or status in {"blocked", "cancelled", "error"}

    def update(current: dict[str, Any]) -> dict[str, Any]:
        succeeded = _count(current.get("tool_calls_succeeded")) or 0
        failed = _count(current.get("tool_calls_failed")) or 0
        attempted = max(
            _count(current.get("tool_calls_attempted")) or 0,
            succeeded + failed + 1,
        )
        if failed_result:
            failed += 1
        else:
            succeeded += 1
        patch: dict[str, Any] = {
            "tool_calls": attempted,
            "tool_calls_attempted": attempted,
            "tool_calls_succeeded": succeeded,
            "tool_calls_failed": failed,
            "tool_calls_incomplete": max(0, attempted - succeeded - failed),
        }
        if isinstance(error, str) and error.strip():
            from agent.redact import redact_sensitive_text
            prior_errors = current.get("tool_errors")
            errors = list(prior_errors) if isinstance(prior_errors, list) else []
            errors.append(redact_sensitive_text(error.strip(), force=True)[:2000])
            patch["tool_errors"] = errors[-10:]
        return patch
    return _update_run_telemetry(trusted, update)


def record_session_start(trusted, *, session_id: Any = None, **event: Any) -> dict[str, Any]:
    body = trusted.task.body if isinstance(getattr(trusted.task, "body", None), str) else ""
    def update(current: dict[str, Any]) -> dict[str, Any]:
        model = current.get("model") if isinstance(current.get("model"), dict) else {}
        return {
            "model": {"sessions": (_count(model.get("sessions")) or 0) + 1},
            "context_bytes": len(body.encode("utf-8")),
            "session_started_at_ms": current.get("session_started_at_ms") or int(time.time() * 1000),
        }
    return _update_run_telemetry(trusted, update)


def record_docatlas_call(trusted, *, args: Any, result: Any) -> dict[str, Any]:
    request = json.dumps(args if isinstance(args, dict) else {}, sort_keys=True, separators=(",", ":")).encode()
    response = result.encode() if isinstance(result, str) else json.dumps(result, sort_keys=True, default=str).encode()
    try:
        payload = json.loads(response)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    if isinstance(payload, dict) and isinstance(payload.get("structuredContent"), dict):
        payload = payload["structuredContent"]
    elif isinstance(payload, dict) and isinstance(payload.get("result"), str):
        try:
            nested = json.loads(payload["result"])
        except json.JSONDecodeError:
            nested = None
        if isinstance(nested, dict):
            payload = nested
    packet = payload.get("action_packet") if isinstance(payload, dict) and isinstance(payload.get("action_packet"), dict) else payload
    source_ids = packet.get("source_ids", packet.get("evidence_ids", [])) if isinstance(packet, dict) else []
    packet_hash = (packet.get("packet_hash") or packet.get("packet_sha256")) if isinstance(packet, dict) else None
    visible_tokens = _count(packet.get("estimated_tokens")) if isinstance(packet, dict) else None
    request_value = args if isinstance(args, dict) else {}
    response_value = result
    audit_record = {
        "schema_version": 1,
        "task_id": trusted.task_id,
        "run_id": trusted.run_id,
        "recorded_at_ms": int(time.time() * 1000),
        "request": request_value,
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "response": response_value,
        "response_sha256": hashlib.sha256(response).hexdigest(),
    }
    audit_payload = json.dumps(
        audit_record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    if __package__:
        from .storage import store_immutable_json
    else:
        from storage import store_immutable_json
    audit_path = store_immutable_json(
        Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser(),
        audit_payload,
        _namespace="docatlas-audit",
    )
    audit_sha256 = hashlib.sha256(audit_payload).hexdigest()

    def update(current: dict[str, Any]) -> dict[str, Any]:
        prior = current.get("docatlas") if isinstance(current.get("docatlas"), dict) else {}
        started = current.get("docatlas_started_at_ms")
        return {"docatlas": {
            "call_count": (_count(prior.get("call_count")) or 0) + 1,
            "request_bytes": (_count(prior.get("request_bytes")) or 0) + len(request),
            "response_bytes": (_count(prior.get("response_bytes")) or 0) + len(response),
            "estimated_visible_tokens": (
                (_count(prior.get("estimated_visible_tokens")) or 0) + visible_tokens
                if visible_tokens is not None else None
            ),
            "packet_hash": packet_hash or hashlib.sha256(response).hexdigest(),
            "source_ids": source_ids if isinstance(source_ids, list) else [],
            "response_status": payload.get("status") if isinstance(payload, dict) else None,
            "docs_status": payload.get("docs_status") if isinstance(payload, dict) else None,
            "latency_ms": max(0, int(time.time() * 1000) - started) if isinstance(started, int) else None,
            "audit_artifact_path": str(audit_path),
            "audit_artifact_sha256": audit_sha256,
        }}
    return _update_run_telemetry(trusted, update)
