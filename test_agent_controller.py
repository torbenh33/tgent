"""Tests for agent controller request validation and state boundaries."""

from __future__ import annotations

import pytest
from importlib.metadata import EntryPoint, EntryPoints
from unittest.mock import patch

from agent_controller import AgentController, AgentControllerError


def test_start_request_seeds_copilotchat_user_message() -> None:
    history, max_calls, max_runtime = AgentController._validate_start_request(
        {
            "schema_version": 1,
            "task": "Fix the failing test.",
        }
    )

    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Fix the failing test."
    assert isinstance(history[0]["id"], str)
    assert max_calls == 200
    assert max_runtime == 43200


def test_start_request_preserves_supplied_history_and_fields() -> None:
    transcript = [{"role": "assistant", "content": "answer", "id": "m1", "model": "test"}]

    history, max_calls, max_runtime = AgentController._validate_start_request(
        {
            "schema_version": 1,
            "history": transcript,
            "limits": {"max_model_calls": 7, "max_runtime_seconds": 90},
        }
    )

    assert history == transcript
    assert history[0] is transcript[0]
    assert max_calls == 7
    assert max_runtime == 90


def test_start_request_appends_task_after_existing_history() -> None:
    transcript = [{"role": "user", "content": "initial", "id": "u1"}]

    history, _calls, _runtime = AgentController._validate_start_request(
        {"schema_version": 1, "history": transcript, "task": "follow-up"}
    )

    assert history[0] is transcript[0]
    assert history[1]["content"] == "follow-up"


@pytest.mark.parametrize(
    "start_request, message",
    [
        ([], "object"),
        ({"schema_version": 2, "task": "task"}, "schema_version"),
        ({"schema_version": 1}, "Provide task"),
        ({"schema_version": 1, "task": " "}, "non-empty"),
        ({"schema_version": 1, "task": "task", "project": "/tmp"}, "Unknown"),
        ({"schema_version": 1, "task": "task", "limits": {"network": True}}, "limits"),
        ({"schema_version": 1, "task": "task", "limits": {"max_model_calls": 0}}, "positive integers"),
    ],
)
def test_rejects_invalid_start_requests(start_request: object, message: str) -> None:
    with pytest.raises(AgentControllerError, match=message):
        AgentController._validate_start_request(start_request)


