"""Pure, fail-closed validation for `.hermes/engineering.toml`."""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_COMPLETION_GATE = "bounded-engineering/v1"
_HARD_CEILINGS = {
    "workspace.max_changed_files": 100,
    "workspace.max_total_changed_bytes": 2 * 1024 * 1024,
    "workspace.max_single_file_bytes": 1024 * 1024,
    "context.hard_bytes": 16 * 1024,
    "execution.default_max_runtime_seconds": 1800,
}


class ContractValidationError(ValueError):
    """Raised when a repository engineering contract is unsafe or ambiguous."""


@dataclass(frozen=True)
class Verification:
    id: str
    kind: str
    argv: tuple[str, ...]
    timeout_seconds: int
    parser: str
    minimum_collected: int | None
    required: bool


@dataclass(frozen=True)
class Contract:
    project_id: str
    profile: str
    tenant: str
    completion_gate: str
    workspace: dict[str, Any]
    context: dict[str, Any]
    sandbox: dict[str, Any]
    execution: dict[str, Any]
    policy: dict[str, Any]
    verification: tuple[Verification, ...]
    canonical_json: bytes
    sha256: str


_TOP_LEVEL_KEYS = {
    "schema_version", "project_id", "profile", "tenant", "completion_gate",
    "project_map", "workspace", "context", "sandbox", "execution", "policy",
    "verification",
}
_SECTION_KEYS = {
    "workspace": {
        "kind", "require_clean_source", "require_clean_task_baseline", "allow_commits",
        "allow_submodule_changes", "allow_new_symlinks", "max_changed_files",
        "max_total_changed_bytes", "max_single_file_bytes", "max_binary_files",
    },
    "context": {
        "target_bytes", "hard_bytes", "project_map_max_bytes", "latest_failure_max_bytes",
        "max_evidence_refs",
    },
    "sandbox": {
        "terminal_backend", "docker_image", "network", "mount_workspace", "run_as_host_user",
        "forward_env", "require_no_host_home_mount", "require_no_docker_socket",
        "require_no_ssh_agent", "memory", "cpus", "pids_limit",
    },
    "execution": {
        "default_max_runtime_seconds", "default_max_retries", "max_active_writer_tasks",
        "goal_mode", "auto_decompose", "allow_delegate_task", "allow_child_cards",
    },
    "policy": {"forbidden_tools", "forbidden_git_subcommands", "forbidden_command_tokens"},
}
_REQUIRED_TOP_LEVEL = {
    "schema_version", "project_id", "profile", "tenant", "completion_gate",
    "workspace", "context", "sandbox", "execution", "policy", "verification",
}


def _fail(message: str) -> None:
    raise ContractValidationError(message)


def _expect_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{name} must be a table")
    return value


def _expect_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{name} must be a non-empty string")
    return value.strip()


def _validate_ceiling(section: dict[str, Any], key: str, qualified_name: str) -> None:
    value = section.get(key)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(f"{qualified_name} must be a non-negative integer")
    if value > _HARD_CEILINGS[qualified_name]:
        _fail(f"{qualified_name} exceeds hard ceiling of {_HARD_CEILINGS[qualified_name]}")


