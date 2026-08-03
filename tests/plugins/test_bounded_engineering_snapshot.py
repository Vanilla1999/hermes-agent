"""Read-only Git snapshot tests for bounded-engineering."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bounded-engineering"
sys.path.insert(0, str(PLUGIN_DIR))

import snapshot as snapshot_module  # noqa: E402
from snapshot import SnapshotError, capture_snapshot, enforce_limits, enforce_scope  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "baseline")
    return repo


def test_capture_snapshot_of_clean_baseline_is_deterministic_and_read_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    first = capture_snapshot(repo)
    second = capture_snapshot(repo)

    assert first.digest == second.digest
    assert first.entries == ()
    assert first.head == head_before
    assert first.patch == b""
    assert subprocess.check_output(["git", "status", "--porcelain=v2"], cwd=repo) == b""


def test_capture_snapshot_digest_changes_when_untracked_content_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    untracked = repo / "new.txt"
    untracked.write_text("one\n", encoding="utf-8")
    first = capture_snapshot(repo)
    untracked.write_text("two\n", encoding="utf-8")
    second = capture_snapshot(repo)

    assert first.digest != second.digest
    assert first.patch != second.patch
    assert [entry.path for entry in second.entries] == ["new.txt"]


def test_capture_snapshot_records_tracked_worktree_content(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tracked = repo / "tracked.txt"
    tracked.write_text("changed\n", encoding="utf-8")

    snapshot = capture_snapshot(repo)

    assert [(entry.path, entry.status) for entry in snapshot.entries] == [("tracked.txt", "unstaged")]
    assert "changed" in snapshot.patch.decode("utf-8")


def test_enforce_scope_rejects_precise_unallowed_changed_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "outside.txt").write_text("not allowed\n", encoding="utf-8")
    snapshot = capture_snapshot(repo)

    with pytest.raises(SnapshotError, match=r"outside.txt is outside allowed scope"):
        enforce_scope(snapshot, allowed_roots=("src",), allowed_files=())


def test_capture_snapshot_rejects_untracked_symlink(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "link").symlink_to("tracked.txt")

    with pytest.raises(SnapshotError, match="symlink"):
        capture_snapshot(repo)


def test_enforce_limits_rejects_too_many_changed_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "one.txt").write_text("one\n", encoding="utf-8")
    (repo / "two.txt").write_text("two\n", encoding="utf-8")

    with pytest.raises(SnapshotError, match="changed-file limit"):
        enforce_limits(capture_snapshot(repo), max_changed_files=1, max_total_changed_bytes=1024)


def test_capture_snapshot_rejects_concurrent_workspace_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("first\n", encoding="utf-8")
    original_git = snapshot_module._git

    def mutate_after_diff(repo_path: Path, *args: str) -> bytes:
        result = original_git(repo_path, *args)
        if args[:2] == ("diff", "--binary"):
            (repo / "tracked.txt").write_text("second\n", encoding="utf-8")
        return result

    monkeypatch.setattr(snapshot_module, "_git", mutate_after_diff)

    with pytest.raises(SnapshotError, match="workspace changed during snapshot"):
        capture_snapshot(repo)


def test_capture_snapshot_rejects_concurrent_untracked_content_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    untracked = repo / "new.txt"
    untracked.write_text("first\n", encoding="utf-8")
    original_patch = snapshot_module._untracked_patch
    calls = 0

    def mutate_after_untracked_patch(repo_path: Path, paths: tuple[str, ...]) -> bytes:
        nonlocal calls
        result = original_patch(repo_path, paths)
        calls += 1
        if calls == 1:
            untracked.write_text("second\n", encoding="utf-8")
        return result

    monkeypatch.setattr(snapshot_module, "_untracked_patch", mutate_after_untracked_patch)

    with pytest.raises(SnapshotError, match="workspace changed during snapshot"):
        capture_snapshot(repo)


def test_capture_snapshot_marks_staged_change_as_staged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")

    snapshot = capture_snapshot(repo)

    assert [(entry.path, entry.status) for entry in snapshot.entries] == [("tracked.txt", "staged")]
