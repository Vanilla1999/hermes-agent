from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / ".product-truth" / "model_workspace.py"
POLICY = ROOT / ".product-truth" / "oracle_terminal_policy.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workspace = _load(MODULE, "hermes_model_workspace")
policy = _load(POLICY, "hermes_oracle_terminal_policy")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "benchmark@example.invalid")
    _git(repo, "config", "user.name", "Benchmark")
    (repo / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / ".product-truth").mkdir()
    (repo / ".product-truth" / "secret.json").write_text('{"gold": true}\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "broken base")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_materialize_exact_source_without_git_or_benchmark_metadata(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    output = tmp_path / "model"
    attestation = tmp_path / "evaluator" / "attestation.json"
    payload = workspace.materialize(repo, base, output, attestation)
    assert (output / "src.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (output / ".git").exists()
    assert not (output / ".product-truth").exists()
    assert payload["base_commit"] == base
    assert payload["git_metadata_absent"] is True
    assert payload["benchmark_metadata_absent"] is True
    assert payload["claim_boundary"]["real_model_oracle_authorized"] is False
    assert json.loads(attestation.read_text(encoding="utf-8"))["tree_sha256"] == payload["tree_sha256"]


def test_attestation_cannot_be_written_inside_model_workspace(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    output = tmp_path / "model"
    with pytest.raises(ValueError, match="attestation must be outside"):
        workspace.materialize(repo, base, output, output / "attestation.json")


def test_oracle_policy_disables_all_host_data_and_env_passthrough(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    home = tmp_path / "home"
    env = policy.build_oracle_terminal_env(
        workspace=model,
        hermes_home=home,
        base_env={
            "TERMINAL_DOCKER_MOUNT_HOST_DATA": "true",
            "HERMES_DISABLE_ENV_PASSTHROUGH": "0",
        },
    )
    assert env["TERMINAL_DOCKER_MOUNT_HOST_DATA"] == "false"
    assert env["HERMES_DISABLE_ENV_PASSTHROUGH"] == "1"
    claim = policy.isolation_claim()
    assert claim["mount_host_data"] is False
    assert claim["implicit_env_passthrough"] is False

    forged = dict(env)
    forged["TERMINAL_DOCKER_MOUNT_HOST_DATA"] = "true"
    with pytest.raises(ValueError, match="MOUNT_HOST_DATA"):
        policy.assert_oracle_terminal_env(forged, workspace=model, hermes_home=home)

    forged = dict(env)
    forged["HERMES_DISABLE_ENV_PASSTHROUGH"] = "0"
    with pytest.raises(ValueError, match="HERMES_DISABLE_ENV_PASSTHROUGH"):
        policy.assert_oracle_terminal_env(forged, workspace=model, hermes_home=home)


def test_env_passthrough_hard_disable_wins_over_skill_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import env_passthrough

    env_passthrough.clear_env_passthrough()
    monkeypatch.setenv("HERMES_DISABLE_ENV_PASSTHROUGH", "1")
    env_passthrough.register_env_passthrough(["THIRD_PARTY_SECRET"])
    assert env_passthrough.get_all_passthrough() == frozenset()
    assert env_passthrough.is_env_passthrough("THIRD_PARTY_SECRET") is False
