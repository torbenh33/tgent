"""Persistent controller service and fixed SSH stdio proxy."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
from datetime import datetime, timezone
import inspect
import json
import logging
import os
import secrets
import re
import warnings
import socket
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from agent_controller import AgentController
from agent_sandbox import RuntimeManifest, SandboxError, RuntimeMount, SandboxRuntimeLayout
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
    """Describe the single deployed app as both service and worker runtime."""
    from agent_sandbox import read_runtime_manifest

    runtime_root = Path(str(config.runtime_dir))
    versions = runtime_root / "versions"
    candidates: list[RuntimeManifest] = []
    if versions.is_dir():
        for manifest_path in versions.glob("*/manifest.json"):
            try:
                candidates.append(read_runtime_manifest(manifest_path))
            except (OSError, ValueError, SandboxError):
                continue
    if candidates:
        return max(candidates, key=lambda manifest: manifest.rsync_completed_at)

    app = Path(str(config.remote_base_dir / "app"))
    python = app / "bin" / "python"
    if not python.is_file() or not (app / "pyvenv.cfg").is_file():
        raise SandboxError(f"No valid deployed app environment at {app}")
    return RuntimeManifest(
        version="app-current",
        rsync_completed_at=datetime.now(timezone.utc),
        layout=SandboxRuntimeLayout((RuntimeMount(app, app),)),
    )

def _runtime_worker_command(manifest: RuntimeManifest) -> tuple[str, ...]:
    """Use the sandbox path corresponding to the mounted Python environment."""
    for mount in manifest.layout.mounts:
        source = Path(str(mount.source))
        if not (source / "pyvenv.cfg").is_file():
            continue
        for executable in ("python", "python3"):
            if (source / "bin" / executable).is_file():
                target = Path(str(mount.target)) / "bin" / executable
                return (str(target), "-m", "agent_worker")
    raise SandboxError(f"Runtime {manifest.version} has no mounted Python virtual environment")


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
            config.project_root_dir,
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
            patch_submit_callback=self._submit_patch_from_controller,
            runtime_layout=self.runtime_manifest.layout,
            runtime_version=self.runtime_manifest.version,
            runtime_app_path=str(config.remote_base_dir / "app"),
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
                "ensure_project": self._ensure_project,
                "stop": self._stop,
                "stop_agents": self._stop_agents,
                "restart_agents": self._restart_agents,
                "logs": self._logs,
                "reap": self._reap,
                "reap_agents": self._reap_agents,
                "acknowledge_patch_applied": self._acknowledge_patch_applied,
                "nack_patch": self._nack_patch,
                "patches": self._patches,
                "submit_patch": self._submit_patch_protocol,
                "pending_patches": self._pending_patches,
                "list_agents": self._list_agents,
                "set_tools": self._set_tools,
                "agent_tools": self._agent_tools,
                "summarize_history": self._summarize_history,
                "review_replies": self._review_replies,
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

    async def _submit_patch_from_controller(self, snapshot, params: dict[str, Any]) -> dict[str, Any]:
        """Use the authoritative controller registration to resolve the work repo."""
        agent_id = params["agent_id"]
        metadata = self.controller.store.get_metadata(agent_id)
        if Path(metadata["snapshot_project"]).resolve() != Path(snapshot.project_root).resolve():
            raise RuntimeError("Submission snapshot does not match registered agent work repository")
        request = {**params, "agent_id": agent_id}
        return await self._submit_patch_request(request)

    async def _submit_patch_protocol(self, request: dict[str, Any]) -> dict[str, Any]:
        agent_id = request["agent_id"]
        metadata = self.controller.store.get_metadata(agent_id)
        head = request["head_commit"]
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
            raise RuntimeError("head_commit must be a full object id")
        if metadata.get("base_commit") != request["base_commit"]:
            raise RuntimeError("base_commit does not match registered agent")
        snapshot_path = Path(metadata["snapshot_project"])
        snapshot_root = Path(metadata["snapshot_root"])
        if snapshot_path.is_symlink() or snapshot_root.is_symlink() or snapshot_path.parent != snapshot_root:
            raise RuntimeError("Unsafe service-managed agent work repository path")
        snapshot = type("SubmissionSnapshot", (), {"root": snapshot_root, "project_root": snapshot_path, "base_commit": metadata["base_commit"]})()
        # Do not permit the caller to supply source or destination: both derive
        # solely from the registered agent and service project configuration.
        request = {**request, "_project_mirror": metadata.get("project_mirror")}
        return await self._submit_patch(snapshot, request)

    async def _submit_patch_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Resolve submission source from controller-managed agent metadata."""
        agent_id = request["agent_id"]
        metadata = self.controller.store.get_metadata(agent_id)
        snapshot_path = Path(metadata["snapshot_project"])
        snapshot_root = Path(metadata["snapshot_root"])
        if snapshot_path.is_symlink() or snapshot_root.is_symlink() or snapshot_path.parent != snapshot_root:
            raise RuntimeError("Unsafe service-managed agent work repository path")
        snapshot = type("SubmissionSnapshot", (), {
            "root": snapshot_root, "project_root": snapshot_path,
            "base_commit": metadata["base_commit"],
        })()
        request = {**request, "_project_mirror": metadata.get("project_mirror")}
        return await self._submit_patch(snapshot, request)

    async def _submit_patch(self, snapshot, params: dict[str, Any]) -> dict[str, Any]:
        """Publish a submitted commit into the project bare mirror."""
        return publish_submission({**params, "_work_repository": str(snapshot.project_root)}, state_dir=Path(str(self.config.state_dir)), project_root_dir=Path(str(self.config.project_root_dir)))




    def _repository(self, name):
        if not isinstance(name, str) or not name.startswith("_") or name in {"_", "_.."} or "/" in name or "\x00" in name:
            raise RemoteProtocolError("Invalid project mirror name")
        repository = Path(str(self.config.project_root_dir)) / name
        if repository.is_symlink():
            raise RemoteProtocolError("Project mirror must not be a symlink")
        return repository

    async def _ensure_project(self, request):
        name, source = request["project_name"], request["source_path"]
        repository = self._repository(name)
        if not isinstance(source, str) or not source.startswith("/") or source.replace("/", "_") != name:
            raise RemoteProtocolError("Invalid project source path")
        subprocess.run(["git", "init", "--bare", str(repository)], check=True, capture_output=True)
        marker = repository / "agent-source-path"
        collision = False
        try:
            with marker.open("x", encoding="utf-8") as stream:
                stream.write(source)
        except FileExistsError:
            if marker.read_text(encoding="utf-8") != source:
                collision = True
                warnings.warn(f"Project mirror name collision: {name}", RuntimeWarning)
        return {"status": "ready", "collision": collision}

    async def _start_agent(self, checkout: Path, request: dict[str, object]) -> dict[str, Any]:
        request = dict(request)
        name, base = request.pop("_project_name", None), request.pop("_base_commit", None)
        repository = self._repository(name)
        if not isinstance(base, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base) is None:
            raise RemoteProtocolError("Invalid project mirror base commit")
        work = repository.parent / (name + "-work-" + secrets.token_hex(8))
        subprocess.run(["git", "clone", "--no-local", "--no-checkout", "--reference", str(repository), str(repository), str(work)], check=True, capture_output=True)
        # Fetch refs under a temporary namespace first: writing directly to
        # refs/* can fail when that includes the currently checked-out branch.
        subprocess.run(["git", "-C", str(work), "fetch", str(repository), "+refs/*:refs/agent-mirror/*"], check=True, capture_output=True)
        mirrored_refs = subprocess.run(["git", "-C", str(work), "for-each-ref", "--format=%(refname) %(objectname)", "refs/agent-mirror/"], check=True, capture_output=True, text=True).stdout
        for line in mirrored_refs.splitlines():
            temporary_ref, object_id = line.split()
            original_ref = "refs/" + temporary_ref.removeprefix("refs/agent-mirror/")
            subprocess.run(["git", "-C", str(work), "update-ref", original_ref, object_id], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(work), "update-ref", "-d", temporary_ref], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work), "checkout", "--detach", base], check=True, capture_output=True)
        subprocess.run(["rsync", "-a", "--checksum", "--exclude", ".git", "--delete", str(checkout) + "/", str(work) + "/"], check=True, capture_output=True)
        return await self.controller.start_agent(request, source_root=work, _base_commit=base, _mirror_path=repository, _project_name=name)

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

    async def _review_replies(self, request):
        from mcp_tgit_review import get_review_replies

        return get_review_replies(
            repo_path=request["repo_path"],
            unacknowledged_only=request["unacknowledged_only"],
        )

    async def _continue(self, request):
        agent_id = request["agent_id"]
        messages = [dict(message) for message in request["messages"]]
        return await self.controller.continue_agent(agent_id, messages)

    async def _stop(self, request):
        return await self.controller.stop_agent(request["agent_id"])

    async def _logs(self, request):
        return await self.controller.get_agent_log(request["agent_id"], max_bytes=request["max_bytes"])

    async def _stop_agents(self, request):
        return await self.controller.stop_agents()

    async def _restart_agents(self, request):
        return await self.controller.restart_agents(request["agent_ids"])

    async def _reap(self, request):
        result = await self.controller.reap_agents([request["agent_id"]])
        return result["results"][0]

    async def _reap_agents(self, request):
        return await self.controller.reap_agents(request["agent_ids"])

    async def _acknowledge_patch_applied(self, request):
        return await self.controller.acknowledge_submitted_patch_applied(request["agent_id"])

    async def _nack_patch(self, request):
        return await self.controller.reject_submitted_patch(request["agent_id"], request["reason"] )

    async def _agent_tools(self, request):
        return await self.controller.get_agent_tools(request["agent_id"])

    async def _set_tools(self, request):
        """Apply an explicit operator request to the controller allowlist."""
        return await self.controller.set_agent_tools(request["agent_id"], request["tools"])

    async def _list_agents(self, request):
        """Return metadata for all registered remote agents."""
        return {"agents": await self.controller.list_agents()}

    async def _pending_patches(self, request):
        """Enumerate submitted patch metadata in one protocol response."""
        agents = await self.controller.list_agents()
        patches = []
        for agent in agents:
            agent_id = agent.get("agent_id") or agent.get("id")
            if not isinstance(agent_id, str):
                continue
            metadata = self.controller.store.get_metadata(agent_id)
            if not metadata.get("submitted_head"):
                continue
            path = Path(str(self.config.patches_dir)) / f"{agent_id}.patch"
            if path.is_symlink() or not path.is_file():
                continue
            import hashlib
            size = path.stat().st_size
            if size > 16 * 1024 * 1024:
                continue
            patches.append({"agent_id": agent_id, "artifact": f"{agent_id}.patch", "bytes": size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "submitted_head": metadata.get("submitted_head"), "base_commit": metadata.get("base_commit"), "state": agent.get("state")})
        return {"patches": patches}

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



