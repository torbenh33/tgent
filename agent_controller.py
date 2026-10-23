"""Trusted single-project agent lifecycle coordinator.

This module wires together durable state, reflink snapshots, Bubblewrap command
construction, and the controller/worker socket. It deliberately accepts model
and patch-submission callbacks rather than embedding project-specific policy.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import inspect
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_ipc import AgentControlServer, AgentIPCError
from agent_project import ProjectConfig, discover_project_root, load_project_config
from agent_sandbox import (
    SandboxCommand,
    SandboxPolicy,
    SandboxRuntimeLayout,
    build_runtime_layout,
    build_sandbox_command,
)
from agent_snapshot import ProjectSnapshot, SnapshotError, create_project_snapshot, remove_project_snapshot
from agent_store import AgentStore, AgentStoreError, validate_agent_id

MAX_HISTORY_MESSAGES = 20_000
MAX_HISTORY_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_MODEL_REQUEST_BYTES = 8 * 1024 * 1024
_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")

_ALLOWED_STATES = {
    "starting",
    "running",
    "halted",
    "awaiting_review",
    "review_received",
    "failed",
    "stopping",
    "stopped",
    "reaped",
}

ModelCallback = Callable[[dict[str, Any]], Any | Awaitable[Any]]
PatchSubmitCallback = Callable[[ProjectSnapshot, dict[str, Any]], Any | Awaitable[Any]]


def _configure_snapshot_git_identity(source_root: Path, snapshot_root: Path) -> None:
    """Copy only the controller project's effective commit identity locally."""
    identity: dict[str, str] = {}
    for key in ("user.name", "user.email"):
        try:
            result = subprocess.run(
                ["git", "-C", str(source_root), "config", "--get", key],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise AgentControllerError(f"Could not read Git {key} from controller project: {err}") from err
        if result.returncode != 0 or not result.stdout.strip():
            raise AgentControllerError(
                f"Git {key} is not configured for the controller project; configure it before starting agents"
            )
        identity[key] = result.stdout.rstrip("\n")

    for key, value in identity.items():
        try:
            result = subprocess.run(
                ["git", "-C", str(snapshot_root), "config", "--local", key, value],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise AgentControllerError(f"Could not set snapshot Git {key}: {err}") from err
        if result.returncode != 0:
            detail = result.stderr.strip() or f"git config failed with exit code {result.returncode}"
            raise AgentControllerError(f"Could not set snapshot Git {key}: {detail}")


class AgentControllerError(RuntimeError):
    """Raised for invalid controller requests or lifecycle operations."""


@dataclass
class _AgentRuntime:
    agent_id: str
    snapshot: ProjectSnapshot
    ipc: AgentControlServer
    process: asyncio.subprocess.Process
    monitor: asyncio.Task[None]
    lock: asyncio.Lock
    commands: asyncio.Queue[dict[str, Any]]
    model_calls: int = 0
    stop_requested: bool = False
    runtime_timer: asyncio.Task[None] | None = None


class AgentController:
    """Manage workers for one Git project root."""

    def __init__(
        self,
        *,
        project_root: str | os.PathLike[str] | None,
        state_root: str | os.PathLike[str],
        snapshot_root: str | os.PathLike[str],
        model_callback: ModelCallback,
        patch_submit_callback: PatchSubmitCallback | None = None,
        sandbox_policy: SandboxPolicy = SandboxPolicy(),
        worker_command: tuple[str, ...] | None = None,
        runtime_layout: SandboxRuntimeLayout | None = None,
        runtime_version: str | None = None,
        bwrap: str = "bwrap",
        max_model_calls: int = 200,
        max_runtime_seconds: int = 43200,
    ) -> None:
        self.project_root = None if project_root is None else discover_project_root(project_root)
        self.project_config: ProjectConfig | None = (
            None if self.project_root is None else load_project_config(self.project_root)
        )
        self.store = AgentStore(state_root)
        state_path = self.store.state_root
        state_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if state_path.is_symlink() or not state_path.is_dir() or state_path.stat().st_uid != os.getuid():
            raise AgentControllerError("Agent state root must be a real directory owned by the controller user")
        os.chmod(state_path, 0o700)
        self.log_root = state_path / "logs"
        self.log_root.mkdir(mode=0o700, exist_ok=True)
        if self.log_root.is_symlink() or not self.log_root.is_dir() or self.log_root.stat().st_uid != os.getuid():
            raise AgentControllerError("Agent log directory must be a real directory owned by the controller user")
        os.chmod(self.log_root, 0o700)
        self.snapshot_root = Path(snapshot_root).expanduser().absolute()
        if self.project_root is not None and (
            self.snapshot_root == self.project_root or self.snapshot_root.is_relative_to(self.project_root)
        ):
            raise AgentControllerError("Snapshot root must be outside the source project")
        self.snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.snapshot_root.is_symlink() or not self.snapshot_root.is_dir():
            raise AgentControllerError("Snapshot root must be a real directory")
        if self.snapshot_root.stat().st_uid != os.getuid():
            raise AgentControllerError("Snapshot root must be owned by the controller user")
        os.chmod(self.snapshot_root, 0o700)
        self.model_callback = model_callback
        self.patch_submit_callback = patch_submit_callback
        self.sandbox_policy = sandbox_policy
        self.worker_command = worker_command or (sys.executable, "-m", "agent_worker")
        self.runtime_version = runtime_version
        self.runtime_layout = runtime_layout or build_runtime_layout(
            runtime_dirs=(Path(sys.prefix),),
            runtime_files=(Path(__file__).with_name("agent_worker.py"), Path(__file__).with_name("agent_ipc.py")),
        )
        self.bwrap = bwrap
        self.max_model_calls = max(1, int(max_model_calls))
        self.max_runtime_seconds = max(1, int(max_runtime_seconds))
        self._runtimes: dict[str, _AgentRuntime] = {}
        # Dynamic project MCP tools are opt-in and controlled only by this controller.
        self._tool_allowlists: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _validate_start_request(request: Any) -> tuple[list[dict[str, Any]], int, int]:
        if not isinstance(request, dict):
            raise AgentControllerError("start request must be an object")
        allowed = {"schema_version", "task", "history", "limits"}
        unknown = set(request) - allowed
        if unknown:
            raise AgentControllerError(f"Unknown start request field(s): {', '.join(sorted(unknown))}")
        if type(request.get("schema_version")) is not int or request["schema_version"] != 1:
            raise AgentControllerError("schema_version must be 1")

        task = request.get("task")
        if task is not None and (not isinstance(task, str) or not task.strip()):
            raise AgentControllerError("task must be a non-empty string when provided")
        history = request.get("history", [])
        if not isinstance(history, list) or any(not isinstance(message, dict) for message in history):
            raise AgentControllerError("history must be an array of message objects")
        if task is None and not history:
            raise AgentControllerError("Provide task, history, or both")
        if task is not None:
            history = [*history, {"role": "user", "content": task, "id": secrets.token_hex(16)}]
        if len(history) > MAX_HISTORY_MESSAGES:
            raise AgentControllerError("Initial history contains too many messages")
        if len(json.dumps(history, ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_MESSAGE_BYTES:
            raise AgentControllerError("Initial history exceeds size limit")

        limits = request.get("limits", {})
        if not isinstance(limits, dict) or set(limits) - {"max_model_calls", "max_runtime_seconds"}:
            raise AgentControllerError("limits must contain only max_model_calls and max_runtime_seconds")
        calls = limits.get("max_model_calls", 200)
        runtime = limits.get("max_runtime_seconds", 43200)
        if type(calls) is not int or calls < 1 or type(runtime) is not int or runtime < 1:
            raise AgentControllerError("requested limits must be positive integers")
        return history, calls, runtime

    async def start_agent(
        self,
        request: dict[str, Any],
        *,
        source_root: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Start an agent from the controller project or a trusted uploaded checkout.

        ``source_root`` is intended for the remote upload manager, which passes
        only its privately managed, finalized upload path. It is not an
        externally supplied worker path.
        """
        history, requested_calls, requested_runtime = self._validate_start_request(request)
        if source_root is None:
            if self.project_root is None:
                raise AgentControllerError("A project source is required for this controller")
            selected_project_root = self.project_root
        else:
            selected_project_root = discover_project_root(source_root)
        selected_project_config = (
            self.project_config
            if selected_project_root == self.project_root
            else load_project_config(selected_project_root)
        )
        agent_id = secrets.token_hex(16)
        destination = self.snapshot_root / agent_id
        state = "starting"
        snapshot: ProjectSnapshot | None = None
        ipc: AgentControlServer | None = None
        process: asyncio.subprocess.Process | None = None
        worker_log = None
        created = False
        try:
            snapshot = create_project_snapshot(selected_project_root, destination)
            _configure_snapshot_git_identity(selected_project_root, snapshot.project_root)
            socket_dir = Path(self.store.state_root) / "sockets"
            socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(socket_dir, 0o700)
            socket_path = socket_dir / f"{agent_id}.sock"

            metadata = {
                "schema_version": 1,
                "agent_id": agent_id,
                "project_root": str(selected_project_root),
                "state": state,
                "created_at": time.time(),
                "updated_at": time.time(),
                "base_commit": snapshot.base_commit,
                "runtime_version": self.runtime_version,
                "snapshot_root": str(snapshot.root),
                "snapshot_project": str(snapshot.project_root),
                "linked_worktree": snapshot.linked_worktree,
                "worker_pid": None,
                "stop_reason": None,
                "model_calls": 0,
                "max_model_calls": min(requested_calls, self.max_model_calls),
                "max_runtime_seconds": min(requested_runtime, self.max_runtime_seconds),
                "socket_path": str(socket_path),
                "worker_log": str(self.log_root / f"{agent_id}.log"),
                "parent_agent_id": None,
            }
            self.store.create(agent_id, metadata, history)
            created = True

            handlers = {
                "worker.hello": self._handler(agent_id, self._hello),
                "history.append": self._handler(agent_id, self._append_history),
                "history.get": self._handler(agent_id, self._get_history),
                "model.complete": self._handler(agent_id, self._model_complete),
                "agent.update_state": self._handler(agent_id, self._update_state),
                "patch.submit": self._handler(agent_id, self._submit_patch),
                "agent.await_command": self._handler(agent_id, self._await_command),
                "agent.tools.get": self._handler(agent_id, self._get_tool_allowlist),
            }
            ipc = AgentControlServer(socket_path, handlers)
            await ipc.start()
            sandbox = build_sandbox_command(
                snapshot=snapshot,
                project_config=selected_project_config,
                policy=self.sandbox_policy,
                command=self.worker_command,
                bwrap=self.bwrap,
                controller_socket=socket_path,
                worker_env={
                    "AGENT_ID": agent_id,
                    "AGENT_BASE_COMMIT": snapshot.base_commit,
                    "PYTHONPATH": "/opt/agent-runtime",
                },
                runtime_layout=self.runtime_layout,
            )
            log_path = self.log_root / f"{agent_id}.log"
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            worker_log = os.fdopen(log_fd, "wb", buffering=0)
            process = await asyncio.create_subprocess_exec(
                *sandbox.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=worker_log,
                start_new_session=True,
            )
            worker_log.close()
            worker_log = None
            self.store.update_metadata(agent_id, {"worker_pid": process.pid, "state": "running"})
            runtime = _AgentRuntime(
                agent_id=agent_id,
                snapshot=snapshot,
                ipc=ipc,
                process=process,
                monitor=asyncio.create_task(asyncio.sleep(0)),
                lock=asyncio.Lock(),
                commands=asyncio.Queue(),
            )
            async with self._lock:
                self._runtimes[agent_id] = runtime
            runtime.monitor = asyncio.create_task(self._monitor_process(agent_id, process))
            runtime.runtime_timer = asyncio.create_task(
                self._enforce_runtime_limit(agent_id, metadata["max_runtime_seconds"])
            )
            return {"status": "ok", "agent_id": agent_id, "state": "running", "base_commit": snapshot.base_commit}
        except Exception as err:
            if worker_log is not None:
                worker_log.close()
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if ipc is not None:
                await ipc.stop()
            if snapshot is not None:
                try:
                    remove_project_snapshot(snapshot)
                except SnapshotError:
                    pass
            if created:
                try:
                    self.store.remove(agent_id)
                except AgentStoreError:
                    pass
            raise AgentControllerError(f"Could not start agent: {err}") from err

    def _handler(self, agent_id: str, callback: Callable[..., Any]) -> Callable[[dict[str, Any]], Awaitable[Any]]:
        async def dispatch(params: dict[str, Any]) -> Any:
            runtime = self._runtimes.get(agent_id)
            if runtime is None:
                # Startup handshake and early messages can arrive before the
                # runtime map is populated; yield briefly for registration.
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    runtime = self._runtimes.get(agent_id)
                    if runtime is not None:
                        break
            return await callback(agent_id, params)
        return dispatch

    async def _get_tool_allowlist(self, agent_id: str, _params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": sorted(self._tool_allowlists.get(agent_id, set()))}

    async def set_agent_tools(self, agent_id: str, tools: list[str]) -> dict[str, Any]:
        """Set opt-in MCP module names for a live agent (trusted operator API)."""
        if not isinstance(tools, list) or any(not isinstance(name, str) or re.fullmatch(r"mcp_[A-Za-z0-9_-]+", name) is None for name in tools):
            raise AgentControllerError("tools must be MCP module names such as mcp_example")
        if len(set(tools)) != len(tools):
            raise AgentControllerError("tools must not contain duplicates")
        metadata = self.store.get_metadata(agent_id)
        runtime = self._runtimes.get(agent_id)
        if metadata.get("state") not in {"running", "halted", "awaiting_review", "review_received"} or runtime is None or runtime.process.returncode is not None:
            raise AgentControllerError("Tools can only be changed for a live agent")
        available = {path.stem for path in Path(metadata["snapshot_project"]).glob("mcp_*.py")}
        if not set(tools) <= available:
            raise AgentControllerError("Requested MCP module is not available in this agent project")
        self._tool_allowlists[agent_id] = set(tools)
        return {"agent_id": agent_id, "tools": sorted(tools)}

    async def _hello(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("agent_id") != agent_id or params.get("protocol_version") != 1:
            raise AgentControllerError("Worker identity or protocol version mismatch")
        return {"protocol_version": 1, "agent_id": agent_id}

    async def _get_history(self, agent_id: str, _params: dict[str, Any]) -> dict[str, Any]:
        return {"history": self.store.get_history(agent_id)}

    async def _append_history(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        messages = params.get("messages")
        if not isinstance(messages, list) or len(messages) > MAX_HISTORY_MESSAGES:
            raise AgentControllerError("messages must be a bounded array")
        encoded = json.dumps(messages, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_HISTORY_MESSAGE_BYTES:
            raise AgentControllerError("history append exceeds size limit")
        runtime = self._runtimes.get(agent_id)
        lock = runtime.lock if runtime is not None else asyncio.Lock()
        async with lock:
            history = self.store.append_history(agent_id, messages)
        return {"appended": len(messages), "message_count": len(history)}

    async def _model_complete(self, agent_id: str, params: dict[str, Any]) -> Any:
        if not isinstance(params.get("messages"), list):
            raise AgentControllerError("model.complete requires a messages array")
        if "tools" in params and not isinstance(params["tools"], list):
            raise AgentControllerError("tools must be an array")
        if len(json.dumps(params, ensure_ascii=False).encode("utf-8")) > MAX_MODEL_REQUEST_BYTES:
            raise AgentControllerError("model request exceeds size limit")
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            raise AgentControllerError("Agent runtime is unavailable")
        async with runtime.lock:
            metadata = self.store.get_metadata(agent_id)
            if runtime.model_calls >= metadata["max_model_calls"]:
                raise AgentControllerError("Agent model-call budget exhausted")
            runtime.model_calls += 1
            request_bytes = len(json.dumps(params, ensure_ascii=False).encode("utf-8"))
            self.store.update_metadata(
                agent_id,
                {
                    "model_calls": runtime.model_calls,
                    "last_model_request_at": time.time(),
                    "last_model_request_bytes": request_bytes,
                },
            )
        result = self.model_callback(params)
        if inspect.isawaitable(result):
            result = await result
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        if not isinstance(result, dict):
            raise AgentControllerError("Model callback must return a JSON object")
        return result

    async def _update_state(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        new_state = params.get("state")
        if new_state not in {"running", "halted", "awaiting_review", "review_received"}:
            raise AgentControllerError("Invalid worker-reported state")
        metadata = self.store.get_metadata(agent_id)
        if metadata.get("state") in {"stopped", "failed", "reaped"}:
            raise AgentControllerError(f"Cannot change terminal agent state {metadata.get('state')}")
        updated = self.store.update_metadata(agent_id, {"state": new_state, "stop_reason": params.get("stop_reason")})
        return {"state": updated["state"]}

    async def _submit_patch(self, agent_id: str, params: dict[str, Any]) -> Any:
        if self.patch_submit_callback is None:
            raise AgentControllerError("Patch submission is not configured")
        snapshot = self._runtimes[agent_id].snapshot
        base = params.get("base_commit")
        head = params.get("head_commit")
        if base != snapshot.base_commit or not isinstance(head, str) or _COMMIT_RE.fullmatch(head) is None:
            raise AgentControllerError("Patch request must identify the recorded base commit and a full commit hash")
        resolved = await asyncio.create_subprocess_exec(
            "git", "-C", str(snapshot.project_root), "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        resolved_out, resolved_err = await resolved.communicate()
        if resolved.returncode != 0 or resolved_out.decode().strip().lower() != head.lower():
            raise AgentControllerError(f"Patch head does not resolve to a commit in the snapshot: {resolved_err.decode(errors='replace')}")
        check = await asyncio.create_subprocess_exec(
            "git", "-C", str(snapshot.project_root), "merge-base", "--is-ancestor", base, head,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await check.communicate()
        if check.returncode != 0:
            raise AgentControllerError(f"Patch head is not based on the recorded base commit: {stderr.decode(errors='replace')}")
        result = self.patch_submit_callback(snapshot, params)
        if inspect.isawaitable(result):
            result = await result
        self.store.update_metadata(agent_id, {"state": "awaiting_review", "submitted_head": head})
        return result

    async def _await_command(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        timeout = params.get("timeout_seconds", 3600)
        if type(timeout) not in {int, float} or timeout < 0 or timeout > 86400:
            raise AgentControllerError("timeout_seconds must be between 0 and 86400")
        runtime = self._runtimes[agent_id]
        try:
            return await asyncio.wait_for(runtime.commands.get(), timeout=float(timeout))
        except asyncio.TimeoutError:
            return {"command": "timeout"}

    async def _monitor_process(self, agent_id: str, process: asyncio.subprocess.Process) -> None:
        await process.wait()
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            return
        metadata = self.store.get_metadata(agent_id)
        if runtime.runtime_timer is not None:
            runtime.runtime_timer.cancel()
        if metadata.get("state") not in {"stopped", "failed", "reaped"}:
            self.store.update_metadata(
                agent_id,
                {
                    "state": "stopped" if runtime.stop_requested else ("failed" if process.returncode else "halted"),
                    "exit_code": process.returncode,
                },
            )
        await runtime.ipc.stop()

    async def _enforce_runtime_limit(self, agent_id: str, seconds: int) -> None:
        await asyncio.sleep(seconds)
        runtime = self._runtimes.get(agent_id)
        if runtime is not None and runtime.process.returncode is None:
            runtime.stop_requested = True
            self.store.update_metadata(agent_id, {"state": "stopping", "stop_reason": "runtime_limit"})
            runtime.process.terminate()
            try:
                await asyncio.wait_for(runtime.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                runtime.process.kill()
                await runtime.process.wait()

    async def list_agents(self) -> list[dict[str, Any]]:
        """Return metadata for every safely readable agent record."""
        agents = []
        for entry in self.store.state_root.iterdir():
            if entry.name == "logs" or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                agents.append(self.store.get_metadata(entry.name))
            except (AgentStoreError, OSError):
                continue
        agents.sort(key=lambda metadata: (metadata.get("created_at", 0), metadata.get("agent_id", "")))
        return agents

    async def get_status(self, agent_id: str) -> dict[str, Any]:
        return self.store.get_metadata(agent_id)

    async def get_agent_log(self, agent_id: str, *, max_bytes: int = 64 * 1024) -> dict[str, Any]:
        """Return the bounded tail of a worker's captured stderr log."""
        validate_agent_id(agent_id)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024:
            raise AgentControllerError("max_bytes must be between 1 and 1048576")
        self.store.get_metadata(agent_id)
        log_path = self.log_root / f"{agent_id}.log"
        if log_path.is_symlink() or not log_path.is_file():
            return {"agent_id": agent_id, "log": "", "truncated": False}
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            truncated = size > max_bytes
            stream.seek(max(0, size - max_bytes))
            content = stream.read(max_bytes).decode("utf-8", errors="replace")
        return {"agent_id": agent_id, "log": content, "truncated": truncated}

    async def get_history(self, agent_id: str) -> list[dict[str, Any]]:
        return self.store.get_history(agent_id)

    async def continue_agent(self, agent_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        validate = self.store.get_metadata(agent_id)
        if validate.get("state") not in {"halted", "awaiting_review", "review_received"}:
            raise AgentControllerError(f"Agent cannot continue from state {validate.get('state')}")
        if not messages or any(not isinstance(message, dict) for message in messages):
            raise AgentControllerError("continue requires one or more message objects")
        runtime = self._runtimes.get(agent_id)
        if runtime is None or runtime.process.returncode is not None:
            raise AgentControllerError("Worker process is not available to resume")
        self.store.append_history(agent_id, messages)
        command = {"command": "review_feedback", "messages": messages}
        await runtime.commands.put(command)
        self.store.update_metadata(agent_id, {"state": "review_received"})
        return {"agent_id": agent_id, "state": "review_received", "queued_messages": len(messages)}

    async def stop_agent(self, agent_id: str) -> dict[str, Any]:
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            metadata = self.store.get_metadata(agent_id)
            return {"agent_id": agent_id, "state": metadata.get("state"), "message": "No live worker process."}
        runtime.stop_requested = True
        self.store.update_metadata(agent_id, {"state": "stopping"})
        await runtime.ipc.close_worker()
        if runtime.process.returncode is None:
            runtime.process.terminate()
            try:
                await asyncio.wait_for(runtime.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                runtime.process.kill()
                await runtime.process.wait()
        await runtime.monitor
        return self.store.get_metadata(agent_id)

    async def shutdown(self) -> None:
        """Stop all live workers before the controller process exits."""
        agent_ids = list(self._runtimes)
        if agent_ids:
            await asyncio.gather(*(self.stop_agent(agent_id) for agent_id in agent_ids), return_exceptions=True)

    async def reap_agent(self, agent_id: str) -> dict[str, Any]:
        metadata = self.store.get_metadata(agent_id)
        if metadata.get("state") not in {"stopped", "failed", "halted", "reaped"}:
            raise AgentControllerError("Stop the agent before reaping it")
        runtime = self._runtimes.get(agent_id)
        if runtime is not None and runtime.process.returncode is None:
            raise AgentControllerError("Worker process is still running")
        snapshot_path = Path(metadata["snapshot_root"])
        if not snapshot_path.is_relative_to(self.snapshot_root):
            raise AgentControllerError("Recorded snapshot path is outside managed snapshot root")
        remove_project_snapshot(snapshot_path)
        if runtime is not None:
            self._runtimes.pop(agent_id, None)
        self.store.remove(agent_id)
        return {"agent_id": agent_id, "state": "reaped"}

