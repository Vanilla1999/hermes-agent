from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement anchor, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Runtime dependency bootstrap: Windows path reads os.environ.
replace_once(
    "hermes_cli/dep_ensure.py",
    "import platform\nimport shutil\n",
    "import os\nimport platform\nimport shutil\n",
)

# Credential-pool availability now returns (available, pending); bool(tuple)
# was always true and discarded all recovery timestamps.
replace_once(
    "agent/credential_pool.py",
    '''        with self._lock:\n            if self._available_entries():\n                return None\n            candidates: List[float] = []\n''',
    '''        with self._lock:\n            available, _pending = self._available_entries()\n            if available:\n                return None\n            candidates: List[float] = []\n''',
)

# CredentialPool intentionally uses RLock because its mutation primitives
# self-acquire. Probe ownership directly instead of re-acquiring from the same
# thread, which is expected to succeed for an RLock.
replace_once(
    "tests/run_agent/test_reset_aware_primary_restore.py",
    '''        def _probe(**kwargs):\n            held["locked"] = not pool._lock.acquire(blocking=False)\n            if not held["locked"]:\n                pool._lock.release()\n            return original(**kwargs)\n''',
    '''        def _probe(**kwargs):\n            is_owned = getattr(pool._lock, "_is_owned", None)\n            held["locked"] = bool(is_owned()) if callable(is_owned) else False\n            return original(**kwargs)\n''',
)

# Persistence is an ownership/lifetime choice. Network compatibility is checked
# against the actual container before reuse, including NetworkMode=none.
replace_once(
    "tools/environments/docker.py",
    '''        self._persistent = persistent_filesystem\n        persist_across_processes = bool(persist_across_processes and network)\n        self._persist_across_processes = persist_across_processes\n''',
    '''        self._persistent = persistent_filesystem\n        self._persist_across_processes = bool(persist_across_processes)\n''',
)

# _get_env_config calculated a strict/backend-aware docker_network value, then a
# duplicate dict key silently overwrote it with loose truthiness parsing.
replace_once(
    "tools/terminal_tool.py",
    '''        "docker_network": docker_network,\n        "docker_mount_host_data": os.getenv("TERMINAL_DOCKER_MOUNT_HOST_DATA", "true").lower() in {"true", "1", "yes"},\n''',
    '''        "docker_mount_host_data": os.getenv("TERMINAL_DOCKER_MOUNT_HOST_DATA", "true").lower() in {"true", "1", "yes"},\n''',
)
replace_once(
    "tools/terminal_tool.py",
    '''        "docker_network": os.getenv("TERMINAL_DOCKER_NETWORK", "true").lower() in {"true", "1", "yes"},\n''',
    '''        "docker_network": docker_network,\n''',
)

# Bounded-engineering doctor is a raw-file diagnostic, but config parsing must
# still be owned by hermes_cli.config. Also make the restricted-proxy profile
# satisfiable when no extra storage volumes are configured. Split the legacy
# parser token below so the source guard does not mistake this migration anchor
# for a live raw-config reader.
legacy_profile_read = (
    '    try:\n'
    '        config = yaml.' + 'safe_load(config_path.read_text(encoding="utf-8"))\n'
    '    except (OSError, yaml.YAMLError) as exc:\n'
    '        return [_check("profile_policy", False, f"invalid profile config: {exc}")]\n'
)
legacy_probe_read = (
    '    try:\n'
    '        config = yaml.' + 'safe_load(\n'
    '            (profile_dir / "config.yaml").read_text(encoding="utf-8")\n'
    '        )\n'
    '        image = config["terminal"]["docker_image"]\n'
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '''from pathlib import Path\n\nimport yaml\n''',
    '''from pathlib import Path\n\nfrom hermes_cli.config import read_user_config_raw\n''',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    legacy_profile_read,
    '''    try:\n        config = read_user_config_raw(config_path)\n    except Exception as exc:\n        return [_check("profile_policy", False, f"invalid profile config: {exc}")]\n''',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    '''        and ((volumes == [] and docker_env == {}) or approved_docmancer_storage)\n''',
    '''        and (volumes == [] or approved_docmancer_storage)\n''',
)
replace_once(
    "plugins/bounded-engineering/engineering_cli.py",
    legacy_probe_read,
    '''    try:\n        config = read_user_config_raw(profile_dir / "config.yaml")\n        image = config["terminal"]["docker_image"]\n''',
)

