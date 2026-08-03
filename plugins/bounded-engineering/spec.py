"""Canonical, fail-closed bounded-engineering task specification validation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection


class SpecValidationError(ValueError):
    """Raised when a task specification is unsafe or ambiguous."""


class ExternalEvidenceError(SpecValidationError):
    """Raised when a local content-addressed evidence reference is invalid."""


@dataclass(frozen=True)
class CanonicalSpec:
    canonical_json: bytes
    sha256: str
    allowed_roots: tuple[str, ...]
    allowed_files: tuple[str, ...]


@dataclass(frozen=True)
class ExternalEvidence:
    id: str
    path: Path
    sha256: str
    required: bool
    description: str


_ALLOWED_KEYS = {
    "schema_version", "title", "objective", "acceptance", "allowed_paths",
    "required_verification_ids", "risk", "external_evidence", "max_runtime_seconds",
    "max_retries", "model_override", "provider_override", "notes", "forbidden_paths",
    "diagnostic_context", "network_access", "benchmark",
}
_REQUIRED_KEYS = {
    "schema_version", "title", "objective", "acceptance", "allowed_paths",
    "required_verification_ids", "risk",
}
_RISKS = {"mechanical", "local_behavior", "semantic_contract", "security_or_migration"}
RESTRICTED_PROXY_HOSTS = frozenset({
    "api.dart.dev",
    "api.flutter.dev",
    "api.kotlinlang.org",
    "dart.dev",
    "dev.java",
    "docs.flutter.dev",
    "docs.oracle.com",
    "github.com",
    "go.dev",
    "kotlinlang.org",
    "pkg.go.dev",
    "pub.dev",
    "raw.githubusercontent.com",
    "repo.maven.apache.org",
})


def _fail(message: str) -> None:
    raise SpecValidationError(message)


def _non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{name} must be a non-empty string")
    return value.strip()


def _repository_path(value: Any) -> str:
    path = _non_empty_string(value, "path").replace("\\", "/")
    segments = path.split("/")
    if path.startswith("/") or any(segment in {"", ".", "..", ".git"} for segment in segments) or "\x00" in path:
        _fail(f"unsafe repository path: {path!r}")
    return path


def _path_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{name} must be an array")
    paths = tuple(_repository_path(item) for item in value)
    if len(set(paths)) != len(paths):
        _fail(f"{name} contains duplicate paths")
    folded = [path.casefold() for path in paths]
    if len(set(folded)) != len(folded):
        _fail(f"{name} contains case-colliding paths")
    return tuple(sorted(paths))


def canonicalize_spec(raw: Any, *, verification_ids: Collection[str]) -> CanonicalSpec:
    """Validate and canonicalize a v1 task spec without filesystem or runtime access."""
    if not isinstance(raw, dict):
        _fail("spec must be an object")
    if unknown := set(raw) - _ALLOWED_KEYS:
        _fail(f"unknown key: {sorted(unknown)[0]}")
    if missing := _REQUIRED_KEYS - set(raw):
        _fail(f"missing key: {sorted(missing)[0]}")
    if raw["schema_version"] != 1:
        _fail(f"unsupported schema_version: {raw['schema_version']!r}")
    _non_empty_string(raw["title"], "title")
    _non_empty_string(raw["objective"], "objective")
    if raw["risk"] not in _RISKS:
        _fail(f"unsupported risk: {raw['risk']!r}")
    diagnostic_context = raw.get("diagnostic_context")
    if diagnostic_context is not None:
        if not isinstance(diagnostic_context, dict) or set(diagnostic_context) != {
            "source_task_id", "persisted_report", "worker_log"
        }:
            _fail("diagnostic_context has invalid fields")
        _non_empty_string(diagnostic_context["source_task_id"], "diagnostic_context.source_task_id")
        if not isinstance(diagnostic_context["persisted_report"], dict):
            _fail("diagnostic_context.persisted_report must be an object")
        if not isinstance(diagnostic_context["worker_log"], dict):
            _fail("diagnostic_context.worker_log must be an object")
    network_access = raw.get("network_access")
    if network_access is not None:
        if not isinstance(network_access, dict) or set(network_access) != {"mode", "allowed_hosts"}:
            _fail("network_access must contain mode and allowed_hosts")
        if network_access["mode"] != "restricted_proxy":
            _fail("network_access.mode must be restricted_proxy")
        hosts = network_access["allowed_hosts"]
        if not isinstance(hosts, list) or not hosts or not all(isinstance(host, str) for host in hosts):
            _fail("network_access.allowed_hosts must be a non-empty string array")
        if len(hosts) != len(set(hosts)) or not set(hosts).issubset(RESTRICTED_PROXY_HOSTS):
            _fail("network_access.allowed_hosts contains duplicate or unsupported hosts")
    benchmark = raw.get("benchmark")
    if benchmark is not None:
        fields = {
            "experiment_id", "lane", "model", "reasoning", "baseline_head",
            "docatlas_index_revision", "benchmark_spec_sha256", "max_docatlas_calls",
            "max_visible_docatlas_tokens",
        }
        if not isinstance(benchmark, dict) or set(benchmark) != fields:
            _fail("benchmark has invalid fields")
        _non_empty_string(benchmark["experiment_id"], "benchmark.experiment_id")
        lane = benchmark["lane"]
        if lane not in {"repo_only", "docatlas_once"}:
            _fail("benchmark.lane is unsupported")
        if benchmark["model"] != "gpt-5.6-terra" or benchmark["reasoning"] != "medium":
            _fail("benchmark model and reasoning must match the pinned runtime")
        for name, length in (("baseline_head", 40), ("docatlas_index_revision", 64),
                             ("benchmark_spec_sha256", 64)):
            value = benchmark[name]
            if not isinstance(value, str) or len(value) != length or any(
                char not in "0123456789abcdef" for char in value
            ):
                _fail(f"benchmark.{name} has invalid digest")
        if benchmark["max_docatlas_calls"] != (1 if lane == "docatlas_once" else 0):
            _fail("benchmark.max_docatlas_calls does not match lane")
        if benchmark["max_visible_docatlas_tokens"] != (2000 if lane == "docatlas_once" else 0):
            _fail("benchmark.max_visible_docatlas_tokens does not match lane")
        base = dict(raw)
        base.pop("benchmark")
        base_canonical = json.dumps(
            base, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).replace("\r\n", "\n").encode("utf-8")
        if hashlib.sha256(base_canonical).hexdigest() != benchmark["benchmark_spec_sha256"]:
            _fail("benchmark.benchmark_spec_sha256 does not match specification")
    acceptance = raw["acceptance"]
    if not isinstance(acceptance, list) or not acceptance:
        _fail("acceptance must be a non-empty array")
    seen_acceptance: set[str] = set()
    for index, item in enumerate(acceptance):
        if not isinstance(item, dict) or set(item) != {"id", "text", "verification_ids"}:
            _fail(f"acceptance[{index}] must contain id, text, and verification_ids")
        identifier = _non_empty_string(item["id"], f"acceptance[{index}].id")
        if identifier in seen_acceptance:
            _fail(f"duplicate acceptance id: {identifier}")
        seen_acceptance.add(identifier)
        _non_empty_string(item["text"], f"acceptance[{index}].text")
        if not isinstance(item["verification_ids"], list) or not item["verification_ids"]:
            _fail(f"acceptance[{index}].verification_ids must be a non-empty array")
        if not set(item["verification_ids"]).issubset(verification_ids):
            _fail(f"acceptance[{index}] references unknown verification")
    allowed = raw["allowed_paths"]
    if not isinstance(allowed, dict) or set(allowed) != {"roots", "files"}:
        _fail("allowed_paths must contain roots and files")
    roots = _path_list(allowed["roots"], "allowed_paths.roots")
    files = _path_list(allowed["files"], "allowed_paths.files")
    if not roots and not files:
        _fail("allowed_paths must not be empty")
    required = raw["required_verification_ids"]
    if not isinstance(required, list) or not required or not all(isinstance(item, str) for item in required):
        _fail("required_verification_ids must be a non-empty string array")
    if not set(required).issubset(verification_ids):
        _fail("required_verification_ids references unknown verification")
    normalized = dict(raw)
    normalized["allowed_paths"] = {"roots": list(roots), "files": list(files)}
    if network_access is not None:
        normalized["network_access"] = {
            "mode": "restricted_proxy",
            "allowed_hosts": sorted(network_access["allowed_hosts"]),
        }
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).replace("\r\n", "\n").encode("utf-8")
    return CanonicalSpec(canonical, hashlib.sha256(canonical).hexdigest(), roots, files)


def validate_external_evidence(
    references: Any, *, repo_root: Path, max_bytes: int = 1_048_576
) -> tuple[ExternalEvidence, ...]:
    """Validate local evidence references against their required SHA-256 bytes."""
    if not isinstance(references, list):
        raise ExternalEvidenceError("external_evidence must be an array")
    validated: list[ExternalEvidence] = []
    seen: set[str] = set()
    for index, reference in enumerate(references):
        if not isinstance(reference, dict) or set(reference) != {
            "id", "path", "sha256", "required", "description"
        }:
            raise ExternalEvidenceError(f"external_evidence[{index}] has invalid fields")
        identifier = _non_empty_string(reference["id"], f"external_evidence[{index}].id")
        if identifier in seen:
            raise ExternalEvidenceError(f"duplicate external evidence id: {identifier}")
        seen.add(identifier)
        raw_path = _non_empty_string(reference["path"], f"external_evidence[{index}].path")
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path(repo_root) / _repository_path(raw_path)
        path = path.resolve()
        if not path.is_file() or path.is_symlink():
            raise ExternalEvidenceError(f"external evidence is not a regular file: {raw_path}")
        if path.stat().st_size > max_bytes:
            raise ExternalEvidenceError(f"external evidence exceeds size cap: {raw_path}")
        digest = _non_empty_string(reference["sha256"], f"external_evidence[{index}].sha256")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ExternalEvidenceError(f"external evidence has invalid digest: {raw_path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise ExternalEvidenceError(f"external evidence digest mismatch: {raw_path}")
        if not isinstance(reference["required"], bool):
            raise ExternalEvidenceError(f"external_evidence[{index}].required must be boolean")
        validated.append(ExternalEvidence(
            identifier, path, digest, reference["required"],
            _non_empty_string(reference["description"], f"external_evidence[{index}].description"),
        ))
    return tuple(validated)
