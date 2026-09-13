import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionPhase(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    WAITING_INPUT = "waiting_input"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class BridgeRequest:
    todo_path: str
    timeout_s: int
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future[dict[str, Any]] | None = None


class RebaseiSession:
    def __init__(self, repo_path: str, timeout_s: int) -> None:
        self.repo_path = repo_path
        self.timeout_s = max(1, int(timeout_s))
        self.phase: SessionPhase = SessionPhase.IDLE
        self.process: asyncio.subprocess.Process | None = None
        self.output_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        self.pending_request: BridgeRequest | None = None
        self.result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.created_at = time.time()
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.output_task is not None and not self.output_task.done()

    async def start(self, command: list[str], env: dict[str, str]) -> dict[str, Any]:
        async with self._lock:
            if self.running:
                return {
                    "status": "ok",
                    "state": self.phase.value,
                    "repo_path": self.repo_path,
                    "message": "Session already running.",
                }

            self.phase = SessionPhase.STARTING
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self.output_task = asyncio.create_task(self.process.communicate())
            self.phase = SessionPhase.RUNNING
            asyncio.create_task(self._watch_process())

            return {
                "status": "ok",
                "state": self.phase.value,
                "repo_path": self.repo_path,
                "pid": self.process.pid,
                "message": "Session started.",
            }

    async def _watch_process(self) -> None:
        task = self.output_task
        proc = self.process
        if task is None or proc is None:
            return

        stdout, stderr = await task
        rc = proc.returncode
        self.result = {
            "status": "ok" if rc == 0 else "error",
            "state": "completed" if rc == 0 else "error",
            "running": False,
            "returncode": rc,
            "repo_path": self.repo_path,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
        self.phase = SessionPhase.DONE if rc == 0 else SessionPhase.ERROR

    async def on_bridge_request(self, todo_path: str, timeout_s: int) -> dict[str, Any]:
        async with self._lock:
            if self.pending_request is not None and self.pending_request.future is not None:
                if not self.pending_request.future.done():
                    return {
                        "status": "error",
                        "message": "Another bridge request is already active.",
                        "repo_path": self.repo_path,
                    }

            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self.pending_request = BridgeRequest(
                todo_path=todo_path,
                timeout_s=max(1, int(timeout_s)),
                future=fut,
            )
            self.phase = SessionPhase.WAITING_INPUT

        try:
            return await asyncio.wait_for(fut, timeout=max(1, int(timeout_s)))
        except asyncio.TimeoutError:
            async with self._lock:
                if self.pending_request is not None and self.pending_request.future is fut:
                    self.pending_request = None
                    self.last_error = "Timed out waiting for rebase todo edits."
                    self.phase = SessionPhase.ERROR
            return {
                "status": "error",
                "message": "Timed out waiting for rebase todo edits.",
                "repo_path": self.repo_path,
            }

    async def submit_todo_resolution(self, todo_content: str | None = None) -> dict[str, Any]:
        async with self._lock:
            req = self.pending_request
            if req is None or req.future is None or req.future.done():
                return {"status": "error", "message": "No active todo request."}

            if todo_content is not None:
                try:
                    with open(req.todo_path, "w", encoding="utf-8") as handle:
                        handle.write(todo_content)
                except OSError as err:
                    return {
                        "status": "error",
                        "message": "Failed to write todo file.",
                        "error": str(err),
                        "todo_path": req.todo_path,
                    }
            else:
                try:
                    with open(req.todo_path, "r", encoding="utf-8") as handle:
                        handle.read()
                except OSError as err:
                    return {
                        "status": "error",
                        "message": "Failed to read todo file.",
                        "error": str(err),
                        "todo_path": req.todo_path,
                    }

            req.future.set_result(
                {
                    "status": "ok",
                    "message": "Rebase todo edits acknowledged.",
                    "repo_path": self.repo_path,
                    "todo_path": req.todo_path,
                }
            )
            self.pending_request = None
            self.phase = SessionPhase.RUNNING

            return {"status": "ok", "message": "Resolution submitted."}

    async def get_state(self) -> dict[str, Any]:
        async with self._lock:
            req = self.pending_request
            if req is not None and req.future is not None and not req.future.done():
                todo_text = ""
                try:
                    with open(req.todo_path, "r", encoding="utf-8") as handle:
                        todo_text = handle.read()
                except OSError:
                    todo_text = ""

                return {
                    "status": "ok",
                    "state": "needs_todo",
                    "repo_path": self.repo_path,
                    "todo_path": req.todo_path,
                    "todo_content": todo_text,
                }

            if self.result is not None:
                return self.result

            if self.running:
                return {
                    "status": "ok",
                    "state": "running",
                    "repo_path": self.repo_path,
                    "pid": self.process.pid if self.process is not None else None,
                }

            if self.last_error:
                return {
                    "status": "error",
                    "state": "error",
                    "repo_path": self.repo_path,
                    "message": self.last_error,
                }

            return {
                "status": "ok",
                "state": self.phase.value,
                "repo_path": self.repo_path,
            }


class RebaseiSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, RebaseiSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, repo_path: str, timeout_s: int) -> RebaseiSession:
        async with self._lock:
            session = self._sessions.get(repo_path)
            if session is None:
                session = RebaseiSession(repo_path=repo_path, timeout_s=timeout_s)
                self._sessions[repo_path] = session
            return session

    async def get(self, repo_path: str) -> RebaseiSession | None:
        async with self._lock:
            return self._sessions.get(repo_path)

    async def remove_if_finished(self, repo_path: str) -> None:
        async with self._lock:
            session = self._sessions.get(repo_path)
            if session is None:
                return
            if not session.running and session.pending_request is None:
                self._sessions.pop(repo_path, None)
