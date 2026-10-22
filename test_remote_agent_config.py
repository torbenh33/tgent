"""Tests for remote deployment configuration."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from remote_agent_config import (
    RemoteAgentConfig,
    RemoteAgentConfigError,
    load_remote_agent_config,
)


def test_loads_remote_agent_config_and_derives_managed_paths(tmp_path) -> None:
    config_path = tmp_path / "remote-agent.toml"
    config_path.write_text(
        'ssh_host = "beep"\nremote_base_dir = "/srv/tgent"\n',
        encoding="utf-8",
    )

    config = load_remote_agent_config(config_path)

    assert config.ssh_host == "beep"
    assert config.remote_base_dir == PurePosixPath("/srv/tgent")
    assert config.runtime_dir == PurePosixPath("/srv/tgent/runtime")
    assert config.uploads_dir == PurePosixPath("/srv/tgent/uploads")
    assert config.state_dir == PurePosixPath("/srv/tgent/state")
    assert config.snapshots_dir == PurePosixPath("/srv/tgent/snapshots")
    assert config.logs_dir == PurePosixPath("/srv/tgent/logs")
    assert config.patches_dir == PurePosixPath("/srv/tgent/patches")


@pytest.mark.parametrize(
    "contents",
    [
        'ssh_host = "beep"\n',
        'ssh_host = "beep"\nremote_base_dir = "/srv/tgent"\nextra = true\n',
        'ssh_host = "-oProxyCommand=bad"\nremote_base_dir = "/srv/tgent"\n',
        'ssh_host = "beep"\nremote_base_dir = "relative/path"\n',
        'ssh_host = "beep"\nremote_base_dir = "/"\n',
        'ssh_host = "beep"\nremote_base_dir = "/srv/../etc"\n',
    ],
)
def test_rejects_invalid_remote_agent_config(tmp_path, contents: str) -> None:
    config_path = tmp_path / "remote-agent.toml"
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(RemoteAgentConfigError):
        load_remote_agent_config(config_path)


def test_missing_config_reports_path(tmp_path) -> None:
    with pytest.raises(RemoteAgentConfigError, match="does not exist"):
        load_remote_agent_config(tmp_path / "missing.toml")


def test_config_object_rejects_unsafe_base_path_and_ssh_host() -> None:
    with pytest.raises(RemoteAgentConfigError, match="filesystem root"):
        RemoteAgentConfig("beep", PurePosixPath("/"))
    with pytest.raises(RemoteAgentConfigError, match="SSH host"):
        RemoteAgentConfig("-oProxyCommand=bad", PurePosixPath("/srv/tgent"))
