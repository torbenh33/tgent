"""Trusted single-project agent lifecycle coordinator.

This module wires together durable state, reflink snapshots, Bubblewrap command
construction, and the controller/worker socket. It deliberately accepts model
and patch-submission callbacks rather than embedding project-specific policy.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
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
    RuntimeMount,
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


logger = logging.getLogger(__name__)


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
        runtime_app_path: str | os.PathLike[str] | None = None,
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
        self.runtime_app_path = Path(runtime_app_path or sys.prefix).absolute()
        self.runtime_layout = runtime_layout or build_runtime_layout(
            runtime_dirs=(Path(sys.prefix),),
            runtime_files=tuple(
                Path(__file__).parent / name
                for name in (
                    "agent_worker.py", "agent_ipc.py", "agent_sandbox.py", "agent_snapshot.py",
                    "agent_project.py", "agent_store.py", "git_patch_selection.py", "test_subprocess.py",
                    "remote_agent_service.py", "remote_agent_config.py", "remote_agent_daemon.py",
                    "remote_agent_protocol.py", "remote_agent_upload.py", "llm.py", "mcp_test_runner.py",
                )
            ),
        )
        self.bwrap = bwrap
        self.max_model_calls = max(1, int(max_model_calls))
        self.max_runtime_seconds = max(1, int(max_runtime_seconds))
        self._runtimes: dict[str, _AgentRuntime] = {}
        # Dynamic project MCP tools are opt-in and controlled only by this controller.
        self._tool_allowlists: dict[str, set[str]] = {}
        self._tool_catalogs: dict[str, list[dict[str, Any]]] = {}
        self.default_tool_modules = ["mcp_test_runner"]
        self.default_tool_modules.extend(
            name.strip()
            for name in os.environ.get("TGIT_AGENT_DEFAULT_TOOLS", "").split(",")
            if name.strip() and name.strip() not in self.default_tool_modules
        )
        self._lock = asyncio.Lock()
        # Worker processes cannot survive a controller restart as managed workers.
        # Recover stale running bookkeeping without touching patch metadata/history.
        for entry in self.store.state_root.iterdir():
            if entry.name == "logs" or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                metadata = self.store.get_metadata(entry.name)
                if metadata.get("state") == "running":
                    self.store.update_metadata(entry.name, {
                        "state": "stopped", "stop_reason": "controller_restarted", "worker_pid": None,
                    })
            except (AgentStoreError, OSError):
                continue

    @staticmethod
    def _validate_start_request(request: Any) -> tuple[list[dict[str, Any]], int, int]:
        if not isinstance(request, dict):
            raise AgentControllerError("start request must be an object")
        allowed = {"schema_version", "task", "history", "limits", "name"}
        unknown = set(request) - allowed
        if unknown:
            raise AgentControllerError(f"Unknown start request field(s): {', '.join(sorted(unknown))}")
        if type(request.get("schema_version")) is not int or request["schema_version"] != 1:
            raise AgentControllerError("schema_version must be 1")
        name = request.get("name")
        if name is not None and (
            not isinstance(name, str) or not name.strip() or len(name.strip()) > 100
            or any(ord(character) < 32 for character in name)
        ):
            raise AgentControllerError("name must be a non-empty string of at most 100 characters without controls")

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
        _extra_runtime_mounts: tuple[Any, ...] = (),
        _effective_base_commit: str | None = None,
        _original_base_commit: str | None = None,
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
        # MCP servers are installed entry points in the worker runtime, not project files.
        seeded = set(self.default_tool_modules)
        self._tool_allowlists[agent_id] = seeded
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
            original_base_commit = _original_base_commit or snapshot.base_commit
            if _effective_base_commit is not None:
                if _COMMIT_RE.fullmatch(_effective_base_commit) is None:
                    raise AgentControllerError("Continuation sync returned an invalid base commit")
                snapshot = ProjectSnapshot(snapshot.root, snapshot.project_root, _effective_base_commit, snapshot.linked_worktree)
            socket_dir = Path(self.store.state_root) / "sockets"
            socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(socket_dir, 0o700)
            socket_path = socket_dir / f"{agent_id}.sock"

            metadata = {
                "schema_version": 1,
                "agent_id": agent_id,
                "name": request.get("name", "").strip() or f"Agent {agent_id[:8]}",
                "project_root": str(selected_project_root),
                "state": state,
                "created_at": time.time(),
                "updated_at": time.time(),
                "base_commit": snapshot.base_commit,
                **({"original_base_commit": original_base_commit, "sync_base": snapshot.base_commit} if _effective_base_commit is not None else {}),
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
                "agent.tools.report": self._handler(agent_id, self._report_tool_catalog),
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
                    "PYTHONPATH": str(self.runtime_app_path),
                },
                runtime_layout=SandboxRuntimeLayout(
                    self.runtime_layout.mounts + tuple(_extra_runtime_mounts),
                    self.runtime_layout.symlinks,
                ),
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
                self._tool_allowlists.pop(agent_id, None)
                self._tool_catalogs.pop(agent_id, None)
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

    async def _report_tool_catalog(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        modules = params.get("modules")
        if not isinstance(modules, list) or len(modules) > 256:
            raise AgentControllerError("Invalid MCP catalog")
        encoded = json.dumps(modules, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 512_000:
            raise AgentControllerError("MCP catalog exceeds size limit")
        valid = []
        for entry in modules:
            if not isinstance(entry, dict) or set(entry) != {"name", "items"} or not isinstance(entry["name"], str) or re.fullmatch(r"mcp_[A-Za-z0-9_-]+", entry["name"]) is None or not isinstance(entry["items"], list):
                raise AgentControllerError("Invalid MCP catalog entry")
            valid.append(entry)
        self._tool_catalogs[agent_id] = valid
        summary = [f"{entry['name']}[{', '.join(str(item.get('name', '?')) for item in entry['items'] if isinstance(item, dict)) or 'no tools'}]" for entry in valid]
        logger.info("Agent %s reported MCP catalog: %s", agent_id, "; ".join(summary) if summary else "empty catalog")
        return {"reported": len(valid)}

    async def get_agent_tools(self, agent_id: str) -> dict[str, Any]:
        agent_id = self.resolve_agent_id(agent_id)
        metadata = self.store.get_metadata(agent_id)
        enabled = self._tool_allowlists.get(agent_id, set())
        modules = [{**item, "enabled": item["name"] in enabled} for item in self._tool_catalogs.get(agent_id, [])]
        summary = [f"{item['name']}={'enabled' if item['enabled'] else 'disabled'}" for item in modules]
        logger.info("Agent %s MCP tool status: %s", agent_id, ", ".join(summary) if summary else "empty catalog (no discovered modules)")
        return {"agent_id": agent_id, "modules": modules}

    async def set_agent_tools(self, agent_id: str, tools: list[str]) -> dict[str, Any]:
        """Set opt-in MCP module names for a live agent (trusted operator API)."""
        agent_id = self.resolve_agent_id(agent_id)
        if not isinstance(tools, list) or any(not isinstance(name, str) or re.fullmatch(r"mcp_[A-Za-z0-9_-]+", name) is None for name in tools):
            raise AgentControllerError("tools must be MCP module names such as mcp_example")
        if len(set(tools)) != len(tools):
            raise AgentControllerError("tools must not contain duplicates")
        metadata = self.store.get_metadata(agent_id)
        if metadata.get("state") not in {"running", "halted", "awaiting_review", "review_received"}:
            raise AgentControllerError("Tools can only be changed for a live agent")
        runtime = self._runtimes.get(agent_id)
        if runtime is None or runtime.process.returncode is not None:
            raise AgentControllerError("Tools can only be changed for a live agent")
        available = {
            entry_point.name
            for entry_point in importlib.metadata.entry_points().select(group="tgit_agent.mcp_servers")
            if re.fullmatch(r"mcp_[A-Za-z0-9_-]+", entry_point.name)
        }
        if not set(tools) <= available:
            raise AgentControllerError("Requested MCP module is not installed in the agent runtime")
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
        result = self.patch_submit_callback(snapshot, {**params, "agent_id": agent_id})
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
                metadata = self.store.get_metadata(entry.name)
                metadata["history_summary"] = self.store.get_cached_summary(entry.name)
                agents.append(metadata)
            except (AgentStoreError, OSError):
                continue
        agents.sort(key=lambda metadata: (metadata.get("created_at", 0), metadata.get("agent_id", "")))
        return agents

    def resolve_agent_id(self, agent_id: str) -> str:
        """Resolve a full ID or a unique hexadecimal prefix against stored agents."""
        validate_agent_id(agent_id)
        try:
            self.store.get_metadata(agent_id)
            return agent_id
        except AgentStoreError:
            pass
        if not re.fullmatch(r"[0-9a-fA-F]+", agent_id):
            raise AgentControllerError(f"Unknown agent ID or invalid hexadecimal prefix: {agent_id}")
        matches = []
        for entry in self.store.state_root.iterdir():
            if entry.name == "logs" or entry.is_symlink() or not entry.is_dir():
                continue
            if entry.name.lower().startswith(agent_id.lower()):
                try:
                    self.store.get_metadata(entry.name)
                    matches.append(entry.name)
                except AgentStoreError:
                    continue
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise AgentControllerError(f"Unknown agent ID prefix: {agent_id}")
        raise AgentControllerError(f"Ambiguous agent ID prefix: {agent_id}")

    async def get_status(self, agent_id: str) -> dict[str, Any]:
        return self.store.get_metadata(self.resolve_agent_id(agent_id))

    async def list_agents_requiring_attention(self) -> list[dict[str, Any]]:
        """List agents awaiting operator input or carrying submitted patches."""
        result = []
        for metadata in await self.list_agents():
            state = metadata.get("state")
            submitted = bool(metadata.get("submitted_head"))
            patch_decision_matches = (
                (metadata.get("patch_applied_acknowledgement") or {}).get("submitted_head")
                == metadata.get("submitted_head")
                or (metadata.get("patch_rejection") or {}).get("submitted_head")
                == metadata.get("submitted_head")
            )
            if (submitted and not patch_decision_matches) or state in {"awaiting_review", "halted"} or metadata.get("stop_reason") == "awaiting_command":
                reasons = (["submitted_patch"] if submitted and not patch_decision_matches else [])
                if state in {"awaiting_review", "halted"}: reasons.append(state)
                if metadata.get("stop_reason") == "awaiting_command": reasons.append("awaiting_command")
                result.append({**metadata, "requires_attention": True, "attention_reasons": reasons})
        return result

    async def acknowledge_submitted_patch_applied(self, agent_id: str) -> dict[str, Any]:
        """Record an operator's explicit acknowledgement that submitted work is applied.

        This preserves submitted_head and review artifacts; it only unlocks reaping.
        Repeated acknowledgements for the same head are idempotent.
        """
        metadata = self.store.get_metadata(agent_id)
        head = metadata.get("submitted_head")
        if not head:
            if metadata.get("patch_applied_acknowledgement"):
                return metadata
            raise AgentControllerError("Agent has no submitted patch to acknowledge")
        runtime = self._runtimes.get(agent_id)
        if runtime is not None and runtime.process.returncode is None:
            raise AgentControllerError("Stop the agent before acknowledging its submitted patch")
        if metadata.get("state") not in {"awaiting_review", "stopped", "halted", "failed"}:
            raise AgentControllerError("Agent must be inactive before acknowledging its submitted patch")
        acknowledgement = metadata.get("patch_applied_acknowledgement")
        if acknowledgement and acknowledgement.get("submitted_head") == head:
            return metadata
        from datetime import datetime, timezone
        acknowledgement = {"submitted_head": head, "acknowledged_at": datetime.now(timezone.utc).isoformat()}
        return self.store.update_metadata(agent_id, {"patch_applied_acknowledgement": acknowledgement})

    async def reject_submitted_patch(self, agent_id: str, reason: str | None = None) -> dict[str, Any]:
        """Record an operator rejection without treating it as application."""
        if reason is not None and (not isinstance(reason, str) or len(reason) > 4000):
            raise AgentControllerError("reason must be a string of at most 4000 characters")
        metadata = self.store.get_metadata(agent_id)
        head = metadata.get("submitted_head")
        if not head:
            if metadata.get("patch_rejection"):
                return metadata
            raise AgentControllerError("Agent has no submitted patch to reject")
        runtime = self._runtimes.get(agent_id)
        if runtime is not None and runtime.process.returncode is None:
            raise AgentControllerError("Stop the agent before rejecting its submitted patch")
        if metadata.get("state") not in {"awaiting_review", "stopped", "halted", "failed"}:
            raise AgentControllerError("Agent must be inactive before rejecting its submitted patch")
        rejection = metadata.get("patch_rejection")
        if rejection and rejection.get("submitted_head") == head:
            return metadata
        from datetime import datetime, timezone
        rejection = {"submitted_head": head, "rejected_at": datetime.now(timezone.utc).isoformat()}
        if reason is not None:
            rejection["reason"] = reason
        return self.store.update_metadata(agent_id, {"patch_rejection": rejection})

    async def reap_agents(self, agent_ids: list[str]) -> dict[str, Any]:
        """Safely bulk-reap stopped/failed agents without submitted patches."""
        agent_ids = [self.resolve_agent_id(item) for item in agent_ids] if isinstance(agent_ids, list) else agent_ids
        if not isinstance(agent_ids, list) or not agent_ids or any(not isinstance(item, str) for item in agent_ids):
            raise AgentControllerError("agent_ids must be a non-empty list of agent IDs")
        results = []
        seen: set[str] = set()
        for agent_id in agent_ids:
            if agent_id in seen:
                results.append({"agent_id": agent_id, "status": "excluded", "reason": "duplicate_id"})
                continue
            seen.add(agent_id)
            try:
                metadata = self.store.get_metadata(agent_id)
                submitted_head = metadata.get("submitted_head")
                patch_decision_matches = submitted_head and (
                    (metadata.get("patch_applied_acknowledgement") or {}).get("submitted_head") == submitted_head
                    or (metadata.get("patch_rejection") or {}).get("submitted_head") == submitted_head
                )
                if (submitted_head and not patch_decision_matches) or metadata.get("state") == "awaiting_review":
                    results.append({"agent_id": agent_id, "status": "excluded", "reason": "submitted_patch_requires_operator_review", "submitted_head": submitted_head})
                elif metadata.get("state") not in {"stopped", "failed"}:
                    results.append({"agent_id": agent_id, "status": "excluded", "reason": "state_not_stopped_or_failed", "state": metadata.get("state")})
                else:
                    results.append({**(await self._reap_agent(agent_id)), "status": "reaped"})
            except (AgentStoreError, AgentControllerError, OSError) as err:
                results.append({"agent_id": agent_id, "status": "excluded", "reason": str(err)})
        return {"results": results}

    async def stop_agents(self, agent_ids: list[str] | None = None) -> dict[str, Any]:
        """Stop eligible workers in bulk; submitted-patch workers are preserved for review."""
        if isinstance(agent_ids, list):
            agent_ids = [self.resolve_agent_id(item) for item in agent_ids]
        selected = agent_ids if agent_ids is not None else [a["agent_id"] for a in await self.list_agents()]
        if not isinstance(selected, list) or any(not isinstance(i, str) for i in selected):
            raise AgentControllerError("agent_ids must be a list of agent IDs")
        results = []
        for agent_id in dict.fromkeys(selected):
            try:
                metadata = self.store.get_metadata(agent_id)
                runtime = self._runtimes.get(agent_id)
                if metadata.get("submitted_head"):
                    results.append({"agent_id": agent_id, "status": "excluded", "reason": "submitted_patch_requires_operator_review"})
                elif runtime is None and metadata.get("state") not in {"awaiting_review"}:
                    results.append({"agent_id": agent_id, "status": "excluded", "reason": "no_active_worker", "state": metadata.get("state")})
                else:
                    stopped = await self.stop_agent(agent_id)
                    results.append({**stopped, "agent_id": agent_id, "status": "stopped"})
            except Exception as err:
                results.append({"agent_id": agent_id, "status": "error", "reason": str(err)})
        return {"results": results}

    async def restart_agents(self, agent_ids: list[str]) -> dict[str, Any]:
        """Recover eligible stopped agents as new children, retaining original history/snapshot."""
        agent_ids = [self.resolve_agent_id(item) for item in agent_ids] if isinstance(agent_ids, list) else agent_ids
        if not isinstance(agent_ids, list) or not agent_ids or any(not isinstance(i, str) for i in agent_ids):
            raise AgentControllerError("agent_ids must be a non-empty list of agent IDs")
        results = []
        for agent_id in dict.fromkeys(agent_ids):
            try:
                metadata = self.store.get_metadata(agent_id)
                if metadata.get("state") != "stopped" or metadata.get("submitted_head"):
                    results.append({"agent_id": agent_id, "status": "excluded", "reason": "not_restartable", "state": metadata.get("state")})
                    continue
                result = await self.continue_agent(agent_id, [{"role": "user", "content": "Resume the previous task from the saved history."}])
                results.append({**result, "status": "restarted"})
            except Exception as err:
                results.append({"agent_id": agent_id, "status": "error", "reason": str(err)})
        return {"results": results}

    async def get_agent_log(self, agent_id: str, *, max_bytes: int = 64 * 1024) -> dict[str, Any]:
        """Return the bounded tail of a worker's captured stderr log."""
        agent_id = self.resolve_agent_id(agent_id)
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
        return self.store.get_history(self.resolve_agent_id(agent_id))

    async def continue_agent(self, agent_id: str, messages: list[dict[str, Any]], *, sync_callback: Callable[[Path], str] | None = None, sync_bundle_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        agent_id = self.resolve_agent_id(agent_id)
        validate = self.store.get_metadata(agent_id)
        if validate.get("state") not in {"halted", "stopped", "awaiting_review", "review_received"}:
            raise AgentControllerError(f"Agent cannot continue from state {validate.get('state')}")
        if not messages or any(not isinstance(message, dict) for message in messages):
            raise AgentControllerError("continue requires one or more message objects")
        runtime = self._runtimes.get(agent_id)
        if runtime is None or runtime.process.returncode is not None:
            # Stopped is an operator-requested termination (or controller shutdown),
            # while halted is a worker's natural completion. Both are recoverable
            # only when no patch has already been submitted.
            if validate.get("state") not in {"halted", "stopped"}:
                raise AgentControllerError("Only inactive halted or stopped agents can be restarted")
            if validate.get("submitted_head"):
                raise AgentControllerError("An agent with a submitted patch cannot be restarted")
            restarting = getattr(self, "_restarting_agents", None)
            if restarting is None:
                restarting = self._restarting_agents = set()
            snapshot_project = Path(validate.get("snapshot_project", ""))
            snapshot_root = Path(validate.get("snapshot_root", ""))
            if (not snapshot_project.is_absolute() or not snapshot_root.is_relative_to(self.snapshot_root)
                    or snapshot_project.is_symlink() or not snapshot_project.is_dir()
                    or snapshot_project.parent != snapshot_root):
                raise AgentControllerError("Recorded agent snapshot is unavailable or invalid")
            history = self.store.get_history(agent_id)
            if len(history) + len(messages) > MAX_HISTORY_MESSAGES or len(json.dumps([*history, *messages], ensure_ascii=False).encode("utf-8")) > MAX_HISTORY_MESSAGE_BYTES:
                raise AgentControllerError("Continued history exceeds size limit")
            if agent_id in restarting:
                raise AgentControllerError("Agent restart is already in progress")
            restarting.add(agent_id)
            try:
                sync_base = sync_callback(snapshot_project) if sync_callback is not None else None
                if sync_base is not None and (not isinstance(sync_base, str) or _COMMIT_RE.fullmatch(sync_base) is None):
                    raise AgentControllerError("Continuation sync returned an invalid base commit")
                extra = list(messages)
                # Sync instructions are queued by the adapter in the exact continuation request.
                extra_mounts = ()
                if sync_callback is not None:
                    if sync_bundle_path is None:
                        raise AgentControllerError("Continuation bundle path is required for sync")
                    bundle = Path(sync_bundle_path)
                    if bundle.is_symlink() or not bundle.is_file():
                        raise AgentControllerError("Continuation bundle is unavailable")
                    bundle = bundle.resolve(strict=True)
                    extra_mounts = (RuntimeMount(bundle, Path("/run/agent-sync/repository.bundle")),)
                start_options = {"source_root": snapshot_project}
                if sync_base is not None:
                    start_options["_effective_base_commit"] = sync_base
                    start_options["_original_base_commit"] = validate.get("base_commit")
                if extra_mounts:
                    start_options["_extra_runtime_mounts"] = extra_mounts
                restarted = await self.start_agent(
                    {"schema_version": 1, "history": [*history, *extra], "name": validate.get("name", "Agent")},
                    **start_options,
                )
            finally:
                restarting.discard(agent_id)
            self.store.update_metadata(restarted["agent_id"], {"parent_agent_id": agent_id})
            return {**restarted, "parent_agent_id": agent_id, "queued_messages": len(messages)}
        self.store.append_history(agent_id, messages)
        command = {"command": "review_feedback", "messages": messages}
        await runtime.commands.put(command)
        self.store.update_metadata(agent_id, {"state": "review_received"})
        return {"agent_id": agent_id, "state": "review_received", "queued_messages": len(messages)}

    async def stop_agent(self, agent_id: str) -> dict[str, Any]:
        agent_id = self.resolve_agent_id(agent_id)
        runtime = self._runtimes.get(agent_id)
        if runtime is None:
            metadata = self.store.get_metadata(agent_id)
            if metadata.get("state") == "awaiting_review":
                # No runtime means there is no worker to receive a stop command.
                # Restrict this transition to the inactive review-waiting state.
                self.store.update_metadata(agent_id, {"state": "stopped", "stop_reason": "operator_stopped"})
                return self.store.get_metadata(agent_id)
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

    async def _reap_agent(self, agent_id: str) -> dict[str, Any]:
        metadata = self.store.get_metadata(agent_id)
        runtime = self._runtimes.get(agent_id)
        if runtime is not None and runtime.process.returncode is None:
            raise AgentControllerError("Worker process is still running")
        if metadata.get("state") not in {"stopped", "failed", "halted", "reaped"}:
            raise AgentControllerError("Stop the agent before reaping it")
        submitted_head = metadata.get("submitted_head")
        rejected = metadata.get("patch_rejection", {}).get("submitted_head") == submitted_head
        acknowledged = metadata.get("patch_applied_acknowledgement", {}).get("submitted_head") == submitted_head
        if submitted_head and not (rejected or acknowledged):
            raise AgentControllerError("Agent has a submitted patch; preserve its snapshot and review artifacts, reject it, or acknowledge it as already applied")
        snapshot_path = Path(metadata["snapshot_root"])
        if not snapshot_path.is_relative_to(self.snapshot_root):
            raise AgentControllerError("Recorded snapshot path is outside managed snapshot root")
        remove_project_snapshot(snapshot_path)
        if runtime is not None:
            self._runtimes.pop(agent_id, None)
        self.store.remove(agent_id)
        return {"agent_id": agent_id, "state": "reaped"}

