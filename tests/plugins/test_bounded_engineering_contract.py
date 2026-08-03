"""Pure validation tests for the bounded-engineering repository contract."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

from contract import ContractValidationError, load_contract  # noqa: E402


def _write_contract(repo: Path, body: str) -> Path:
    path = repo / ".hermes" / "engineering.toml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_load_contract_normalizes_valid_v1_contract(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_contract(
        repo,
        """
schema_version = 1
project_id = "example"
profile = "bounded-engineer"
tenant = "bounded-engineering/v1"
completion_gate = "bounded-engineering/v1"

[workspace]
kind = "worktree"
require_clean_source = true
require_clean_task_baseline = true
allow_commits = false
allow_submodule_changes = false
allow_new_symlinks = false
max_changed_files = 24
max_total_changed_bytes = 262144
max_single_file_bytes = 131072
max_binary_files = 0

[context]
target_bytes = 6144
hard_bytes = 12288
project_map_max_bytes = 4096
latest_failure_max_bytes = 2048
max_evidence_refs = 12

[sandbox]
terminal_backend = "docker"
network = false
mount_workspace = true
run_as_host_user = true
forward_env = []
require_no_host_home_mount = true
require_no_docker_socket = true
require_no_ssh_agent = true
memory = "4g"
cpus = 4
pids_limit = 512

[execution]
default_max_runtime_seconds = 1800
default_max_retries = 2
max_active_writer_tasks = 1
goal_mode = false
auto_decompose = false
allow_delegate_task = false
allow_child_cards = false

[policy]
forbidden_tools = ["web_search"]
forbidden_git_subcommands = ["push"]
forbidden_command_tokens = ["sudo"]

[[verification]]
id = "focused"
kind = "test"
argv = ["python3", "-m", "pytest", "tests/focused", "-q"]
timeout_seconds = 600
parser = "pytest"
minimum_collected = 1
required = true
""",
    )

    contract = load_contract(repo)

    assert contract.project_id == "example"
    assert contract.completion_gate == "bounded-engineering/v1"
    assert contract.verification[0].argv == ("python3", "-m", "pytest", "tests/focused", "-q")
    assert len(contract.sha256) == 64


def test_load_contract_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_contract(repo, 'schema_version = 1\nproject_id = "x"\nunexpected = true\n')

    with pytest.raises(ContractValidationError, match="unknown key: unexpected"):
        load_contract(repo)


def test_load_contract_rejects_workspace_limit_above_hard_ceiling(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_contract(
        repo,
        """
schema_version = 1
project_id = "example"
profile = "bounded-engineer"
tenant = "bounded-engineering/v1"
completion_gate = "bounded-engineering/v1"

[workspace]
kind = "worktree"
allow_commits = false
max_changed_files = 101

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
""",
    )

    with pytest.raises(ContractValidationError, match="workspace.max_changed_files"):
        load_contract(repo)


def test_load_contract_rejects_more_than_sixteen_verification_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    verification = "\n".join(
        f'''[[verification]]
id = "check-{index}"
kind = "test"
argv = ["python3", "-m", "pytest", "tests", "-q"]
timeout_seconds = 60
parser = "pytest"
required = true'''
        for index in range(17)
    )
    _write_contract(
        repo,
        f'''schema_version = 1
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

{verification}
''',
    )

    with pytest.raises(ContractValidationError, match="at most 16"):
        load_contract(repo)


def test_load_contract_rejects_verification_executable_path_traversal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_contract(
        repo,
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
argv = ["../outside/pytest", "-q"]
timeout_seconds = 60
parser = "pytest"
required = true
''',
    )

    with pytest.raises(ContractValidationError, match="executable path traversal"):
        load_contract(repo)
