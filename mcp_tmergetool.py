import argparse
import asyncio
import os
import shlex
import sys
from typing import Any

from fastmcp import FastMCP
from interactive_workflow import InteractiveBridgeServer, InteractiveSession, InteractiveSessionManager, resolve_repo_path

mcp = FastMCP("tgit-mergetool-async")

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


async def _has_unmerged_paths(repo_path: str) -> bool:
    rc, out, _ = await _git_command(repo_path, ["diff", "--name-only", "--diff-filter=U"])
    if rc != 0:
        return False
    return bool(out.strip())


async def _classify_unmerged_paths(repo_path: str) -> dict[str, list[str]]:
    rc, out, err = await _git_command(repo_path, ["ls-files", "-u", "--"])
    if rc != 0:
        raise RuntimeError(err.strip() or "git ls-files -u failed")

    stages_by_path: dict[str, set[int]] = {}
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        left, sep, path = line.partition("\t")
        if not sep or not path:
            continue

        parts = left.split()
        if len(parts) < 3:
            continue

        try:
            stage = int(parts[2])
        except ValueError:
            continue

        stages_by_path.setdefault(path, set()).add(stage)

    categorized: dict[str, list[str]] = {
        "deleted_by_us": [],
        "deleted_by_them": [],
        "both_added": [],
        "both_modified_or_complex": [],
    }

    for path, stages in sorted(stages_by_path.items()):
        has_2 = 2 in stages
        has_3 = 3 in stages

        if has_2 and not has_3:
            categorized["deleted_by_them"].append(path)
        elif has_3 and not has_2:
            categorized["deleted_by_us"].append(path)
        elif has_2 and has_3 and 1 not in stages:
            categorized["both_added"].append(path)
        else:
            categorized["both_modified_or_complex"].append(path)

    return categorized


async def _on_bridge_request_error(_repo_path: str) -> None:
    return None


BRIDGE_SERVER = InteractiveBridgeServer(
    sessions=SESSIONS,
    socket_path=SOCKET_PATH,
    on_request_error=_on_bridge_request_error,
)


def _build_bridge_cmd() -> str:
    script = os.path.abspath(__file__)
    return f"{shlex.quote(sys.executable)} {shlex.quote(script)} --bridge"


def _make_mergetool_launcher(
    repo_path: str,
    timeout_seconds: int,
):
    repo = resolve_repo_path(repo_path, discover_git_root=True)

    async def _launcher(_session: Any) -> dict[str, Any]:
        await BRIDGE_SERVER.start()

        if not await _has_unmerged_paths(repo):
            return {
                "status": "ok",
                "state": "completed",
                "repo_path": repo,
                "running": False,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "message": "No unmerged paths found.",
            }

        bridge_cmd = _build_bridge_cmd()
        cmd = [
            "git",
            "-C",
            repo,
            "-c",
            "merge.tool=tgit_mcp",
            "-c",
            f"mergetool.tgit_mcp.cmd={bridge_cmd}",
            "-c",
            "mergetool.tgit_mcp.trustExitCode=true",
            "mergetool",
            "--no-prompt",
        ]

        env = dict(os.environ)
        env["TGIT_INTERACTIVE_SOCKET"] = SOCKET_PATH
        env["TGIT_INTERACTIVE_TIMEOUT_S"] = str(max(1, int(timeout_seconds)))

        session = await SESSIONS.get_or_create(repo, timeout_seconds)
        start_result = await session.start(command=cmd, env=env)
        start_result["socket_path"] = SOCKET_PATH
        start_result["mode"] = "start"
        return start_result

    return _launcher


@mcp.tool()
async def step_git_mergetool(
    repo_path: str = ".",
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_S,
    has_edit: bool = False,
    wait_for_change_seconds: int = 30,
) -> dict[str, Any]:
    """Drive a `git mergetool` workflow through one idempotent step call.

    Call this tool repeatedly for the same repo. The first call starts the session;
    later calls advance it. The tool returns `needs_edit` when an editor bridge
    request is active and provides `edit_paths` to open and edit externally. After
    finishing edits, call again with `has_edit=true` to acknowledge completion.
    Terminal states are `completed` and `error`.
    """
    repo = resolve_repo_path(repo_path, discover_git_root=True)
    launcher = _make_mergetool_launcher(repo, timeout_seconds)
    session = await SESSIONS.get_or_create(repo, timeout_seconds, launcher=launcher)

    state = await session.step(
        has_edit=has_edit,
        wait_timeout_s=max(1, int(wait_for_change_seconds)),
    )

    if state.get("state") in {"completed", "error"}:
        await SESSIONS.remove_if_finished(repo)
    return state


@mcp.tool()
async def inspect_unmerged_conflicts(repo_path: str = ".") -> dict[str, Any]:
    """Classify unmerged paths, including delete/modify conflict indicators."""
    repo = resolve_repo_path(repo_path, discover_git_root=True)
    try:
        conflicts = await _classify_unmerged_paths(repo)
    except RuntimeError as err:
        return {
            "status": "error",
            "state": "error",
            "repo_path": repo,
            "message": str(err),
        }

    manual_choice_paths = sorted(set(conflicts["deleted_by_us"] + conflicts["deleted_by_them"]))
    return {
        "status": "ok",
        "state": "inspected",
        "repo_path": repo,
        "has_unmerged": any(conflicts.values()),
        "needs_manual_delete_modify_resolution": bool(manual_choice_paths),
        "manual_choice_paths": manual_choice_paths,
        "conflicts": conflicts,
    }


@mcp.resource("mergetool://status")
async def mergetool_status() -> dict[str, Any]:
    """Return basic mergetool server status."""
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
    parser = argparse.ArgumentParser(description="tgit async MCP server + mergetool bridge")
    parser.add_argument("--bridge", action="store_true", help="Run as bridge callback")
    parser.add_argument("edit_paths", nargs="*", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.bridge:
        edit_paths = [p for p in args.edit_paths if isinstance(p, str) and p.strip()]
        if not edit_paths:
            print("bridge mode requires at least one edit path", file=sys.stderr)
            return 2
        return InteractiveSession.bridge_call(
            edit_paths=edit_paths,
            socket_path=os.environ.get("TGIT_INTERACTIVE_SOCKET", SOCKET_PATH),
            default_timeout_s=DEFAULT_JOB_TIMEOUT_S,
        )

    asyncio.run(_run_mcp_server())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
