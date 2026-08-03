"""Read-only bounded-engineering status schema tests."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from status import build_engineering_status, status_payload  # noqa: E402


def test_status_payload_is_bounded_and_excludes_workspace() -> None:
    context = SimpleNamespace(
        task_id="task-1",
        run_id=7,
        completion_gate="bounded-engineering/v1",
        task=SimpleNamespace(status="running", workspace_path="/private/workspace"),
        contract=SimpleNamespace(verification=(
            SimpleNamespace(id="required", required=True),
            SimpleNamespace(id="optional", required=False),
        )),
        spec={
            "objective": "Fix the parser.",
            "acceptance": [{"id": "A1", "text": "Tests pass.", "verification_ids": ["required"]}],
            "allowed_paths": {"roots": ["src"], "files": ["tests/test_parser.py"]},
        },
    )

    payload = status_payload(build_engineering_status(context, evidence_refs=("a", "b")))

    assert payload == {
        "task_id": "task-1",
        "run_id": 7,
        "completion_gate": "bounded-engineering/v1",
        "status": "running",
        "required_verification_ids": ["required"],
        "evidence_count": 2,
        "objective": "Fix the parser.",
        "acceptance": [{"id": "A1", "text": "Tests pass.", "verification_ids": ["required"]}],
        "allowed_paths": {"roots": ["src"], "files": ["tests/test_parser.py"]},
    }
    assert "workspace" not in str(payload)
