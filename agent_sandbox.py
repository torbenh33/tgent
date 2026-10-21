"""Conservative Bubblewrap command construction for agent workers.

This module builds a command only; the trusted controller remains responsible
for starting, supervising, and terminating the process. Project config expresses
requests, while controller-owned allowlists determine which home mounts are
actually permitted.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_project import ProjectConfig
from agent_snapshot import ProjectSnapshot


class SandboxError(RuntimeError):
    """Raised when the requested sandbox cannot be built safely."""


@dataclass(frozen=True)
class SandboxPolicy:
    """Controller-authorized home-directory mounts, relative to the user's home."""

    home_read_only: frozenset[Path] = frozenset()
    home_read_write: frozenset[Path] = frozenset()
    network: bool = False


@dataclass(frozen=True)
class SandboxCommand:
    """Inspectable Bubblewrap invocation and key sandbox paths."""

    argv: tuple[str, ...]
    project_path: Path
    home_path: Path


_SYSTEM_DIRS = ("/usr", "/lib", "/lib64", "/bin", "/sbin")
_SYSTEM_SYMLINKS = {
    "/bin": "usr/bin",
    "/sbin": "usr/sbin",
    "/lib": "usr/lib",
    "/lib64": "usr/lib64",
}


def _relative_home_path(value: Path) -> Path:
    path = PurePosixPath(value.as_posix())
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SandboxError(f"Invalid home-relative mount path: {value}")
    if "\\" in value.as_posix() or "\x00" in value.as_posix():
        raise SandboxError(f"Invalid home-relative mount path: {value}")
    return Path(*path.parts)


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False

def _add_parent_dirs(argv: list[str], target: Path, stop: Path, created: set[Path]) -> None:
    """Create target's missing parents once in the empty sandbox root."""
    try:
        relative = target.parent.relative_to(stop)
    except ValueError as err:
        raise SandboxError(f"Mount target is outside sandbox base {stop}: {target}") from err
    current = stop
    for component in relative.parts:
        current /= component
        if current not in created:
            argv.extend(("--dir", str(current)))
            created.add(current)


