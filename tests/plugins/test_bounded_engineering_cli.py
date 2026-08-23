"""No-provider contracts for the bounded engineering CLI."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from engineering_cli import entrypoint, run, setup_parser  # noqa: E402


def _args(*argv: str):
    parser = argparse.ArgumentParser()
    setup_parser(parser)
    return parser.parse_args(list(argv))


def test_doctor_json_reports_typed_local_failures(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    code = run(_args("doctor", "--repo", str(tmp_path / "missing"), "--board", "missing", "--json"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["schema_version"] == 1
    assert payload["ready"] is False
    assert {item["name"] for item in payload["checks"]} == {
        "repository", "board", "profile", "plugin_manifest", "completion_gate",
    }


def test_doctor_fails_closed_without_network_none_config(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    profile = home / "profiles" / "bounded-engineer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("""terminal:
  backend: docker
  docker_forward_env: []
  docker_mount_cwd_to_workspace: true
  docker_run_as_host_user: true
""", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    code = run(_args("doctor", "--repo", str(tmp_path / "missing"), "--board", "missing", "--json"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["profile_policy"]["ok"] is False
    assert checks["network_isolation"] == {
        "name": "network_isolation",
        "ok": False,
        "detail": "restricted proxy-only Docker network",
    }


def test_plugin_registers_top_level_engineering_cli() -> None:
    spec = importlib.util.spec_from_file_location("bounded_engineering_cli_plugin", PLUGIN_DIR / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registered = []

    class Context:
        def register_cli_command(self, *args):
            registered.append(args)
        def register_hook(self, *args):
            pass
        def register_tool(self, **kwargs):
            pass
        def register_skill(self, *args, **kwargs):
            pass

    module.register(Context())
    assert registered[0][0:2] == ("engineering", "Deterministic bounded-engineering controls")


def test_entrypoint_preserves_nonzero_doctor_exit(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with pytest.raises(SystemExit) as exc_info:
        entrypoint(_args("doctor", "--repo", str(tmp_path / "missing"), "--board", "missing"))
    assert exc_info.value.code == 1


def test_plan_and_apply_plan_parser_contracts() -> None:
    planned = _args("plan", "--repo", "/repo", "--board", "b",
                    "--objective", "Find one issue", "--output", "plan.json")
    assert planned.engineering_action == "plan"
    assert planned.max_prompt_bytes == 12288
    applied = _args("apply-plan", "--repo", "/repo", "--board", "b", "--plan", "plan.json")
    assert applied.engineering_action == "apply-plan"
