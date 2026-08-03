"""Pinned-Hermes isolation contract for the planner provider boundary."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

import planner_runtime  # noqa: E402


def test_runtime_forces_no_tools_memory_or_fallback_and_low_reasoning(monkeypatch) -> None:
    captured = {}

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self._api_call_count = 1
            self.total_input_tokens = 17
            self.total_output_tokens = 9

        def chat(self, prompt):
            return '{"schema_version":1}'

    fake_run_agent = SimpleNamespace(AIAgent=FakeAgent)
    fake_runtime = SimpleNamespace(resolve_runtime_provider=lambda **kwargs: {
        "api_key": "key", "base_url": "https://example.invalid", "provider": "openai-codex",
        "api_mode": "codex_responses", "credential_pool": None,
    })
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", SimpleNamespace(
        detect_provider_for_model=lambda model, current: ("openai-codex", model)))
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime)

    result = planner_runtime.run_isolated_planner("prompt")

    assert result.api_requests == 1
    assert result.input_tokens == 17 and result.output_tokens == 9
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["enabled_toolsets"] == []
    assert captured["max_iterations"] == 1
    assert captured["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert captured["skip_context_files"] is True
    assert captured["skip_memory"] is True
    assert captured["session_db"] is None
    assert captured["fallback_model"] is None


def test_runtime_reports_missing_token_metrics_truthfully(monkeypatch) -> None:
    class FakeAgent:
        def __init__(self, *args, **kwargs):
            self._api_call_count = 1
        def chat(self, prompt):
            return "{}"

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    monkeypatch.setitem(sys.modules, "hermes_cli.models", SimpleNamespace(
        detect_provider_for_model=lambda model, current: None))
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", SimpleNamespace(
        resolve_runtime_provider=lambda **kwargs: {"provider": "openai-codex"}))
    result = planner_runtime.run_isolated_planner("prompt")
    assert result.input_tokens is None and result.output_tokens is None
    assert result.missing_metrics_reasons == {
        "input_tokens": "pinned_hermes_did_not_expose_metric",
        "output_tokens": "pinned_hermes_did_not_expose_metric",
    }
