"""No-provider contract tests for generic plugin capabilities."""

from __future__ import annotations


def test_pre_verify_is_a_registered_plugin_hook():
    from hermes_cli.plugins import VALID_HOOKS, PluginContext, PluginManager, PluginManifest

    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name="test-plugin", key="test-plugin"),
        manager,
    )
    observed: list[dict] = []
    context.register_hook("pre_verify", lambda **kwargs: observed.append(kwargs) or "continue")

    results = manager.invoke_hook(
        "pre_verify",
        session_id="session-1",
        changed_paths=["src/example.py"],
        attempts=0,
        nudge="verify now",
    )

    assert "pre_verify" in VALID_HOOKS
    assert results == ["continue"]
    assert observed == [{
        "session_id": "session-1",
        "changed_paths": ["src/example.py"],
        "attempts": 0,
        "nudge": "verify now",
        "telemetry_schema_version": "hermes.observer.v1",
    }]


def test_pre_verify_hook_can_replace_a_verify_on_stop_nudge(monkeypatch):
    from agent.verification_stop import apply_pre_verify_hook

    observed: dict = {}

    def invoke_hook(name: str, **kwargs):
        observed["name"] = name
        observed.update(kwargs)
        return [None, {"continuation": "run `pytest -q` before completing"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)

    assert apply_pre_verify_hook(
        "verify now",
        session_id="session-1",
        changed_paths=["src/example.py"],
        attempts=0,
    ) == "run `pytest -q` before completing"
    assert observed == {
        "name": "pre_verify",
        "session_id": "session-1",
        "changed_paths": ["src/example.py"],
        "attempts": 0,
        "nudge": "verify now",
    }


def test_pre_verify_hook_keeps_builtin_nudge_when_plugin_fails(monkeypatch):
    from agent.verification_stop import apply_pre_verify_hook

    def invoke_hook(_name: str, **_kwargs):
        raise RuntimeError("plugin manager unavailable")

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)

    assert apply_pre_verify_hook(
        "verify now",
        session_id="session-1",
        changed_paths=["src/example.py"],
        attempts=0,
    ) == "verify now"


def test_plugin_context_exposes_active_session_id(monkeypatch):
    from types import SimpleNamespace

    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    manager = PluginManager()
    manager._cli_ref = SimpleNamespace(agent=SimpleNamespace(session_id="session-123"))
    context = PluginContext(PluginManifest(name="probe"), manager)

    assert context.session_id == "session-123"

    manager._cli_ref = None
    monkeypatch.setenv("HERMES_SESSION_ID", "session-from-env")
    assert context.session_id == "session-from-env"