def build_sandbox_command(
    *,
    snapshot: ProjectSnapshot,
    project_config: ProjectConfig,
    policy: SandboxPolicy = SandboxPolicy(),
    command: tuple[str, ...] | list[str] = ("/usr/bin/python3", "-m", "agent_worker"),
    bwrap: str = "bwrap",
    home: str | os.PathLike[str] | None = None,
    controller_socket: str | os.PathLike[str] | None = None,
    runtime_home: str | os.PathLike[str] | None = None,
    worker_env: dict[str, str] | None = None,
    runtime_files: tuple[str | os.PathLike[str], ...] = (),
) -> SandboxCommand:
    """Build a restrictive Bubblewrap argv for a worker in a project snapshot.

    The snapshot is mounted read-write at the source project's original absolute
    path. The home directory itself is a private tmpfs; only explicitly
    requested and controller-authorized child paths are mounted into it.
    Networking is disabled unless the trusted controller explicitly enables it.
    """
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise SandboxError("Worker command must be a non-empty argument vector")
    bwrap_path = shutil.which(bwrap)
    if bwrap_path is None:
        raise SandboxError(f"Bubblewrap executable not found: {bwrap}")

    project_target = project_config.root.resolve()
    snapshot_project = snapshot.project_root.resolve()
    if not snapshot_project.is_dir():
        raise SandboxError(f"Snapshot project directory does not exist: {snapshot_project}")
    if project_target == Path(project_target.anchor):
        raise SandboxError("Refusing to mount a project at the filesystem root")

    home_root = Path(home if home is not None else Path.home()).expanduser().resolve()
    if home_root == Path(home_root.anchor):
        raise SandboxError("Refusing to use the filesystem root as sandbox HOME")
    home_target = Path(runtime_home if runtime_home is not None else home_root).expanduser().absolute()
    if home_target != home_root:
        raise SandboxError("Sandbox HOME must match the host home path for path-preserving mounts")
    if project_target == home_root:
        raise SandboxError("Project root cannot be the home directory itself")
    if _path_is_within(home_root, project_target):
        raise SandboxError("Sandbox HOME cannot be inside the project mount")
    protected_targets = [Path(path) for path in (*_SYSTEM_DIRS, "/etc", "/dev", "/proc", "/tmp", "/run")]
    for protected in protected_targets:
        # /tmp is a private tmpfs inside the sandbox, so projects below it are
        # safe. Never allow a project mount to replace /tmp or one of its parents.
        project_is_inside_private_tmp = protected == Path("/tmp") and _path_is_within(
            project_target, protected
        )
        if _path_is_within(protected, project_target) or (
            _path_is_within(project_target, protected) and not project_is_inside_private_tmp
        ):
            raise SandboxError("Project mount overlaps a protected sandbox system path")

    requested_ro = {_relative_home_path(path) for path in project_config.home_read_only}
    requested_rw = {_relative_home_path(path) for path in project_config.home_read_write}
    allowed_ro = {_relative_home_path(path) for path in policy.home_read_only}
    allowed_rw = {_relative_home_path(path) for path in policy.home_read_write}

    if requested_ro & requested_rw:
        raise SandboxError("A home path cannot be requested with conflicting permissions")
    denied = (requested_ro - allowed_ro) | (requested_rw - allowed_rw)
    if denied:
        denied_path = sorted(denied, key=str)[0]
        raise SandboxError(f"Project requested a home mount not authorized by controller policy: {denied_path}")

    mounts: list[tuple[Path, Path, bool]] = []
    for relative, read_only in [*((p, True) for p in requested_ro), *((p, False) for p in requested_rw)]:
        source = home_root / relative
        if source.is_symlink() or not source.exists() or not (source.is_file() or source.is_dir()):
            raise SandboxError(f"Authorized home mount is missing or not a regular file/directory: {source}")
        resolved_source = source.resolve()
        if not _path_is_within(resolved_source, home_root):
            raise SandboxError(f"Authorized home mount resolves outside home directory: {source}")
        target = home_target / relative
        if _path_is_within(target, project_target) or _path_is_within(project_target, target):
            raise SandboxError("Home mount overlaps the project mount")
        mounts.append((resolved_source, target, read_only))

    for index, (_source_a, target_a, _ro_a) in enumerate(mounts):
        for _source_b, target_b, _ro_b in mounts[index + 1 :]:
            if _path_is_within(target_a, target_b) or _path_is_within(target_b, target_a):
                raise SandboxError(f"Overlapping home mount targets: {target_a} and {target_b}")

    argv: list[str] = [
        bwrap_path,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
    ]
    if policy.network:
        argv.append("--share-net")

    # Bubblewrap starts with an empty root. Bind only required runtime trees;
    # preserve standard symlink locations without exposing their host targets.
    for system_dir in _SYSTEM_DIRS:
        source = Path(system_dir)
        if source.is_symlink():
            link_target = _SYSTEM_SYMLINKS.get(system_dir)
            if link_target is None:
                raise SandboxError(f"Unsupported system mount symlink: {system_dir}")
            argv.extend(("--symlink", link_target, system_dir))
        elif source.is_dir():
            argv.extend(("--ro-bind", system_dir, system_dir))

    argv.extend(("--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/run"))
    argv.extend(("--dir", "/etc"))
    created_dirs: set[Path] = set()
    _add_parent_dirs(argv, home_root, Path(home_root.anchor), created_dirs)
    argv.extend(("--tmpfs", str(home_root)))
    created_dirs.add(home_root)

    # Mount the project at its normal host path, constructing only its parent
    # directories in the sandbox root. Project edits are the intended writes.
    _add_parent_dirs(argv, project_target, Path(project_target.anchor), created_dirs)
    argv.extend(("--bind", str(snapshot_project), str(project_target)))

    for source, target, read_only in sorted(mounts, key=lambda entry: str(entry[1])):
        _add_parent_dirs(argv, target, home_target, created_dirs)
        kind = "--ro-bind" if read_only else "--bind"
        argv.extend((kind, str(source), str(target)))

    if runtime_files:
        runtime_dir = Path("/opt/agent-runtime")
        argv.extend(("--dir", "/opt", "--dir", str(runtime_dir)))
        used_names: set[str] = set()
        for raw_runtime_file in runtime_files:
            source = Path(raw_runtime_file).expanduser().resolve(strict=True)
            if not source.is_file():
                raise SandboxError(f"Trusted runtime entry is not a regular file: {source}")
            if source.name in used_names:
                raise SandboxError(f"Trusted runtime files have duplicate basenames: {source.name}")
            used_names.add(source.name)
            argv.extend(("--ro-bind", str(source), str(runtime_dir / source.name)))

    if controller_socket is not None:
        socket_source = Path(controller_socket).expanduser().absolute()
        try:
            mode = socket_source.stat().st_mode
        except OSError as err:
            raise SandboxError(f"Cannot inspect controller socket {socket_source}: {err}") from err
        if not stat.S_ISSOCK(mode):
            raise SandboxError(f"Controller endpoint is not a Unix socket: {socket_source}")
        socket_target = Path("/run/agent-controller.sock")
        argv.extend(("--bind", str(socket_source), str(socket_target)))

    argv.extend(("--setenv", "HOME", str(home_target)))
    argv.extend(("--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin"))
    if controller_socket is not None:
        argv.extend(("--setenv", "AGENT_CONTROLLER_SOCKET", "/run/agent-controller.sock"))
    for name, value in (worker_env or {}).items():
        if name not in {"AGENT_ID", "AGENT_BASE_COMMIT", "PYTHONPATH"}:
            raise SandboxError(f"Refusing unsafe worker environment variable: {name}")
        if not isinstance(value, str) or "\x00" in value:
            raise SandboxError(f"Invalid worker environment value for {name}")
        argv.extend(("--setenv", name, value))
    argv.extend(("--chdir", str(project_target), "--", *command))
    return SandboxCommand(argv=tuple(argv), project_path=project_target, home_path=home_target)
