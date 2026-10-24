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
_LLM_CONFIG = os.environ.get("TGIT_AGENT_LLM_CONFIG", "config.toml")
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

    Request schema: {"schema_version": 1, "task": "...", "name": "optional label",
    "history": [], "limits": {"max_model_calls": 200, "max_runtime_seconds": 43200}}.
    The project root is the directory from which this MCP server was launched.
    """
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.start, _PROJECT_ROOT, request)
    return await _controller().start_agent(request)


@mcp.resource("agent://status")
async def agent_status_default_resource() -> list[dict[str, Any]]:
    """List agents with names and statuses for a quick operator overview."""
    return await list_remote_agents()


@mcp.resource("agent://status/{agent_id}")
async def agent_status_resource(agent_id: str) -> dict[str, Any]:
    """Convenient read-only resource for one agent's controller status."""
    return await get_agent_status(agent_id)


@mcp.resource("agents://")
async def agents_resource() -> list[dict[str, Any]]:
    """Convenient read-only resource listing agent names and lifecycle status."""
    return await list_remote_agents()


@mcp.tool()
async def list_remote_agents() -> list[dict[str, Any]]:
    """List all agents managed by the local or remote controller."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.list_agents)
    return await _controller().list_agents()


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
async def summarize_agent_history(agent_id: str) -> str:
    """Summarize using the controller-side LLM (remote service LLM in remote mode)."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.summarize_history, agent_id)
    history = await _controller().get_history(agent_id)
    import json
    prompt_history = json.dumps(history[-200:], ensure_ascii=False)[-100_000:]
    response = await _get_llm().model_callback({"messages": [
        {"role": "system", "content": "Summarize this coding agent conversation for an operator: task, key actions and decisions, outcome, unresolved issues, and evidence. Be concise and distinguish facts from uncertainty."},
        {"role": "user", "content": prompt_history},
    ], "max_tokens": 1200})
    try:
        content = response["choices"][0]["message"]["content"]
        if not isinstance(content, str): raise TypeError("content is not text")
        return content
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM returned an invalid conversation summary response") from exc

@mcp.tool()
async def continue_agent(agent_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Append operator or review messages and resume a live waiting agent."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.continue_agent, agent_id, messages)
    return await _controller().continue_agent(agent_id, messages)


@mcp.tool()
async def get_agent_tools(agent_id: str) -> dict[str, Any]:
    """List all discovered project MCP tools/resources and their enabled status."""
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.get_agent_tools, agent_id)
    return await _controller().get_agent_tools(agent_id)


@mcp.tool()
async def set_agent_tools(agent_id: str, tools: list[str]) -> dict[str, Any]:
    """Enable project MCP modules for a live agent (operator-only controller tool).

    Supply module stems such as ["mcp_example"]. Workers cannot invoke this API.
    """
    remote = _remote_adapter()
    if remote is not None:
        return await asyncio.to_thread(remote.set_agent_tools, agent_id, tools)
    return await _controller().set_agent_tools(agent_id, tools)


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
async def review_remote_agent(agent_id: str, artifact_directory: str) -> dict[str, Any]:
    """Retrieve a complete remote mbox and import it into local review state.

    The retained artifact is kept in ``artifact_directory`` for audit. Repeated
    calls for the same artifact digest are idempotent.
    """
    remote = _remote_adapter()
    if remote is None:
        return {"status": "error", "message": "Remote review requires remote mode."}
    metadata = await asyncio.to_thread(remote.patches, agent_id)
    if metadata.get("available") is not True:
        return {"status": "unavailable", "agent_id": agent_id}
    artifact = await asyncio.to_thread(remote.retrieve_patch, agent_id, artifact_directory)
    if artifact is None:
        return {"status": "unavailable", "agent_id": agent_id}
    import_review = getattr(importlib.import_module("mcp_tgit_review"), "import_patch_series")
    import hashlib
    raw = artifact.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != metadata.get("sha256"):
        return {"status": "error", "message": "Retrieved artifact digest does not match remote metadata.", "artifact": str(artifact)}
    result = await asyncio.to_thread(
        import_review, raw, base=metadata.get("base_commit"),
        submitted_head=metadata.get("submitted_head"), repo_path=str(_PROJECT_ROOT),
        series_id=f"remote-{agent_id}", artifact_sha256=digest,
    )
    return {**result, "agent_id": agent_id, "artifact": str(artifact), "artifact_sha256": digest}


@mcp.tool()
async def import_pending_remote_agent_patches(artifact_directory: str) -> dict[str, Any]:
    """Import available remote submissions, reporting each agent independently."""
    remote = _remote_adapter()
    if remote is None:
        return {"status": "error", "message": "Remote review requires remote mode.", "agents": []}
    agents = await asyncio.to_thread(remote.list_agents)
    results: list[dict[str, Any]] = []
    for agent in agents:
        agent_id = agent.get("agent_id") or agent.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        try:
            result = await review_remote_agent(agent_id, artifact_directory)
            if result.get("status") == "unavailable":
                result = {**result, "status": "skipped", "reason": "no-submission"}
            elif result.get("idempotent"):
                result = {**result, "status": "skipped", "reason": "already-imported"}
            results.append(result)
        except Exception as err:
            results.append({"agent_id": agent_id, "status": "error", "message": str(err)})
    return {"status": "ok", "agents": results}


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
