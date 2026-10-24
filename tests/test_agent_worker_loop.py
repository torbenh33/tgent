"""Fast, deterministic tests for the agent worker model/tool loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_controller import AgentController
from agent_worker import AgentWorker


class FakeWorkerClient:
    """In-memory controller/IPC stand-in with predetermined model responses."""

    def __init__(self, history: list[dict[str, Any]], completions: list[dict[str, Any]]) -> None:
        self.history = list(history)
        self.completions = list(completions)
        self.model_requests: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []
        self.awaited_commands: list[dict[str, Any]] = []
        self.reported_tool_catalogs: list[dict[str, Any]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "agent.tools.get":
            return {"tools": []}
        if method == "agent.tools.report":
            self.reported_tool_catalogs.append(params)
            return {"reported": len(params["modules"])}
        if method == "worker.hello":
            return {"protocol_version": 1, "agent_id": params["agent_id"]}
        if method == "history.get":
            return {"history": list(self.history)}
        if method == "history.append":
            messages = params["messages"]
            self.history.extend(messages)
            return {"appended": len(messages), "message_count": len(self.history)}
        if method == "agent.update_state":
            self.states.append(dict(params))
            return {"state": params["state"]}
        if method == "model.complete":
            self.model_requests.append(params)
            return self.completions.pop(0)
        if method == "agent.await_command":
            command = {"command": "shutdown"}
            self.awaited_commands.append(dict(params))
            return command
        raise AssertionError(f"Unexpected IPC method: {method}")


def test_worker_runs_tool_call_then_final_response_without_live_model(tmp_path: Path) -> None:
    project_file = tmp_path / "hello.txt"
    project_file.write_text("hello from the project\n", encoding="utf-8")
    tool_call = {
        "id": "call-read-file",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": json.dumps({"path": "hello.txt"}),
        },
    }
    client = FakeWorkerClient(
        history=[{"role": "user", "content": "Read hello.txt", "id": "user-message"}],
        completions=[
            {
                "model": "fake-model",
                "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tool_call]}}],
            },
            {
                "model": "fake-model",
                "choices": [{"message": {"role": "assistant", "content": "The file says hello."}}],
            },
        ],
    )
    worker = AgentWorker(client, tmp_path, "test-agent")  # type: ignore[arg-type]

    asyncio.run(worker.run())

    assert len(client.model_requests) == 2
    assert client.model_requests[0]["tools"]
    assert [message["role"] for message in client.history] == ["user", "assistant", "tool", "assistant"]
    assert client.history[1]["tool_calls"] == [tool_call]
    tool_result = json.loads(client.history[2]["content"])
    assert tool_result["content"] == "hello from the project"
    assert client.history[3]["content"] == "The file says hello."
    assert any(message.get("role") == "tool" for message in client.model_requests[1]["messages"])
    assert client.awaited_commands
    assert client.states[-1]["state"] == "halted"


class _IntegrationStore:
    def __init__(self) -> None:
        self.metadata = {"max_model_calls": 4}
        self.updates: list[dict[str, Any]] = []

    def get_metadata(self, _agent_id: str) -> dict[str, Any]:
        return dict(self.metadata)

    def update_metadata(self, _agent_id: str, values: dict[str, Any]) -> dict[str, Any]:
        self.updates.append(dict(values))
        self.metadata.update(values)
        return dict(self.metadata)


class _ControllerWorkerBridge:
    """Route worker IPC to controller handlers while keeping state in memory."""

    def __init__(self, controller: AgentController, history: list[dict[str, Any]]) -> None:
        self.controller = controller
        self.history = list(history)
        self.states: list[dict[str, Any]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "agent.tools.get":
            return {"tools": []}
        if method == "agent.tools.report":
            return await self.controller._report_tool_catalog("test-agent", params)
        if method == "worker.hello":
            return await self.controller._hello("test-agent", params)
        if method == "history.get":
            return {"history": list(self.history)}
        if method == "history.append":
            messages = params["messages"]
            self.history.extend(messages)
            return {"appended": len(messages), "message_count": len(self.history)}
        if method == "model.complete":
            return await self.controller._model_complete("test-agent", params)
        if method == "agent.update_state":
            self.states.append(dict(params))
            return {"state": params["state"]}
        if method == "agent.await_command":
            return {"command": "shutdown"}
        raise AssertionError(f"Unexpected IPC method: {method}")


def test_worker_loop_uses_controller_model_proxy_without_live_model(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello through the controller\n", encoding="utf-8")
    tool_call = {
        "id": "call-read-file",
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "hello.txt"})},
    }
    completions = [
        {
            "model": "fake-model",
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tool_call]}}],
        },
        {
            "model": "fake-model",
            "choices": [{"message": {"role": "assistant", "content": "The controller-proxied tool found the file."}}],
        },
    ]
    model_requests: list[dict[str, Any]] = []

    def fake_model_callback(params: dict[str, Any]) -> dict[str, Any]:
        model_requests.append(params)
        return completions.pop(0)

    controller = AgentController.__new__(AgentController)
    controller.model_callback = fake_model_callback
    controller.store = _IntegrationStore()
    controller._tool_catalogs = {}
    runtime = SimpleNamespace(model_calls=0, lock=asyncio.Lock())
    controller._runtimes = {"test-agent": runtime}
    bridge = _ControllerWorkerBridge(
        controller,
        [{"role": "user", "content": "Read hello.txt", "id": "user-message"}],
    )
    worker = AgentWorker(bridge, tmp_path, "test-agent")  # type: ignore[arg-type]

    asyncio.run(worker.run())

    assert len(model_requests) == 2
    assert runtime.model_calls == 2
    assert controller.store.metadata["model_calls"] == 2
    assert [message["role"] for message in bridge.history] == ["user", "assistant", "tool", "assistant"]
    tool_result = json.loads(bridge.history[2]["content"])
    assert tool_result["content"] == "hello through the controller"
    assert any(message.get("role") == "tool" for message in model_requests[1]["messages"])
    assert bridge.history[3]["content"] == "The controller-proxied tool found the file."
    assert bridge.states[-1]["state"] == "halted"


def test_worker_resumes_after_patch_review_feedback() -> None:
    head_commit = "a" * 40
    patch_call = {
        "id": "call-submit-patch",
        "type": "function",
        "function": {
            "name": "submit_patch_queue",
            "arguments": json.dumps({"head_commit": head_commit}),
        },
    }
    completions = [
        {
            "model": "fake-model",
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [patch_call]}}],
        },
        {
            "model": "fake-model",
            "choices": [{"message": {"role": "assistant", "content": "Review feedback addressed."}}],
        },
    ]

    class ReviewWorkerClient:
        def __init__(self) -> None:
            self.history: list[dict[str, Any]] = [
                {"role": "user", "content": "Prepare a patch", "id": "user-message"}
            ]
            self.model_requests: list[dict[str, Any]] = []
            self.states: list[dict[str, Any]] = []
            self.await_count = 0

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "agent.tools.get":
                return {"tools": []}
            if method == "agent.tools.report":
                return {"reported": len(params["modules"])}
            if method == "worker.hello":
                return {"protocol_version": 1, "agent_id": params["agent_id"]}
            if method == "history.get":
                return {"history": list(self.history)}
            if method == "history.append":
                messages = params["messages"]
                self.history.extend(messages)
                return {"appended": len(messages), "message_count": len(self.history)}
            if method == "agent.update_state":
                self.states.append(dict(params))
                return {"state": params["state"]}
            if method == "model.complete":
                self.model_requests.append(params)
                return completions.pop(0)
            if method == "patch.submit":
                assert params["head_commit"] == head_commit
                return {"status": "ok"}
            if method == "agent.await_command":
                self.await_count += 1
                if self.await_count == 1:
                    self.history.append(
                        {"role": "user", "content": "Please address the naming feedback.", "id": "review-feedback"}
                    )
                    return {"command": "review_feedback"}
                return {"command": "shutdown"}
            raise AssertionError(f"Unexpected IPC method: {method}")

    client = ReviewWorkerClient()
    worker = AgentWorker(client, Path.cwd(), "test-agent")  # type: ignore[arg-type]

    asyncio.run(worker.run())

    assert len(client.model_requests) == 2
    assert client.await_count == 2
    assert [state["state"] for state in client.states] == [
        "running",
        "awaiting_review",
        "running",
        "halted",
        "halted",
    ]
    assert [message["role"] for message in client.history] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    patch_result = json.loads(client.history[2]["content"])
    assert patch_result == {"status": "ok"}
    assert any(
        message.get("content") == "Please address the naming feedback."
        for message in client.model_requests[1]["messages"]
    )
    assert client.history[-1]["content"] == "Review feedback addressed."


def test_worker_discovers_mcp_tools_through_allowlist_rpc(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "mcp_demo.py").write_text(
        """
