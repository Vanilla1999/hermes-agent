"""Exact run telemetry contracts without a provider or session runtime."""
from __future__ import annotations

import json
import hashlib
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

import telemetry  # noqa: E402


class FakeConnection:
    def __init__(self):
        self.metadata = {}
        self.closed = False

    def execute(self, sql, params):
        if sql.startswith("SELECT"):
            return SimpleNamespace(fetchone=lambda: {
                "metadata": json.dumps(self.metadata, sort_keys=True)
            })
        self.metadata = json.loads(params[0])
        return SimpleNamespace()

    def close(self):
        self.closed = True


def _install_db(monkeypatch, conn):
    from hermes_cli import kanban_db

    @contextmanager
    def transaction(_conn):
        yield

    monkeypatch.setattr(kanban_db, "connect", lambda: conn)
    monkeypatch.setattr(kanban_db, "write_txn", transaction)


def _trusted(metadata=None):
    return SimpleNamespace(
        task_id="task-1", run_id=7,
        task=SimpleNamespace(body="objective: compact\n"),
        run=SimpleNamespace(metadata=metadata or {}),
    )


def test_post_api_usage_is_exact_and_missing_values_are_truthful(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)
    result = telemetry.record_api_request(
        _trusted(), provider="openai-codex", model="gpt-5.6-sol",
        response_model="gpt-5.6-sol", usage={
            "input_tokens": 120, "output_tokens": 30, "cache_read_tokens": 80,
        },
    )

    assert result["model"] == {
        "api_requests": 1, "provider": "openai-codex", "model": "gpt-5.6-sol",
        "input_tokens": 120, "output_tokens": 30, "cache_read_tokens": 80,
        "cache_write_tokens": None, "reasoning_tokens": None,
    }
    assert result["missing_metrics_reasons"] == {
        "model.cache_write_tokens": "provider_usage_metric_unavailable",
        "model.reasoning_tokens": "provider_usage_metric_unavailable",
    }
    assert conn.closed is True


def test_pre_api_request_persists_identity_before_response(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)

    result = telemetry.record_api_request_start(
        _trusted(), provider="openai-codex", model="gpt-5.6-terra",
        reasoning_effort="medium",
    )

    assert result["model"] == {
        "api_requests": 1, "provider": "openai-codex", "model": "gpt-5.6-terra",
        "reasoning": "medium",
    }


def test_pre_api_request_records_provider_facing_tool_names(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)
    request = {"body": {"tools": [
        {"type": "function", "name": "terminal"},
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "mcp_docatlas_benchmark_get_docs_context"}},
    ]}}

    result = telemetry.record_api_request_start(
        _trusted(), provider="openai-codex", model="gpt-5.6-terra", request=request,
    )

    assert result["model"]["provider_tool_count"] == 3
    assert result["model"]["provider_tools"] == [
        "terminal", "read_file", "mcp_docatlas_benchmark_get_docs_context",
    ]
    assert result["model"]["mcp_tool_catalog_bytes"] > 0


def test_read_only_terminal_attempt_does_not_mark_first_edit(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)

    result = telemetry.record_tool_attempt(_trusted(), tool_name="terminal")

    assert "first_edit_at_ms" not in result


def test_observed_workspace_edit_marks_first_edit(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)

    result = telemetry.record_workspace_edit(_trusted())

    assert isinstance(result["first_edit_at_ms"], int)


def test_docatlas_transport_and_docs_status_are_recorded_separately(monkeypatch, tmp_path):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn.metadata = {"docatlas_started_at_ms": 1}

    result = telemetry.record_docatlas_call(
        _trusted(conn.metadata), args={"question": "q"}, result=json.dumps({
            "status": "ok",
            "docs_status": "insufficient_evidence",
            "action_packet": {
                "status": "insufficient_evidence", "source_ids": [], "estimated_tokens": 160,
            },
        }),
    )

    assert result["docatlas"]["response_status"] == "ok"
    assert result["docatlas"]["docs_status"] == "insufficient_evidence"
    assert result["docatlas"]["estimated_visible_tokens"] == 160
    audit_path = Path(result["docatlas"]["audit_artifact_path"])
    assert audit_path.parent == tmp_path / "engineering" / "docatlas-audit"
    assert audit_path.stat().st_mode & 0o777 == 0o600
    audit = json.loads(audit_path.read_text())
    assert audit["request"] == {"question": "q"}
    assert json.loads(audit["response"])["action_packet"]["estimated_tokens"] == 160
    assert hashlib.sha256(audit_path.read_bytes()).hexdigest() == result["docatlas"]["audit_artifact_sha256"]


def test_docatlas_status_is_unwrapped_from_mcp_structured_content(monkeypatch, tmp_path):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = telemetry.record_docatlas_call(
        _trusted(), args={}, result=json.dumps({
            "result": '{"status":"ok"}',
            "structuredContent": {"status": "ok", "docs_status": "insufficient_evidence"},
        }),
    )

    assert result["docatlas"]["response_status"] == "ok"
    assert result["docatlas"]["docs_status"] == "insufficient_evidence"


def test_session_and_tool_metrics_merge_without_conversation_content(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)
    first = telemetry.record_session_start(_trusted(), session_id="secret-session")
    conn.metadata = first
    second = telemetry.record_tool_attempt(_trusted(first))
    conn.metadata = second
    third = telemetry.record_tool_result(_trusted(second), result='{"output":"ok"}')

    assert third["model"]["sessions"] == 1
    assert third["context_bytes"] == len(b"objective: compact\n")
    assert third["tool_calls"] == 1
    assert third["tool_calls_attempted"] == 1
    assert third["tool_calls_succeeded"] == 1
    assert third["tool_calls_failed"] == 0
    assert third["tool_calls_incomplete"] == 0
    assert "secret-session" not in json.dumps(third)


def test_tool_error_is_preserved_for_operator_report(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)

    telemetry.record_tool_attempt(_trusted())
    result = telemetry.record_tool_result(
        _trusted(), result='{"error":"Tool execution failed: RuntimeError: docker unavailable"}',
    )

    assert result["tool_calls"] == 1
    assert result["tool_calls_failed"] == 1
    assert result["tool_errors"] == [
        "Tool execution failed: RuntimeError: docker unavailable"
    ]


def test_tool_error_redacts_authorization_credentials(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)
    telemetry.record_tool_attempt(_trusted())

    result = telemetry.record_tool_result(
        _trusted(), result='{"error":"Authorization: Bearer abc123"}',
    )

    rendered = json.dumps(result["tool_errors"])
    assert "abc123" not in rendered
    assert "***" in rendered


def test_policy_block_is_counted_as_failed_outcome(monkeypatch):
    conn = FakeConnection()
    _install_db(monkeypatch, conn)

    result = telemetry.record_tool_result(
        _trusted(), result="tool blocked by policy", status="blocked",
    )

    assert result["tool_calls_attempted"] == 1
    assert result["tool_calls_failed"] == 1
    assert result["tool_calls_incomplete"] == 0
