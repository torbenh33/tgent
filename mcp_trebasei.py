import argparse
import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
from typing import Any

from fastmcp import FastMCP
from interactive_workflow import RebaseiSessionManager

mcp = FastMCP("tgit-rebasei-async")

SOCKET_PATH = os.environ.get("TGIT_REBASEI_SOCKET", "/tmp/tgit-rebasei.sock")
DEFAULT_JOB_TIMEOUT_S = int(os.environ.get("TGIT_REBASEI_TIMEOUT_S", "900"))

SESSIONS = RebaseiSessionManager()
_SOCKET_SERVER: asyncio.base_events.Server | None = None


def _resolve_repo_path(repo_path: str | None) -> str:
    candidate = repo_path.strip() if isinstance(repo_path, str) else ""
    base = candidate or "."
    return os.path.realpath(os.path.abspath(os.path.expanduser(base)))


async def _git_command(repo_path: str, args: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        _resolve_repo_path(repo_path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def _detect_operation(repo_path: str) -> str | None:
    rc, out, _ = await _git_command(repo_path, ["rev-parse", "--git-dir"])
    if rc != 0:
        return None

    git_dir = out.strip()
    if not os.path.isabs(git_dir):
        git_dir = os.path.abspath(os.path.join(_resolve_repo_path(repo_path), git_dir))

    if os.path.exists(os.path.join(git_dir, "MERGE_HEAD")):
        return "merge"
    if os.path.exists(os.path.join(git_dir, "CHERRY_PICK_HEAD")):
        return "cherry-pick"
    if os.path.exists(os.path.join(git_dir, "rebase-merge")) or os.path.exists(os.path.join(git_dir, "rebase-apply")):
        return "rebase"
    return None


async def _abort_git_operation(repo_path: str) -> None:
    op = await _detect_operation(repo_path)
    if op == "merge":
        await _git_command(repo_path, ["merge", "--abort"])
    elif op == "rebase":
        await _git_command(repo_path, ["rebase", "--abort"])
    elif op == "cherry-pick":
        await _git_command(repo_path, ["cherry-pick", "--abort"])


async def _read_json_line(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    raw = await reader.readline()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


async def _write_json_line(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()


async def _socket_client_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        req = await _read_json_line(reader)
        if req is None:
            await _write_json_line(writer, {"status": "error", "message": "Invalid or empty request."})
            return

        if req.get("action") != "enqueue_job":
            await _write_json_line(writer, {"status": "error", "message": "Unsupported action."})
            return

        repo_path = _resolve_repo_path(str(req.get("repo_path") or "."))
        todo_path = str(req.get("todo_path") or "")
        timeout_s = int(req.get("timeout_s") or DEFAULT_JOB_TIMEOUT_S)

        if not todo_path:
            await _write_json_line(writer, {"status": "error", "message": "Missing todo_path."})
            return

        session = await SESSIONS.get_or_create(repo_path, timeout_s)
        result = await session.on_bridge_request(todo_path=todo_path, timeout_s=timeout_s)
        if result.get("status") != "ok":
            await _abort_git_operation(repo_path)
        await _write_json_line(writer, result)
    finally:
        writer.close()
        await writer.wait_closed()


async def start_socket_server(socket_path: str = SOCKET_PATH) -> None:
    global _SOCKET_SERVER
    if _SOCKET_SERVER is not None:
        return

    if os.path.exists(socket_path):
        os.unlink(socket_path)

    _SOCKET_SERVER = await asyncio.start_unix_server(_socket_client_handler, path=socket_path)


async def stop_socket_server(socket_path: str = SOCKET_PATH) -> None:
    global _SOCKET_SERVER
    if _SOCKET_SERVER is not None:
        _SOCKET_SERVER.close()
        await _SOCKET_SERVER.wait_closed()
        _SOCKET_SERVER = None

    if os.path.exists(socket_path):
        os.unlink(socket_path)


def _build_bridge_cmd() -> str:
    script = os.path.abspath(__file__)
    return f"{shlex.quote(sys.executable)} {shlex.quote(script)} --bridge"


async def _launch_git_rebasei(
    repo_path: str,
    upstream: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    await start_socket_server()

    repo = _resolve_repo_path(repo_path)
    chosen_upstream = upstream.strip() or "HEAD~1"
    cmd = ["git", "-C", repo, "rebase", "-i", chosen_upstream]

    env = dict(os.environ)
    env["GIT_SEQUENCE_EDITOR"] = _build_bridge_cmd()
    env["TGIT_REBASEI_SOCKET"] = SOCKET_PATH
    env["TGIT_REBASEI_TIMEOUT_S"] = str(max(1, int(timeout_seconds)))

    session = await SESSIONS.get_or_create(repo, timeout_seconds)
    start_result = await session.start(command=cmd, env=env)
    start_result["socket_path"] = SOCKET_PATH
    start_result["upstream"] = chosen_upstream
    return start_result


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


def _bridge_call(todo_path: str) -> int:
    socket_path = os.environ.get("TGIT_REBASEI_SOCKET", SOCKET_PATH)
    timeout_s = int(os.environ.get("TGIT_REBASEI_TIMEOUT_S", str(DEFAULT_JOB_TIMEOUT_S)))

    req = {
        "action": "enqueue_job",
        "repo_path": _bridge_repo_path(),
        "todo_path": todo_path,
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


@mcp.tool()
async def step_git_rebasei(
    repo_path: str = ".",
    upstream: str = "HEAD~1",
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_S,
    todo_content: str | None = None,
) -> dict[str, Any]:
    """Single-entry state-machine tool for git rebase -i."""
    repo = _resolve_repo_path(repo_path)
    session = await SESSIONS.get_or_create(repo, timeout_seconds)

    if not session.running and session.result is None and session.pending_request is None:
        start_result = await _launch_git_rebasei(repo, upstream, timeout_seconds)
        if start_result.get("status") != "ok":
            return start_result

    if todo_content is not None:
        submit_result = await session.submit_todo_resolution(todo_content=todo_content)
        if submit_result.get("status") != "ok":
            return submit_result

    state = await session.get_state()
    if state.get("state") in {"completed", "error"}:
        await SESSIONS.remove_if_finished(repo)
    return state


@mcp.resource("rebasei://status")
async def rebasei_status() -> dict[str, Any]:
    """Return basic rebase editor server status."""
    return {
        "status": "ok",
        "socket_path": SOCKET_PATH,
        "server_running": _SOCKET_SERVER is not None,
    }


async def _run_mcp_server() -> None:
    await start_socket_server()
    run_async = getattr(mcp, "run_async", None)
    if not callable(run_async):
        raise RuntimeError("FastMCP.run_async is required for this async server.")

    try:
        await run_async()
    finally:
        await stop_socket_server()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="tgit async MCP server + rebase -i bridge")
    parser.add_argument("--bridge", action="store_true", help="Run as git sequence editor bridge")
    parser.add_argument("todo_path", nargs="?", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.bridge:
        if not args.todo_path:
            print("bridge mode requires todo file path", file=sys.stderr)
            return 2
        return _bridge_call(args.todo_path)

    asyncio.run(_run_mcp_server())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
