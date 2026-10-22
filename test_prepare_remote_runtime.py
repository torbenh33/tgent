"""Tests for versioned remote runtime preparation."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

import prepare_remote_runtime as preparation
from agent_sandbox import RuntimeMount, SandboxRuntimeLayout
from remote_agent_config import RemoteAgentConfig


def test_preparation_publishes_manifest_after_rsync(tmp_path, monkeypatch) -> None:
    source_dir = tmp_path / "python"
    source_dir.mkdir()
    source_file = tmp_path / "agent_worker.py"
    source_file.write_text("worker", encoding="utf-8")
    layout = SandboxRuntimeLayout(
        mounts=(
            RuntimeMount(source_dir, Path("/usr/local/python")),
            RuntimeMount(source_file, Path("/opt/agent-runtime/agent_worker.py")),
        ),
        symlinks=((Path("/bin"), "usr/bin"),),
    )
    monkeypatch.setattr(preparation, "build_runtime_layout", lambda **_kwargs: layout)
    calls = []

    def fake_run(argv, *, check, **kwargs):
        assert check is True
        calls.append(tuple(str(part) for part in argv))
        if kwargs.get("capture_output"):
            return preparation.subprocess.CompletedProcess(argv, 0, stdout="x86_64\n", stderr="")

    monkeypatch.setattr(preparation.subprocess, "run", fake_run)
    config = RemoteAgentConfig("beep", PurePosixPath("/srv/tgent"))

    manifest = preparation.prepare_remote_runtime(
        config,
        version="runtime-test",
        python_executable=source_dir / "bin" / "python",
    )

    assert manifest.version == "runtime-test"
    assert manifest.rsync_completed_at.tzinfo is not None
    assert manifest.layout.symlinks == ((Path("/bin"), "usr/bin"),)
    assert len(manifest.layout.mounts) == 2
    assert all(
        str(mount.source).startswith("/srv/tgent/runtime/versions/runtime-test/mounts/")
        for mount in manifest.layout.mounts
    )
    rsync_calls = [call for call in calls if call[0] == "rsync"]
    assert len(rsync_calls) == 3
    assert all("--no-owner" in call and "--no-group" in call for call in rsync_calls[:2])
    assert any(call[:3] == ("ssh", "beep", "uname -m") for call in calls)
    assert any("PYTHONPATH=/srv/tgent/runtime/.staging-runtime-test/mounts/agent-runtime" in call[2]
               for call in calls if call[:2] == ("ssh", "beep"))
    assert any(call[2] == "sh -c 'command -v bwrap >/dev/null && bwrap --version >/dev/null'"
               for call in calls if call[:2] == ("ssh", "beep"))
    assert calls[-1] == (
        "ssh",
        "beep",
        "mv -T -- /srv/tgent/runtime/.staging-runtime-test "
        "/srv/tgent/runtime/versions/runtime-test",
    )


def test_preparation_rejects_non_amd64_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "python"
    source_dir.mkdir()
    layout = SandboxRuntimeLayout((RuntimeMount(source_dir, Path("/usr/local/python")),))
    monkeypatch.setattr(preparation, "build_runtime_layout", lambda **_kwargs: layout)
    calls = []

    def fake_run(argv, *, check, **kwargs):
        calls.append(tuple(str(part) for part in argv))
        if kwargs.get("capture_output"):
            return preparation.subprocess.CompletedProcess(argv, 0, stdout="aarch64\n", stderr="")

    monkeypatch.setattr(preparation.subprocess, "run", fake_run)

    with pytest.raises(preparation.SandboxError, match="requires amd64"):
        preparation.prepare_remote_runtime(
            RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")),
            version="runtime-test",
            python_executable=source_dir / "bin" / "python",
        )

    assert not any(call[:2] == ("ssh", "beep") and "mv -T" in call[2] for call in calls)


def test_preparation_rejects_unsafe_version_before_remote_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(preparation.subprocess, "run", fake_run)

    with pytest.raises(preparation.SandboxError, match="Invalid runtime version"):
        preparation.prepare_remote_runtime(
            RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")),
            version="../unsafe",
        )

    assert not called
