"""Tests for the remote agent's bounded stdio transport."""

from __future__ import annotations

import io
import json

import pytest

from remote_agent_daemon import build_operation_handlers, serve_stdio
from remote_agent_upload import RemoteUploadManager


def test_build_operation_handlers_exposes_only_explicit_operations(tmp_path) -> None:
    uploads = RemoteUploadManager(tmp_path / "uploads")
    status = lambda request: {"agent_id": request["agent_id"], "state": "running"}

    handlers = build_operation_handlers(
        uploads,
        start_agent=lambda _checkout, _request: {"agent_id": "agent-1"},
        lifecycle_handlers={"status": status},
    )

    assert set(handlers) == {"prepare_upload", "finalize_upload", "start", "status"}
    assert handlers["status"] is status
    with pytest.raises(ValueError, match="Unsupported lifecycle handler"):
        build_operation_handlers(
            uploads,
            start_agent=lambda _checkout, _request: None,
            lifecycle_handlers={"exec": lambda _request: None},
        )


def test_stdio_dispatches_one_request_and_emits_one_response() -> None:
    request = {
        "schema_version": 1,
        "operation": "status",
        "request_id": "request-1",
        "agent_id": "agent-1",
    }
    output = io.StringIO()

    result = serve_stdio(
        {"status": lambda message: {"agent_id": message["agent_id"], "state": "running"}},
        stdin=io.StringIO(json.dumps(request) + "\n"),
        stdout=output,
    )

    assert result == 0
    assert json.loads(output.getvalue()) == {
        "schema_version": 1,
        "request_id": "request-1",
        "ok": True,
        "result": {"agent_id": "agent-1", "state": "running"},
        "error": None,
    }


def test_stdio_rejects_more_than_one_request_per_invocation() -> None:
    request = {
        "schema_version": 1,
        "operation": "status",
        "request_id": "request-1",
        "agent_id": "agent-1",
    }
    payload = json.dumps(request) + chr(10) + json.dumps(request) + chr(10)
    output = io.StringIO()

    serve_stdio({"status": lambda _message: {"state": "running"}}, stdin=io.StringIO(payload), stdout=output)

    response = json.loads(output.getvalue())
    assert response["ok"] is False
    assert "Only one request" in response["error"]["message"]


def test_stdio_returns_structured_error_for_invalid_request() -> None:
    output = io.StringIO()

    result = serve_stdio({}, stdin=io.StringIO('{"operation":"exec"}\\n'), stdout=output)

    response = json.loads(output.getvalue())
    assert result == 0
    assert response["ok"] is False
    assert response["request_id"] is None
    assert response["error"]["code"] == "request_failed"


def test_stdio_rejects_request_without_newline_or_over_size_limit() -> None:
    output = io.StringIO()

    serve_stdio({}, stdin=io.StringIO("{}"), stdout=output)

    response = json.loads(output.getvalue())
    assert response["ok"] is False
    assert "size limit" in response["error"]["message"]


def test_stdio_eof_returns_failure_without_response() -> None:
    output = io.StringIO()

    result = serve_stdio({}, stdin=io.StringIO(""), stdout=output)

    assert result == 1
    assert output.getvalue() == ""
