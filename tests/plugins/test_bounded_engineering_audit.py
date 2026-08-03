"""Bounded audit privacy/shape tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from audit import append_audit  # noqa: E402


def test_audit_hashes_raw_values_without_retaining_them(tmp_path: Path) -> None:
    target = append_audit(tmp_path, task_id="task-1", run_id=7, event="tool", fields={"args": {"token": "secret-value"}, "result": "private output"})

    line = target.read_text(encoding="utf-8")
    record = json.loads(line)
    assert record["event"] == "tool"
    assert record["task_id"] == "task-1"
    assert len(record["fields"]["args"]) == 64
    assert "secret-value" not in line
    assert "private output" not in line
