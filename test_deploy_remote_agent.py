"""Tests for remote-agent application and systemd deployment."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import deploy_remote_agent as deployment
from remote_agent_config import RemoteAgentConfig


def test_render_unit_uses_configured_remote_paths() -> None:
    config = RemoteAgentConfig("beep", "/srv/tgent")

    rendered = deployment._render_unit(config)

    assert "WorkingDirectory=/srv/tgent/app" in rendered
    assert (
        'ExecStart="/srv/tgent/app/.venv/bin/python" -m remote_agent_service '
        '--serve --base-dir "/srv/tgent"'
    ) in rendered
    assert "@APP_DIR@" not in rendered
    assert "@PYTHON@" not in rendered


def test_deploy_syncs_app_and_installs_rendered_user_unit(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "remote-agent.toml"
    config_path.write_text(
        'ssh_host = "beep"\nremote_base_dir = "/srv/tgent"\n',
        encoding="utf-8",
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    events: list[str] = []

    def fake_prepare(config, *, rsync):
        assert str(config.remote_base_dir) == "/srv/tgent"
        assert rsync == "rsync"
        events.append("runtime")

    def fake_run(argv, **kwargs):
        calls.append((tuple(str(part) for part in argv), kwargs))
        if argv[0] == "rsync" and "--delete" in argv:
            events.append("app")
        elif argv[0] == "rsync":
            events.append("unit")
        if argv[0] == "rsync" and "--delete" not in argv:
            rendered_file = Path(argv[-2])
            assert rendered_file.read_text(encoding="utf-8").startswith("[Unit]")
            assert "WorkingDirectory=/srv/tgent/app" in rendered_file.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(deployment.subprocess, "run", fake_run)
    monkeypatch.setattr(deployment, "prepare_remote_runtime", fake_prepare)

    deployment.deploy_remote_agent(config_path=config_path)

    commands = [argv[2] for argv, _kwargs in calls if argv[0] == "ssh"]
    assert commands[0] == "systemctl --user stop tgent-remote-agent.service"
    assert "install -d -- /srv/tgent .config/systemd/user" in commands[1]
    assert commands[-3:] == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now tgent-remote-agent.service",
        "rm -rf -- /srv/tgent/app.rollback",
    ]
    assert events == ["app", "runtime", "unit"]
    app_sync = next(argv for argv, _kwargs in calls if argv[0] == "rsync" and "--delete" in argv)
    assert app_sync[1:3] == ("-a", "--delete")
    assert not any(
        app_sync[index] == "--exclude" and app_sync[index + 1] == ".venv/"
        for index in range(len(app_sync) - 1)
    )
    assert app_sync[-1] == "beep:/srv/tgent/app.staging/"
    unit_sync = next(argv for argv, _kwargs in calls if argv[0] == "rsync" and "--delete" not in argv)
    assert unit_sync[-1] == "beep:.config/systemd/user/tgent-remote-agent.service"


def test_self_hosted_mode_refreshes_checkout_package_before_runtime(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "remote-agent.toml"
    config_path.write_text(
        'ssh_host = "beep"\nremote_base_dir = "/srv/tgent"\n', encoding="utf-8"
    )
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(deployment, "prepare_remote_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        deployment.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(tuple(str(part) for part in argv)) or subprocess.CompletedProcess(argv, 0),
    )
    deployment.deploy_remote_agent(config_path=config_path, install_project=True)
    installer = next(call for call in calls if call[0] == "ssh" and "pip" in call[2])
    assert "--no-deps" in installer[2]
    assert "--editable /srv/tgent/app" in installer[2]


def test_deploy_skip_rootfs_keeps_rootfs_unmodified_but_deploys_agent_and_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "remote-agent.toml"
    config_path.write_text(
        'ssh_host = "beep"\nremote_base_dir = "/srv/tgent"\n',
        encoding="utf-8",
    )
    # Represents pre-existing rootfs state. There is no rootfs-building or
    # rootfs-sync operation in this app/runtime deployment flow.
    rootfs_contents = {"sentinel": "preserve exactly"}
    original_rootfs_contents = rootfs_contents.copy()
    calls: list[tuple[str, ...]] = []

    def fake_prepare(config, *, rsync):
        assert str(config.remote_base_dir) == "/srv/tgent"
        assert rsync == "rsync"
        calls.append(("prepare-runtime",))

    def fake_run(argv, **_kwargs):
        normalized = tuple(str(part) for part in argv)
        calls.append(normalized)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(deployment.subprocess, "run", fake_run)
    monkeypatch.setattr(deployment, "prepare_remote_runtime", fake_prepare)

    deployment.deploy_remote_agent(config_path=config_path, skip_rootfs=True)

    assert rootfs_contents == original_rootfs_contents
    assert any(call[0] == "rsync" and "--delete" in call for call in calls)
    assert ("prepare-runtime",) in calls
    assert any("systemctl --user enable --now" in call[-1] for call in calls if call[0] == "ssh")
    assert not any("rootfs" in " ".join(call).lower() for call in calls)


def test_cli_exposes_skip_rootfs_option(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        deployment.main(["--help"])
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "--skip-rootfs" in output
    assert "--install-project" in output
