"""Create and remove snapshots of Git checkouts.

Directory contents are copied with GNU ``cp --reflink=auto``, which uses
reflinks when supported and falls back to ordinary copies otherwise. Linked
worktrees need special handling because their ``.git`` file points into shared
Git metadata; the complete common Git directory is copied beside the snapshot
and the copied worktree metadata is redirected to that copy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_project import ProjectRootError, discover_project_root


class SnapshotError(RuntimeError):
    """Raised when a safe, independent checkout snapshot cannot be created."""


_SNAPSHOT_MARKER = ".agent-snapshot"


@dataclass(frozen=True)
class ProjectSnapshot:
    """Paths and Git identity for one private agent snapshot."""

    root: Path
    project_root: Path
    base_commit: str
    linked_worktree: bool

def _git_path(project_root: Path, *arguments: str) -> Path:
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SnapshotError(f"Could not inspect Git metadata ({' '.join(arguments)}): {err}") from err
    if proc.returncode != 0:
        raise SnapshotError(proc.stderr.strip() or f"git rev-parse {' '.join(arguments)} failed")
    path = Path(proc.stdout.strip())
    return (project_root / path).resolve() if not path.is_absolute() else path.resolve()


def _base_commit(project_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SnapshotError(f"Could not determine snapshot base commit: {err}") from err
    if proc.returncode != 0:
        raise SnapshotError(proc.stderr.strip() or "HEAD does not resolve to a commit")
    return proc.stdout.strip()


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a directory tree, using reflinks when supported."""
    try:
        proc = subprocess.run(
            ["cp", "-a", "--reflink=auto", "--", str(source), str(destination)],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SnapshotError(f"Could not reflink-copy {source}: {err}") from err
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "cp failed"
        raise SnapshotError(f"Could not create reflink snapshot of {source}: {detail}")

def _validate_self_contained_source(project_root: Path, git_dir: Path, common_dir: Path) -> None:
    """Reject repository features that could retain references outside the copy."""
    alternates = {git_dir / "objects" / "info" / "alternates", common_dir / "objects" / "info" / "alternates"}
    if any(path.exists() for path in alternates):
        raise SnapshotError("Git object alternates are unsupported; refusing a non-self-contained snapshot")

    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--stage", "-z"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise SnapshotError(f"Could not inspect tracked files for submodules: {err}") from err
    if proc.returncode != 0:
        raise SnapshotError(proc.stderr.decode("utf-8", errors="replace").strip() or "git ls-files failed")
    if any(entry.startswith(b"160000 ") for entry in proc.stdout.split(b"\x00") if entry):
        raise SnapshotError("Repositories containing submodules are not yet supported for snapshots")


def _worktree_admin_dir(project_root: Path, git_dir: Path, common_dir: Path) -> Path:
    """Validate that a linked-worktree gitdir is an admin dir in common_dir."""
    try:
        relative = git_dir.relative_to(common_dir)
    except ValueError as err:
        raise SnapshotError("Linked worktree Git directory is outside its common Git directory") from err
    if len(relative.parts) != 2 or relative.parts[0] != "worktrees":
        raise SnapshotError(f"Unsupported linked-worktree Git directory layout: {git_dir}")
    if not (git_dir / "commondir").is_file() or not (git_dir / "gitdir").is_file():
        raise SnapshotError(f"Incomplete linked-worktree metadata: {git_dir}")
    git_file = project_root / ".git"
    if not git_file.is_file() or git_file.is_symlink():
        raise SnapshotError("Expected a regular .git pointer file for linked worktree")
    return relative


def create_project_snapshot(
    source_root: str | os.PathLike[str],
    snapshot_root: str | os.PathLike[str],
) -> ProjectSnapshot:
    """Create an independent snapshot beneath a new destination root.

    ``snapshot_root`` must not already exist. The checkout is placed at its
    ``project`` child, with copied linked-worktree common metadata at
    ``git-common`` when needed. Callers can bind ``project_root`` at the
    source checkout's original absolute path inside Bubblewrap.

    The source must be a Git repository root. Copy failure, unsupported Git
    layouts, or verification failure remove the partial destination and raise
    ``SnapshotError``.
    """
    try:
        source = discover_project_root(source_root)
    except ProjectRootError as err:
        raise SnapshotError(str(err)) from err
    destination = Path(snapshot_root).expanduser().absolute()
    if destination.exists():
        raise SnapshotError(f"Snapshot destination already exists: {destination}")
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise SnapshotError("Snapshot destination must not overlap the source checkout")

    git_entry = source / ".git"
    if git_entry.is_symlink():
        raise SnapshotError("Refusing to snapshot a checkout with a symlinked .git entry")
    if not (git_entry.is_dir() or git_entry.is_file()):
        raise SnapshotError("Checkout .git entry must be a directory or linked-worktree pointer file")

    git_dir = _git_path(source, "--absolute-git-dir")
    common_dir = _git_path(source, "--path-format=absolute", "--git-common-dir")
    linked = git_entry.is_file()
    if not linked and git_dir != (source / ".git").resolve():
        raise SnapshotError("Unsupported nonstandard Git directory; refusing a potentially shared metadata copy")
    admin_relative = _worktree_admin_dir(source, git_dir, common_dir) if linked else None
    _validate_self_contained_source(source, git_dir, common_dir)
    commit = _base_commit(source)

    try:
        destination.mkdir(parents=True, mode=0o700)
        project_copy = destination / "project"
        _copy_tree(source, project_copy)

        if linked:
            common_copy = destination / "git-common"
            _copy_tree(common_dir, common_copy)
            assert admin_relative is not None
            copied_admin = common_copy / admin_relative
            if not copied_admin.is_dir() or not (copied_admin / "gitdir").is_file():
                raise SnapshotError("Copied Git metadata is missing linked-worktree admin directory")
            copied_worktrees = common_copy / "worktrees"
            for other_admin in copied_worktrees.iterdir():
                if other_admin != copied_admin:
                    if other_admin.is_symlink() or other_admin.is_file():
                        other_admin.unlink()
                    else:
                        shutil.rmtree(other_admin)
            (copied_admin / "gitdir").write_text(f"{project_copy / '.git'}\n", encoding="utf-8")
            (project_copy / ".git").write_text(f"gitdir: {copied_admin}\n", encoding="utf-8")

        verify = subprocess.run(
            ["git", "-C", str(project_copy), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if verify.returncode != 0 or Path(verify.stdout.strip()).resolve() != project_copy.resolve():
            detail = verify.stderr.strip() or "snapshot Git root did not resolve to the copied project"
            raise SnapshotError(f"Snapshot Git metadata is not independent: {detail}")
        snapshot_commit = _base_commit(project_copy)
        if snapshot_commit != commit:
            raise SnapshotError("Snapshot HEAD differs from source HEAD")
        (destination / _SNAPSHOT_MARKER).write_text("agent snapshot v1\n", encoding="utf-8")

        return ProjectSnapshot(
            root=destination,
            project_root=project_copy,
            base_commit=commit,
            linked_worktree=linked,
        )
    except Exception as err:
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(err, SnapshotError):
            raise
        raise SnapshotError(f"Failed to create snapshot: {err}") from err


def remove_project_snapshot(snapshot: ProjectSnapshot | str | os.PathLike[str]) -> None:
    """Remove a snapshot root, rejecting filesystem roots and symlink roots."""
    root = snapshot.root if isinstance(snapshot, ProjectSnapshot) else Path(snapshot)
    root = root.expanduser().absolute()
    if root.is_symlink():
        raise SnapshotError(f"Refusing to remove symlink snapshot root: {root}")
    if root == Path(root.anchor):
        raise SnapshotError("Refusing to remove filesystem root")
    if root.exists() and not (root / "project").exists():
        raise SnapshotError(f"Refusing to remove directory that is not an agent snapshot: {root}")
    if root.exists():
        marker = root / _SNAPSHOT_MARKER
        if not marker.is_file() or marker.is_symlink():
            raise SnapshotError(f"Refusing to remove directory without an agent snapshot marker: {root}")
        shutil.rmtree(root)
