"""Persistent controller service and fixed SSH stdio proxy."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import inspect
import json
import logging
import mailbox
import os
import secrets
import socket
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
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


def _runtime_worker_command(manifest: RuntimeManifest) -> tuple[str, ...]:
    """Return the Python command at the selected runtime's sandbox target."""
    system_targets = {Path("/usr"), Path("/lib"), Path("/lib64"), Path("/bin"), Path("/sbin")}
    candidates = [
        mount for mount in manifest.layout.mounts
        if Path(mount.target) not in system_targets
    ]
    for executable in (Path("bin/python"), Path("bin/python3")):
        for mount in candidates:
            source = Path(mount.source)
            if source.is_dir() and (source / executable).is_file():
                return (str(Path(mount.target) / executable), "-m", "agent_worker")
    raise SandboxError(
        f"Runtime {manifest.version} has no mounted bin/python or bin/python3 executable"
    )


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
            model_callback=self._get_llm().model_callback,
            patch_submit_callback=self._submit_patch,
            runtime_layout=self.runtime_manifest.layout,
            runtime_version=self.runtime_manifest.version,
            worker_command=_runtime_worker_command(self.runtime_manifest),
            max_model_calls=int(os.environ.get("TGIT_AGENT_MAX_MODEL_CALLS", "200")),
            max_runtime_seconds=int(os.environ.get("TGIT_AGENT_MAX_RUNTIME_SECONDS", "43200")),
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
                "acknowledge_patch_applied": self._acknowledge_patch_applied,
                "patches": self._patches,
                "list_agents": self._list_agents,
                "set_tools": self._set_tools,
                "agent_tools": self._agent_tools,
                "summarize_history": self._summarize_history,
            },
        )
        self.socket_path = Path(str(config.state_dir)) / "remote-agent.sock"
        self.server: asyncio.AbstractServer | None = None
        self._service_lock_fd: int | None = None

    def _acquire_service_lock_and_clear_stale_socket(self) -> None:
        """Lock service ownership and remove only a stale Unix socket."""
        lock_path = self.socket_path.with_suffix(".lock")
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Service lock must be a regular file owned by this user")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                raise RuntimeError("Remote agent service is already running") from err

            try:
                socket_info = self.socket_path.lstat()
            except FileNotFoundError:
                socket_info = None
            if socket_info is not None:
                if not stat.S_ISSOCK(socket_info.st_mode):
                    raise RuntimeError(f"Refusing to replace non-socket service path: {self.socket_path}")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(os.fspath(self.socket_path))
                    except (ConnectionRefusedError, FileNotFoundError):
                        self.socket_path.unlink()
                    else:
                        raise RuntimeError(f"Service socket is active: {self.socket_path}")
            self._service_lock_fd = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(os.environ.get("TGIT_AGENT_LLM_CONFIG", "config.toml"))
        return self._llm

    async def _submit_patch(self, snapshot, params: dict[str, Any]) -> dict[str, Any]:
        # Local review state and Maildir do not exist on the remote service.
        # Generate an mbox artifact directly; local import creates its series.
        exported = await asyncio.to_thread(
            subprocess.run,
            ("git", "-C", str(snapshot.project_root), "format-patch", "--stdout", "--thread=shallow", "--numbered", "--cover-letter", f"{snapshot.base_commit}..{params['head_commit']}"),
            check=True, capture_output=True, timeout=60,
        )
        patch_bytes = exported.stdout
        if not patch_bytes.strip():
            raise RuntimeError("Patch export produced an empty mbox")
        from mcp_tgit_review import _apply_reply_to
        reply_to = os.environ.get("TGIT_REVIEW_REPLY_TO", "agent <agent@localhost>").strip()
        try:
            patch_bytes = _apply_reply_to(patch_bytes, reply_to)
        except (ValueError, OSError, mailbox.Error) as err:
            raise RuntimeError(f"Could not set patch Reply-To headers: {err}") from err
        result = {"status": "ok", "base": snapshot.base_commit, "head": params["head_commit"]}
        if len(patch_bytes) > 16 * 1024 * 1024:
            raise RuntimeError("Patch artifact exceeds 16 MiB limit")
        artifact_name = f"{snapshot.root.name}.patch"
        artifact_path = Path(str(self.config.patches_dir)) / artifact_name
        temporary_path = artifact_path.with_suffix(".patch.tmp")
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(patch_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, artifact_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return {**result, "patch_artifact": artifact_name, "patch_bytes": len(patch_bytes), "patch_sha256": __import__("hashlib").sha256(patch_bytes).hexdigest()}

    async def _start_agent(self, checkout: Path, request: dict[str, object]) -> dict[str, Any]:
        return await self.controller.start_agent(request, source_root=checkout)

    async def _status(self, request):
        return await self.controller.get_status(request["agent_id"])

    async def _history(self, request):
        history = await self.controller.get_history(request["agent_id"])
        return {"agent_id": request["agent_id"], "messages": history[-request["limit"]:]}

    async def _summarize_history(self, request):
        history = await self.controller.get_history(request["agent_id"])
        encoded = json.dumps(history[-200:], ensure_ascii=False)[-100_000:]
        response = await self._get_llm().model_callback({"messages": [
            {"role": "system", "content": "Summarize this coding agent conversation for an operator: task, key actions and decisions, outcome, unresolved issues, and evidence. Be concise and distinguish facts from uncertainty."},
            {"role": "user", "content": encoded},
        ], "max_tokens": 1200})
        try:
            content = response["choices"][0]["message"]["content"]
            if not isinstance(content, str): raise TypeError("content is not text")
            return {"summary": content}
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM returned an invalid conversation summary response") from exc

    async def _continue(self, request):
        messages = [dict(message) for message in request["messages"]]
        return await self.controller.continue_agent(request["agent_id"], messages)

    async def _stop(self, request):
        return await self.controller.stop_agent(request["agent_id"])

    async def _logs(self, request):
        return await self.controller.get_agent_log(request["agent_id"], max_bytes=request["max_bytes"])

    async def _reap(self, request):
        return await self.controller.reap_agent(request["agent_id"])

    async def _acknowledge_patch_applied(self, request):
        return await self.controller.acknowledge_submitted_patch_applied(request["agent_id"])

    async def _agent_tools(self, request):
        return await self.controller.get_agent_tools(request["agent_id"])

    async def _set_tools(self, request):
        """Apply an explicit operator request to the controller allowlist."""
        return await self.controller.set_agent_tools(request["agent_id"], request["tools"])

    async def _list_agents(self, request):
        """Return metadata for all registered remote agents."""
        return {"agents": await self.controller.list_agents()}

    async def _patches(self, request):
        agent_id = request["agent_id"]
        metadata = self.controller.store.get_metadata(agent_id)
        artifact_name = f"{agent_id}.patch"
        artifact_path = Path(str(self.config.patches_dir)) / artifact_name
        if artifact_path.is_symlink() or not artifact_path.is_file():
            return {"agent_id": agent_id, "available": False}
        return {
            "agent_id": agent_id,
            "available": True,
            "artifact": artifact_name,
            "bytes": artifact_path.stat().st_size,
            "submitted_head": metadata.get("submitted_head"),
            "base_commit": metadata.get("base_commit"),
            "sha256": __import__("hashlib").sha256(artifact_path.read_bytes()).hexdigest(),
        }

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
            logging.getLogger(__name__).exception(
                "Remote operation failed (operation=%r, request_id=%r)",
                message.get("operation") if isinstance(message, dict) else None,
                request_id,
            )
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
        self._acquire_service_lock_and_clear_stale_socket()
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
            if self._service_lock_fd is not None:
                os.close(self._service_lock_fd)
                self._service_lock_fd = None


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
        diagnostic = (
            err if isinstance(err, RemoteProtocolError)
            else RemoteProtocolError(f"Remote service socket request failed: {type(err).__name__}: {err}")
        )
        sys.stdout.buffer.write(_json_line(error_response(None, diagnostic)))
    sys.stdout.buffer.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serve", action="store_true", help="run the persistent service")
    group.add_argument("--stdio-v1", action="store_true", help="proxy one SSH request")
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument("--config", type=Path, help="deployment TOML path")
    config_group.add_argument(
        "--base-dir",
        help="managed service directory (for rendered service units and fixed SSH proxy calls)",
    )
    arguments = parser.parse_args(argv)
    config = (
        RemoteAgentConfig("localhost", PurePosixPath(arguments.base_dir))
        if arguments.base_dir is not None
        else load_remote_agent_config(arguments.config)
    )
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
