#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

_TRUE = {"1", "true", "yes"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


def _require_hermetic_workspace(workspace: Path) -> Path:
    root = workspace.resolve()
    if not root.is_dir():
        raise ValueError("oracle workspace must exist")
    for forbidden in (".git", ".product-truth"):
        if (root / forbidden).exists():
            raise ValueError(f"oracle workspace contains forbidden {forbidden}")
    return root


def _require_empty_hermes_home(hermes_home: Path) -> Path:
    root = hermes_home.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = root / "config.yaml"
    if config.exists():
        raise ValueError(
            "benchmark Hermes home must not contain config.yaml; explicit config "
            "could override the evaluator-owned Docker isolation environment"
        )
    return root


def build_oracle_terminal_env(
    *,
    workspace: Path,
    hermes_home: Path,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the only terminal environment authorized for Product Truth oracle runs."""
    source = _require_hermetic_workspace(workspace)
    home = _require_empty_hermes_home(hermes_home)
    env = dict(base_env or os.environ)
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_DISABLE_ENV_PASSTHROUGH": "1",
            "TERMINAL_ENV": "docker",
            "TERMINAL_CWD": str(source),
            "TERMINAL_DOCKER_NETWORK": "false",
            "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES": "false",
            "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "true",
            "TERMINAL_DOCKER_MOUNT_HOST_DATA": "false",
            "TERMINAL_DOCKER_VOLUMES": "[]",
            "TERMINAL_DOCKER_FORWARD_ENV": "[]",
            "TERMINAL_DOCKER_ENV": "{}",
            "TERMINAL_DOCKER_EXTRA_ARGS": "[]",
            "TERMINAL_DOCKER_RUN_AS_HOST_USER": "false",
            "PRODUCT_TRUTH_MODEL_WORKSPACE": str(source),
        }
    )
    assert_oracle_terminal_env(env, workspace=source, hermes_home=home)
    return env


def assert_oracle_terminal_env(
    env: Mapping[str, str],
    *,
    workspace: Path,
    hermes_home: Path,
) -> None:
    source = _require_hermetic_workspace(workspace)
    home = _require_empty_hermes_home(hermes_home)
    exact = {
        "HERMES_HOME": str(home),
        "HERMES_DISABLE_ENV_PASSTHROUGH": "1",
        "TERMINAL_ENV": "docker",
        "TERMINAL_CWD": str(source),
        "TERMINAL_DOCKER_NETWORK": "false",
        "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES": "false",
        "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "true",
        "TERMINAL_DOCKER_MOUNT_HOST_DATA": "false",
        "TERMINAL_DOCKER_VOLUMES": "[]",
        "TERMINAL_DOCKER_FORWARD_ENV": "[]",
        "TERMINAL_DOCKER_ENV": "{}",
        "TERMINAL_DOCKER_EXTRA_ARGS": "[]",
        "TERMINAL_DOCKER_RUN_AS_HOST_USER": "false",
        "PRODUCT_TRUTH_MODEL_WORKSPACE": str(source),
    }
    for key, expected in exact.items():
        if env.get(key) != expected:
            raise ValueError(f"unsafe oracle terminal setting {key}: expected {expected!r}")

    if json.loads(env["TERMINAL_DOCKER_VOLUMES"]) != []:
        raise ValueError("oracle terminal forbids arbitrary Docker volumes")
    if json.loads(env["TERMINAL_DOCKER_FORWARD_ENV"]) != []:
        raise ValueError("oracle terminal forbids forwarding host environment")
    if json.loads(env["TERMINAL_DOCKER_ENV"]) != {}:
        raise ValueError("oracle terminal forbids injected Docker environment")
    if json.loads(env["TERMINAL_DOCKER_EXTRA_ARGS"]) != []:
        raise ValueError("oracle terminal forbids unreviewed Docker extra args")
    if _truthy(env.get("TERMINAL_DOCKER_NETWORK")):
        raise ValueError("oracle terminal network must be disabled")
    if _truthy(env.get("TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES")):
        raise ValueError("oracle terminal container reuse must be disabled")
    if _truthy(env.get("TERMINAL_DOCKER_MOUNT_HOST_DATA")):
        raise ValueError("oracle terminal host credential/cache/skill mounts must be disabled")
    if not _truthy(env.get("HERMES_DISABLE_ENV_PASSTHROUGH")):
        raise ValueError("oracle terminal implicit skill/config env passthrough must be disabled")


def isolation_claim() -> dict[str, object]:
    return {
        "terminal_backend": "docker",
        "network": False,
        "persist_across_processes": False,
        "host_bind": "exact_model_workspace_only",
        "mount_host_data": False,
        "arbitrary_host_volumes": False,
        "forwarded_host_env": False,
        "implicit_env_passthrough": False,
        "config_override": False,
        "scope": "benchmark-oracle-only",
    }
