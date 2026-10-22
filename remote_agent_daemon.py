"""Bounded one-request stdio transport for the remote agent protocol."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import TextIO

from remote_agent_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    RemoteProtocolError,
    dispatch_request,
    error_response,
)


def serve_stdio(
    handlers: Mapping[str, object],
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Handle one bounded JSON line, returning one JSON response line."""
    request_stream = stdin if stdin is not None else sys.stdin
    response_stream = stdout if stdout is not None else sys.stdout
    raw_line = request_stream.readline(MAX_REQUEST_BYTES + 2)
    request: object = None
    if not raw_line:
        return 1

    if len(raw_line.encode("utf-8")) > MAX_REQUEST_BYTES + 1 or not raw_line.endswith(chr(10)):
        response = error_response(None, RemoteProtocolError("Request line is missing or exceeds the size limit"))
    elif request_stream.read(1):
        response = error_response(None, RemoteProtocolError("Only one request is allowed per SSH invocation"))
    else:
        try:
            request = json.loads(raw_line)
            response = dispatch_request(request, handlers)
        except Exception as err:
            request_id = request.get("request_id") if isinstance(request, dict) else None
            response = error_response(request_id, err)

    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
        encoded = json.dumps(
            error_response(response.get("request_id"), RemoteProtocolError("Response exceeds the size limit")),
            separators=(",", ":"),
        )
    response_stream.write(encoded + "\n")
    response_stream.flush()
    return 0
