from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / ".product-truth" / "real_task_pack.py"
POLICY = ROOT / ".product-truth" / "oracle_terminal_policy.py"
MANIFEST = json.loads((ROOT / ".product-truth" / "real-task-pack.json").read_text(encoding="utf-8"))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load(RUNNER, "hermes_product_truth")
policy = _load(POLICY, "hermes_product_truth_terminal_policy")


def _attempt(number: int) -> dict:
    return {
        "attempt": number,
        "public_base": {"returncode": 0},
        "hidden_base": {
            "returncode": 1,
            "junit_parsed": True,
            "testcases": 1,
            "test_failures": 1,
            "test_errors": 0,
        },
        "patch_applied": True,
        "gold_surface_exact": True,
        "public_gold": {"returncode": 0},
        "hidden_gold": {"returncode": 0},
        "passed": True,
    }


def _report() -> dict:
    tasks = []
    for task in MANIFEST["tasks"]:
        tasks.append({
            "id": task["id"],
            "fix_commit": task["fix_commit"],
            "base_commit": "0" * 40,
            "issue_sha256": "1" * 64,
            "hidden_test_path": task["hidden_test_path"],
            "hidden_test_sha256": "2" * 64,
            "production_paths": ["example.py"],
            "gold_patch_sha256": "3" * 64,
            "attempts": [_attempt(1), _attempt(2)],
            "gold_reproducible": True,
            "real_model_oracle_executed": False,
            "real_model_oracle_passed": False,
            "valid": False,
        })
    return {
        "schema_version": 1,
        "protocol": runner.REPORT_PROTOCOL,
        "repository": MANIFEST["repository"],
        "frozen_inventory_head": MANIFEST["frozen_inventory_head"],
        "manifest_sha256": runner.manifest_sha256(MANIFEST),
        "tasks": tasks,
        "summary": {"task_count": 8, "gold_reproducible_tasks": 8, "real_model_oracle_tasks": 0, "valid_tasks": 0},
        "claim_boundary": {
            "gold_control_complete": True,
            "real_model_oracle_complete": False,
            "task_pack_ready": False,
            "product_truth_proven": False,
            "product_failure_proven": False,
            "product_maturity": "Beta",
        },
    }


@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_only_assertion_failure_is_hidden_red(code: int) -> None:
    row = _attempt(1)
    row["hidden_base"]["returncode"] = code
    assert runner.attempt_stage_valid(row) is False


def test_setup_error_and_missing_junit_are_not_hidden_red() -> None:
    row = _attempt(1)
    row["hidden_base"].update(test_failures=0, test_errors=1)
    assert runner.attempt_stage_valid(row) is False
    row = _attempt(1)
    row["hidden_base"]["junit_parsed"] = False
    assert runner.attempt_stage_valid(row) is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["tasks"][0]["attempts"][0]["public_base"].update(returncode=1),
        lambda r: r["tasks"][0]["attempts"][0]["hidden_base"].update(returncode=2),
        lambda r: r["tasks"][0]["attempts"][0]["hidden_base"].update(test_failures=0, test_errors=1),
        lambda r: r["tasks"][0]["attempts"][0].update(patch_applied=False),
        lambda r: r["tasks"][0]["attempts"][0].update(gold_surface_exact=False),
        lambda r: r["tasks"][0]["attempts"][0]["public_gold"].update(returncode=1),
        lambda r: r["tasks"][0]["attempts"][0]["hidden_gold"].update(returncode=1),
        lambda r: r["tasks"][0].update(real_model_oracle_executed=True),
        lambda r: r["tasks"][0].update(real_model_oracle_passed=True),
        lambda r: r["tasks"][0].update(valid=True),
        lambda r: r["summary"].update(gold_reproducible_tasks=9),
        lambda r: r["claim_boundary"].update(product_truth_proven=True),
        lambda r: r["claim_boundary"].update(product_maturity="Stable"),
    ],
)
def test_report_semantic_mutations_fail_closed(mutate, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    monkeypatch.setattr(runner, "verify_task_provenance", lambda *_args, **_kwargs: None)
    mutate(report)
    with pytest.raises(ValueError):
        runner.verify_report(report, MANIFEST)


def test_provenance_mutations_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    task = MANIFEST["tasks"][0]
    expected = {
        "id": task["id"],
        "fix_commit": task["fix_commit"],
        "base_commit": "a" * 40,
        "issue_sha256": "b" * 64,
        "hidden_test_path": task["hidden_test_path"],
        "hidden_test_sha256": "c" * 64,
        "production_paths": ["src/a.py", "src/b.py"],
        "gold_patch_sha256": "d" * 64,
        "gold_patch": b"patch",
    }
    monkeypatch.setattr(runner, "task_provenance", lambda *_args, **_kwargs: copy.deepcopy(expected))
    row = {key: value for key, value in expected.items() if key != "gold_patch"}
    runner.verify_task_provenance(MANIFEST, task, row)
    for field in ("id", "fix_commit", "base_commit", "issue_sha256", "hidden_test_path", "hidden_test_sha256", "production_paths", "gold_patch_sha256"):
        forged = copy.deepcopy(row)
        forged[field] = ["forged.py"] if field == "production_paths" else "forged"
        with pytest.raises(ValueError):
            runner.verify_task_provenance(MANIFEST, task, forged)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_surface_accounting_includes_untracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "benchmark@example.invalid")
    _git(repo, "config", "user.name", "Benchmark")
    (repo / "tracked.py").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-qm", "base")
    (repo / "tracked.py").write_text("new\n", encoding="utf-8")
    (repo / "added.py").write_text("new\n", encoding="utf-8")
    assert runner.changed_worktree_paths(repo) == ["added.py", "tracked.py"]


