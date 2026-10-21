"""Sandboxed worker agent loop using controller-proxied model access."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
from pathlib import Path
from typing import Any

from agent_ipc import AgentIPCError, AgentWorkerClient

MAX_TOOL_RESULT_CHARS = 24_000
MAX_FILE_READ_BYTES = 1_000_000
MAX_SUBPROCESS_OUTPUT_BYTES = 256_000
MAX_CONTEXT_MESSAGES = 120
MAX_CONTEXT_BYTES = 1_000_000
MAX_COMMAND_TIMEOUT_SECONDS = 120
MAX_TOOL_CALLS_PER_TURN = 32

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 file in the project, optionally selecting 1-based inclusive lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Apply ordered exact literal search/replace pairs to a project file. Each search must match exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "replacements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"search": {"type": "string"}, "replace": {"type": "string"}},
                            "required": ["search", "replace"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["path", "replacements"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search project text files for a literal, case-sensitive string and return matching paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command in the sandboxed project. Provide argv as an array; no shell is used. Network and host access are controlled by Bubblewrap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_patch_queue",
            "description": "Submit committed snapshot changes for review. Use the full current HEAD commit hash.",
            "parameters": {
                "type": "object",
                "properties": {"head_commit": {"type": "string"}},
                "required": ["head_commit"],
                "additionalProperties": False,
            },
        },
    },
]

SYSTEM_PROMPT = """You are a coding agent working in an isolated Git snapshot of the project.
Use tools to inspect and change files, run tests, and commit your work in the snapshot.
Do not claim changes are applied to the main checkout. When a coherent committed
patch is ready, call submit_patch_queue with its HEAD commit. After submission,
wait for review feedback; address it as a new revision when received. You may
finish a turn without tools; that halts the agent for inspection. Keep tool use
focused and avoid dumping entire large files into the conversation."""


class WorkerError(RuntimeError):
    """Raised when the worker cannot safely execute its loop."""


def _project_root() -> Path:
    root = Path.cwd().resolve()
    if not (root / ".git").exists():
        raise WorkerError(f"Worker cwd is not a Git checkout: {root}")
    return root


def _resolve_project_file(root: Path, supplied: Any, *, must_exist: bool) -> Path:
    if not isinstance(supplied, str) or not supplied or "\x00" in supplied:
        raise WorkerError("path must be a non-empty string")
    candidate = Path(supplied)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as err:
        raise WorkerError("path resolves outside the project snapshot") from err
    if must_exist and (not resolved.exists() or not resolved.is_file()):
        raise WorkerError(f"File does not exist or is not a regular file: {supplied}")
    if not must_exist and resolved.exists() and not resolved.is_file():
        raise WorkerError(f"Target is not a regular file: {supplied}")
    return resolved


def _format_tool_result(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n...[tool result truncated]"
    return text


def _read_file(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_project_file(root, arguments.get("path"), must_exist=True)
    if path.stat().st_size > MAX_FILE_READ_BYTES:
        raise WorkerError(f"File exceeds {MAX_FILE_READ_BYTES} byte read limit; request a smaller range or search it")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_READ_BYTES:
        raise WorkerError(f"File exceeds {MAX_FILE_READ_BYTES} byte read limit; request a smaller range or search it")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        raise WorkerError("File is not valid UTF-8 text") from err
    lines = text.splitlines()
    start = arguments.get("start_line", 1)
    end = arguments.get("end_line", len(lines))
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise WorkerError("start_line/end_line must be a valid 1-based inclusive range")
    selected = lines[start - 1 : end]
    return {
        "path": path.relative_to(root).as_posix(),
        "start_line": start,
        "end_line": min(end, len(lines)),
        "total_lines": len(lines),
        "content": "\n".join(selected),
    }


def _edit_file(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    path = _resolve_project_file(root, arguments.get("path"), must_exist=True)
    replacements = arguments.get("replacements")
    if not isinstance(replacements, list) or not replacements:
        raise WorkerError("replacements must be a non-empty array")
    original = path.read_text(encoding="utf-8")
    updated = original
    applied: list[int] = []
    for index, replacement in enumerate(replacements):
        if not isinstance(replacement, dict):
            raise WorkerError(f"replacements[{index}] must be an object")
        search, replace = replacement.get("search"), replacement.get("replace")
        if not isinstance(search, str) or not search or not isinstance(replace, str):
            raise WorkerError(f"replacements[{index}] requires non-empty search and string replace")
        matches = updated.count(search)
        if matches != 1:
            raise WorkerError(f"replacements[{index}] expected one exact match, found {matches}; no changes written")
        updated = updated.replace(search, replace, 1)
        applied.append(index)
    if updated == original:
        return {"status": "ok", "path": path.relative_to(root).as_posix(), "changed": False}
    temporary = path.with_name(f".{path.name}.agent-{secrets.token_hex(8)}")
    try:
        temporary.write_text(updated, encoding="utf-8", newline="")
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "ok", "path": path.relative_to(root).as_posix(), "changed": True, "replacements_applied": applied}


def _search_files(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query:
        raise WorkerError("query must be a non-empty string")
    paths = arguments.get("paths", ["."])
    if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
        raise WorkerError("paths must be an array of project paths")
    limit = arguments.get("max_results", 100)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise WorkerError("max_results must be between 1 and 200")
    matches: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for item in paths:
        target = _resolve_project_file(root, item, must_exist=True) if item != "." else root
        candidates = [target] if target.is_file() else target.rglob("*")
        for candidate in candidates:
            if len(matches) >= limit:
                return {"matches": matches, "truncated": True}
            if candidate.is_symlink() or not candidate.is_file() or candidate in seen:
                continue
            seen.add(candidate)
            try:
                if candidate.stat().st_size > MAX_FILE_READ_BYTES:
                    continue
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(lines, 1):
                if query in line:
                    matches.append({"path": candidate.relative_to(root).as_posix(), "line": line_number, "text": line[:1000]})
                    if len(matches) >= limit:
                        return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}


async def _read_limited_stream(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while chunk := await stream.read(64 * 1024):
        remaining = MAX_SUBPROCESS_OUTPUT_BYTES - retained
        if remaining > 0:
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks), truncated


async def _run_command(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    argv = arguments.get("argv")
    timeout = arguments.get("timeout_seconds", 30)
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
        raise WorkerError("argv must be a non-empty array of strings")
    if type(timeout) is not int or not 1 <= timeout <= MAX_COMMAND_TIMEOUT_SECONDS:
        raise WorkerError(f"timeout_seconds must be between 1 and {MAX_COMMAND_TIMEOUT_SECONDS}")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp")}
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=root,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as err:
        raise WorkerError(f"Could not start command: {err}") from err
    stdout_task = asyncio.create_task(_read_limited_stream(process.stdout))
    stderr_task = asyncio.create_task(_read_limited_stream(process.stderr))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    stdout, stdout_truncated = await stdout_task
    stderr, stderr_truncated = await stderr_task
    return {
        "status": "timeout" if timed_out else ("ok" if process.returncode == 0 else "error"),
        "returncode": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "output_truncated": stdout_truncated or stderr_truncated,
    }


class AgentWorker:
    """Run the model/tool loop inside the sandbox, using controller RPC."""

    def __init__(self, client: AgentWorkerClient, root: Path, agent_id: str) -> None:
        self.client = client
        self.root = root.resolve()
        self.agent_id = agent_id
        self.history: list[dict[str, Any]] = []
        self.model: str | None = None

    async def initialize(self) -> None:
        await self.client.request("worker.hello", {"agent_id": self.agent_id, "protocol_version": 1})
        result = await self.client.request("history.get", {})
        history = result.get("history") if isinstance(result, dict) else None
        if not isinstance(history, list) or any(not isinstance(message, dict) for message in history):
            raise WorkerError("Controller returned invalid history")
        self.history = history
        await self.client.request("agent.update_state", {"state": "running"})

    @staticmethod
    def _api_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build a bounded model view without modifying durable transcript."""
        allowed = {"role", "content", "name", "tool_calls", "tool_call_id"}
        clean_history = [
            {key: value for key, value in message.items() if key in allowed}
            for message in history
            if isinstance(message.get("role"), str)
        ]
        selected: list[dict[str, Any]] = []
        total_bytes = len(SYSTEM_PROMPT.encode("utf-8"))
        for message in reversed(clean_history[-MAX_CONTEXT_MESSAGES:]):
            message_bytes = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
            if total_bytes + message_bytes > MAX_CONTEXT_BYTES:
                if not selected:
                    content = message.get("content")
                    if isinstance(content, str):
                        remaining = max(0, MAX_CONTEXT_BYTES - total_bytes - 256)
                        message["content"] = content[-remaining:] if remaining else "[message omitted: context budget]"
                        message_bytes = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
                    if total_bytes + message_bytes > MAX_CONTEXT_BYTES:
                        continue
                else:
                    break
            selected.append(message)
            total_bytes += message_bytes
        selected.reverse()
        # Never start a request with orphaned tool results if the budget cut
        # landed in the middle of a tool exchange.
        while selected and selected[0].get("role") == "tool":
            selected.pop(0)
        output: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        output.extend(selected)
        return output

    async def _persist(self, messages: list[dict[str, Any]]) -> None:
        result = await self.client.request("history.append", {"messages": messages})
        if not isinstance(result, dict) or result.get("appended") != len(messages):
            raise WorkerError("Controller did not confirm transcript persistence")
        self.history.extend(messages)

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "read_file":
            return _read_file(self.root, arguments)
        if name == "edit_file":
            return _edit_file(self.root, arguments)
        if name == "search_files":
            return _search_files(self.root, arguments)
        if name == "run_command":
            return await _run_command(self.root, arguments)
        if name == "submit_patch_queue":
            head = arguments.get("head_commit")
            if not isinstance(head, str) or not head:
                raise WorkerError("head_commit must be a non-empty string")
            result = await self.client.request(
                "patch.submit", {"base_commit": os.environ.get("AGENT_BASE_COMMIT"), "head_commit": head}
            )
            return result
        raise WorkerError(f"Unknown tool: {name}")

    async def _wait_for_command(self, *, awaiting_review: bool = False) -> bool:
        """Wait for continuation; return False when shutdown is requested."""
        wait_state = "awaiting_review" if awaiting_review else "halted"
        await self.client.request(
            "agent.update_state",
            {"state": wait_state, "stop_reason": "awaiting_review" if awaiting_review else "awaiting_command"},
        )
        while True:
            command = await self.client.request("agent.await_command", {"timeout_seconds": 3600})
            if not isinstance(command, dict):
                continue
            if command.get("command") == "timeout":
                continue
            if command.get("command") == "shutdown":
                return False
            if command.get("command") in {"continue", "review_feedback"}:
                fresh = await self.client.request("history.get", {})
                updated = fresh.get("history") if isinstance(fresh, dict) else None
                if not isinstance(updated, list):
                    raise WorkerError("Controller returned invalid resumed history")
                self.history = updated
                await self.client.request("agent.update_state", {"state": "running"})
                return True

    async def _handle_tool_calls(self, tool_calls: list[dict[str, Any]]) -> bool:
        if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
            raise WorkerError("Model returned too many tool calls in one turn")
        submitted_patch = False
        for call in tool_calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(call_id, str) or not isinstance(function, dict):
                raise WorkerError("Malformed model tool call")
            name = function.get("name")
            raw_arguments = function.get("arguments", "{}")
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError as err:
                arguments = None
            if not isinstance(name, str) or not isinstance(arguments, dict):
                result: Any = {"status": "error", "message": "Malformed tool name or arguments"}
            else:
                try:
                    result = await self._execute_tool(name, arguments)
                except Exception as err:
                    result = {"status": "error", "message": str(err)}
            tool_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _format_tool_result(result),
                "id": secrets.token_hex(16),
            }
            await self._persist([tool_message])
            if name == "submit_patch_queue" and isinstance(result, dict) and result.get("status") == "ok":
                submitted_patch = True
        if submitted_patch:
            if not await self._wait_for_command(awaiting_review=True):
                return False
        return True

    async def run(self) -> None:
        await self.initialize()
        while True:
            completion = await self.client.request(
                "model.complete",
                {"messages": self._api_messages(self.history), "tools": TOOLS, "tool_choice": "auto"},
            )
            if not isinstance(completion, dict):
                raise WorkerError("Controller returned an invalid completion")
            choices = completion.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise WorkerError("Model completion has no choices")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise WorkerError("Model completion choice has no message")
            self.model = completion.get("model") if isinstance(completion.get("model"), str) else self.model
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content"),
                "id": secrets.token_hex(16),
            }
            if message.get("tool_calls") is not None:
                assistant["tool_calls"] = message["tool_calls"]
            if self.model:
                assistant["model"] = self.model
            await self._persist([assistant])

            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                if not await self._handle_tool_calls(tool_calls):
                    return
                continue

            await self.client.request("agent.update_state", {"state": "halted", "stop_reason": "no_tool_calls"})
            if not await self._wait_for_command():
                return


async def async_main() -> int:
    socket_path = os.environ.get("AGENT_CONTROLLER_SOCKET")
    agent_id = os.environ.get("AGENT_ID")
    if not socket_path or not agent_id:
        raise WorkerError("AGENT_CONTROLLER_SOCKET and AGENT_ID must be set")
    client = AgentWorkerClient(socket_path)
    try:
        await client.connect()
        worker = AgentWorker(client, _project_root(), agent_id)
        await worker.run()
    finally:
        await client.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except (AgentIPCError, WorkerError) as err:
        print(f"agent worker: {err}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
