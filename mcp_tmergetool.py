import argparse
import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("tgit-async")

SOCKET_PATH = os.environ.get("TGIT_MERGETOOL_SOCKET", "/tmp/tgit-mergetool.sock")
DEFAULT_JOB_TIMEOUT_S = int(os.environ.get("TGIT_MERGETOOL_TIMEOUT_S", "900"))


@dataclass
class MergeJob:
    repo_path: str
    base: str
    local: str
    remote: str
    merged: str
    timeout_s: int
    created_at: float = field(default_factory=time.time)
    claimed: bool = False
    done_future: asyncio.Future[dict[str, Any]] | None = None


@dataclass
class WorkflowRun:
    workflow: str
    repo_path: str
    command: list[str]
    process: asyncio.subprocess.Process
    output_task: asyncio.Task[tuple[bytes, bytes]]
    started_at: float = field(default_factory=time.time)


class MergeToolCoordinator:
    def __init__(self) -> None:
        self._incoming: asyncio.Queue[MergeJob] = asyncio.Queue(maxsize=1)
        self._active_job: MergeJob | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _is_live(job: MergeJob | None) -> bool:
        return job is not None and job.done_future is not None and not job.done_future.done()

    @staticmethod
    def _job_payload(job: MergeJob) -> dict[str, Any]:
        return {
            "repo_path": job.repo_path,
            "base": job.base,
            "local": job.local,
            "remote": job.remote,
            "merged": job.merged,
            "timeout_s": job.timeout_s,
            "created_at": job.created_at,
        }

    async def enqueue_and_wait(
        self,
        repo_path: str,
        base: str,
        local: str,
        remote: str,
        merged: str,
        timeout_s: int,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._is_live(self._active_job) or not self._incoming.empty():
                return {
                    "status": "error",
                    "message": "Another mergetool job is already active.",
                }

            loop = asyncio.get_running_loop()
            done_future: asyncio.Future[dict[str, Any]] = loop.create_future()
            job = MergeJob(
                repo_path=_resolve_repo_path(repo_path),
                base=base,
                local=local,
                remote=remote,
                merged=merged,
                timeout_s=max(1, timeout_s),
                done_future=done_future,
            )
            self._incoming.put_nowait(job)

        try:
            return await asyncio.wait_for(job.done_future, timeout=job.timeout_s)
        except asyncio.TimeoutError:
            await self._expire_job(job)
            await _abort_git_operation(job.repo_path)
            return {
                "status": "error",
                "message": "Timed out waiting for conflict resolution. Git operation aborted.",
                "repo_path": job.repo_path,
            }

    async def _expire_job(self, job: MergeJob) -> None:
        async with self._lock:
            if self._active_job is job:
                self._active_job = None
                return

            if not job.claimed and not self._incoming.empty():
                queued = self._incoming.get_nowait()
                if queued is not job:
                    self._incoming.put_nowait(queued)

    async def get_next_job(self, timeout_seconds: int) -> dict[str, Any]:
        async with self._lock:
            if self._is_live(self._active_job):
                return {"status": "ok", "job": self._job_payload(self._active_job)}

        timeout = max(0, int(timeout_seconds))
        try:
            if timeout == 0:
                job = self._incoming.get_nowait()
            else:
                job = await asyncio.wait_for(self._incoming.get(), timeout=timeout)
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return {"status": "ok", "job": None}

        async with self._lock:
            job.claimed = True
            self._active_job = job
            return {"status": "ok", "job": self._job_payload(job)}

    async def submit_resolution(self) -> dict[str, Any]:
        async with self._lock:
            job = self._active_job

        if not self._is_live(job):
            return {"status": "error", "message": "No active merge job."}

        try:
            with open(job.merged, "r", encoding="utf-8") as handle:
                merged_content = handle.read()
        except OSError as err:
            return {
                "status": "error",
                "message": "Failed to read merged file.",
                "error": str(err),
                "merged": job.merged,
            }

        validation_error = _validate_merged_content(merged_content)
        if validation_error is not None:
            return {"status": "error", "message": validation_error}

        if job.done_future is None or job.done_future.done():
            return {"status": "error", "message": "No active merge job."}

        job.done_future.set_result(
            {
                "status": "ok",
                "message": "Conflict resolved and merged file validated.",
                "repo_path": job.repo_path,
                "merged": job.merged,
            }
        )

        async with self._lock:
            if self._active_job is job:
                self._active_job = None

        return {"status": "ok", "message": "Resolution submitted."}

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            active = self._active_job
            has_live_active = self._is_live(active)
            queued = self._incoming.qsize()

        return {
            "status": "ok",
            "socket_path": SOCKET_PATH,
            "has_active_job": has_live_active,
            "queued_jobs": queued,
            "active_job": None
            if not has_live_active or active is None
            else self._job_payload(active),
        }


class GitWorkflowRunner:
    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], WorkflowRun] = {}

    def _key(self, workflow: str, repo_path: str) -> tuple[str, str]:
        return workflow, _resolve_repo_path(repo_path)

    async def start(
        self,
        workflow: str,
        repo_path: str,
        command: list[str],
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        repo = _resolve_repo_path(repo_path)
        key = self._key(workflow, repo)
        existing = self._runs.get(key)
        if existing is not None and not existing.output_task.done():
            return {
                "status": "error",
                "message": f"git {workflow} is already running for this repository.",
                "repo_path": repo,
                "pid": existing.process.pid,
            }

        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        output_task: asyncio.Task[tuple[bytes, bytes]] = asyncio.create_task(proc.communicate())
        self._runs[key] = WorkflowRun(
            workflow=workflow,
            repo_path=repo,
            command=command,
            process=proc,
            output_task=output_task,
        )

        return {
            "status": "ok",
            "message": f"git {workflow} started",
            "pid": proc.pid,
            "repo_path": repo,
        }

    async def collect(
        self,
        workflow: str,
        repo_path: str,
        wait: bool = True,
        timeout_seconds: int = 0,
        cleanup: bool = True,
    ) -> dict[str, Any]:
        repo = _resolve_repo_path(repo_path)
        key = self._key(workflow, repo)
        run = self._runs.get(key)
        if run is None:
            return {
                "status": "error",
                "message": f"No tracked git {workflow} run for this repository.",
                "repo_path": repo,
            }

        if not run.output_task.done():
            if not wait:
                return {
                    "status": "ok",
                    "running": True,
                    "pid": run.process.pid,
                    "repo_path": run.repo_path,
                }

            try:
                if int(timeout_seconds) > 0:
                    await asyncio.wait_for(asyncio.shield(run.output_task), timeout=int(timeout_seconds))
                else:
                    await run.output_task
            except asyncio.TimeoutError:
                return {
                    "status": "ok",
                    "running": True,
                    "pid": run.process.pid,
                    "repo_path": run.repo_path,
                    "message": "Still running.",
                }

        stdout, stderr = await run.output_task
        result = {
            "status": "ok" if run.process.returncode == 0 else "error",
            "running": False,
            "returncode": run.process.returncode,
            "repo_path": run.repo_path,
            "command": run.command,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }

        if cleanup:
            self._runs.pop(key, None)

        return result

    def list_runs(self, workflow: str | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for run in self._runs.values():
            if workflow is not None and run.workflow != workflow:
                continue
            result.append(
                {
                    "workflow": run.workflow,
                    "pid": run.process.pid,
                    "repo_path": run.repo_path,
                    "started_at": run.started_at,
                    "running": not run.output_task.done(),
                }
            )
        return result


COORDINATOR = MergeToolCoordinator()
WORKFLOW_RUNNER = GitWorkflowRunner()
_SOCKET_SERVER: asyncio.base_events.Server | None = None


def _resolve_repo_path(repo_path: str | None) -> str:
    candidate = repo_path.strip() if isinstance(repo_path, str) else ""
    return candidate or "."


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


def _validate_merged_content(content: str) -> str | None:
    if "<<<<<<<" in content or "=======" in content or ">>>>>>>" in content:
        return "Merged content still contains conflict markers."
    return None


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

        repo_path = str(req.get("repo_path") or ".")
        base = str(req.get("base") or "")
        local = str(req.get("local") or "")
        remote = str(req.get("remote") or "")
        merged = str(req.get("merged") or "")
        timeout_s = int(req.get("timeout_s") or DEFAULT_JOB_TIMEOUT_S)

        if not all([base, local, remote, merged]):
            await _write_json_line(writer, {"status": "error", "message": "Missing required merge tuple paths."})
            return

        result = await COORDINATOR.enqueue_and_wait(
            repo_path=repo_path,
            base=base,
            local=local,
            remote=remote,
            merged=merged,
            timeout_s=timeout_s,
        )
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
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(script)} "
        "--bridge \"$BASE\" \"$LOCAL\" \"$REMOTE\" \"$MERGED\""
    )


