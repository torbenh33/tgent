"""Bounded JSON-RPC transport over a private Unix-domain socket.

The controller listens and the sandboxed worker connects. This module provides
transport and dispatch only; agent state, authorization, persistence, and MCP
management remain responsibilities of the controller integration.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

JSONRPC_VERSION = "2.0"
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 7200

JsonObject = dict[str, Any]
RpcHandler = Callable[[JsonObject], Any | Awaitable[Any]]


class AgentIPCError(RuntimeError):
    """Raised for local transport or protocol failures."""


class JsonRpcRemoteError(AgentIPCError):
    """Raised when the remote JSON-RPC peer returns an error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def _encode_line(value: JsonObject, max_bytes: int) -> bytes:
    try:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError) as err:
        raise AgentIPCError(f"Could not encode JSON-RPC message: {err}") from err
    if len(data) > max_bytes:
        raise AgentIPCError(f"JSON-RPC message exceeds {max_bytes} byte limit")
    return data


async def _read_line(reader: asyncio.StreamReader, max_bytes: int) -> JsonObject:
    try:
        raw = await reader.readline()
    except (asyncio.LimitOverrunError, ValueError) as err:
        raise AgentIPCError("JSON-RPC message exceeds stream limit") from err
    if not raw:
        raise AgentIPCError("Peer closed the connection")
    if len(raw) > max_bytes:
        raise AgentIPCError(f"JSON-RPC message exceeds {max_bytes} byte limit")
    if not raw.endswith(b"\n"):
        raise AgentIPCError("JSON-RPC message must end with newline")
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise AgentIPCError("Invalid JSON-RPC JSON") from err
    if not isinstance(message, dict):
        raise AgentIPCError("JSON-RPC message must be an object")
    return message


def _validate_request(message: JsonObject) -> tuple[str, Any, JsonObject]:
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise AgentIPCError("Unsupported JSON-RPC version")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise AgentIPCError("JSON-RPC request requires a non-empty method")
    request_id = message.get("id")
    if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
        raise AgentIPCError("JSON-RPC id must be a string or integer")
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise AgentIPCError("JSON-RPC params must be an object")
    return method, request_id, params


class AgentControlServer:
    """Accept one worker connection and dispatch bounded JSON-RPC requests."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        handlers: dict[str, RpcHandler],
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.socket_path = Path(socket_path).expanduser().absolute()
        self.handlers = dict(handlers)
        self.max_message_bytes = max(1024, int(max_message_bytes))
        self.request_timeout_seconds = max(0.1, float(request_timeout_seconds))
        self._server: asyncio.AbstractServer | None = None
        self._connected = asyncio.Event()
        self._connection_writer: asyncio.StreamWriter | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def worker_connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        """Create the private socket listener; refuse to unlink active sockets."""
        if self._server is not None:
            return
        self._closing = False
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = self.socket_path.parent.stat()
        if parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise AgentIPCError(f"Socket directory must be owned by this user and mode 0700: {self.socket_path.parent}")

        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise AgentIPCError(f"Refusing to replace non-socket path: {self.socket_path}")
            raise AgentIPCError(
                f"Socket path already exists; refusing to unlink an endpoint not created by this server: {self.socket_path}"
            )

        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._client_connected,
                path=os.fspath(self.socket_path),
                limit=self.max_message_bytes + 1,
            )
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, 0o600)

    async def _client_connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._connected.is_set() or self._closing:
            writer.close()
            await writer.wait_closed()
            return
        self._connected.set()
        self._connection_writer = writer
        self._connection_task = asyncio.current_task()
        try:
            await self._serve_peer(reader, writer)
        except (AgentIPCError, asyncio.TimeoutError, ConnectionError):
            # The caller observes disconnection through worker_connected and
            # process supervision; do not leak arbitrary request data to logs.
            pass
        finally:
            self._connected.clear()
            self._connection_writer = None
            self._connection_task = None
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _serve_peer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while not self._closing:
            request = await _read_line(reader, self.max_message_bytes)
            try:
                method, request_id, params = _validate_request(request)
            except AgentIPCError as err:
                if "id" in request:
                    await self._send_error(writer, request.get("id"), -32600, str(err))
                    continue
                raise

            if request_id is None:
                # Notifications are deliberately ignored in v1: state changes
                # must be acknowledged before the worker relies on them.
                continue
            handler = self.handlers.get(method)
            if handler is None:
                await self._send_error(writer, request_id, -32601, "Method not found")
                continue
            try:
                result = handler(params)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=self.request_timeout_seconds)
                response = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}
                writer.write(_encode_line(response, self.max_message_bytes))
                await writer.drain()
            except asyncio.TimeoutError:
                await self._send_error(writer, request_id, -32001, "Request timed out")
            except Exception as err:
                await self._send_error(writer, request_id, -32000, str(err))

    async def _send_error(self, writer: asyncio.StreamWriter, request_id: Any, code: int, message: str) -> None:
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        writer.write(_encode_line(response, self.max_message_bytes))
        await writer.drain()

    async def wait_for_worker(self, timeout_seconds: float | None = None) -> None:
        """Wait until the worker connects, optionally timing out."""
        waiter = self._connected.wait()
        if timeout_seconds is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout=max(0.0, timeout_seconds))

    async def close_worker(self) -> None:
        """Close the active worker connection, if any."""
        writer = self._connection_writer
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def stop(self) -> None:
        self._closing = True
        await self.close_worker()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            if stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass


class AgentWorkerClient:
    """Sequential JSON-RPC client for the sandboxed worker."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        *,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.socket_path = Path(socket_path).expanduser().absolute()
        self.max_message_bytes = max(1024, int(max_message_bytes))
        self.request_timeout_seconds = max(0.1, float(request_timeout_seconds))
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, *, timeout_seconds: float = 10) -> None:
        if self.connected:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    os.fspath(self.socket_path),
                    limit=self.max_message_bytes + 1,
                ),
                timeout=max(0.1, timeout_seconds),
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise AgentIPCError(f"Could not connect to controller socket: {err}") from err

    async def request(self, method: str, params: JsonObject | None = None) -> Any:
        if not self.connected or self._reader is None or self._writer is None:
            raise AgentIPCError("Worker is not connected")
        if not isinstance(method, str) or not method:
            raise AgentIPCError("Method must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise AgentIPCError("Params must be an object")

        async with self._lock:
            self._request_id += 1
            request_id = self._request_id
            request = {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "method": method,
                "params": params or {},
            }
            self._writer.write(_encode_line(request, self.max_message_bytes))
            try:
                await asyncio.wait_for(self._writer.drain(), timeout=self.request_timeout_seconds)
                response = await asyncio.wait_for(
                    _read_line(self._reader, self.max_message_bytes),
                    timeout=self.request_timeout_seconds,
                )
            except asyncio.TimeoutError as err:
                await self.close()
                raise AgentIPCError(f"Request timed out: {method}; connection closed to avoid response desynchronization") from err
            if response.get("jsonrpc") != JSONRPC_VERSION or response.get("id") != request_id:
                raise AgentIPCError("Mismatched JSON-RPC response")
            error = response.get("error")
            if error is not None:
                if not isinstance(error, dict):
                    raise AgentIPCError("Malformed JSON-RPC error response")
                raise JsonRpcRemoteError(
                    int(error.get("code", -32000)),
                    str(error.get("message", "Remote error")),
                    error.get("data"),
                )
            if "result" not in response:
                raise AgentIPCError("JSON-RPC response has neither result nor error")
            return response["result"]

    async def close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
