"""Tests for the persistent remote-agent controller service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
