"""Token-bounded, fail-closed planner contracts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

import plan_cli  # noqa: E402


def _response() -> dict:
    return {
        "schema_version": 1,
        "board_revision": "rev-1",
        "task": {
            "schema_version": 1,
            "title": "Fix parser boundary",
            "objective": "Preserve import boundaries.",
            "acceptance": [{"id": "A1", "text": "Regression passes.", "verification_ids": ["unit"]}],
            "allowed_paths": {"roots": ["src"], "files": ["tests/test_parser.py"]},
            "required_verification_ids": ["unit"],
            "risk": "local_behavior",
            "max_runtime_seconds": 600,
            "max_retries": 1,
        },
    }


def test_validate_plan_is_canonical_and_binds_revision() -> None:
    first = plan_cli.validate_plan(_response(), expected_revision="rev-1", verification_ids={"unit"})
    second = plan_cli.validate_plan(json.loads(first.canonical_json), expected_revision="rev-1", verification_ids={"unit"})
    assert first.sha256 == second.sha256
    assert first.task_sha256 == second.task_sha256


def test_validate_plan_rejects_stale_revision_unknown_fields_and_multiple_tasks() -> None:
    with pytest.raises(plan_cli.PlanError, match="stale_board_revision"):
        plan_cli.validate_plan(_response(), expected_revision="rev-2", verification_ids={"unit"})
    bad = _response(); bad["surprise"] = True
    with pytest.raises(plan_cli.PlanError, match="plan fields"):
        plan_cli.validate_plan(bad, expected_revision="rev-1", verification_ids={"unit"})
    bad = _response(); bad["tasks"] = [bad.pop("task")]
    with pytest.raises(plan_cli.PlanError, match="plan fields"):
        plan_cli.validate_plan(bad, expected_revision="rev-1", verification_ids={"unit"})


def test_prompt_is_bounded_and_explicitly_tool_free() -> None:
    prompt = plan_cli.build_prompt(
        objective="Find one bounded regression", repo_summary="x" * 50_000, board_summary="y" * 50_000,
        board_revision="rev-1", contract_summary="z" * 50_000, max_bytes=4096,
    )
    assert len(prompt.encode()) <= 4096
    assert "exactly one" in prompt
    assert "Do not use tools" in prompt
    assert "gpt-5.6-sol" not in prompt


def test_planner_runs_one_request_with_pinned_model_and_low_effort(monkeypatch) -> None:
    class Result:
        text = json.dumps(_response())
        api_requests = 1
        input_tokens = 123
        output_tokens = 45
        missing_metrics_reasons = {}

    monkeypatch.setattr(plan_cli, "run_isolated_planner", lambda prompt: Result())
    raw, metrics = plan_cli.invoke_planner("compact prompt")
    assert raw == _response()
    assert metrics == {"api_requests": 1, "input_tokens": 123, "output_tokens": 45,
                       "missing_metrics_reasons": {}}


def test_planner_rejects_non_json_without_retry(monkeypatch) -> None:
    calls = []
    result = type("Result", (), {"text": "not json", "api_requests": 1,
        "input_tokens": None, "output_tokens": None,
        "missing_metrics_reasons": {"input_tokens": "not_recorded", "output_tokens": "not_recorded"}})()
    monkeypatch.setattr(plan_cli, "run_isolated_planner", lambda prompt: calls.append(1) or result)
    with pytest.raises(plan_cli.PlanError, match="planner_non_json"):
        plan_cli.invoke_planner("p")
    assert len(calls) == 1


def test_generate_stores_immutable_plan_and_truthful_offline_metrics(tmp_path, monkeypatch) -> None:
    plan = plan_cli.validate_plan(_response(), expected_revision="rev-1", verification_ids={"unit"})
    path = plan_cli.store_plan(tmp_path, plan)
    assert path == tmp_path / "engineering" / "plans" / f"{plan.sha256}.json"
    assert path.read_bytes() == plan.canonical_json
    assert plan_cli.store_plan(tmp_path, plan) == path


def test_existing_task_wins_before_stale_revision() -> None:
    task = type("Task", (), {"idempotency_key": "same", "status": "running"})()
    assert plan_cli.preflight_apply((task,), expected_revision="old", idempotency_key="same") is task
    with pytest.raises(plan_cli.PlanError, match="stale_board_revision"):
        plan_cli.preflight_apply((task,), expected_revision="old", idempotency_key="absent")
