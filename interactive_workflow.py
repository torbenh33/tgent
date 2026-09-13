import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
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
class EditRequest:
    edit_paths: list[str]
    timeout_s: int
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future[dict[str, Any]] | None = None


class InteractiveSession:
    def __init__(
        self,
        repo_path: str,
        timeout_s: int,
        launcher: Callable[["InteractiveSession"], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.timeout_s = max(1, int(timeout_s))
        self.phase: SessionPhase = SessionPhase.IDLE
        self.process: asyncio.subprocess.Process | None = None
        self.output_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        self.pending_request: EditRequest | None = None
        self.result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.created_at = time.time()
        self._lock = asyncio.Lock()
        self.needs_input_event = asyncio.Event()
        self.finished_event = asyncio.Event()
        self.launcher = launcher

    @property
    def running(self) -> bool:
        return self.output_task is not None and not self.output_task.done()

    @staticmethod
    def _bridge_repo_path() -> str:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return "."
        return proc.stdout.strip() or "."

    @staticmethod
    def bridge_call(edit_paths: list[str], socket_path: str, default_timeout_s: int) -> int:
        timeout_s = int(os.environ.get("TGIT_REBASEI_TIMEOUT_S", str(default_timeout_s)))

        req = {
            "action": "enqueue_job",
            "repo_path": InteractiveSession._bridge_repo_path(),
            "edit_paths": [p for p in edit_paths if isinstance(p, str) and p.strip()],
            "timeout_s": max(1, timeout_s),
        }

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(socket_path)
                sock.sendall((json.dumps(req) + "\n").encode("utf-8"))

                data = b""
                while not data.endswith(b"\n"):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
        except OSError as err:
            print(f"rebasei bridge socket error: {err}", file=sys.stderr)
            return 1

        if not data:
            print("rebasei bridge received empty response", file=sys.stderr)
            return 1

        try:
            resp = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            print("rebasei bridge received invalid JSON response", file=sys.stderr)
            return 1

        if resp.get("status") == "ok":
            return 0

        message = str(resp.get("message") or "rebasei edit job failed")
        print(message, file=sys.stderr)
        return 1

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
            self.result = None
            self.last_error = None
            self.pending_request = None
            self.needs_input_event.clear()
            self.finished_event.clear()
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
        self.needs_input_event.clear()
        self.finished_event.set()

    async def on_edit_request(self, edit_paths: list[str], timeout_s: int) -> dict[str, Any]:
        async with self._lock:
            if self.pending_request is not None and self.pending_request.future is not None:
                if not self.pending_request.future.done():
                    return {
                        "status": "error",
                        "message": "Another bridge request is already active.",
                        "repo_path": self.repo_path,
                    }

            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            normalized_paths = [p for p in edit_paths if isinstance(p, str) and p.strip()]
            if not normalized_paths:
                return {
                    "status": "error",
                    "message": "Missing edit paths.",
                    "repo_path": self.repo_path,
                }

            self.pending_request = EditRequest(
                edit_paths=normalized_paths,
                timeout_s=max(1, int(timeout_s)),
                future=fut,
            )
            self.phase = SessionPhase.WAITING_INPUT
            self.needs_input_event.set()

        try:
            return await asyncio.wait_for(fut, timeout=max(1, int(timeout_s)))
        except asyncio.TimeoutError:
            async with self._lock:
                if self.pending_request is not None and self.pending_request.future is fut:
                    self.pending_request = None
                    self.last_error = "Timed out waiting for required edit."
                    self.phase = SessionPhase.ERROR
                    self.needs_input_event.clear()
            return {
                "status": "error",
                "message": "Timed out waiting for required edit.",
                "repo_path": self.repo_path,
            }

    async def submit_edit_resolution(self) -> dict[str, Any]:
        async with self._lock:
            req = self.pending_request
            if req is None or req.future is None or req.future.done():
                return {"status": "error", "message": "No active edit request."}

            req.future.set_result(
                {
                    "status": "ok",
                    "message": "Required edit acknowledged.",
                    "repo_path": self.repo_path,
                    "edit_paths": req.edit_paths,
                }
            )
            self.pending_request = None
            self.phase = SessionPhase.RUNNING
            self.needs_input_event.clear()

            return {"status": "ok", "message": "Resolution submitted."}

    async def get_state(self) -> dict[str, Any]:
        async with self._lock:
            req = self.pending_request
            if req is not None and req.future is not None and not req.future.done():
                return {
                    "status": "ok",
                    "state": "needs_edit",
                    "repo_path": self.repo_path,
                    "edit_paths": req.edit_paths,
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

    async def step(self, has_edit: bool, wait_timeout_s: int) -> dict[str, Any]:
        if not self.running and self.result is None and self.pending_request is None:
            if self.launcher is None:
                return {
                    "status": "error",
                    "state": "error",
                    "repo_path": self.repo_path,
                    "message": "Missing launcher for session.",
                }
            start_result = await self.launcher(self)
            if start_result.get("status") != "ok":
                return start_result

        if has_edit:
            submit_result = await self.submit_edit_resolution()
            if submit_result.get("status") != "ok":
                return submit_result

        return await self.wait_for_action(timeout_s=wait_timeout_s)

    async def wait_for_action(self, timeout_s: int) -> dict[str, Any]:
        state = await self.get_state()
        if state.get("state") in {"needs_edit", "completed", "error"}:
            return state

        waiters = {
            asyncio.create_task(self.needs_input_event.wait()),
            asyncio.create_task(self.finished_event.wait()),
        }

        try:
            done, pending = await asyncio.wait(
                waiters,
                timeout=max(1, int(timeout_s)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            _ = done
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()

        return await self.get_state()


class InteractiveSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, InteractiveSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        repo_path: str,
        timeout_s: int,
        launcher: Callable[[InteractiveSession], Awaitable[dict[str, Any]]] | None = None,
    ) -> InteractiveSession:
        async with self._lock:
            session = self._sessions.get(repo_path)
            if session is None:
                session = InteractiveSession(repo_path=repo_path, timeout_s=timeout_s, launcher=launcher)
                self._sessions[repo_path] = session
            elif launcher is not None:
                session.launcher = launcher
            return session

    async def get(self, repo_path: str) -> InteractiveSession | None:
        async with self._lock:
            return self._sessions.get(repo_path)

    async def remove_if_finished(self, repo_path: str) -> None:
        async with self._lock:
            session = self._sessions.get(repo_path)
            if session is None:
                return
            if not session.running and session.pending_request is None:
                self._sessions.pop(repo_path, None)


class InteractiveBridgeServer:
    def __init__(
        self,
        sessions: InteractiveSessionManager,
        socket_path: str,
        on_request_error: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.socket_path = socket_path
        self.on_request_error = on_request_error
        self._server: asyncio.base_events.Server | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    async def _read_json_line(self, reader: asyncio.StreamReader) -> dict[str, Any] | None:
        raw = await reader.readline()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    async def _write_json_line(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()

    async def _client_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await self._read_json_line(reader)
            if req is None:
                await self._write_json_line(writer, {"status": "error", "message": "Invalid or empty request."})
                return

            if req.get("action") != "enqueue_job":
                await self._write_json_line(writer, {"status": "error", "message": "Unsupported action."})
                return

            repo_path = str(req.get("repo_path") or ".")
            raw_edit_paths = req.get("edit_paths")
            timeout_s = int(req.get("timeout_s") or 900)

            edit_paths = raw_edit_paths if isinstance(raw_edit_paths, list) else []
            if not edit_paths:
                await self._write_json_line(writer, {"status": "error", "message": "Missing edit_paths."})
                return

            session = await self.sessions.get_or_create(repo_path, timeout_s)
            result = await session.on_edit_request(edit_paths=edit_paths, timeout_s=timeout_s)
            if result.get("status") != "ok" and self.on_request_error is not None:
                await self.on_request_error(repo_path)
            await self._write_json_line(writer, result)
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self) -> None:
        if self._server is not None:
            return

        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server = await asyncio.start_unix_server(self._client_handler, path=self.socket_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
