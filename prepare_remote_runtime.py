"""Prepare and atomically publish a versioned Bubblewrap runtime on Beep.

Run locally after changing the Python environment or worker runtime files::

    python -m prepare_remote_runtime

The selected runtime sources match the local sandbox inputs: the active Python
``sys.prefix`` plus the worker IPC and entry-point modules. System runtime trees
and their expected symlink layout come from :func:`build_runtime_layout`.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from agent_sandbox import (
    RuntimeManifest,
    RuntimeMount,
    SandboxError,
    SandboxRuntimeLayout,
    build_runtime_layout,
    write_runtime_manifest,
)
from remote_agent_config import (
    RemoteAgentConfig,
    RemoteAgentConfigError,
    load_remote_agent_config,
)


def _version_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"runtime-{timestamp}-{uuid.uuid4().hex[:8]}"


def _remote_path(host: str, path: PurePosixPath) -> str:
    return f"{host}:{shlex.quote(str(path))}"


def _ssh(config: RemoteAgentConfig, *remote_argv: str) -> None:
    # Remote operations are fixed utilities with separately quoted arguments;
    # no config value is interpreted as a command or shell fragment.
    command = " ".join(shlex.quote(argument) for argument in remote_argv)
    subprocess.run(("ssh", config.ssh_host, command), check=True)


def _ssh_output(config: RemoteAgentConfig, *remote_argv: str) -> str:
    command = " ".join(shlex.quote(argument) for argument in remote_argv)
    completed = subprocess.run(
        ("ssh", config.ssh_host, command),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def prepare_remote_runtime(
    config: RemoteAgentConfig,
    *,
    version: str | None = None,
    rsync: str = "rsync",
    python_executable: Path | None = None,
) -> RuntimeManifest:
    """Mirror this Python runtime to Beep and publish it as an immutable version."""
    selected_version = version or _version_id()
    # RuntimeManifest applies strict version validation before the value is
    # incorporated into any remote path.
    RuntimeManifest(
        version=selected_version,
        rsync_completed_at=datetime.now(timezone.utc),
        layout=SandboxRuntimeLayout(()),
    )

    local_layout = build_runtime_layout(
        runtime_dirs=(sys.prefix,),
        runtime_files=(
            Path(__file__).with_name("agent_worker.py"),
            Path(__file__).with_name("agent_ipc.py"),
        ),
    )
    runtime_root = config.runtime_dir
    versions_root = runtime_root / "versions"
    staging = runtime_root / f".staging-{selected_version}"
    published = versions_root / selected_version

    # Use a deterministic per-mount directory in the mirror. The manifest
    # points at the final version path, not the temporary upload location.
    remote_mounts = []
    rsync_jobs: list[tuple[Path, PurePosixPath]] = []
    staged_mounts: list[tuple[Path, PurePosixPath]] = []
    for index, mount in enumerate(local_layout.mounts):
        source = mount.source.expanduser().resolve(strict=True)
        mirror_path = published / "mounts" / f"mount-{index:03d}"
        staged_path = staging / "mounts" / f"mount-{index:03d}"
        remote_mounts.append(RuntimeMount(mirror_path, mount.target))
        rsync_jobs.append((source, staged_path))
        staged_mounts.append((source, staged_path))

    manifest = RuntimeManifest(
        version=selected_version,
        rsync_completed_at=datetime.now(timezone.utc),
        layout=SandboxRuntimeLayout(tuple(remote_mounts), local_layout.symlinks),
    )

    _ssh(config, "install", "-d", "--", str(staging / "mounts"), str(versions_root))
    try:
        for source, destination in rsync_jobs:
            subprocess.run(
                (
                    rsync,
                    "-aHAX",
                    "--no-owner",
                    "--no-group",
                    "--",
                    os.fspath(source) + ("/" if source.is_dir() else ""),
                    _remote_path(config.ssh_host, destination),
                ),
                check=True,
            )
        _preflight_staged_runtime(
            config,
            staging,
            local_layout,
            staged_mounts,
            python_executable=(python_executable or Path(sys.executable)).absolute(),
        )
        # Capture the completion time after all source trees have transferred
        # and the remote ABI/import/Bubblewrap checks have succeeded.
        manifest = RuntimeManifest(
            version=selected_version,
            rsync_completed_at=datetime.now(timezone.utc),
            layout=manifest.layout,
        )
        import tempfile

        with tempfile.TemporaryDirectory(prefix="tgent-runtime-manifest-") as temporary_dir:
            local_manifest = Path(temporary_dir) / "manifest.json"
            write_runtime_manifest(local_manifest, manifest)
            subprocess.run(
                (
                    rsync,
                    "-a",
                    "--",
                    os.fspath(local_manifest),
                    _remote_path(config.ssh_host, staging / "manifest.json"),
                ),
                check=True,
            )

        # A same-filesystem rename publishes the complete tree atomically.
        _ssh(
            config,
            "mv",
            "-T",
            "--",
            str(staging),
            str(published),
        )
    except BaseException:
        try:
            _ssh(config, "rm", "-rf", "--", str(staging))
        except (OSError, subprocess.CalledProcessError):
            pass
        raise
    return manifest

def _preflight_staged_runtime(
    config: RemoteAgentConfig,
    staging: PurePosixPath,
    layout: SandboxRuntimeLayout,
    staged_mounts: list[tuple[Path, PurePosixPath]],
    *,
    python_executable: Path,
) -> None:
    """Validate Beep and execute the staged interpreter before publication."""
    architecture = _ssh_output(config, "uname", "-m")
    if architecture not in {"x86_64", "amd64"}:
        raise SandboxError(
            f"Remote runtime requires amd64; Beep reports architecture {architecture!r}"
        )

    executable_mount: tuple[Path, PurePosixPath] | None = None
    for source, staged in staged_mounts:
        try:
            relative_executable = python_executable.relative_to(source)
        except ValueError:
            continue
        executable_mount = (source, staged / relative_executable)
        break
    if executable_mount is None:
        raise SandboxError(
            f"Active Python executable {python_executable} is not inside a mirrored runtime mount"
        )

    _source, staged_python = executable_mount
    staged_agent_runtime = staging / "mounts" / "agent-runtime"
    # Runtime files are mounted as individual files. Their staged mirror files
    # are assembled into a private directory for the import check.
    _ssh(config, "install", "-d", "--", str(staged_agent_runtime))
    for index, mount in enumerate(layout.mounts):
        if mount.target.parent == Path("/opt/agent-runtime"):
            _ssh(
                config,
                "ln",
                "-s",
                "--",
                str(staging / "mounts" / f"mount-{index:03d}"),
                str(staged_agent_runtime / mount.target.name),
            )

    check_script = (
        "import importlib, platform, sys; "
        "assert platform.machine() in ('x86_64', 'amd64'), platform.machine(); "
        "[importlib.import_module(name) for name in ('agent_worker', 'agent_ipc')]; "
        "print(platform.machine())"
    )
    python_command = (
        "env",
        "-u",
        "PYTHONHOME",
        "-u",
        "PYTHONPATH",
        "PYTHONPATH=" + str(staged_agent_runtime),
        str(staged_python),
        "-c",
        check_script,
    )
    _ssh(config, *python_command)
    _ssh(config, "sh", "-c", "command -v bwrap >/dev/null && bwrap --version >/dev/null")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="deployment TOML path")
    parser.add_argument("--version", help="explicit runtime version (generated by default)")
    arguments = parser.parse_args(argv)
    try:
        config = load_remote_agent_config(arguments.config)
        manifest = prepare_remote_runtime(config, version=arguments.version)
    except (RemoteAgentConfigError, SandboxError, OSError, subprocess.CalledProcessError) as err:
        print(f"prepare remote runtime: {err}", file=sys.stderr)
        return 1
    print(f"Published runtime {manifest.version} on {config.ssh_host}")
    print(f"Rsync completed at {manifest.rsync_completed_at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
