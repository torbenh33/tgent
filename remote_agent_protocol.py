"""Versioned, allowlisted JSON protocol for the fixed remote SSH endpoint."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Mapping

from remote_agent_config import RemoteAgentConfig


SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 4_194_304
REMOTE_COMMAND = "python3 -m remote_agent_service --stdio-v1"
_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_OPERATIONS = frozenset({"prepare_upload", "finalize_upload", "start", "status", "history", "continue", "stop", "logs", "reap"})


class RemoteProtocolError(ValueError):
    """Raised for invalid protocol messages or failed remote responses."""


def _validate_id(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        or any(character not in _ID_CHARACTERS for character in value)
    ):
        raise RemoteProtocolError(f"Invalid {name}")
    return value


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RemoteProtocolError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def validate_request(value: object) -> dict[str, object]:
    """Validate a request envelope without accepting shell commands or paths."""
    if not isinstance(value, dict):
        raise RemoteProtocolError("Remote request must be a JSON object")
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise RemoteProtocolError("Unknown remote operation")

    common = {"schema_version", "operation", "request_id"}
    expected: dict[str, set[str]] = {
        "prepare_upload": common | {"upload_id"},
        "finalize_upload": common | {"upload_id"},
        "start": common | {"upload_id", "request"},
        "status": common | {"agent_id"},
        "history": common | {"agent_id", "limit"},
        "continue": common | {"agent_id", "messages"},
        "stop": common | {"agent_id"},
        "logs": common | {"agent_id", "max_bytes"},
        "reap": common | {"agent_id"},
    }
    if set(value) != expected[operation]:
        raise RemoteProtocolError(f"Invalid fields for remote operation {operation!r}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RemoteProtocolError("Unsupported remote protocol schema version")
    _validate_id(value.get("request_id"), "request_id")
    if operation in {"prepare_upload", "finalize_upload", "start"}:
        _validate_id(value.get("upload_id"), "upload_id")
        if operation == "start" and not isinstance(value.get("request"), dict):
            raise RemoteProtocolError("start request payload must be an object")
    else:
        _validate_id(value.get("agent_id"), "agent_id")
        if operation == "history":
            _bounded_int(value.get("limit"), "limit", 1, 10_000)
        elif operation == "logs":
            _bounded_int(value.get("max_bytes"), "max_bytes", 1, 1_048_576)
        elif operation == "continue":
            messages = value.get("messages")
            if (
                not isinstance(messages, list)
                or not messages
                or len(messages) > 1000
                or any(not isinstance(message, dict) for message in messages)
                or len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > 100_000
            ):
                raise RemoteProtocolError("continue messages must be a non-empty bounded array of objects")
    return value


def dispatch_request(
    message: object,
    handlers: Mapping[str, object],
) -> dict[str, object]:
    """Validate then dispatch one operation through a daemon-owned handler map."""
    request = validate_request(message)
    operation = request["operation"]
    handler = handlers.get(operation)
    if not callable(handler):
        raise RemoteProtocolError(f"Remote operation is unavailable: {operation}")
    result = handler(request)
    try:
        # Verify handler output is JSON-safe before it reaches the wire.
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as err:
        raise RemoteProtocolError("Remote operation returned a non-JSON result") from err
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request["request_id"],
        "ok": True,
        "result": result,
        "error": None,
    }


def error_response(request_id: str | None, error: Exception) -> dict[str, object]:
    """Build a bounded error envelope without exposing exception details."""
    safe_request_id = request_id if isinstance(request_id, str) and _is_valid_id(request_id) else None
    message = str(error)[:500] if isinstance(error, RemoteProtocolError) else "Remote operation failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": safe_request_id,
        "ok": False,
        "result": None,
        "error": {"code": "request_failed", "message": message},
    }


def _is_valid_id(value: str) -> bool:
    return (
        1 <= len(value) <= 128
        and value[0] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        and all(character in _ID_CHARACTERS for character in value)
    )


@dataclass(frozen=True)
class RemoteAgentClient:
    """Send validated JSON requests over one fixed SSH command."""

    config: RemoteAgentConfig
    timeout_seconds: float = 30.0

    def request(self, message: Mapping[str, object]) -> dict[str, object]:
        validated = validate_request(dict(message))
        try:
            encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as err:
            raise RemoteProtocolError(f"Request is not JSON serializable: {err}") from err
        if len(encoded) > MAX_REQUEST_BYTES:
            raise RemoteProtocolError("Remote request exceeds the size limit")

        try:
            completed = subprocess.run(
                ("ssh", self.config.ssh_host, REMOTE_COMMAND),
                input=encoded + bytes((10,)),
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise RemoteProtocolError(f"Remote SSH request failed: {err}") from err
        if completed.returncode != 0:
            raise RemoteProtocolError(f"Remote endpoint exited with status {completed.returncode}")
        if len(completed.stdout) > MAX_RESPONSE_BYTES:
            raise RemoteProtocolError("Remote response exceeds the size limit")
        try:
            response = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise RemoteProtocolError("Remote endpoint returned invalid JSON") from err
        if not isinstance(response, dict) or set(response) != {
            "schema_version", "request_id", "ok", "result", "error"
        }:
            raise RemoteProtocolError("Remote endpoint returned an invalid response envelope")
        if response["schema_version"] != SCHEMA_VERSION:
            raise RemoteProtocolError("Remote endpoint protocol version mismatch")
        if response["request_id"] != validated["request_id"]:
            raise RemoteProtocolError("Remote response request_id does not match")
        if not isinstance(response["ok"], bool):
            raise RemoteProtocolError("Remote response has an invalid ok field")
        if response["ok"] and response["error"] is not None:
            raise RemoteProtocolError("Successful remote response unexpectedly has an error")
        if not response["ok"] and not isinstance(response["error"], dict):
            raise RemoteProtocolError("Failed remote response has no structured error")
        return response