def _validate_verification(value: Any) -> tuple[Verification, ...]:
    if not isinstance(value, list) or not value:
        _fail("verification must be a non-empty array")
    if len(value) > 16:
        _fail("verification supports at most 16 commands")
    seen: set[str] = set()
    records: list[Verification] = []
    allowed = {"id", "kind", "argv", "timeout_seconds", "parser", "minimum_collected", "required"}
    for index, item in enumerate(value):
        item = _expect_dict(item, f"verification[{index}]")
        unknown = set(item) - allowed
        if unknown:
            _fail(f"verification[{index}] unknown key: {sorted(unknown)[0]}")
        required = {"id", "kind", "argv", "timeout_seconds", "parser", "required"}
        if missing := required - set(item):
            _fail(f"verification[{index}] missing key: {sorted(missing)[0]}")
        identifier = _expect_string(item["id"], f"verification[{index}].id")
        if identifier in seen:
            _fail(f"duplicate verification id: {identifier}")
        seen.add(identifier)
        argv = item["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
            _fail(f"verification[{index}].argv must be a non-empty string array")
        if any(any(token in part for token in ("|", ">", "<", "\x00")) for part in argv):
            _fail(f"verification[{index}].argv contains shell syntax")
        executable = argv[0].replace("\\", "/")
        if executable.startswith("/") or ".." in executable.split("/"):
            _fail(f"verification[{index}] executable path traversal")
        timeout = item["timeout_seconds"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 1800:
            _fail(f"verification[{index}].timeout_seconds must be between 1 and 1800")
        minimum = item.get("minimum_collected")
        if minimum is not None and (not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0):
            _fail(f"verification[{index}].minimum_collected must be a non-negative integer")
        if not isinstance(item["required"], bool):
            _fail(f"verification[{index}].required must be boolean")
        records.append(Verification(identifier, _expect_string(item["kind"], "verification.kind"), tuple(argv), timeout, _expect_string(item["parser"], "verification.parser"), minimum, item["required"]))
    return tuple(records)


def load_contract(repo: Path) -> Contract:
    """Load and semantically canonicalize a contract without Hermes runtime access."""
    repo = Path(repo).resolve()
    path = repo / ".hermes" / "engineering.toml"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractValidationError(f"cannot read contract: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ContractValidationError(f"invalid TOML: {exc}") from exc
    if not isinstance(raw, dict):
        _fail("contract must be a TOML table")
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        _fail(f"unknown key: {sorted(unknown)[0]}")
    if missing := _REQUIRED_TOP_LEVEL - set(raw):
        _fail(f"missing key: {sorted(missing)[0]}")
    if raw["schema_version"] != SUPPORTED_SCHEMA_VERSION:
        _fail(f"unsupported schema_version: {raw['schema_version']!r}")
    if raw["completion_gate"] != SUPPORTED_COMPLETION_GATE:
        _fail(f"unsupported completion_gate: {raw['completion_gate']!r}")
    sections: dict[str, dict[str, Any]] = {}
    for name, allowed in _SECTION_KEYS.items():
        section = _expect_dict(raw[name], name)
        if unknown := set(section) - allowed:
            _fail(f"{name} unknown key: {sorted(unknown)[0]}")
        sections[name] = section
    if sections["workspace"].get("kind") != "worktree":
        _fail("workspace.kind must be worktree")
    if sections["workspace"].get("allow_commits") is not False:
        _fail("workspace.allow_commits must be false")
    if sections["sandbox"].get("network") is not False:
        _fail("sandbox.network must be false")
    if sections["sandbox"].get("forward_env") not in ([], None):
        _fail("sandbox.forward_env must be empty")
    if sections["execution"].get("max_active_writer_tasks") != 1:
        _fail("execution.max_active_writer_tasks must be 1")
    for qualified_name in _HARD_CEILINGS:
        section_name, key = qualified_name.split(".", 1)
        _validate_ceiling(sections[section_name], key, qualified_name)
    target = sections["context"].get("target_bytes")
    hard = sections["context"].get("hard_bytes")
    if target is not None and hard is not None and target > hard:
        _fail("context.target_bytes must not exceed context.hard_bytes")
    verification = _validate_verification(raw["verification"])
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).replace("\r\n", "\n").encode("utf-8")
    return Contract(
        project_id=_expect_string(raw["project_id"], "project_id"),
        profile=_expect_string(raw["profile"], "profile"),
        tenant=_expect_string(raw["tenant"], "tenant"),
        completion_gate=SUPPORTED_COMPLETION_GATE,
        workspace=sections["workspace"], context=sections["context"], sandbox=sections["sandbox"],
        execution=sections["execution"], policy=sections["policy"], verification=verification,
        canonical_json=canonical, sha256=hashlib.sha256(canonical).hexdigest(),
    )
