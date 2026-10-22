"""Tests for the fixed SSH remote-agent request protocol."""

from __future__ import annotations

import json
import subprocess

import pytest

from remote_agent_config import RemoteAgentConfig
from remote_agent_protocol import (
    MAX_REQUEST_BYTES,
    REMOTE_COMMAND,
    RemoteAgentClient,
    RemoteProtocolError,
    validate_request,
)


def _request(**overrides):
    request = {
        "schema_version": 1,
        "operation": "status",
        "request_id": "req-1",
        "agent_id": "agent-1",
    }
    request.update(overrides)
    return request


def test_validates_allowlisted_operations_and_rejects_extra_fields() -> None:
    assert validate_request(_request())["operation"] == "status"
    with pytest.raises(RemoteProtocolError, match="Unknown remote operation"):
        validate_request(_request(operation="exec", command="id"))
    with pytest.raises(RemoteProtocolError, match="Invalid fields"):
        validate_request(_request(command="id"))


def test_validates_prepare_upload_operation() -> None:
    request = {
        "schema_version": 1,
        "operation": "prepare_upload",
        "request_id": "req-1",
        "upload_id": "upload-1",
    }
    assert validate_request(request) == request
    with pytest.raises(RemoteProtocolError, match="Invalid fields"):
        validate_request({**request, "remote_path": "/tmp/upload"})


def test_validates_identifiers_and_bounded_operation_fields() -> None:
    with pytest.raises(RemoteProtocolError, match="request_id"):
        validate_request(_request(request_id="../bad"))
    with pytest.raises(RemoteProtocolError, match="between"):
        validate_request(
            {
                "schema_version": 1,
                "operation": "logs",
                "request_id": "req-1",
                "agent_id": "agent-1",
                "max_bytes": 10_000_000,
            }
        )


def test_dispatch_validates_request_and_wraps_handler_result() -> None:
    from remote_agent_protocol import dispatch_request

    response = dispatch_request(
        _request(),
        {"status": lambda request: {"agent_id": request["agent_id"], "state": "running"}},
    )

    assert response == {
        "schema_version": 1,
        "request_id": "req-1",
        "ok": True,
        "result": {"agent_id": "agent-1", "state": "running"},
        "error": None,
    }


def test_dispatch_rejects_missing_operation_handler() -> None:
    from remote_agent_protocol import dispatch_request

    with pytest.raises(RemoteProtocolError, match="unavailable"):
        dispatch_request(_request(), {})


def test_client_uses_fixed_ssh_command_and_checks_response(monkeypatch) -> None:
    calls = []
    response = {
        "schema_version": 1,
        "request_id": "req-1",
        "ok": True,
        "result": {"status": "running"},
        "error": None,
    }

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(response).encode(), stderr=b"")

    monkeypatch.setattr("remote_agent_protocol.subprocess.run", fake_run)
    client = RemoteAgentClient(RemoteAgentConfig("beep", "/srv/tgent"))

    result = client.request(_request())

    assert result["result"] == {"status": "running"}
    argv, kwargs = calls[0]
    assert argv == (
        "ssh",
        "beep",
        "python3 -m remote_agent_service --base-dir /srv/tgent --stdio-v1",
    )
    assert kwargs["input"].endswith(bytes((10,)))
    assert json.loads(kwargs["input"]) == _request()
    assert kwargs["check"] is False


def test_client_rejects_oversized_request_before_ssh(monkeypatch) -> None:
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("remote_agent_protocol.subprocess.run", fake_run)
    client = RemoteAgentClient(RemoteAgentConfig("beep", "/srv/tgent"))
    request = {
        "schema_version": 1,
        "operation": "start",
        "request_id": "req-1",
        "upload_id": "upload-1",
        "request": {"task": "x" * MAX_REQUEST_BYTES},
    }

    with pytest.raises(RemoteProtocolError, match="size limit"):
        client.request(request)
    assert not called


def test_client_rejects_mismatched_response_id(monkeypatch) -> None:
    response = {
        "schema_version": 1,
        "request_id": "different",
        "ok": True,
        "result": None,
        "error": None,
    }
    monkeypatch.setattr(
        "remote_agent_protocol.subprocess.run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(response).encode(), stderr=b""
        ),
    )
    client = RemoteAgentClient(RemoteAgentConfig("beep", "/srv/tgent"))

    with pytest.raises(RemoteProtocolError, match="does not match"):
        client.request(_request())