async def _launch_git_mergetool(
    repo_path: str,
    timeout_seconds: int,
    paths: list[str] | None,
) -> dict[str, Any]:
    await start_socket_server()

    repo = _resolve_repo_path(repo_path)
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

    selected_paths = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
    if selected_paths:
        cmd.extend(["--", *selected_paths])

    env = dict(os.environ)
    env["TGIT_MERGETOOL_SOCKET"] = SOCKET_PATH
    env["TGIT_MERGETOOL_TIMEOUT_S"] = str(max(1, int(timeout_seconds)))

    start_result = await WORKFLOW_RUNNER.start(
        workflow="mergetool",
        repo_path=repo,
        command=cmd,
        env=env,
    )
    if start_result.get("status") != "ok":
        return start_result

    start_result["socket_path"] = SOCKET_PATH
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


def _bridge_call(base: str, local: str, remote: str, merged: str) -> int:
    socket_path = os.environ.get("TGIT_MERGETOOL_SOCKET", SOCKET_PATH)
    timeout_s = int(os.environ.get("TGIT_MERGETOOL_TIMEOUT_S", str(DEFAULT_JOB_TIMEOUT_S)))

    req = {
        "action": "enqueue_job",
        "repo_path": _bridge_repo_path(),
        "base": base,
        "local": local,
        "remote": remote,
        "merged": merged,
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
        print(f"mergetool bridge socket error: {err}", file=sys.stderr)
        return 1

    if not data:
        print("mergetool bridge received empty response", file=sys.stderr)
        return 1

    try:
        resp = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        print("mergetool bridge received invalid JSON response", file=sys.stderr)
        return 1

    if resp.get("status") == "ok":
        return 0

    message = str(resp.get("message") or "mergetool job failed")
    print(message, file=sys.stderr)
    return 1


@mcp.tool()
async def get_next_merge_job(timeout_seconds: int = 30) -> dict[str, Any]:
    """Return active merge job; otherwise wait up to timeout_seconds for one."""
    return await COORDINATOR.get_next_job(timeout_seconds)


@mcp.tool()
async def submit_merge_resolution() -> dict[str, Any]:
    """Finish the active merge job by validating the merged file on disk."""
    return await COORDINATOR.submit_resolution()


@mcp.tool()
async def invoke_git_mergetool(
    repo_path: str = ".",
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_S,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    """Start git mergetool in the background and return immediately with a run_id."""
    return await _launch_git_mergetool(repo_path, timeout_seconds, paths)


@mcp.tool()
async def collect_git_mergetool_result(
    repo_path: str = ".",
    wait: bool = True,
    timeout_seconds: int = 0,
    cleanup: bool = True,
) -> dict[str, Any]:
    """Collect git mergetool process result/output for a repository."""
    repo = _resolve_repo_path(repo_path)

    return await WORKFLOW_RUNNER.collect(
        workflow="mergetool",
        repo_path=repo,
        wait=wait,
        timeout_seconds=timeout_seconds,
        cleanup=cleanup,
    )


@mcp.resource("mergetool://status")
async def mergetool_status() -> dict[str, Any]:
    """Return current in-memory mergetool status."""
    queue_status = await COORDINATOR.status()
    queue_status["active_runs"] = WORKFLOW_RUNNER.list_runs("mergetool")
    return queue_status


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
    parser = argparse.ArgumentParser(description="tgit async MCP server + mergetool bridge")
    parser.add_argument("--bridge", action="store_true", help="Run as git mergetool bridge")
    parser.add_argument("base", nargs="?", default="")
    parser.add_argument("local", nargs="?", default="")
    parser.add_argument("remote", nargs="?", default="")
    parser.add_argument("merged", nargs="?", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.bridge:
        if not all([args.base, args.local, args.remote, args.merged]):
            print("bridge mode requires BASE LOCAL REMOTE MERGED", file=sys.stderr)
            return 2
        return _bridge_call(args.base, args.local, args.remote, args.merged)

    asyncio.run(_run_mcp_server())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
