"""Versioned, allowlisted JSON protocol for the fixed remote SSH endpoint."""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Mapping

from remote_agent_config import RemoteAgentConfig


SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 4_194_304
# Retained for callers that import the original fixed endpoint constant.
REMOTE_COMMAND = "python3 -m remote_agent_service --stdio-v1"
_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_OPERATIONS = frozenset({"prepare_upload", "finalize_upload", "start", "status", "history", "continue", "stop", "stop_agents", "restart_agents", "logs", "reap", "reap_agents", "patches", "acknowledge_patch_applied", "nack_patch", "list_agents", "set_tools", "agent_tools", "summarize_history", "pending_patches", "prepare_sync", "finalize_sync", "review_replies"})


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
        "prepare_sync": common | {"sync_id", "agent_id"},
        "finalize_sync": common | {"sync_id", "agent_id", "size", "sha256", "base_commit"},
        "start": common | {"upload_id", "request"},
        "status": common | {"agent_id"},
        "history": common | {"agent_id", "limit"},
        "continue": common | {"agent_id", "messages"},
        "stop": common | {"agent_id"},
        "stop_agents": common,
        "restart_agents": common | {"agent_ids"},
        "reap_agents": common | {"agent_ids"},
        "logs": common | {"agent_id", "max_bytes"},
        "reap": common | {"agent_id"},
        "patches": common | {"agent_id"},
        "acknowledge_patch_applied": common | {"agent_id"},
        "nack_patch": common | {"agent_id", "reason"},
        "list_agents": common,
        "pending_patches": common,
        "set_tools": common | {"agent_id", "tools"},
        "agent_tools": common | {"agent_id"},
        "summarize_history": common | {"agent_id"},
        "review_replies": common | {"repo_path", "unacknowledged_only"},
    }
    if set(value) != expected[operation]:
        raise RemoteProtocolError(f"Invalid fields for remote operation {operation!r}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RemoteProtocolError("Unsupported remote protocol schema version")
    _validate_id(value.get("request_id"), "request_id")
    if operation in {"prepare_upload", "finalize_upload", "start", "prepare_sync", "finalize_sync"}:
        if operation in {"prepare_sync", "finalize_sync"}:
            _validate_id(value.get("sync_id"), "sync_id")
            _validate_id(value.get("agent_id"), "agent_id")
            if operation == "finalize_sync":
                _bounded_int(value.get("size"), "size", 1, 1_073_741_824)
                if not isinstance(value.get("sha256"), str) or len(value["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in value["sha256"]):
                    raise RemoteProtocolError("Invalid bundle digest")
                if not isinstance(value.get("base_commit"), str) or len(value["base_commit"]) not in {40, 64} or any(c not in "0123456789abcdef" for c in value["base_commit"]):
                    raise RemoteProtocolError("Invalid base commit")
        else:
            _validate_id(value.get("upload_id"), "upload_id")
        if operation == "start" and not isinstance(value.get("request"), dict):
            raise RemoteProtocolError("start request payload must be an object")
    elif operation == "review_replies":
        if not isinstance(value.get("repo_path"), str) or len(value["repo_path"]) > 4096:
            raise RemoteProtocolError("repo_path must be bounded text")
        if not isinstance(value.get("unacknowledged_only"), bool):
            raise RemoteProtocolError("unacknowledged_only must be boolean")
    elif operation not in {"list_agents", "stop_agents", "pending_patches"}:
        _validate_id(value.get("agent_id"), "agent_id")
        if operation == "nack_patch" and (value["reason"] is not None and (not isinstance(value["reason"], str) or len(value["reason"]) > 4000)):
            raise RemoteProtocolError("reason must be null or a string of at most 4000 characters")
        if operation == "set_tools":
            tools = value.get("tools")
            import re
            if (
                not isinstance(tools, list)
                or len(tools) > 256
                or any(not isinstance(name, str) or re.fullmatch(r"mcp_[A-Za-z0-9_-]+", name) is None for name in tools)
                or len(set(tools)) != len(tools)
            ):
                raise RemoteProtocolError("tools must be unique MCP module names")
        elif operation == "history":
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
    if operation in {"reap_agents", "restart_agents"}:
        ids = value.get("agent_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 500 or any(not isinstance(i, str) for i in ids):
            raise RemoteProtocolError("agent_ids must be a non-empty bounded array")
        for agent_id in ids:
            _validate_id(agent_id, "agent_id")
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
            app_dir = self.config.remote_base_dir / "app"
            python = app_dir / ".venv" / "bin" / "python"
            remote_command = (
                f"cd {shlex.quote(str(app_dir))} && "
                f"{shlex.quote(str(python))} -m remote_agent_service --base-dir "
                f"{shlex.quote(str(self.config.remote_base_dir))} --stdio-v1"
            )
            completed = subprocess.run(
                ("ssh", self.config.ssh_host, remote_command),
                input=encoded + bytes((10,)),
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise RemoteProtocolError(f"Remote SSH request failed: {err}") from err
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            if detail:
                detail = detail[-1000:]
                raise RemoteProtocolError(
                    f"Remote endpoint exited with status {completed.returncode}: {detail}"
                )
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
        if not isinstance(response["ok"], bool):
            raise RemoteProtocolError("Remote response has an invalid ok field")
        if response["ok"] and response["error"] is not None:
            raise RemoteProtocolError("Successful remote response unexpectedly has an error")
        if not response["ok"] and not isinstance(response["error"], dict):
            raise RemoteProtocolError("Failed remote response has no structured error")
        if not response["ok"] and response["request_id"] is None:
            error = response["error"]
            detail = error.get("message", "Remote endpoint rejected the request")
            raise RemoteProtocolError(str(detail)[:500])
        if response["request_id"] != validated["request_id"]:
            raise RemoteProtocolError(
                "Remote response request_id does not match "
                f"(expected {validated['request_id']!r}, got {response['request_id']!r})"
            )
        return response
