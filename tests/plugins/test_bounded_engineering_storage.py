"""Content-addressed immutable-spec storage tests."""
from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from storage import store_immutable_completion_evidence, store_immutable_evidence, store_immutable_json  # noqa: E402


def test_store_immutable_json_uses_digest_name_and_reuses_identical_bytes(tmp_path: Path) -> None:
    first = store_immutable_json(tmp_path, b'{"a":1}')
    second = store_immutable_json(tmp_path, b'{"a":1}')

    assert first == second
    assert first.parent == tmp_path / "engineering" / "specs"
    assert first.read_bytes() == b'{"a":1}'
    assert first.stat().st_mode & 0o777 == 0o600
    assert not list(first.parent.glob("*.tmp"))


def test_store_immutable_evidence_uses_separate_content_addressed_namespace(tmp_path: Path) -> None:
    payload = b'{"snapshot":"abc"}'

    path = store_immutable_evidence(tmp_path, payload)

    assert path.parent == tmp_path / "engineering" / "evidence"
    assert path.read_bytes() == payload


def test_store_completion_evidence_uses_dedicated_immutable_namespace(tmp_path: Path) -> None:
    payload = b'{"snapshot_digest":"abc"}'
    path = store_immutable_completion_evidence(tmp_path, payload)

    assert path.parent == tmp_path / "engineering" / "completion-evidence"
    assert path.read_bytes() == payload
