"""User-level deployment configuration for the remote agent service.

The TOML file contains only the SSH destination and the remote service's base
folder. Service-managed paths are derived below that folder so the client and
daemon agree on a predictable layout.

Example ``~/.config/tgent/remote-agent.toml``::

    ssh_host = "beep"
    remote_base_dir = "/srv/tgent"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class RemoteAgentConfigError(ValueError):
    """Raised when remote deployment configuration is missing or invalid."""


_DEFAULT_CONFIG_PATH = Path("~/.config/tgent/remote-agent.toml")
_HOST_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@-")


@dataclass(frozen=True)
class RemoteAgentConfig:
    """SSH destination and managed remote directories for agent deployment."""

    ssh_host: str
    remote_base_dir: PurePosixPath

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ssh_host, str)
            or not self.ssh_host
            or self.ssh_host[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
            or any(character not in _HOST_CHARACTERS for character in self.ssh_host)
        ):
            raise RemoteAgentConfigError("ssh_host must be a valid SSH host alias or user@host")
        raw_base = os.fspath(self.remote_base_dir)
        if not isinstance(raw_base, str) or not raw_base.startswith("/"):
            raise RemoteAgentConfigError("remote_base_dir must be an absolute POSIX path")
        if "\x00" in raw_base or any(ord(char) < 32 or ord(char) == 127 for char in raw_base):
            raise RemoteAgentConfigError("remote_base_dir contains invalid control characters")
        if any(part in {".", ".."} for part in raw_base.split("/")):
            raise RemoteAgentConfigError("remote_base_dir must not contain '.' or '..' components")
        normalized = PurePosixPath(raw_base)
        if normalized == PurePosixPath("/"):
            raise RemoteAgentConfigError("remote_base_dir must not be the filesystem root")
        object.__setattr__(self, "remote_base_dir", normalized)

    @property
    def runtime_dir(self) -> PurePosixPath:
        return self.remote_base_dir / "runtime"

    @property
    def uploads_dir(self) -> PurePosixPath:
        return self.remote_base_dir / "uploads"

    @property
    def state_dir(self) -> PurePosixPath:
        return self.remote_base_dir / "state"

    @property
    def snapshots_dir(self) -> PurePosixPath:
        return self.remote_base_dir / "snapshots"

    @property
    def logs_dir(self) -> PurePosixPath:
        return self.remote_base_dir / "logs"

    @property
    def patches_dir(self) -> PurePosixPath:
        return self.remote_base_dir / "patches"


def load_remote_agent_config(
    path: str | os.PathLike[str] | None = None,
) -> RemoteAgentConfig:
    """Load strict deployment settings from TOML.

    By default, read ``~/.config/tgent/remote-agent.toml``. The file must have
    exactly the ``ssh_host`` and ``remote_base_dir`` string fields.
    """
    config_path = Path(path).expanduser() if path is not None else _DEFAULT_CONFIG_PATH.expanduser()
    try:
        with config_path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError as err:
        raise RemoteAgentConfigError(f"Remote agent config does not exist: {config_path}") from err
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise RemoteAgentConfigError(f"Cannot read remote agent config {config_path}: {err}") from err

    if not isinstance(data, dict) or set(data) != {"ssh_host", "remote_base_dir"}:
        raise RemoteAgentConfigError(
            "Remote agent config must contain exactly 'ssh_host' and 'remote_base_dir'"
        )
    if not isinstance(data["ssh_host"], str) or not isinstance(data["remote_base_dir"], str):
        raise RemoteAgentConfigError("ssh_host and remote_base_dir must be strings")
    return RemoteAgentConfig(data["ssh_host"], PurePosixPath(data["remote_base_dir"]))
