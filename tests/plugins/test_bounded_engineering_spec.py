"""Pure canonicalization tests for bounded-engineering task specifications."""
from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from spec import (  # noqa: E402
    ExternalEvidenceError,
    SpecValidationError,
    canonicalize_spec,
    validate_external_evidence,
)


def _spec() -> dict:
    return {
        "schema_version": 1,
        "title": "Reject invalid environment",
        "objective": "Reject invalid persisted environment values.",
        "acceptance": [{"id": "A1", "text": "Invalid values fail.", "verification_ids": ["focused"]}],
        "allowed_paths": {"roots": ["src/example"], "files": ["tests/test_example.py"]},
        "required_verification_ids": ["focused"],
        "risk": "local_behavior",
    }


def test_canonicalize_spec_is_stable_across_input_key_order() -> None:
    first = _spec()
    second = dict(reversed(list(first.items())))

    left = canonicalize_spec(first, verification_ids={"focused"})
    right = canonicalize_spec(second, verification_ids={"focused"})

    assert left.sha256 == right.sha256
    assert left.canonical_json == right.canonical_json
    assert left.allowed_roots == ("src/example",)


def test_canonicalize_spec_accepts_only_approved_restricted_proxy_hosts() -> None:
    value = _spec()
    value["network_access"] = {
        "mode": "restricted_proxy",
        "allowed_hosts": ["raw.githubusercontent.com", "github.com"],
    }

    canonical = canonicalize_spec(value, verification_ids={"focused"})

    assert b'"allowed_hosts":["github.com","raw.githubusercontent.com"]' in canonical.canonical_json

    value["network_access"]["allowed_hosts"] = ["example.com"]
    with pytest.raises(SpecValidationError, match="unsupported hosts"):
        canonicalize_spec(value, verification_ids={"focused"})


@pytest.mark.parametrize("path", ["../escape", ".git/config", "/absolute/path", "src//double"])
def test_canonicalize_spec_rejects_unsafe_allowed_paths(path: str) -> None:
    value = _spec()
    value["allowed_paths"] = {"roots": [path], "files": []}

    with pytest.raises(SpecValidationError, match="unsafe repository path"):
        canonicalize_spec(value, verification_ids={"focused"})


def test_validate_external_evidence_requires_exact_digest(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(b'{"stable":true}')
    reference = {
        "id": "E1",
        "path": str(evidence),
        "sha256": "f6ae9075446e89443e829410051dee7de57a5455d357a862a38f3208fbc1f6b5",
        "required": True,
        "description": "stable local evidence",
    }

    validated = validate_external_evidence([reference], repo_root=tmp_path)
    assert validated[0].path == evidence

    reference["sha256"] = "0" * 64
    with pytest.raises(ExternalEvidenceError, match="digest mismatch"):
        validate_external_evidence([reference], repo_root=tmp_path)


def test_benchmark_digest_must_match_base_spec() -> None:
    value = _spec()
    digest = hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    value["benchmark"] = {
        "experiment_id": "exp-1", "lane": "repo_only", "model": "gpt-5.6-terra",
        "reasoning": "medium", "baseline_head": "a" * 40,
        "docatlas_index_revision": "b" * 64, "benchmark_spec_sha256": digest,
        "max_docatlas_calls": 0, "max_visible_docatlas_tokens": 0,
    }
    canonicalize_spec(value, verification_ids={"focused"})
    value["objective"] = "changed"
    with pytest.raises(SpecValidationError, match="does not match specification"):
        canonicalize_spec(value, verification_ids={"focused"})