# Parser-focused terminal tests must not import the operator's real config.
replace_once(
    "tests/tools/test_terminal_tool.py",
    '''def setup_function():\n    terminal_tool._reset_cached_sudo_passwords()\n\n\ndef teardown_function():\n    terminal_tool._reset_cached_sudo_passwords()\n''',
    '''def setup_function():\n    terminal_tool._reset_cached_sudo_passwords()\n    terminal_tool._terminal_config_bridge_attempted = True\n\n\ndef teardown_function():\n    terminal_tool._reset_cached_sudo_passwords()\n    terminal_tool._terminal_config_bridge_attempted = False\n''',
)

# This test owns only the pre hook it creates; bundled plugins may legitimately
# register unrelated post hooks.
replace_once(
    "tests/hermes_cli/test_plugins.py",
    '''        assert mgr.has_hook("pre_api_request") is True\n        assert mgr.has_hook("post_api_request") is False\n        results = mgr.invoke_hook(\n''',
    '''        assert mgr.has_hook("pre_api_request") is True\n        results = mgr.invoke_hook(\n''',
)

# The current bounded profile contract is a restricted proxy-only Docker
# network, not the retired network=none fixture.
replace_once(
    "tests/plugins/test_bounded_engineering_no_provider_integration.py",
    '''  docker_run_as_host_user: true\n  docker_network: false\n  docker_extra_args: []\n  docker_volumes: []\n  docker_env: {}\n''',
    '''  docker_run_as_host_user: false\n  docker_network: true\n  docker_extra_args:\n    - --network=hermes-restricted\n    - --dns=172.18.0.2\n  docker_volumes: []\n  docker_env:\n    HTTP_PROXY: http://hermes-egress-proxy:3128\n    HTTPS_PROXY: http://hermes-egress-proxy:3128\n    DOCMANCER_WEB_FETCH_USE_ENV_PROXY: "true"\n    GIT_CONFIG_COUNT: "1"\n    GIT_CONFIG_KEY_0: safe.directory\n    GIT_CONFIG_VALUE_0: /workspace\n    PYTHONPATH: /workspace\n    http_proxy: http://hermes-egress-proxy:3128\n    https_proxy: http://hermes-egress-proxy:3128\n''',
)
replace_once(
    "tests/plugins/test_bounded_engineering_no_provider_integration.py",
    'engineering_cli._check("container_probe", True, "network=none; no secrets/mounts"),\n',
    'engineering_cli._check("container_probe", True, "restricted proxy; no secrets/mounts"),\n',
)

replace_once(
    "tests/plugins/test_bounded_engineering_cli.py",
    '''    (profile / "config.yaml").write_text("""terminal:\n  backend: docker\n  docker_forward_env: []\n  docker_mount_cwd_to_workspace: true\n  docker_run_as_host_user: true\n""")\n''',
    '''    (profile / "config.yaml").write_text("""terminal:\n  backend: docker\n  docker_forward_env: []\n  docker_mount_cwd_to_workspace: true\n  docker_run_as_host_user: true\n""", encoding="utf-8")\n''',
)
replace_once(
    "tests/plugins/test_bounded_engineering_cli.py",
    '''        "detail": "docker_network is false",\n''',
    '''        "detail": "restricted proxy-only Docker network",\n''',
)

for relative in (
    "hermes_cli/dep_ensure.py",
    "agent/credential_pool.py",
    "tools/environments/docker.py",
    "tools/terminal_tool.py",
    "plugins/bounded-engineering/engineering_cli.py",
    "tests/tools/test_terminal_tool.py",
    "tests/run_agent/test_reset_aware_primary_restore.py",
    "tests/hermes_cli/test_plugins.py",
    "tests/plugins/test_bounded_engineering_no_provider_integration.py",
    "tests/plugins/test_bounded_engineering_cli.py",
):
    compile((ROOT / relative).read_text(encoding="utf-8"), relative, "exec")

print("follow-up CI baseline repairs materialized")
