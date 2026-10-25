"""Deploy the remote agent application and user systemd unit to Beep.

The deployment configuration is read locally from
``~/.config/tgent/remote-agent.toml``. This command synchronizes the app and
its ``.venv`` to ``<remote_base_dir>/app``, publishes a versioned sandbox
runtime, renders and installs the user service unit, and enables the service.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from agent_sandbox import SandboxError
from prepare_remote_runtime import prepare_remote_runtime
from remote_agent_config import RemoteAgentConfigError, load_remote_agent_config


_UNIT_NAME = "tgent-remote-agent.service"
_UNIT_TEMPLATE = Path(__file__).parent / "deploy" / "remote-agent" / "remote-agent.service.in"
_RSYNC_EXCLUDES = (
    ".git/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    "tests/",
    "node_modules/",
    ".cache/",
    ".agent-snapshots/",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "secrets/",
)

def _systemd_quote(value: str) -> str:
    """Quote one systemd ExecStart argument, escaping quotes and specifiers."""
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_setting_escape(value: str) -> str:
    """Escape a value for a non-command systemd setting without adding quotes."""
    escaped = value.replace("\\", "\\\\").replace("%", "%%")
    return escaped.replace(" ", "\\x20").replace("\t", "\\x09")


def _render_unit(config) -> str:
    app_dir = str(config.remote_base_dir / "app")
    python = str(config.remote_base_dir / "app" / ".venv" / "bin" / "python")
    try:
        template = _UNIT_TEMPLATE.read_text(encoding="utf-8")
    except OSError as err:
        raise RuntimeError(f"Cannot read systemd unit template {_UNIT_TEMPLATE}: {err}") from err
    command = " ".join(
        (
            _systemd_quote(python),
            "-m",
            "remote_agent_service",
            "--serve",
            "--base-dir",
            _systemd_quote(str(config.remote_base_dir)),
        )
    )
    rendered = template.replace("@APP_DIR@", _systemd_setting_escape(app_dir))
    rendered = rendered.replace("@PYTHON@", command)
    if "@APP_DIR@" in rendered or "@PYTHON@" in rendered:
        raise RuntimeError("Systemd unit template contains an unknown or unresolved placeholder")
    return rendered


def _ssh(config, *remote_argv: str, check: bool = True) -> subprocess.CompletedProcess:
    command = " ".join(shlex.quote(argument) for argument in remote_argv)
    return subprocess.run(("ssh", config.ssh_host, command), check=check)


def deploy_remote_agent(
    *,
    config_path: str | os.PathLike[str] | None = None,
    rsync: str = "rsync",
    skip_rootfs: bool = False,
) -> None:
    """Synchronize the agent/runtime; never build or modify a sandbox rootfs."""
    config = load_remote_agent_config(config_path)
    project_root = Path(__file__).resolve().parent
    app_dir = config.remote_base_dir / "app"
    # The user unit path is relative to the SSH account's home directory.
    _ssh(config, "systemctl", "--user", "stop", _UNIT_NAME, check=False)
    _ssh(config, "install", "-d", "--", str(app_dir), ".config/systemd/user")

    rsync_argv = [rsync, "-a", "--delete"]
    for pattern in _RSYNC_EXCLUDES:
        rsync_argv.extend(("--exclude", pattern))
    rsync_argv.extend(("--", os.fspath(project_root) + "/", f"{config.ssh_host}:{shlex.quote(str(app_dir))}/"))
    subprocess.run(tuple(rsync_argv), check=True)

    # Publish the sandbox runtime only after the application sync succeeds and
    # before enabling a service that selects the newest complete version. This
    # updates the agent runtime, not the sandbox RFS/rootfs itself. This deploy
    # flow has no RFS build/sync step; --skip-rootfs is retained as an explicit
    # assurance that no such step is requested and must not suppress runtime.
    prepare_remote_runtime(config, rsync=rsync)

    rendered = _render_unit(config)
    with tempfile.TemporaryDirectory(prefix="tgent-unit-") as temporary_dir:
        local_unit = Path(temporary_dir) / _UNIT_NAME
        local_unit.write_text(rendered, encoding="utf-8")
        subprocess.run(
            (
                rsync,
                "-a",
                "--",
                os.fspath(local_unit),
                f"{config.ssh_host}:{shlex.quote('.config/systemd/user/' + _UNIT_NAME)}",
            ),
            check=True,
        )

    _ssh(config, "systemctl", "--user", "daemon-reload")
    _ssh(config, "systemctl", "--user", "enable", "--now", _UNIT_NAME)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="local deployment TOML path")
    parser.add_argument("--rsync", default="rsync", help="rsync executable")
    parser.add_argument(
        "--skip-rootfs",
        action="store_true",
        help="do not build or synchronize the sandbox RFS/rootfs (agent runtime is still updated)",
    )
    arguments = parser.parse_args(argv)
    try:
        deploy_remote_agent(
            config_path=arguments.config,
            rsync=arguments.rsync,
            skip_rootfs=arguments.skip_rootfs,
        )
    except (RemoteAgentConfigError, SandboxError, OSError, RuntimeError, subprocess.CalledProcessError) as err:
        print(f"deploy remote agent: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
