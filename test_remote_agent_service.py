"""Tests for the persistent remote-agent controller service."""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
from datetime import datetime, timezone

import pytest
from pathlib import Path, PurePosixPath

from agent_sandbox import RuntimeManifest, SandboxRuntimeLayout, write_runtime_manifest
from remote_agent_config import RemoteAgentConfig
from remote_agent_service import RemoteAgentService, _selected_runtime


def test_selects_latest_complete_runtime_manifest(tmp_path: Path) -> None:
    config = RemoteAgentConfig("beep", PurePosixPath(str(tmp_path / "remote")))
    versions = Path(str(config.runtime_dir / "versions"))
    for version in ("runtime-20260101", "runtime-20260201"):
        directory = versions / version
        directory.mkdir(parents=True)
        write_runtime_manifest(
            directory / "manifest.json",
            RuntimeManifest(
                version=version,
                rsync_completed_at=datetime.now(timezone.utc),
                layout=SandboxRuntimeLayout(()),
            ),
        )
    (versions / "runtime-20260301").mkdir()

    selected = _selected_runtime(config)

    assert selected.version == "runtime-20260201"


def test_dispatch_uses_registered_handler_and_returns_protocol_envelope() -> None:
    service = RemoteAgentService.__new__(RemoteAgentService)
    service.handlers = {"status": lambda request: {"agent_id": request["agent_id"], "state": "running"}}
    request = {
        "schema_version": 1,
        "operation": "status",
        "request_id": "req-1",
        "agent_id": "agent-1",
    }

    response = asyncio.run(service._dispatch(request))

    assert response == {
        "schema_version": 1,
        "request_id": "req-1",
        "ok": True,
        "result": {"agent_id": "agent-1", "state": "running"},
        "error": None,
    }


def test_main_uses_command_line_base_dir_for_stdio_proxy(monkeypatch) -> None:
    import remote_agent_service

    called = []
    monkeypatch.setattr(
        remote_agent_service,
        "proxy_stdio",
        lambda socket_path: called.append(socket_path) or 0,
    )

    result = remote_agent_service.main(["--stdio-v1", "--base-dir", "/srv/tgent"])

    assert result == 0
    assert called == [Path("/srv/tgent/state/remote-agent.sock")]
def test_service_removes_stale_socket_after_acquiring_lock(tmp_path) -> None:
    import remote_agent_service

    service = remote_agent_service.RemoteAgentService.__new__(remote_agent_service.RemoteAgentService)
    service.socket_path = tmp_path / "remote-agent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(service.socket_path))
    listener.close()
    service._service_lock_fd = None

    service._acquire_service_lock_and_clear_stale_socket()

    assert not service.socket_path.exists()
    assert service._service_lock_fd is not None
    os.close(service._service_lock_fd)


def test_service_refuses_active_socket_after_acquiring_lock(tmp_path) -> None:
    import remote_agent_service

    service = remote_agent_service.RemoteAgentService.__new__(remote_agent_service.RemoteAgentService)
    service.socket_path = tmp_path / "remote-agent.sock"
    service._service_lock_fd = None
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(service.socket_path))
    listener.listen()
    try:
        with pytest.raises(RuntimeError, match="Service socket is active"):
            service._acquire_service_lock_and_clear_stale_socket()
    finally:
        listener.close()


def test_stdio_proxy_exposes_service_socket_connection_failure(monkeypatch) -> None:
    import remote_agent_service

    class BrokenSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            raise ConnectionRefusedError("service socket unavailable")

    output = io.BytesIO()
    monkeypatch.setattr(remote_agent_service.socket, "socket", lambda *_args: BrokenSocket())
    monkeypatch.setattr(remote_agent_service.sys, "stdin", type("Input", (), {"buffer": io.BytesIO(b"{}" + bytes((10,)))})())
    monkeypatch.setattr(remote_agent_service.sys, "stdout", type("Output", (), {"buffer": output})())

    result = remote_agent_service.proxy_stdio(Path("/run/remote-agent.sock"))

    response = json.loads(output.getvalue())
    assert result == 0
    assert response["ok"] is False
    assert "ConnectionRefusedError: service socket unavailable" in response["error"]["message"]


def test_dispatch_does_not_expose_unregistered_operations() -> None:
    service = RemoteAgentService.__new__(RemoteAgentService)
    service.handlers = {}

    response = asyncio.run(service._dispatch({
        "schema_version": 1,
        "operation": "status",
        "request_id": "req-1",
        "agent_id": "agent-1",
    }))

    assert response["ok"] is False
    assert response["request_id"] == "req-1"
    assert response["error"]["code"] == "request_failed"
