"""Deterministic, provider-free bounded engineering task creation."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Iterator, NamedTuple

TENANT = "bounded-engineering/v1"
WORKER_SKILL = "bounded-engineering:bounded-engineering-worker"
_WRITER_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "review"}
CREATION_LOCK_TIMEOUT = 15.0
CREATION_LOCK_POLL_INTERVAL = 0.025

class CreationError(RuntimeError):
    """Creation could not be proved safe and deterministic."""


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CreationError(f"git_failed: {exc}") from exc
    if result.returncode:
        raise CreationError(f"git_failed: {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def _slug(title: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    return (value[:40].rstrip("-") or "task")


def _body_field(body: str | None, name: str) -> str | None:
    prefix = f"{name}: "
    for line in (body or "").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
    return None


def _verify_worktree(workspace: Path, branch: str, baseline: str, *, require_clean: bool) -> None:
    if not workspace.is_dir():
        raise CreationError(f"workspace_missing: {workspace}")
    if Path(_git(workspace, "rev-parse", "--show-toplevel")).resolve() != workspace.resolve():
        raise CreationError("workspace is not the exact Git worktree top level")
    if _git(workspace, "branch", "--show-current") != branch:
        raise CreationError("workspace branch does not match deterministic branch")
    if _git(workspace, "rev-parse", "HEAD") != baseline:
        raise CreationError("workspace HEAD does not match baseline")
    if require_clean and _git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CreationError("new workspace is not clean")


def _branch_oid(root: Path, branch: str) -> str | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _owned_worktree(root: Path, workspace: Path, branch: str) -> bool:
    """Return true only for an exact registered path/branch pairing."""
    try:
        lines = _git(root, "worktree", "list", "--porcelain").splitlines()
    except CreationError:
        return False
    current_path = None
    for line in (*lines, ""):
        if line.startswith("worktree "):
            current_path = Path(line[9:]).resolve()
        elif line.startswith("branch ") and current_path == workspace.resolve():
            return line[7:] == f"refs/heads/{branch}"
        elif not line:
            current_path = None
    return False


class _WorktreeReceipt(NamedTuple):
    gitdir: Path
    gitdir_identity: tuple[int, int]
    dotgit_identity: tuple[int, int]


def _worktree_receipt(workspace: Path) -> _WorktreeReceipt:
    gitdir = Path(_git(workspace, "rev-parse", "--absolute-git-dir")).resolve()
    gs, ds = gitdir.stat(), (workspace / ".git").stat()
    return _WorktreeReceipt(gitdir, (gs.st_dev, gs.st_ino), (ds.st_dev, ds.st_ino))


def _preserve_worktree_unless_rollback_is_provably_safe(root: Path, workspace: Path, branch: str,
                      receipt: _WorktreeReceipt | None) -> None:
    """Preserve residue unless ownership-safe destructive rollback is possible.

    The branch is intentionally retained: Git has no ownership-aware CAS that
    distinguishes same-OID deletion/recreation by another actor.
    """
    if receipt is None or not _owned_worktree(root, workspace, branch):
        return
    try:
        if _worktree_receipt(workspace) != receipt:
            return
    except (CreationError, OSError):
        return
    # There is no inode-conditioned ``git worktree remove`` operation.  A
    # pathname can be exchanged after the receipt check but before Git opens
    # it, so even a valid receipt cannot prove *continuing* ownership across a
    # destructive subprocess call.  Preserve the registered residue and let
    # the failing caller report it; a later explicit operator action can inspect
    # it.  This is intentionally fail-closed rather than a check-then-delete.
    return


def _materialize_worktree(root: Path, workspace: Path, branch: str, baseline: str) -> None:
    path_was_absent = not workspace.exists()
    if not path_was_absent:
        raise CreationError(f"refusing to reuse unowned workspace path: {workspace}")
    branch_was_absent = _branch_oid(root, branch) is None
    if not branch_was_absent:
        raise CreationError(f"refusing to reuse unowned branch: {branch}")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    receipt = None
    try:
        _git(root, "worktree", "add", "-b", branch, str(workspace), baseline)
        receipt = _worktree_receipt(workspace)
        _verify_worktree(workspace, branch, baseline, require_clean=True)
    except Exception:
        _preserve_worktree_unless_rollback_is_provably_safe(root, workspace, branch, receipt)
        raise


@contextlib.contextmanager
def creation_lock(home: Path, board: str, project: str, *,
                  timeout: float = CREATION_LOCK_TIMEOUT) -> Iterator[None]:
    """Cross-process advisory serialization, failing closed on any lock error."""
    identity = json.dumps([board, TENANT, project], separators=(",", ":")).encode()
    digest = hashlib.sha256(identity).hexdigest()
    directory = home / "engineering" / "locks"
    fd: int | None = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"create-{digest}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise CreationError(f"creation_lock_unavailable: {exc}") from exc

    try:
        if os.name == "nt":
            import msvcrt

            def try_lock() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def unlock() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def try_lock() -> None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock() -> None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    except (ImportError, OSError) as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        raise CreationError(f"creation_lock_unavailable: {exc}") from exc

    deadline = time.monotonic() + timeout
    acquired = False
    operation_error: BaseException | None = None
    try:
        while True:
            try:
                try_lock()
                acquired = True
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise CreationError("creation_lock_timeout")
                time.sleep(min(CREATION_LOCK_POLL_INTERVAL,
                               max(0.0, deadline - time.monotonic())))
            except OSError as exc:
                raise CreationError(f"creation_lock_unavailable: {exc}") from exc
        yield
    except BaseException as exc:
        operation_error = exc
    finally:
        cleanup_error: OSError | None = None
        try:
            if acquired:
                try:
                    unlock()
                except OSError as exc:
                    cleanup_error = exc
        finally:
            try:
                os.close(fd)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if operation_error is not None:
            raise operation_error
        if cleanup_error is not None:
            raise CreationError(f"creation_lock_cleanup_failed: {cleanup_error}") from cleanup_error


def create(args) -> dict[str, object]:
    """Validate inputs and create exactly one native task under the creation lock."""
    if __package__ and __import__("sys").modules.get(__package__) is not None:
        from .body import render_compact_body
        from .contract import load_contract
        from .spec import canonicalize_spec, validate_external_evidence
        from .storage import store_immutable_json
    else:  # Direct-module tests and scripts.
        from body import render_compact_body
        from contract import load_contract
        from spec import canonicalize_spec, validate_external_evidence
        from storage import store_immutable_json
    from hermes_cli import kanban_db as kb

    repo_arg = Path(args.repo).expanduser()
    root = Path(_git(repo_arg, "rev-parse", "--show-toplevel")).resolve()
    if root != repo_arg.resolve():
        raise CreationError("repository path must be the Git top level")
    contract = load_contract(root)
    if contract.tenant != TENANT or contract.completion_gate != TENANT:
        raise CreationError("contract tenant and completion_gate must be bounded-engineering/v1")
    if contract.profile != "bounded-engineer":
        raise CreationError("contract profile must be bounded-engineer")
    try:
        raw = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CreationError(f"cannot read canonical task spec: {exc}") from exc
    spec = canonicalize_spec(raw, verification_ids={v.id for v in contract.verification})
    validate_external_evidence(raw.get("external_evidence", []), repo_root=root)
    if raw["risk"] == "security_or_migration":
        raise CreationError("security_or_migration tasks cannot be dispatched in MVP")
    baseline = _git(root, "rev-parse", "HEAD")
    benchmark = raw.get("benchmark")
    expected_baseline = getattr(args, "expected_baseline_head", None)
    if isinstance(benchmark, dict) and benchmark.get("baseline_head") != baseline:
        raise CreationError("benchmark baseline_head does not match repository HEAD")
    if expected_baseline is not None and expected_baseline != baseline:
        raise CreationError("expected baseline does not match repository HEAD")
    if contract.workspace.get("require_clean_source") is True:
        if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise CreationError("source repository is dirty")
    contract_runtime = contract.execution.get("default_max_runtime_seconds")
    contract_retries = contract.execution.get("default_max_retries")
    runtime = min(v for v in (contract_runtime, raw.get("max_runtime_seconds")) if v is not None)
    retries = min(v for v in (contract_retries, raw.get("max_retries")) if v is not None)
    if not isinstance(runtime, int) or isinstance(runtime, bool) or runtime < 1:
        raise CreationError("max_runtime_seconds must be a positive integer")
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise CreationError("max_retries must be a non-negative integer")
    project = contract.project_id
    key = f"bounded-engineering:v1:{project}:{baseline}:{spec.sha256}"
    branch = f"be/{_slug(raw['title'])}-{spec.sha256[:8]}"
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    workspace = home / "engineering" / "worktrees" / project / spec.sha256

    with creation_lock(home, args.board, project):
        locked_baseline = _git(root, "rev-parse", "HEAD")
        if locked_baseline != baseline or (
            isinstance(benchmark, dict) and benchmark.get("baseline_head") != locked_baseline
        ):
            raise CreationError("repository HEAD changed before task creation")
        with kb.connect_closing(board=args.board) as conn:
            tasks = kb.list_tasks(conn, tenant=TENANT, include_archived=True)
            expected_revision = getattr(args, "expected_board_revision", None)
            if expected_revision is not None:
                if __package__ and __import__("sys").modules.get(__package__) is not None:
                    from .plan_cli import preflight_apply
                else:
                    from plan_cli import preflight_apply
                same = preflight_apply(tasks, expected_revision=expected_revision,
                                       idempotency_key=key)
            else:
                same = next((t for t in tasks if t.idempotency_key == key and t.status != "archived"), None)
            if same is not None:
                _verify_worktree(workspace, branch, baseline, require_clean=False)
                return _result(same.id, spec.sha256, contract.sha256, baseline, args.board, branch, workspace, same.status, True)
            active = next((
                t for t in tasks
                if t.status in _WRITER_STATUSES and (
                    t.project_id == project
                    or _body_field(getattr(t, "body", None), "repository") == str(root)
                )
            ), None)
            if active is not None:
                raise CreationError(f"active_writer_exists: {active.id}")
            # The exact clean workspace must exist before the card becomes visible.
            _materialize_worktree(root, workspace, branch, baseline)
            try:
                # Immutable storage occurs only after the duplicate/active checks.
                store_immutable_json(home, spec.canonical_json)
                contract_path = store_immutable_json(home, contract.canonical_json, _namespace="contracts")
                if contract_path.stem != contract.sha256:
                    raise CreationError("stored contract digest does not match validated contract")
                body = render_compact_body(
                    spec_sha256=spec.sha256, contract_sha256=contract.sha256,
                    baseline_head=baseline, repository=str(root), objective=raw["objective"],
                    acceptance=((a["id"], a["text"]) for a in raw["acceptance"]),
                    allowed_paths=(*spec.allowed_roots, *spec.allowed_files),
                    verification_ids=raw["required_verification_ids"], risk=raw["risk"],
                    diagnostic_context=raw.get("diagnostic_context"),
                    benchmark=raw.get("benchmark"),
                )
                task_id = kb.create_task(
                    conn, title=raw["title"], body=body, assignee="bounded-engineer",
                    created_by="hermes engineering create", workspace_kind="worktree",
                    workspace_path=str(workspace), branch_name=branch, tenant=TENANT,
                    idempotency_key=key, max_runtime_seconds=runtime,
                    skills=[WORKER_SKILL], max_retries=retries, goal_mode=False,
                    initial_status="running", completion_gate=TENANT, board=args.board,
                    project_id=project,
                )
            except Exception as exc:
                # Native create_task commits internally. If it committed and
                # then raised, the visible matching card owns the workspace.
                visible = next((t for t in kb.list_tasks(conn, tenant=TENANT, include_archived=True)
                                if t.idempotency_key == key and t.status != "archived"), None)
                if visible is not None:
                    task_id = visible.id
                else:
                    _preserve_worktree_unless_rollback_is_provably_safe(root, workspace, branch, _worktree_receipt(workspace))
                    raise CreationError(f"card creation failed; uncertain worktree and branch preserved: {exc}") from exc
            task = kb.get_task(conn, task_id)
            if task is None:
                raise CreationError("native create_task did not return the created task")
    return _result(task_id, spec.sha256, contract.sha256, baseline, args.board, branch, workspace, task.status, False)


def _result(task_id: str, spec: str, contract: str, baseline: str, board: str,
            branch: str, workspace: Path, status: str, reused: bool) -> dict[str, object]:
    return {"schema_version": 1, "task_id": task_id, "spec_sha256": spec,
            "contract_sha256": contract, "baseline_head": baseline, "board": board,
            "branch": branch, "workspace_path": str(workspace), "status": status,
            "reused": reused, "status_command": f"hermes engineering status --board {board} --task {task_id} --json"}