from types import SimpleNamespace

class FakeMCP:
    async def list_tools(self):
        return [SimpleNamespace(name='echo', inputSchema={'type': 'object', 'properties': {'value': {'type': 'string'}}}, description='Echo input')]

    async def call_tool(self, name, arguments):
        return {'tool': name, 'arguments': arguments}

    async def list_resources(self):
        return []

    async def read_resource(self, uri):
        return None

mcp = FakeMCP()
""",
        encoding="utf-8",
    )

    class AllowlistClient:
        def __init__(self) -> None:
            self.methods: list[str] = []

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.methods.append(method)
            if method == "agent.tools.report":
                self.reported_catalog = params["modules"]
                return {"reported": len(self.reported_catalog)}
            assert method == "agent.tools.get"
            return {"tools": ["mcp_demo"]}

    monkeypatch.syspath_prepend(str(tmp_path))
    client = AllowlistClient()
    worker = AgentWorker(client=client, root=tmp_path, agent_id="agent")  # type: ignore[arg-type]
    entry_point = SimpleNamespace(name="mcp_demo", load=lambda: __import__("mcp_demo").mcp)

    async def exercise() -> None:
        with patch("agent_worker.importlib.metadata.entry_points", return_value=SimpleNamespace(select=lambda **_kwargs: [entry_point])):
            await worker._discover_mcp_tools()
        assert client.methods == ["agent.tools.get", "agent.tools.report"]
        assert client.reported_catalog[0]["name"] == "mcp_demo"
        assert [tool["name"] for tool in client.reported_catalog[0]["items"]] == ["echo"]
        assert "mcp_demo__echo" in worker.dynamic_tools
        tool_names = {item["function"]["name"] for item in worker.available_tools}
        assert "mcp_demo__echo" in tool_names
        assert "mcp_demo__echo" in worker._api_messages([])[0]["content"]
        result = await worker._execute_tool("mcp_demo__echo", {"value": "hello"})
        assert result == {"tool": "echo", "arguments": {"value": "hello"}}

    asyncio.run(exercise())
