"""Deterministic composition tests for bounded-engineering task records."""
from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from record import prepare_task_record  # noqa: E402


def _write_contract(repo: Path) -> None:
    path = repo / ".hermes" / "engineering.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '''schema_version = 1
project_id = "example"
profile = "bounded-engineer"
tenant = "bounded-engineering/v1"
completion_gate = "bounded-engineering/v1"

[workspace]
kind = "worktree"
allow_commits = false

[context]

[sandbox]
network = false
forward_env = []

[execution]
max_active_writer_tasks = 1

[policy]

[[verification]]
id = "focused"
kind = "test"
argv = ["python3", "-m", "pytest", "tests", "-q"]
timeout_seconds = 60
parser = "pytest"
required = true
''',
        encoding="utf-8",
    )


def test_prepare_task_record_persists_canonical_spec_and_renders_bounded_body(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_contract(repo)
    spec = {
        "schema_version": 1,
        "title": "Validate persisted records",
        "objective": "Persist a canonical record before task creation.",
        "acceptance": [{"id": "A1", "text": "Digest is stored.", "verification_ids": ["focused"]}],
        "allowed_paths": {"roots": ["src/example"], "files": ["tests/test_example.py"]},
        "required_verification_ids": ["focused"],
        "risk": "local_behavior",
    }

    record = prepare_task_record(repo=repo, hermes_home=tmp_path / "hermes", spec_raw=spec, baseline_head="a" * 40)

    assert record.spec_path.read_bytes() == record.spec.canonical_json
    assert record.spec_path.name == f"{record.spec.sha256}.json"
    assert record.contract_sha256 in record.body
    assert record.spec.sha256 in record.body
    assert "engineering_complete" in record.body