def test_oracle_terminal_policy_forces_exact_air_gapped_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "model"
    workspace.mkdir()
    (workspace / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    home = tmp_path / "hermes-home"
    env = policy.build_oracle_terminal_env(
        workspace=workspace,
        hermes_home=home,
        base_env={
            "TERMINAL_ENV": "local",
            "TERMINAL_CWD": "/",
            "TERMINAL_DOCKER_NETWORK": "true",
            "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES": "true",
            "TERMINAL_DOCKER_VOLUMES": '["/:/host"]',
        },
    )
    assert env["TERMINAL_ENV"] == "docker"
    assert env["TERMINAL_CWD"] == str(workspace.resolve())
    assert env["TERMINAL_DOCKER_NETWORK"] == "false"
    assert env["TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES"] == "false"
    assert env["TERMINAL_DOCKER_VOLUMES"] == "[]"
    assert env["TERMINAL_DOCKER_FORWARD_ENV"] == "[]"
    assert env["TERMINAL_DOCKER_EXTRA_ARGS"] == "[]"
    policy.assert_oracle_terminal_env(env, workspace=workspace, hermes_home=home)


@pytest.mark.parametrize(
    ("key", "unsafe"),
    [
        ("TERMINAL_ENV", "local"),
        ("TERMINAL_CWD", "/"),
        ("TERMINAL_DOCKER_NETWORK", "true"),
        ("TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES", "true"),
        ("TERMINAL_DOCKER_VOLUMES", '["/:/host"]'),
        ("TERMINAL_DOCKER_FORWARD_ENV", '["OPENAI_API_KEY"]'),
        ("TERMINAL_DOCKER_EXTRA_ARGS", '["--network=host"]'),
    ],
)
def test_oracle_terminal_policy_rejects_isolation_drift(tmp_path: Path, key: str, unsafe: str) -> None:
    workspace = tmp_path / "model"
    workspace.mkdir()
    home = tmp_path / "hermes-home"
    env = policy.build_oracle_terminal_env(workspace=workspace, hermes_home=home, base_env={})
    env[key] = unsafe
    with pytest.raises(ValueError):
        policy.assert_oracle_terminal_env(env, workspace=workspace, hermes_home=home)


def test_oracle_terminal_policy_rejects_config_override_and_repo_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "model"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    with pytest.raises(ValueError, match="forbidden .git"):
        policy.build_oracle_terminal_env(workspace=workspace, hermes_home=tmp_path / "home-a", base_env={})

    clean = tmp_path / "clean"
    clean.mkdir()
    home = tmp_path / "home-b"
    home.mkdir()
    (home / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must not contain config.yaml"):
        policy.build_oracle_terminal_env(workspace=clean, hermes_home=home, base_env={})
