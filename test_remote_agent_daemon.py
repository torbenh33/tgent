"""Tests for the remote agent's bounded stdio transport."""

from __future__ import annotations

import io
import json

from remote_agent_daemon import serve_stdio


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