@pytest.mark.anyio
async def test_agent_tool_allowlist_is_operator_set_and_worker_read_only(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mcp_example.py").write_text("mcp = object()\n", encoding="utf-8")

    class Store:
        def get_metadata(self, _agent_id):
            return {"state": "running", "snapshot_project": str(project)}

    controller = AgentController.__new__(AgentController)
    controller.store = Store()
    controller._tool_allowlists = {}
    controller._runtimes = {
        "agent": type("Runtime", (), {"process": type("Process", (), {"returncode": None})()})()
    }

    entry_points = [EntryPoint(name="mcp_example", value="mcp_example:mcp", group="tgit_agent.mcp_servers")]
    with patch("agent_controller.importlib.metadata.entry_points", return_value=EntryPoints(entry_points)):
        assert await controller._get_tool_allowlist("agent", {}) == {"tools": []}
        assert await controller.set_agent_tools("agent", ["mcp_example"]) == {
            "agent_id": "agent", "tools": ["mcp_example"]
        }
        assert await controller._get_tool_allowlist("agent", {}) == {"tools": ["mcp_example"]}
        for tools in (["mcp_agent"], ["mcp_missing"], ["../mcp_example"], ["mcp_example", "mcp_example"]):
            with pytest.raises(AgentControllerError):
                await controller.set_agent_tools("agent", tools)


@pytest.mark.anyio
async def test_stop_inactive_awaiting_review_agent_makes_it_terminal() -> None:
    class Store:
        metadata = {"agent_id": "agent", "state": "awaiting_review"}

        def get_metadata(self, _agent_id):
            return dict(self.metadata)

        def update_metadata(self, _agent_id, values):
            self.metadata.update(values)
            return dict(self.metadata)

    controller = AgentController.__new__(AgentController)
    controller.store = Store()
    controller._runtimes = {}

    result = await controller.stop_agent("agent")

    assert result["state"] == "stopped"
    assert result["stop_reason"] == "operator_stopped"


@pytest.mark.anyio
async def test_tool_allowlist_rejects_terminal_agent(tmp_path) -> None:
    class Store:
        def get_metadata(self, _agent_id):
            return {"state": "stopped", "snapshot_project": str(tmp_path)}

    controller = AgentController.__new__(AgentController)
    controller.store = Store()
    controller._tool_allowlists = {}
    with pytest.raises(AgentControllerError, match="live agent"):
        await controller.set_agent_tools("agent", [])


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["halted", "stopped"])
async def test_continue_inactive_agent_starts_child_from_snapshot(tmp_path, state) -> None:
    class Store:
        def get_metadata(self, _agent_id):
            return {"agent_id": "old", "state": state, "snapshot_root": str(snapshot_root),
                    "snapshot_project": str(snapshot_project), "name": "Original", "submitted_head": None}

        def get_history(self, _agent_id):
            return [{"role": "user", "content": "original"}]

        def update_metadata(self, _agent_id, _values):
            return {}

    snapshot_root = tmp_path / "snapshots" / "old"
    snapshot_project = snapshot_root / "project"
    snapshot_project.mkdir(parents=True)
    controller = AgentController.__new__(AgentController)
    controller.store = Store()
    controller.snapshot_root = tmp_path / "snapshots"
    controller._runtimes = {}
    calls = []

    async def start(request, *, source_root=None):
        calls.append((request, source_root))
        return {"status": "ok", "agent_id": "new", "state": "running"}

    controller.start_agent = start
    result = await controller.continue_agent("old", [{"role": "user", "content": "more"}])
    assert result["agent_id"] == "new"
    assert result["parent_agent_id"] == "old"
    assert calls[0][0]["history"] == [
        {"role": "user", "content": "original"}, {"role": "user", "content": "more"}
    ]
    assert calls[0][1] == snapshot_project


@pytest.mark.anyio
async def test_continue_stopped_agent_rejects_missing_or_unsafe_snapshot(tmp_path) -> None:
    class Store:
        def __init__(self, root, project):
            self.root, self.project = root, project

        def get_metadata(self, _agent_id):
            return {"agent_id": "old", "state": "stopped", "snapshot_root": str(self.root),
                    "snapshot_project": str(self.project), "submitted_head": None}

        def get_history(self, _agent_id):
            return []

    snapshots = tmp_path / "snapshots"
    outside = tmp_path / "outside"
    outside.mkdir()
    controller = AgentController.__new__(AgentController)
    controller.snapshot_root = snapshots
    controller._runtimes = {}
    controller.store = Store(snapshots / "old", outside)
    with pytest.raises(AgentControllerError, match="snapshot"):
        await controller.continue_agent("old", [{"role": "user", "content": "more"}])


@pytest.mark.parametrize("name", ["", "  ", "x" * 101, "bad\nname", 7])
def test_start_request_rejects_invalid_name(name: object) -> None:
    with pytest.raises(AgentControllerError, match="name"):
        AgentController._validate_start_request({"schema_version": 1, "task": "task", "name": name})


def test_start_request_accepts_bounded_human_name() -> None:
    history, _calls, _runtime = AgentController._validate_start_request(
        {"schema_version": 1, "task": "task", "name": "Investigate flaky tests"}
    )
    assert history[0]["content"] == "task"


def test_start_request_rejects_non_message_history_entries() -> None:
    with pytest.raises(AgentControllerError, match="message objects"):
        AgentController._validate_start_request(
            {"schema_version": 1, "history": ["not a message"]}
        )
