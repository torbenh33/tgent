"""MCP server for planning-driven, unprivileged Debian rootfs creation.

The server does not inspect host APT configuration itself. Supply a selected
suite, architecture, package list, and normalized one-line APT sources from the
read-only inventory exposed by mcp_apt.py. Rootfs creation uses mmdebstrap's
unshare mode and requires unprivileged user namespaces to be enabled.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("sandbox-rootfs")
MAX_PACKAGES = 2000
MAX_CONFIG_SIZE = 1024 * 1024
SUITE_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
ARCHITECTURE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?$")


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Command failed (exit {exc.returncode}): {' '.join(command)}"
        ) from exc

def _validate_plan(
    target: Path,
    suite: str,
    architecture: str,
    packages: list[str],
    sources_list: str,
    preferences: str | None,
) -> tuple[list[str], str | None]:
    if not isinstance(suite, str) or not SUITE_NAME.fullmatch(suite):
        raise RuntimeError("Invalid Debian suite name.")
    if not isinstance(architecture, str) or not ARCHITECTURE_NAME.fullmatch(architecture):
        raise RuntimeError("Invalid Debian architecture.")
    if not isinstance(packages, list) or len(packages) > MAX_PACKAGES:
        raise RuntimeError(f"packages must be a list with at most {MAX_PACKAGES} entries.")
    clean_packages: list[str] = []
    for package in packages:
        if not isinstance(package, str) or not PACKAGE_NAME.fullmatch(package):
            raise RuntimeError(f"Invalid package name: {package!r}")
        if package not in clean_packages:
            clean_packages.append(package)
    if not isinstance(sources_list, str) or not sources_list.strip():
        raise RuntimeError("A non-empty normalized one-line sources_list is required.")
    if len(sources_list.encode("utf-8")) > MAX_CONFIG_SIZE:
        raise RuntimeError("sources_list exceeds the configuration size limit.")
    if preferences is not None:
        if not isinstance(preferences, str):
            raise RuntimeError("preferences must be text or null.")
        if len(preferences.encode("utf-8")) > MAX_CONFIG_SIZE:
            raise RuntimeError("preferences exceeds the configuration size limit.")
    validate_target(target)
    return clean_packages, preferences


def validate_target(target: Path) -> None:
    if target == Path("/"):
        raise RuntimeError("Refusing to use / as the rootfs target.")
    if target.exists():
        if not target.is_dir():
            raise RuntimeError(f"Target exists and is not a directory: {target}")
        if any(target.iterdir()):
            raise RuntimeError(f"Target directory must be empty: {target}")


@mcp.tool()
def create_sandbox_rootfs(
    target: str,
    suite: str,
    architecture: str,
    packages: list[str],
    sources_list: str,
    preferences: str | None = None,
    dns_server: str = "1.1.1.1",
) -> dict[str, Any]:
    """Create a Debian rootfs with mmdebstrap using the agent's reviewed APT plan.

    Supply a base suite and architecture, package names, and APT source entries
    normalized to traditional one-line ``deb``/``deb-src`` format. Optional
    pinning preferences are written to a private temporary file. This tool
    modifies only the selected new rootfs target; it does not alter host APT.
    Requires mmdebstrap and enabled unprivileged user namespaces.
    """
    if not isinstance(target, str) or not target.strip():
        return {"status": "error", "message": "target must be provided."}
    if not isinstance(dns_server, str) or not re.fullmatch(
        r"[a-zA-Z0-9.-]+", dns_server
    ):
        return {"status": "error", "message": "Invalid DNS server name or address."}

    target_path = Path(target).expanduser().resolve()
    try:
        clean_packages, clean_preferences = _validate_plan(
            target_path, suite, architecture, packages, sources_list, preferences
        )
        with tempfile.TemporaryDirectory(prefix="sandbox-rootfs-apt-") as temp_dir:
            temp_path = Path(temp_dir)
            sources_path = temp_path / "sources.list"
            sources_path.write_text(sources_list.rstrip() + "\n", encoding="utf-8")
            preferences_path: Path | None = None
            if clean_preferences and clean_preferences.strip():
                preferences_path = temp_path / "preferences"
                preferences_path.write_text(clean_preferences.rstrip() + "\n", encoding="utf-8")

            target_path.mkdir(parents=True, exist_ok=True)
            command = [
                "mmdebstrap",
                "--mode=unshare",
                "--variant=minbase",
                f"--architectures={architecture}",
                f"--sources-list={sources_path}",
            ]
            if preferences_path is not None:
                command.append(
                    f"--aptopt=Dir::Etc::preferences={preferences_path}"
                )
            if clean_packages:
                command.append(f"--include={','.join(clean_packages)}")
            command.extend([suite, str(target_path)])
            _run(command)

        resolver = target_path / "etc/resolv.conf"
        if resolver.is_symlink():
            resolver.unlink()
        resolver.write_text(
            f"# DNS resolver for the sandbox rootfs\nnameserver {dns_server}\n",
            encoding="utf-8",
        )
    except (OSError, RuntimeError) as exc:
        return {"status": "error", "message": str(exc), "target": str(target_path)}

    return {
        "status": "ok",
        "message": "Sandbox rootfs created.",
        "target": str(target_path),
        "suite": suite,
        "architecture": architecture,
        "packages": clean_packages,
    }


SENSITIVE_NAMES = {
    ".aws", ".azure", ".docker", ".gnupg", ".kube", ".ssh",
    ".netrc", ".authinfo", "credentials", "secrets",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def _copy_selected_tree(source: Path, destination: Path, skipped: list[str]) -> None:
    """Copy regular files/directories, rejecting links and omitting obvious secrets."""
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name in SENSITIVE_NAMES or entry.name == ".env" or entry.name.startswith(".env."):
            skipped.append(str(entry))
            continue
        if entry.suffix.lower() in SENSITIVE_SUFFIXES or entry.name.startswith("id_"):
            skipped.append(str(entry))
            continue
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            skipped.append(str(entry))
        elif stat.S_ISDIR(info.st_mode):
            _copy_selected_tree(entry, destination / entry.name, skipped)
        elif stat.S_ISREG(info.st_mode):
            shutil.copy2(entry, destination / entry.name)
        else:
            skipped.append(str(entry))


def _bubblewrap_run(
    rootfs_path: Path,
    home_path: Path,
    staged_home: Path,
    working_directory: Path,
    command: list[str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    uid = os.getuid()
    gid = os.getgid()
    if home_path == Path("/root"):
        home_mount = Path("/root")
        create_home = False
    elif home_path.parent == Path("/home") and len(home_path.name) > 0:
        home_mount = Path("/home")
        create_home = True
    else:
        raise RuntimeError(
            "Rootless sandbox supports home directories at /root or /home/<user>."
        )

    bwrap_command = [
        "bwrap", "--die-with-parent", "--new-session", "--unshare-all",
        "--uid", str(uid), "--gid", str(gid), "--hostname", "sandbox",
        "--clearenv", "--setenv", "PATH",
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--setenv", "HOME", str(home_path), "--setenv", "TMPDIR", "/tmp",
        "--ro-bind", str(rootfs_path), "/",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    # Shadow the rootfs home area before binding only the selected staged files.
    bwrap_command.extend(["--tmpfs", str(home_mount)])
    if create_home:
        bwrap_command.extend(["--dir", str(home_path)])
    bwrap_command.extend(["--bind", str(staged_home), str(home_path)])
    bwrap_command.extend(["--chdir", str(working_directory), "--", *command])
    try:
        return subprocess.run(
            bwrap_command, capture_output=True, text=True, check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        if exc.filename == "bwrap":
            raise RuntimeError("Required command not found: bwrap") from exc
        raise


@mcp.tool()
def run_sandbox_command(
    rootfs: str,
    project_path: str,
    command: list[str],
    include_home_paths: list[str] | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run an argv command with a private, selective copy of the caller's home.

    The current project is copied to the same absolute path inside the sandbox.
    ``include_home_paths`` adds explicitly selected files or directories below
    the current user's home; paths are relative to that home. Nothing else from
    the host home is mounted. Obvious credential files, symlinks, and special
    files are skipped. The rootfs is read-only and network/other namespaces are
    unshared. Changes to copied files are discarded when this call ends.

    The sandbox process uses the caller's numeric UID/GID. The copy is a
    temporary snapshot, not a persistent workspace or a writable rootfs overlay.
    """
    if not isinstance(rootfs, str) or not rootfs.strip():
        return {"status": "error", "message": "rootfs must be provided."}
    if not isinstance(project_path, str) or not project_path.strip():
        return {"status": "error", "message": "project_path must be provided."}
    if (
        not isinstance(command, list) or not command or len(command) > 256
        or any(not isinstance(arg, str) or "\0" in arg for arg in command)
        or not command[0]
    ):
        return {"status": "error", "message": "command must be a non-empty argv list of at most 256 strings."}
    if (
        not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool)
        or not 1 <= timeout_seconds <= 3600
    ):
        return {"status": "error", "message": "timeout_seconds must be from 1 to 3600."}
    if include_home_paths is None:
        include_home_paths = []
    if (
        not isinstance(include_home_paths, list)
        or any(not isinstance(item, str) or not item.strip() for item in include_home_paths)
    ):
        return {"status": "error", "message": "include_home_paths must be a list of non-empty paths relative to the home directory."}

    try:
        rootfs_path = Path(rootfs).expanduser().resolve()
        home_path = Path.home().resolve()
        project = Path(project_path).expanduser().resolve(strict=True)
        if rootfs_path == Path("/") or not rootfs_path.is_dir():
            raise RuntimeError(f"rootfs must be an existing directory other than /: {rootfs_path}")
        if project == Path("/") or not project.is_dir() or home_path not in project.parents:
            raise RuntimeError("project_path must be a directory inside the current user's home.")
        if rootfs_path == home_path or rootfs_path in home_path.parents or home_path in rootfs_path.parents:
            raise RuntimeError("rootfs and caller home must not contain or overlap each other.")
        project_relative = project.relative_to(home_path)
        selected: set[Path] = {project_relative}
        for raw in include_home_paths:
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise RuntimeError(f"Invalid home-relative path: {raw}")
            source = (home_path / relative).resolve(strict=True)
            if home_path not in source.parents and source != home_path:
                raise RuntimeError(f"Selected path escapes home directory: {raw}")
            if not source.exists():
                raise RuntimeError(f"Selected path does not exist: {raw}")
            selected.add(source.relative_to(home_path))
        # Do not allow selected paths to overlap; this avoids ambiguous copies.
        selected_paths = sorted(selected, key=lambda path: (len(path.parts), str(path)))
        for index, item in enumerate(selected_paths):
            if any(item == previous or previous in item.parents for previous in selected_paths[:index]):
                raise RuntimeError(f"Selected home paths overlap: {item}")
        for item in selected_paths:
            source = home_path / item
            if not source.is_dir() or source.is_symlink():
                raise RuntimeError(f"Selected paths must be real directories: {item}")

        if rootfs_path in project.parents or project in rootfs_path.parents:
            raise RuntimeError("rootfs and project directory must not overlap.")
        with tempfile.TemporaryDirectory(prefix="sandbox-home-") as staging_dir:
            staged_home = Path(staging_dir) / "home"
            skipped: list[str] = []
            for item in selected_paths:
                source = home_path / item
                destination = staged_home / item
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_selected_tree(source, destination, skipped)
            workdir_in_sandbox = home_path / project_relative
            proc = _bubblewrap_run(
                rootfs_path, home_path, staged_home, workdir_in_sandbox,
                command, timeout_seconds,
            )

        return {
            "status": "ok",
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "rootfs": str(rootfs_path),
            "project_path": str(project),
            "sandbox_project_path": str(workdir_in_sandbox),
            "uid": os.getuid(),
            "gid": os.getgid(),
            "skipped_sensitive_or_unsupported_paths": skipped,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return {
            "status": "error", "message": f"Sandbox command timed out after {timeout_seconds} seconds.",
            "returncode": None, "stdout": stdout, "stderr": stderr,
            "timed_out": True,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "error", "message": str(exc)}


if __name__ == "__main__":
    mcp.run()
