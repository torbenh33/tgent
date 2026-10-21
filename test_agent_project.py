"""Tests for project root discovery and sandbox config validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_project import (
    ProjectConfigError,
    ProjectRootError,
    discover_project_root,
    load_project_config,
)


def _write_config(root: Path, text: str) -> None:
    (root / ".agent-sandbox.toml").write_text(text, encoding="utf-8")


def test_missing_config_requests_no_home_mounts(tmp_path: Path) -> None:
    config = load_project_config(tmp_path, home=tmp_path / "home")

    assert config.root == tmp_path.resolve()
    assert config.home_read_only == ()
    assert config.home_read_write == ()


def test_loads_home_mount_requests_as_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "project"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    (home / ".cache").mkdir()
    (home / "data.txt").write_text("data", encoding="utf-8")
    _write_config(
        root,
        'version = 1\n[home]\nread_only = [".cache"]\nread_write = ["data.txt"]\n',
    )

    config = load_project_config(root, home=home)

    assert config.home_read_only == (Path(".cache"),)
    assert config.home_read_write == (Path("data.txt"),)


def test_rejects_unknown_config_keys(tmp_path: Path) -> None:
    _write_config(tmp_path, 'version = 1\nnetwork = true\n')

    with pytest.raises(ProjectConfigError, match="Unknown key"):
        load_project_config(tmp_path, home=tmp_path)


def test_rejects_unsupported_config_version(tmp_path: Path) -> None:
    _write_config(tmp_path, "version = 2\n")

    with pytest.raises(ProjectConfigError, match="version must be 1"):
        load_project_config(tmp_path, home=tmp_path)


@pytest.mark.parametrize("requested", ["../outside", "/etc/passwd", "", "./data", "a//b"])
def test_rejects_non_normalized_home_paths(tmp_path: Path, requested: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_config(tmp_path, f'version = 1\n[home]\nread_only = ["{requested}"]\n')

    with pytest.raises(ProjectConfigError, match="normalized home-relative path"):
        load_project_config(tmp_path, home=home)


@pytest.mark.parametrize("requested", [".ssh", ".aws", ".config/gcloud", "credentials", ".netrc"])
def test_rejects_sensitive_home_paths(tmp_path: Path, requested: str) -> None:
    home = tmp_path / "home"
    sensitive = home / requested
    sensitive.mkdir(parents=True)
    _write_config(tmp_path, f'version = 1\n[home]\nread_only = ["{requested}"]\n')

    with pytest.raises(ProjectConfigError, match="sensitive"):
        load_project_config(tmp_path, home=home)


def test_rejects_symlinked_home_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (home / "linked").symlink_to(target, target_is_directory=True)
    _write_config(tmp_path, 'version = 1\n[home]\nread_only = ["linked"]\n')

    with pytest.raises(ProjectConfigError, match="must not contain symlinks"):
        load_project_config(tmp_path, home=home)


def test_rejects_conflicting_permissions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "cache").mkdir()
    _write_config(
        tmp_path,
        'version = 1\n[home]\nread_only = ["cache"]\nread_write = ["cache"]\n',
    )

    with pytest.raises(ProjectConfigError, match="conflicting read-only and read-write permissions"):
        load_project_config(tmp_path, home=home)


def test_rejects_missing_requested_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_config(tmp_path, 'version = 1\n[home]\nread_only = ["missing"]\n')

    with pytest.raises(ProjectConfigError, match="does not exist"):
        load_project_config(tmp_path, home=home)


def test_discover_project_root_and_reject_nested_directory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "subdir"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)

    assert discover_project_root(root) == root.resolve()
    with pytest.raises(ProjectRootError, match="must be the Git project root"):
        discover_project_root(nested)


def test_discover_project_root_rejects_non_repository(tmp_path: Path) -> None:
    with pytest.raises(ProjectRootError, match="Could not determine Git root"):
        discover_project_root(tmp_path)
