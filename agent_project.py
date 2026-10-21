"""Project-root discovery and conservative agent sandbox configuration."""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

CONFIG_FILENAME = ".agent-sandbox.toml"
CONFIG_VERSION = 1
_SENSITIVE_COMPONENTS = frozenset(
    {".aws", ".azure", ".gnupg", ".kube", ".ssh", ".terraform.d", "credentials", "secrets"}
)
_SENSITIVE_FILENAMES = frozenset(
    {".netrc", ".npmrc", ".pypirc", "credentials", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}
)


class ProjectConfigError(ValueError):
    """Raised when project configuration is invalid or unsafe."""


class ProjectRootError(ValueError):
    """Raised when the current directory is not the Git project root."""


@dataclass(frozen=True)
class ProjectConfig:
    """Validated project-requested sandbox settings; requests are not authorization."""

    root: Path
    home_read_only: tuple[Path, ...] = ()
    home_read_write: tuple[Path, ...] = ()

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME


def discover_project_root(cwd: str | os.PathLike[str] | None = None) -> Path:
    """Require cwd itself to be the Git root; do not search parent directories.

    Examples:
        >>> import subprocess, tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     subprocess.run(["git", "init", tmp], check=True, capture_output=True)
        ...     discover_project_root(tmp) == Path(tmp).resolve()
        True
    """
    candidate = Path(cwd if cwd is not None else os.getcwd()).expanduser().resolve()
    if not candidate.is_dir():
        raise ProjectRootError(f"Project directory does not exist: {candidate}")
    try:
        proc = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ProjectRootError(f"Could not run Git to inspect {candidate}: {err}") from err
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "not inside a Git repository"
        raise ProjectRootError(f"Could not determine Git root for {candidate}: {detail}")
    git_root = Path(proc.stdout.strip()).expanduser().resolve()
    if candidate != git_root:
        raise ProjectRootError(
            f"Controller working directory must be the Git project root; "
            f"working directory is {candidate}, Git root is {git_root}"
        )
    return git_root


def load_project_config(
    project_root: str | os.PathLike[str],
    *,
    home: str | os.PathLike[str] | None = None,
) -> ProjectConfig:
    """Load optional .agent-sandbox.toml and validate requested home paths.

    Returned home paths are relative to home. A trusted controller must
    independently authorize every mount against its own allowlist.
    """
    root = Path(project_root).expanduser().resolve()
    config_path = root / CONFIG_FILENAME
    home_root = Path(home if home is not None else Path.home()).expanduser().resolve()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProjectConfig(root=root)
    except (OSError, UnicodeError) as err:
        raise ProjectConfigError(f"Could not read {config_path}: {err}") from err
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as err:
        raise ProjectConfigError(f"Invalid TOML in {config_path}: {err}") from err

    _require_keys(data, {"version", "home"}, "top level")
    version = data.get("version")
    if type(version) is not int or version != CONFIG_VERSION:
        raise ProjectConfigError(f"{config_path}: version must be {CONFIG_VERSION}")
    home_data = data.get("home", {})
    if not isinstance(home_data, dict):
        raise ProjectConfigError(f"{config_path}: [home] must be a table")
    _require_keys(home_data, {"read_only", "read_write"}, "[home]")

    read_only = _requested_paths(home_data.get("read_only", []), "home.read_only", home_root)
    read_write = _requested_paths(home_data.get("read_write", []), "home.read_write", home_root)
    overlap = set(read_only) & set(read_write)
    if overlap:
        raise ProjectConfigError(
            "Home path requested with conflicting read-only and read-write permissions: "
            f"{sorted(overlap)[0]}"
        )
    return ProjectConfig(root=root, home_read_only=read_only, home_read_write=read_write)


def _require_keys(value: dict[str, Any], allowed: set[str], section: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProjectConfigError(f"Unknown key(s) in {section}: {', '.join(unknown)}")


def _requested_paths(value: Any, setting: str, home_root: Path) -> tuple[Path, ...]:
    if not isinstance(value, list):
        raise ProjectConfigError(f"{setting} must be an array of home-relative paths")
    output: list[Path] = []
    seen: set[Path] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ProjectConfigError(
                f"{setting}[{index}] must be a non-empty string representing a normalized home-relative path"
            )
        relative = PurePosixPath(item)
        if (
            relative.is_absolute()
            or "\\" in item
            or "\x00" in item
            or any(part in {"", ".", ".."} for part in item.split("/"))
        ):
            raise ProjectConfigError(f"{setting}[{index}] must be a normalized home-relative path")
        requested = Path(*relative.parts)
        unresolved = home_root / requested
        if any(part.is_symlink() for part in (unresolved, *unresolved.parents) if part != home_root.parent):
            raise ProjectConfigError(f"{setting}[{index}] must not contain symlinks: {item}")
        resolved = unresolved.resolve()
        try:
            resolved.relative_to(home_root)
        except ValueError as err:
            raise ProjectConfigError(f"{setting}[{index}] resolves outside the home directory") from err
        parts = tuple(part.casefold() for part in resolved.relative_to(home_root).parts)
        components = set(parts)
        if (
            components & (_SENSITIVE_COMPONENTS | _SENSITIVE_FILENAMES)
            or any(parts[index : index + 2] == (".config", "gcloud") for index in range(len(parts) - 1))
        ):
            raise ProjectConfigError(f"{setting}[{index}] requests a sensitive home path: {item}")
        if not resolved.exists():
            raise ProjectConfigError(f"{setting}[{index}] does not exist: {item}")
        if not (resolved.is_file() or resolved.is_dir()):
            raise ProjectConfigError(f"{setting}[{index}] must be a regular file or directory: {item}")
        if resolved in seen:
            raise ProjectConfigError(f"{setting} contains a duplicate path: {item}")
        seen.add(resolved)
        output.append(requested)
    return tuple(output)
