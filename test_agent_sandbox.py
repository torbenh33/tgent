"""Tests for conservative Bubblewrap argument construction."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from agent_project import ProjectConfig
from agent_sandbox import SandboxError, SandboxPolicy, build_sandbox_command
from agent_snapshot import ProjectSnapshot


def _fake_bwrap(tmp_path: Path) -> str:
    executable = tmp_path / "bwrap"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return str(executable)


def _snapshot(project: Path, snapshot_project: Path) -> ProjectSnapshot:
    snapshot_project.mkdir(parents=True)
    return ProjectSnapshot(
        root=snapshot_project.parent,
        project_root=snapshot_project,
        base_commit="deadbeef",
        linked_worktree=False,
    )


def test_builds_isolated_project_command_and_defaults_to_no_network(tmp_path: Path) -> None:
    project = tmp_path / "home" / "user" / "project"
    project.mkdir(parents=True)
    home = project.parents[1]
    snapshot = _snapshot(project, tmp_path / "snapshot" / "project")
    config = ProjectConfig(root=project)

    result = build_sandbox_command(
        snapshot=snapshot,
        project_config=config,
        bwrap=_fake_bwrap(tmp_path),
        home=home,
        command=("/usr/bin/python3", "-m", "agent_worker"),
    )

    argv = result.argv
    assert "--unshare-all" in argv
    assert "--share-net" not in argv
    assert "--clearenv" in argv
    assert "--ro-bind" in argv
    project_bind_index = argv.index("--bind")
    assert argv[project_bind_index + 1] == str(snapshot.project_root)
    assert argv[project_bind_index + 2] == str(project)
    assert "--tmpfs" in argv
    assert str(home) in argv
    assert result.project_path == project


def test_mounts_trusted_runtime_directory_read_only_at_original_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "venv"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin" / "python").write_text("interpreter", encoding="utf-8")

    result = build_sandbox_command(
        snapshot=_snapshot(project, tmp_path / "snapshot" / "project"),
        project_config=ProjectConfig(root=project),
        bwrap=_fake_bwrap(tmp_path),
        runtime_dirs=(runtime,),
    )

    runtime_index = result.argv.index(str(runtime))
    assert result.argv[runtime_index - 1] == "--ro-bind"
    assert result.argv[runtime_index + 1] == str(runtime)


def test_rejects_project_home_mount_not_authorized_by_controller(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cache = home / ".cache"
    cache.mkdir()
    config = ProjectConfig(root=project, home_read_only=(Path(".cache"),))

    with pytest.raises(SandboxError, match="not authorized"):
        build_sandbox_command(
            snapshot=_snapshot(project, tmp_path / "snapshot" / "project"),
            project_config=config,
            policy=SandboxPolicy(),
            bwrap=_fake_bwrap(tmp_path),
            home=home,
        )


def test_mounts_only_authorized_requested_home_paths_with_declared_mode(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cache = home / ".cache" / "uv"
    cache.mkdir(parents=True)
    data_file = home / "data.txt"
    data_file.write_text("data", encoding="utf-8")
    config = ProjectConfig(
        root=project,
        home_read_only=(Path(".cache/uv"),),
        home_read_write=(Path("data.txt"),),
    )
    policy = SandboxPolicy(
        home_read_only=frozenset({Path(".cache/uv")}),
        home_read_write=frozenset({Path("data.txt")}),
    )

    result = build_sandbox_command(
        snapshot=_snapshot(project, tmp_path / "snapshot" / "project"),
        project_config=config,
        policy=policy,
        bwrap=_fake_bwrap(tmp_path),
        home=home,
    )

    argv = result.argv
    ro_index = argv.index(str(cache))
    assert argv[ro_index - 1] == "--ro-bind"
    rw_index = argv.index(str(data_file))
    assert argv[rw_index - 1] == "--bind"
    assert str(home) in argv
    assert str(home / ".ssh") not in argv


def test_network_requires_controller_policy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    snapshot = _snapshot(project, tmp_path / "snapshot" / "project")

    result = build_sandbox_command(
        snapshot=snapshot,
        project_config=ProjectConfig(root=project),
        policy=SandboxPolicy(network=True),
        bwrap=_fake_bwrap(tmp_path),
    )

    assert "--share-net" in result.argv


def test_rejects_home_mount_overlapping_project_mount(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    requested_dir = project / "data"
    requested_dir.mkdir()
    relative = Path("project/data")
    config = ProjectConfig(root=project, home_read_only=(relative,))
    policy = SandboxPolicy(home_read_only=frozenset({relative}))

    with pytest.raises(SandboxError, match="overlaps the project"):
        build_sandbox_command(
            snapshot=_snapshot(project, tmp_path / "snapshot" / "project"),
            project_config=config,
            policy=policy,
            bwrap=_fake_bwrap(tmp_path),
            home=home,
        )


def test_controller_socket_must_be_a_unix_socket(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    fake_socket = tmp_path / "not-a-socket"
    fake_socket.write_text("no", encoding="utf-8")

    with pytest.raises(SandboxError, match="not a Unix socket"):
        build_sandbox_command(
            snapshot=_snapshot(project, tmp_path / "snapshot" / "project"),
            project_config=ProjectConfig(root=project),
            bwrap=_fake_bwrap(tmp_path),
            controller_socket=fake_socket,
        )


def test_controller_socket_is_added_as_a_mount(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    socket_path = tmp_path / "controller.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(os.fspath(socket_path))
        result = build_sandbox_command(
            snapshot=_snapshot(project, tmp_path / "snapshot" / "project"),
            project_config=ProjectConfig(root=project),
            bwrap=_fake_bwrap(tmp_path),
            controller_socket=socket_path,
        )
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)

    assert "--bind" in result.argv
    assert str(socket_path) in result.argv
    assert "/run/agent-controller.sock" in result.argv
