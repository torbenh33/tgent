import argparse
import asyncio
import os
import shlex
import sys
from typing import Any

from fastmcp import FastMCP
from interactive_workflow import InteractiveBridgeServer, InteractiveSession, InteractiveSessionManager, resolve_repo_path

mcp = FastMCP("tgit-rebasei-async")

SOCKET_PATH = os.environ.get("TGIT_INTERACTIVE_SOCKET", "/tmp/tgit-interactive.sock")
DEFAULT_JOB_TIMEOUT_S = int(os.environ.get("TGIT_INTERACTIVE_TIMEOUT_S", "900"))

SESSIONS = InteractiveSessionManager()


async def _git_command(repo_path: str, args: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        resolve_repo_path(repo_path),
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
        git_dir = os.path.abspath(os.path.join(resolve_repo_path(repo_path), git_dir))

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


async def _on_bridge_request_error(repo_path: str) -> None:
    await _abort_git_operation(repo_path)


BRIDGE_SERVER = InteractiveBridgeServer(
    sessions=SESSIONS,
    socket_path=SOCKET_PATH,
    on_request_error=_on_bridge_request_error,
)


def _build_bridge_cmd(repo_path: str) -> str:
    script = os.path.abspath(__file__)
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(script)} --bridge "
        f"--repo-path {shlex.quote(repo_path)}"
    )


def _is_recoverable_rebase_resolution_error(stderr: str) -> bool:
    text = (stderr or "").lower()
    return (
        "resolve all conflicts manually" in text
        or "after resolving the conflicts" in text
        or "git rebase --continue" in text
        or "could not apply" in text
    )


def _make_rebase_launcher(
    repo_path: str,
    upstream: str,
    timeout_seconds: int,
):
    repo = resolve_repo_path(repo_path, discover_git_root=True)
    chosen_upstream = upstream.strip() or "HEAD~1"

    async def _launcher(_session: Any) -> dict[str, Any]:
        await BRIDGE_SERVER.start()
        op = await _detect_operation(repo)
        cmd = ["git", "-C", repo, "rebase", "--continue"] if op == "rebase" else ["git", "-C", repo, "rebase", "-i", chosen_upstream]

        env = dict(os.environ)
        env.pop("GIT_SEQUENCE_EDITOR", None)
        env["GIT_EDITOR"] = _build_bridge_cmd(repo)
        env["TGIT_INTERACTIVE_SOCKET"] = SOCKET_PATH
        env["TGIT_INTERACTIVE_TIMEOUT_S"] = str(max(1, int(timeout_seconds)))

        session = await SESSIONS.get_or_create(repo, timeout_seconds)
        start_result = await session.start(command=cmd, env=env)
        start_result["socket_path"] = SOCKET_PATH
        start_result["upstream"] = chosen_upstream
        start_result["mode"] = "continue" if op == "rebase" else "start"
        return start_result

    return _launcher




@mcp.tool()
async def step_git_rebasei(
    repo_path: str = ".",
    upstream: str = "HEAD~1",
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_S,
    has_edit: bool = False,
    wait_for_change_seconds: int = 30,
    abort: bool = False,
) -> dict[str, Any]:
    """Drive an interactive `git rebase -i` workflow through one idempotent step call.

    Call this tool repeatedly for the same repo. The first call starts the rebase session;
    later calls advance it. The tool returns `needs_edit` when Git requests editor input and
    provides `edit_paths` to open and edit externally. After finishing edits, call again with
    `has_edit=true` to acknowledge completion and resume Git. Set `abort=true` to cancel an
    active merge/rebase/cherry-pick operation for the repo and clear finished session state.
    Terminal states are `completed` and `error`, which include final process output and return code.
    """
    repo = resolve_repo_path(repo_path, discover_git_root=True)

    if abort:
        session = await SESSIONS.pop(repo)
        session_state: dict[str, Any] | None = None
        if session is not None:
            session_state = await session.abort()

        await _abort_git_operation(repo)

        if session_state is not None:
            return session_state

        return {
            "status": "ok",
            "state": "aborted",
            "repo_path": repo,
            "message": "Aborted active git operation if present.",
        }

    launcher = _make_rebase_launcher(repo, upstream, timeout_seconds)
    session = await SESSIONS.get_or_create(repo, timeout_seconds, launcher=launcher)

    state = await session.step(
        has_edit=has_edit,
        wait_timeout_s=max(1, int(wait_for_change_seconds)),
    )

    if state.get("state") == "error" and _is_recoverable_rebase_resolution_error(str(state.get("stderr") or "")):
        await SESSIONS.remove_if_finished(repo)
        return {
            "status": "ok",
            "state": "needs_resolution",
            "repo_path": repo,
            "message": "Rebase paused due to conflicts. Resolve conflicts, stage files, then call step_git_rebasei again.",
            "stderr": state.get("stderr", ""),
        }

    if state.get("state") in {"completed", "error"}:
        await SESSIONS.remove_if_finished(repo)
    return state


@mcp.resource("rebasei://status")
async def rebasei_status() -> dict[str, Any]:
    """Return basic rebase editor server status."""
    return {
        "status": "ok",
        "socket_path": SOCKET_PATH,
        "server_running": BRIDGE_SERVER.running,
    }


async def _run_mcp_server() -> None:
    await BRIDGE_SERVER.start()
    run_async = getattr(mcp, "run_async", None)
    if not callable(run_async):
        raise RuntimeError("FastMCP.run_async is required for this async server.")

    try:
        await run_async()
    finally:
        await BRIDGE_SERVER.stop()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="tgit async MCP server + rebase -i bridge")
    parser.add_argument("--bridge", action="store_true", help="Run as git sequence editor bridge")
    parser.add_argument("--repo-path", default="", help="Repository path for bridge session lookup")
    parser.add_argument("edit_path", nargs="?", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.bridge:
        if not args.edit_path:
            print("bridge mode requires edit file path", file=sys.stderr)
            return 2
        return InteractiveSession.bridge_call(
            edit_paths=[args.edit_path],
            socket_path=os.environ.get("TGIT_INTERACTIVE_SOCKET", SOCKET_PATH),
            default_timeout_s=DEFAULT_JOB_TIMEOUT_S,
            repo_path=args.repo_path,
        )

    asyncio.run(_run_mcp_server())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