def publish_submission(params: dict[str, Any], *, state_dir: Path, project_root_dir: Path) -> dict[str, Any]:
    """Fetch a registered commit to its immutable managed submission ref."""
    agent_id = params["agent_id"]
    if params.get("head_commit") == params.get("base_commit"):
        raise RuntimeError(
            "There are no committed changes to submit. Make and commit the intended changes first; "
            "committing is expected and safe in the private agent repository and does not modify the operator checkout."
        )
    identity = re.sub(r"[^A-Za-z0-9_-]", "-", agent_id)
    ref = f"refs/agent-submissions/{identity}"
    mirror_value = params.get("_project_mirror")
    if not isinstance(mirror_value, str):
        raise RuntimeError("Agent has no registered project mirror")
    mirror = Path(mirror_value)
    if mirror.parent != project_root_dir:
        raise RuntimeError("Registered project mirror is outside the managed project directory")
    metadata_dir = state_dir / "submissions"
    _managed_directory(metadata_dir)
    metadata_path = metadata_dir / f"{identity}.json"
    metadata = {"agent_id": agent_id, "submission_id": agent_id, "commit_id": params["head_commit"],
                "base": params["base_commit"], "cover_letter_message": params["cover_letter_message"], "ref": ref}
    if metadata_path.exists():
        previous = json.loads(metadata_path.read_text(encoding="utf-8"))
        if previous != metadata:
            raise RuntimeError("Conflicting metadata for immutable submission ref")
    source = Path(params["_work_repository"])
    if source.is_symlink() or not source.is_dir() or not mirror.is_dir() or mirror.is_symlink():
        raise RuntimeError("Unsafe agent repository or project mirror")
    try:
        existing = subprocess.run(("git", "--git-dir", str(mirror), "rev-parse", "--verify", ref),
                                  capture_output=True, text=True)
        if existing.returncode == 0:
            existing_oid = existing.stdout.strip()
            wanted = params["head_commit"].lower()
            if existing_oid.lower() != wanted:
                raise RuntimeError("Submission ref is already occupied by a different commit")
        # Always fetch, including identical retries. Fetch updates the
        # internally generated ref only after successful object transfer.
        subprocess.run(
            ["git", "--git-dir", str(mirror), "fetch", "--no-tags", str(source),
             f"{params['head_commit']}:{ref}"], check=True, capture_output=True, timeout=60)
        temporary = metadata_path.with_name(f".{metadata_path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, metadata_path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"status": "ok", **metadata}
    except BaseException:
        raise


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
