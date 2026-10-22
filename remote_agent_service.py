"""Persistent controller service and fixed SSH stdio proxy."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
import secrets
import socket
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

from agent_controller import AgentController
from agent_sandbox import RuntimeManifest, SandboxError, read_runtime_manifest
from llm import LLMClient
from remote_agent_config import RemoteAgentConfig, load_remote_agent_config
from remote_agent_daemon import build_async_operation_handlers
from remote_agent_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    SCHEMA_VERSION,
    RemoteProtocolError,
    error_response,
    validate_request,
)
from remote_agent_upload import RemoteUploadManager


def _json_line(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + bytes((10,))


def _managed_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise RuntimeError(f"Managed directory must be a real directory owned by this user: {path}")
    os.chmod(path, 0o700)


def _selected_runtime(config: RemoteAgentConfig) -> RuntimeManifest:
    """Select the latest complete runtime, ignoring partial staging dirs."""
    versions_root = Path(str(config.runtime_dir / "versions"))
    if versions_root.is_symlink() or not versions_root.is_dir():
        raise SandboxError(f"No prepared runtime versions directory: {versions_root}")
    candidates = sorted(
        (
            item for item in versions_root.iterdir()
            if item.is_dir() and not item.is_symlink() and item.name.startswith("runtime-")
        ),
        key=lambda item: item.name,
        reverse=True,
    )
    for directory in candidates:
        try:
            manifest = read_runtime_manifest(directory / "manifest.json")
        except SandboxError:
            continue
        if manifest.version == directory.name:
            return manifest
    raise SandboxError(f"No complete prepared runtime under {versions_root}")


class RemoteAgentService:
    """Long-lived controller process; SSH invocations only proxy requests."""

    def __init__(self, config: RemoteAgentConfig) -> None:
        self.config = config
        for configured in (
            config.remote_base_dir,
            config.state_dir,
            config.snapshots_dir,
            config.logs_dir,
            config.uploads_dir,
            config.patches_dir,
        ):
            _managed_directory(Path(str(configured)))
        self.runtime_manifest = _selected_runtime(config)
        self.uploads = RemoteUploadManager(config.uploads_dir)
        self._llm: LLMClient | None = None
        self.controller = AgentController(
            project_root=None,
            state_root=str(config.state_dir),
            snapshot_root=str(config.snapshots_dir),
            model_callback=self._model_callback,
            patch_submit_callback=self._submit_patch,
            runtime_layout=self.runtime_manifest.layout,
            runtime_version=self.runtime_manifest.version,
            max_model_calls=int(os.environ.get("TGIT_AGENT_MAX_MODEL_CALLS", "100")),
            max_runtime_seconds=int(os.environ.get("TGIT_AGENT_MAX_RUNTIME_SECONDS", "3600")),
        )
        self.handlers = build_async_operation_handlers(
            self.uploads,
            start_agent=self._start_agent,
            lifecycle_handlers={
                "status": self._status,
                "history": self._history,
                "continue": self._continue,
                "stop": self._stop,
                "logs": self._logs,
                "reap": self._reap,
            },
        )
        self.socket_path = Path(str(config.state_dir)) / "remote-agent.sock"
        self.server: asyncio.AbstractServer | None = None

    async def _model_callback(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._llm is None:
            self._llm = LLMClient(os.environ.get("TGIT_AGENT_LLM_CONFIG", "config.ini"))
        allowed = {"messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens", "stop"}
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(f"Unsupported model request field(s): {', '.join(sorted(unknown))}")
        request = dict(params)
        request["model"] = self._llm.model
        response = await asyncio.to_thread(self._llm.client.chat.completions.create, **request)
        if not hasattr(response, "model_dump"):
            raise RuntimeError("OpenAI-compatible client returned an unsupported completion object")
        return response.model_dump(mode="json")

    async def _submit_patch(self, snapshot, params: dict[str, Any]) -> dict[str, Any]:
        module = importlib.import_module("mcp_tgit_review")
        export = getattr(module, "export_patch_series", None)
        if not callable(export):
            raise RuntimeError("mcp_tgit_review.export_patch_series is unavailable")
        result = await asyncio.to_thread(
            export,
            base=snapshot.base_commit,
            head=params["head_commit"],
            repo_path=str(snapshot.project_root),
        )
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise RuntimeError("Patch queue export failed")
        return result

    async def _start_agent(self, checkout: Path, request: dict[str, object]) -> dict[str, Any]:
        return await self.controller.start_agent(request, source_root=checkout)

    async def _status(self, request):
        return await self.controller.get_status(request["agent_id"])

    async def _history(self, request):
        history = await self.controller.get_history(request["agent_id"])
        return {"agent_id": request["agent_id"], "messages": history[-request["limit"]:]}

    async def _continue(self, request):
        messages = [dict(message) for message in request["messages"]]
        return await self.controller.continue_agent(request["agent_id"], messages)

    async def _stop(self, request):
        return await self.controller.stop_agent(request["agent_id"])

    async def _logs(self, request):
        return await self.controller.get_agent_log(request["agent_id"], max_bytes=request["max_bytes"])

    async def _reap(self, request):
        return await self.controller.reap_agent(request["agent_id"])

    async def _dispatch(self, message: object) -> dict[str, object]:
        request_id = message.get("request_id") if isinstance(message, dict) else None
        try:
            request = validate_request(message)
            handler = self.handlers.get(request["operation"])
            if not callable(handler):
                raise RemoteProtocolError(f"Remote operation is unavailable: {request['operation']}")
            result = handler(request)
            if inspect.isawaitable(result):
                result = await result
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            return {
                "schema_version": SCHEMA_VERSION,
                "request_id": request["request_id"],
                "ok": True,
                "result": result,
                "error": None,
            }
        except Exception as err:
            return error_response(request_id, err)

    async def _client_connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response: dict[str, object]
        try:
            raw = await reader.readline()
            if not raw:
                return
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(bytes((10,))):
                response = error_response(None, RemoteProtocolError("Request line is missing or exceeds the size limit"))
            elif await reader.read(1):
                response = error_response(None, RemoteProtocolError("Only one request is allowed per connection"))
            else:
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    response = error_response(None, RemoteProtocolError("Invalid request JSON"))
                else:
                    response = await self._dispatch(message)
            payload = _json_line(response)
            if len(payload) > MAX_RESPONSE_BYTES:
                payload = _json_line(error_response(response.get("request_id"), RemoteProtocolError("Response exceeds the size limit")))
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self) -> None:
        parent = self.socket_path.parent
        info = parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("Service socket directory must be private and owned by this user")
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            raise RuntimeError(f"Refusing to replace existing service socket path: {self.socket_path}")
        self.server = await asyncio.start_unix_server(
            self._client_connected,
            path=os.fspath(self.socket_path),
            limit=MAX_REQUEST_BYTES + 1,
        )
        os.chmod(self.socket_path, 0o600)
        try:
            await self.server.serve_forever()
        finally:
            await self.controller.shutdown()
            self.server.close()
            await self.server.wait_closed()
            self.socket_path.unlink(missing_ok=True)


def proxy_stdio(socket_path: Path) -> int:
    """Forward exactly one bounded SSH request to the persistent service."""
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 2)
    if not raw:
        return 1
    if len(raw) > MAX_REQUEST_BYTES + 1 or not raw.endswith(bytes((10,))):
        response = error_response(None, RemoteProtocolError("Request line is missing or exceeds the size limit"))
        sys.stdout.buffer.write(_json_line(response))
        sys.stdout.buffer.flush()
        return 0
    if sys.stdin.buffer.read(1):
        response = error_response(None, RemoteProtocolError("Only one request is allowed per SSH invocation"))
        sys.stdout.buffer.write(_json_line(response))
        sys.stdout.buffer.flush()
        return 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(60)
            client.connect(os.fspath(socket_path))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            response = bytearray()
            while len(response) <= MAX_RESPONSE_BYTES:
                chunk = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(response)))
                if not chunk:
                    break
                response.extend(chunk)
        if len(response) > MAX_RESPONSE_BYTES or not response.endswith(bytes((10,))):
            raise RemoteProtocolError("Remote service returned invalid response framing")
        sys.stdout.buffer.write(response)
    except (OSError, TimeoutError, RemoteProtocolError) as err:
        sys.stdout.buffer.write(_json_line(error_response(None, err)))
    sys.stdout.buffer.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serve", action="store_true", help="run the persistent service")
    group.add_argument("--stdio-v1", action="store_true", help="proxy one SSH request")
    parser.add_argument("--config", type=Path, help="deployment TOML path")
    arguments = parser.parse_args(argv)
    config = load_remote_agent_config(arguments.config)
    if arguments.stdio_v1:
        return proxy_stdio(Path(str(config.state_dir)) / "remote-agent.sock")
    try:
        asyncio.run(RemoteAgentService(config).run())
    except (OSError, RuntimeError, SandboxError) as err:
        print(f"remote agent service: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
