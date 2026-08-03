"""Read-only, content-aware Git snapshot and deterministic patch rendering."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SnapshotError(RuntimeError):
    """Raised when a repository cannot be snapshotted safely."""


@dataclass(frozen=True)
class SnapshotEntry:
    path: str
    status: str
    sha256: str
    mode: int


@dataclass(frozen=True)
class RepositorySnapshot:
    head: str
    digest: str
    entries: tuple[SnapshotEntry, ...]
    patch: bytes


def enforce_scope(
    snapshot: RepositorySnapshot, *, allowed_roots: tuple[str, ...], allowed_files: tuple[str, ...]
) -> None:
    """Fail closed when a changed path is outside the task's declared scope."""
    roots = tuple(root.strip("/") for root in allowed_roots)
    files = {path.strip("/") for path in allowed_files}
    for entry in snapshot.entries:
        path = entry.path.strip("/")
        if path in files or any(path == root or path.startswith(root + "/") for root in roots):
            continue
        raise SnapshotError(f"{entry.path} is outside allowed scope")


def enforce_limits(
    snapshot: RepositorySnapshot, *, max_changed_files: int, max_total_changed_bytes: int
) -> None:
    """Fail closed when the captured change set exceeds declared task limits."""
    if len(snapshot.entries) > max_changed_files:
        raise SnapshotError(f"changed-file limit exceeded: {len(snapshot.entries)} > {max_changed_files}")
    if len(snapshot.patch) > max_total_changed_bytes:
        raise SnapshotError(f"changed-byte limit exceeded: {len(snapshot.patch)} > {max_total_changed_bytes}")


def _git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SnapshotError(f"read-only git command failed: {args!r}") from exc


def _untracked_paths(repo: Path) -> tuple[str, ...]:
    raw = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    records = raw.split(b"\0")
    paths: list[str] = []
    for record in records:
        if record.startswith(b"?? "):
            paths.append(record[3:].decode("utf-8", "surrogateescape"))
    return tuple(sorted(paths))


def _untracked_patch(repo: Path, paths: tuple[str, ...]) -> bytes:
    parts: list[bytes] = []
    for path in paths:
        encoded = path.encode("utf-8", "surrogateescape")
        content = (repo / path).read_bytes()
        digest = hashlib.sha256(content).hexdigest().encode("ascii")
        parts.extend((
            b"diff --git a/" + encoded + b" b/" + encoded + b"\n",
            b"new file mode 100644\n",
            b"index 0000000.." + digest[:7] + b"\n",
            b"--- /dev/null\n",
            b"+++ b/" + encoded + b"\n",
            b"@@ bounded-engineering-untracked bytes=" + str(len(content)).encode() + b" @@\n",
            content,
            b"\n" if content and not content.endswith(b"\n") else b"",
        ))
    return b"".join(parts)


def capture_snapshot(repo: Path) -> RepositorySnapshot:
    """Capture repository state using only read-only Git commands and file reads."""
    repo = Path(repo).resolve()
    status_before = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    head = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    tracked_patch = _git(repo, "diff", "--binary", "HEAD", "--")
    tracked = tuple(sorted(
        path.decode("utf-8", "surrogateescape")
        for path in _git(repo, "diff", "--name-only", "-z", "HEAD", "--").split(b"\0")
        if path
    ))
    staged = {
        path.decode("utf-8", "surrogateescape")
        for path in _git(repo, "diff", "--cached", "--name-only", "-z", "--").split(b"\0")
        if path
    }
    untracked = _untracked_paths(repo)
    entries: list[SnapshotEntry] = []
    for path in tracked:
        file_path = repo / path
        if file_path.is_file():
            content = file_path.read_bytes()
            status = "staged" if path in staged else "unstaged"
            entries.append(SnapshotEntry(path, status, hashlib.sha256(content).hexdigest(), file_path.stat().st_mode & 0o777))
        else:
            entries.append(SnapshotEntry(path, "deleted", "", 0))
    for path in untracked:
        file_path = repo / path
        if file_path.is_symlink():
            raise SnapshotError(f"untracked symlink is forbidden: {path}")
        content = file_path.read_bytes()
        entries.append(SnapshotEntry(path, "untracked", hashlib.sha256(content).hexdigest(), file_path.stat().st_mode & 0o777))
    patch = tracked_patch + _untracked_patch(repo, untracked)
    status_after = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    tracked_patch_after = _git(repo, "diff", "--binary", "HEAD", "--")
    untracked_patch_after = _untracked_patch(repo, _untracked_paths(repo))
    if (
        status_before != status_after
        or tracked_patch != tracked_patch_after
        or patch[len(tracked_patch):] != untracked_patch_after
    ):
        raise SnapshotError("workspace changed during snapshot")
    canonical = json.dumps(
        {"head": head, "entries": [entry.__dict__ for entry in entries], "patch_sha256": hashlib.sha256(patch).hexdigest()},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return RepositorySnapshot(head, hashlib.sha256(canonical).hexdigest(), tuple(sorted(entries, key=lambda entry: entry.path)), patch)
