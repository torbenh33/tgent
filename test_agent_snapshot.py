"""Tests for independent reflink snapshots of Git checkouts."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_snapshot import SnapshotError, create_project_snapshot, remove_project_snapshot


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    _git(path, "config", "user.name", "Snapshot Test")
    _git(path, "config", "user.email", "snapshot@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "initial")


def _snapshot_or_skip(source: Path, destination: Path):
    try:
        return create_project_snapshot(source, destination)
    except SnapshotError as err:
        if "reflink" in str(err).lower() or "operation not supported" in str(err).lower():
            pytest.skip(f"Filesystem does not support required reflink copies: {err}")
        raise


def _assert_snapshot_is_independent(source: Path, snapshot_root: Path) -> None:
    source_head = _git(source, "rev-parse", "HEAD")
    snapshot = _snapshot_or_skip(source, snapshot_root)
    try:
        assert snapshot.base_commit == source_head
        assert _git(snapshot.project_root, "rev-parse", "HEAD") == source_head
        assert snapshot.project_root != source

        (snapshot.project_root / "tracked.txt").write_text("snapshot edit\n", encoding="utf-8")
        _git(snapshot.project_root, "add", "tracked.txt")
        _git(snapshot.project_root, "commit", "-m", "snapshot-only change")

        assert _git(snapshot.project_root, "rev-parse", "HEAD") != source_head
        assert _git(source, "rev-parse", "HEAD") == source_head
        assert (source / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    finally:
        remove_project_snapshot(snapshot)
    assert not snapshot_root.exists()
    assert _git(source, "rev-parse", "HEAD") == source_head


def test_snapshot_regular_checkout_is_independent(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    _init_repo(source)

    _assert_snapshot_is_independent(source, tmp_path / "snapshot")


def test_snapshot_linked_worktree_is_independent(tmp_path: Path) -> None:
    main_repo = tmp_path / "main"
    _init_repo(main_repo)
    linked = tmp_path / "linked"
    _git(main_repo, "worktree", "add", "-b", "agent-source", str(linked))

    original_common = Path(_git(linked, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    original_admin_entries = sorted(path.name for path in (original_common / "worktrees").iterdir())

    snapshot_root = tmp_path / "snapshot-linked"
    snapshot = _snapshot_or_skip(linked, snapshot_root)
    try:
        assert snapshot.linked_worktree
        assert _git(snapshot.project_root, "rev-parse", "--show-toplevel") == str(snapshot.project_root)
        copied_common = Path(_git(snapshot.project_root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        assert copied_common.is_relative_to(snapshot.root)

        base = snapshot.base_commit
        (snapshot.project_root / "tracked.txt").write_text("linked snapshot edit\n", encoding="utf-8")
        _git(snapshot.project_root, "add", "tracked.txt")
        _git(snapshot.project_root, "commit", "-m", "linked snapshot-only change")

        assert _git(snapshot.project_root, "rev-parse", "HEAD") != base
        assert _git(linked, "rev-parse", "HEAD") == base
        assert _git(main_repo, "rev-parse", "HEAD") == base
        assert (linked / "tracked.txt").read_text(encoding="utf-8") == "base\n"
        assert sorted(path.name for path in (original_common / "worktrees").iterdir()) == original_admin_entries
    finally:
        remove_project_snapshot(snapshot)

    assert not snapshot_root.exists()
    assert _git(linked, "rev-parse", "HEAD") == _git(main_repo, "rev-parse", "HEAD")
