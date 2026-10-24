"""Conservative Bubblewrap command construction for agent workers.

This module builds a command only; the trusted controller remains responsible
for starting, supervising, and terminating the process. Project config expresses
requests, while controller-owned allowlists determine which home mounts are
actually permitted.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
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


@dataclass(frozen=True)
class RuntimeMount:
    """Host runtime source and its path inside the sandbox."""

    source: Path
    target: Path


@dataclass(frozen=True)
class SandboxRuntimeLayout:
    """Prepared runtime trees and links used by Bubblewrap."""

    mounts: tuple[RuntimeMount, ...]
    symlinks: tuple[tuple[Path, str], ...] = ()


@dataclass(frozen=True)
class RuntimeManifest:
    """Versioned runtime layout and the completion time of its rsync."""

    version: str
    rsync_completed_at: datetime
    layout: SandboxRuntimeLayout

    def __post_init__(self) -> None:
        if (
            not self.version
            or self.version in {".", ".."}
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in self.version)
        ):
            raise SandboxError(f"Invalid runtime version: {self.version!r}")
        if self.rsync_completed_at.tzinfo is None:
            raise SandboxError("Runtime rsync completion time must include a timezone")

    def to_dict(self) -> dict[str, object]:
        completed_at = self.rsync_completed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "schema_version": 1,
            "version": self.version,
            "rsync_completed_at": completed_at,
            "mounts": [
                {"source": str(mount.source), "target": str(mount.target)}
                for mount in self.layout.mounts
            ],
            "symlinks": [
                {"target": str(target), "link_target": link_target}
                for target, link_target in self.layout.symlinks
            ],
        }

    @classmethod
    def from_dict(cls, data: object) -> RuntimeManifest:
        if not isinstance(data, dict) or set(data) != {
            "schema_version", "version", "rsync_completed_at", "mounts", "symlinks"
        }:
            raise SandboxError("Invalid runtime manifest fields")
        if data["schema_version"] != 1:
            raise SandboxError("Unsupported runtime manifest schema version")
        if not isinstance(data["version"], str) or not isinstance(data["rsync_completed_at"], str):
            raise SandboxError("Invalid runtime manifest version or timestamp")
        try:
            completed_at = datetime.fromisoformat(data["rsync_completed_at"].replace("Z", "+00:00"))
        except ValueError as err:
            raise SandboxError("Invalid runtime manifest timestamp") from err
        raw_mounts = data["mounts"]
        raw_symlinks = data["symlinks"]
        if not isinstance(raw_mounts, list) or not isinstance(raw_symlinks, list):
            raise SandboxError("Invalid runtime manifest mount lists")
        mounts: list[RuntimeMount] = []
        for entry in raw_mounts:
            if not isinstance(entry, dict) or set(entry) != {"source", "target"}:
                raise SandboxError("Invalid runtime manifest mount")
            if not isinstance(entry["source"], str) or not isinstance(entry["target"], str):
                raise SandboxError("Invalid runtime manifest mount paths")
            mounts.append(RuntimeMount(Path(entry["source"]), Path(entry["target"])))
        symlinks: list[tuple[Path, str]] = []
        for entry in raw_symlinks:
            if not isinstance(entry, dict) or set(entry) != {"target", "link_target"}:
                raise SandboxError("Invalid runtime manifest symlink")
            if not isinstance(entry["target"], str) or not isinstance(entry["link_target"], str):
                raise SandboxError("Invalid runtime manifest symlink paths")
            symlinks.append((Path(entry["target"]), entry["link_target"]))
        return cls(
            version=data["version"],
            rsync_completed_at=completed_at,
            layout=SandboxRuntimeLayout(tuple(mounts), tuple(symlinks)),
        )


def write_runtime_manifest(path: str | os.PathLike[str], manifest: RuntimeManifest) -> None:
    """Atomically write a manifest after the version's rsync has completed."""
    destination = Path(path)
    if not destination.parent.is_dir():
        raise SandboxError(f"Runtime manifest directory does not exist: {destination.parent}")
    encoded = json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n"
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def read_runtime_manifest(path: str | os.PathLike[str]) -> RuntimeManifest:
    """Load and validate a prepared runtime manifest."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as err:
        raise SandboxError(f"Cannot read runtime manifest {path}: {err}") from err
    return RuntimeManifest.from_dict(data)


_SYSTEM_DIRS = ("/usr", "/lib", "/lib64", "/bin", "/sbin")
_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
_SYSTEM_SYMLINKS = {
    "/bin": "usr/bin",
    "/sbin": "usr/sbin",
    "/lib": "usr/lib",
    "/lib64": "usr/lib64",
}


def build_runtime_layout(
    *,
    runtime_dirs: tuple[str | os.PathLike[str], ...] = (),
    runtime_files: tuple[str | os.PathLike[str], ...] = (),
) -> SandboxRuntimeLayout:
    """Describe local host runtime mounts using their normal sandbox paths."""
    mounts: list[RuntimeMount] = []
    symlinks: list[tuple[Path, str]] = []
    for raw_system_dir in _SYSTEM_DIRS:
        source = Path(raw_system_dir)
        if source.is_symlink():
            link_target = _SYSTEM_SYMLINKS.get(raw_system_dir)
            if link_target is None:
                raise SandboxError(f"Unsupported system mount symlink: {raw_system_dir}")
            symlinks.append((source, link_target))
        elif source.is_dir():
            mounts.append(RuntimeMount(source, source))
    for raw_dir in runtime_dirs:
        source = Path(raw_dir).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise SandboxError(f"Trusted runtime directory is not a directory: {source}")
        if any(_path_is_within(source, Path(system_dir)) for system_dir in _SYSTEM_DIRS):
            continue
        mounts.append(RuntimeMount(source, source))
    ca_bundle = _CA_BUNDLE
    try:
        pem = ca_bundle.read_text(encoding="ascii")
        certificate_der = ssl.PEM_cert_to_DER_cert(
            pem[pem.index("-----BEGIN CERTIFICATE-----"):pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
        ) if "-----BEGIN CERTIFICATE-----" in pem and "-----END CERTIFICATE-----" in pem else None
    except (OSError, UnicodeError, ValueError) as exc:
        raise SandboxError(f"Cannot read or parse system CA bundle {ca_bundle}: {exc}") from exc
    if not pem.strip() or not certificate_der:
        raise SandboxError(f"System CA bundle is empty or contains no certificate: {ca_bundle}")
    mounts.append(RuntimeMount(ca_bundle.resolve(strict=True), ca_bundle))
    for raw_file in runtime_files:
        source = Path(raw_file).expanduser().resolve(strict=True)
        if not source.is_file():
            raise SandboxError(f"Trusted runtime entry is not a regular file: {source}")
        if source.name in names:
            raise SandboxError(f"Trusted runtime files have duplicate basenames: {source.name}")
        names.add(source.name)
        mounts.append(RuntimeMount(source, Path("/opt/agent-runtime") / source.name))
    return SandboxRuntimeLayout(tuple(mounts), tuple(symlinks))


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
    runtime_dirs: tuple[str | os.PathLike[str], ...] = (),
    runtime_layout: SandboxRuntimeLayout | None = None,
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

    if runtime_layout is not None and (runtime_dirs or runtime_files):
        raise SandboxError("Specify runtime_layout or runtime_dirs/runtime_files, not both")
    if runtime_layout is None:
        runtime_layout = build_runtime_layout(runtime_dirs=runtime_dirs, runtime_files=runtime_files)

    runtime_mounts: list[RuntimeMount] = []
    runtime_targets: list[Path] = []
    for mount in runtime_layout.mounts:
        source = Path(mount.source).expanduser().resolve(strict=True)
        target = Path(mount.target)
        if not target.is_absolute() or target == Path(target.anchor):
            raise SandboxError(f"Invalid sandbox runtime mount target: {target}")
        if not source.is_file() and not source.is_dir():
            raise SandboxError(f"Trusted runtime source is not a regular file or directory: {source}")
        if source == Path(source.anchor):
            raise SandboxError("Refusing to mount the filesystem root as a runtime source")
        if source.is_dir() and (target == project_target or _path_is_within(project_target, target)):
            raise SandboxError(f"Trusted runtime directory contains or replaces project mount: {target}")
        if any(_path_is_within(target, prior) or _path_is_within(prior, target) for prior in runtime_targets):
            raise SandboxError(f"Overlapping sandbox runtime mount targets: {target}")
        runtime_targets.append(target)
        runtime_mounts.append(RuntimeMount(source, target))
    symlink_targets: set[Path] = set()
    for target, link_target in runtime_layout.symlinks:
        target = Path(target)
        if not target.is_absolute() or target == Path(target.anchor) or not link_target:
            raise SandboxError(f"Invalid sandbox runtime symlink: {target}")
        if target in symlink_targets or any(
            _path_is_within(target, mount_target) or _path_is_within(mount_target, target)
            for mount_target in runtime_targets
        ):
            raise SandboxError(f"Sandbox runtime symlink overlaps another runtime target: {target}")
        symlink_targets.add(target)

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
    for runtime_mount in runtime_mounts:
        for _source, target, _read_only in mounts:
            if _path_is_within(runtime_mount.target, target) or _path_is_within(target, runtime_mount.target):
                raise SandboxError(
                    f"Trusted runtime mount overlaps an authorized home mount: {runtime_mount.target}"
                )

    argv: list[str] = [
        bwrap_path,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
    ]
    if policy.network:
        argv.append("--share-net")

    # Bubblewrap starts with an empty root. Recreate symlink locations and
    # bind only explicitly selected runtime trees read-only.
    created_dirs: set[Path] = set()
    for target, link_target in runtime_layout.symlinks:
        target = Path(target)
        _add_parent_dirs(argv, target, Path(target.anchor), created_dirs)
        argv.extend(("--symlink", link_target, str(target)))

    argv.extend(("--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/run"))
    argv.extend(("--dir", "/etc"))
    _add_parent_dirs(argv, home_root, Path(home_root.anchor), created_dirs)
    argv.extend(("--tmpfs", str(home_root)))
    created_dirs.add(home_root)

    # Mount the project at its normal host path, constructing only its parent
    # directories in the sandbox root. Project edits are the intended writes.
    _add_parent_dirs(argv, project_target, Path(project_target.anchor), created_dirs)
    argv.extend(("--bind", str(snapshot_project), str(project_target)))

    # Runtime mounts retain their sandbox paths while their sources may come
    # from a prepared remote mirror.
    for mount in sorted(runtime_mounts, key=lambda entry: str(entry.target)):
        _add_parent_dirs(argv, mount.target, Path(mount.target.anchor), created_dirs)
        argv.extend(("--ro-bind", str(mount.source), str(mount.target)))

    for source, target, read_only in sorted(mounts, key=lambda entry: str(entry[1])):
        _add_parent_dirs(argv, target, home_target, created_dirs)
        kind = "--ro-bind" if read_only else "--bind"
        argv.extend((kind, str(source), str(target)))


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
