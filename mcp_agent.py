"""MCP management server for sandboxed project agents."""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from agent_controller import AgentController
from agent_sandbox import SandboxPolicy
from llm import LLMClient
from remote_agent_adapter import RemoteAgentAdapter
from remote_agent_config import load_remote_agent_config

mcp = FastMCP("tgit-agent-controller")

_PROJECT_ROOT = Path.cwd().resolve()
_STATE_ROOT = Path(
    os.environ.get("TGIT_AGENT_STATE_DIR")
    or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tgit-agent"
).expanduser()
_SNAPSHOT_ROOT = Path(
    os.environ.get("TGIT_AGENT_SNAPSHOT_DIR") or _PROJECT_ROOT.parent / ".agent-snapshots"
).expanduser()
_LLM_CONFIG = os.environ.get("TGIT_AGENT_LLM_CONFIG", "config.ini")
_CONTROLLER: AgentController | None = None
_LLM: LLMClient | None = None
_REMOTE_ADAPTER: RemoteAgentAdapter | None = None


def _get_llm() -> LLMClient:
    global _LLM
    if _LLM is None:
        _LLM = LLMClient(_LLM_CONFIG)
    return _LLM


async def _submit_patch(snapshot: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Use the existing review MCP implementation to export snapshot commits."""
    module = importlib.import_module("mcp_tgit_review")
    export = getattr(module, "export_patch_series", None)
    if not callable(export):
        raise RuntimeError("mcp_tgit_review.export_patch_series is unavailable")
    result = await asyncio.to_thread(
        export,
        base=snapshot.base_commit,
        head=params["head_commit"],
        repo_path=str(snapshot.project_root),
    )
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise RuntimeError(f"Patch queue export failed: {result}")
    return result

def _remote_adapter() -> RemoteAgentAdapter | None:
    """Load remote deployment settings only when remote mode is enabled."""
    global _REMOTE_ADAPTER
    if os.environ.get("TGIT_AGENT_REMOTE", "").casefold() not in {"1", "true", "yes"}:
        return None
    if _REMOTE_ADAPTER is None:
        _REMOTE_ADAPTER = RemoteAgentAdapter(load_remote_agent_config())
    return _REMOTE_ADAPTER


def _controller() -> AgentController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = AgentController(
            project_root=_PROJECT_ROOT,
            state_root=_STATE_ROOT,
            snapshot_root=_SNAPSHOT_ROOT,
            model_callback=_get_llm().model_callback,
            patch_submit_callback=_submit_patch,
            sandbox_policy=SandboxPolicy(),
            max_model_calls=int(os.environ.get("TGIT_AGENT_MAX_MODEL_CALLS", "200")),
            max_runtime_seconds=int(os.environ.get("TGIT_AGENT_MAX_RUNTIME_SECONDS", "43200")),
        )
    return _CONTROLLER


@mcp.tool()
async def start_agent(request: dict[str, Any]) -> dict[str, Any]:
    """Start an isolated agent for the controller's current Git project.

    Request schema: {"schema_version": 1, "task": "...", "history": [],
    "limits": {"max_model_calls": 200, "max_runtime_seconds": 43200}}.
    The project root is the directory from which this MCP server was launched.
    """
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.start, _PROJECT_ROOT, request)
    return await _controller().start_agent(request)


@mcp.tool()
async def get_agent_status(agent_id: str) -> dict[str, Any]:
    """Return persisted controller metadata for an agent."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.status, agent_id)
    return await _controller().get_status(agent_id)


@mcp.tool()
async def get_agent_log(agent_id: str, max_bytes: int = 65536) -> dict[str, Any]:
    """Return the bounded tail of a worker's captured stderr log."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.logs, agent_id, max_bytes=max_bytes)
    return await _controller().get_agent_log(agent_id, max_bytes=max_bytes)


@mcp.tool()
async def get_agent_history(agent_id: str) -> list[dict[str, Any]]:
    """Return the complete CopilotChat-format conversation message array."""
    remote = _remote_adapter()
    if remote is not None:
        result = await asyncio.to_thread(remote.history, agent_id)
        return result["messages"]
    return await _controller().get_history(agent_id)


@mcp.tool()
async def continue_agent(agent_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Append operator or review messages and resume a live waiting agent."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.continue_agent, agent_id, messages)
    return await _controller().continue_agent(agent_id, messages)


@mcp.tool()
async def stop_agent(agent_id: str) -> dict[str, Any]:
    """Request worker shutdown and wait for its process to exit."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.stop, agent_id)
    return await _controller().stop_agent(agent_id)


@mcp.tool()
async def reap_agent(agent_id: str) -> dict[str, Any]:
    """Remove a stopped agent's snapshot and controller state."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.reap, agent_id)
    return await _controller().reap_agent(agent_id)


@mcp.tool()
async def retrieve_agent_patch(agent_id: str, destination: str) -> dict[str, Any]:
    """Download a submitted remote patch artifact for local review.

    Returns the local artifact path, or ``available: false`` if the agent has
    not submitted a patch yet. Patch retrieval is available in remote mode.
    """
    remote = _remote_adapter()
    if remote is None:
        return {"agent_id": agent_id, "available": False, "message": "Patch retrieval requires remote mode."}
    path = await asyncio.to_thread(remote.retrieve_patch, agent_id, destination)
    if path is None:
        return {"agent_id": agent_id, "available": False}
    return {"agent_id": agent_id, "available": True, "path": str(path)}


async def _run() -> None:
    controller = None if _remote_adapter() is not None else _controller()
    run_async = getattr(mcp, "run_async", None)
    if not callable(run_async):
        raise RuntimeError("FastMCP.run_async is required for this async server")
    try:
        await run_async()
    finally:
        if controller is not None:
            await controller.shutdown()


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
