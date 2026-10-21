"""Tests for durable controller-side agent storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_store import AgentStore, AgentStoreError, validate_agent_id


def test_create_and_read_metadata_and_history_without_conversion(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "state")
    metadata = {"agent_id": "agent-1", "state": "starting", "updated_at": 1}
    history = [
        {
            "role": "assistant",
            "content": "##buffer://file.py?version=2",
            "id": "message-1",
            "model": "example-model",
            "reasoning": "",
        }
    ]

    store.create("agent-1", metadata, history)

    assert store.get_metadata("agent-1") == metadata
    assert store.get_history("agent-1") == history
    assert json.loads(store.history_path("agent-1").read_text(encoding="utf-8")) == history


def test_append_history_preserves_all_message_fields_and_order(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "state")
    initial = [{"role": "user", "content": "task", "id": "u1"}]
    appended = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}], "id": "a1"},
        {"role": "tool", "content": "result", "tool_call_id": "call-1", "id": "t1"},
    ]
    store.create("a1", {"agent_id": "a1"}, initial)

    result = store.append_history("a1", appended)

    assert result == initial + appended
    assert store.get_history("a1") == initial + appended


def test_update_metadata_merges_and_updates_timestamp(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "state")
    store.create("agent", {"state": "starting", "custom": 7}, [])

    updated = store.update_metadata("agent", {"state": "running"})

    assert updated["state"] == "running"
    assert updated["custom"] == 7
    assert isinstance(updated["updated_at"], float)


def test_create_refuses_existing_agent_id(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "state")
    store.create("same", {}, [])

    with pytest.raises(AgentStoreError, match="already exists"):
        store.create("same", {}, [])


def test_rejects_path_traversal_agent_ids() -> None:
    for value in ("../outside", "/absolute", "with space", "", "a.b"):
        with pytest.raises(AgentStoreError, match="Invalid agent_id"):
            validate_agent_id(value)


def test_rejects_invalid_message_shapes(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "state")

    with pytest.raises(AgentStoreError, match="message object"):
        store.create("bad", {}, ["not a message"])  # type: ignore[list-item]

    store.create("good", {}, [])
    with pytest.raises(AgentStoreError, match="messages must be"):
        store.append_history("good", [{"role": "assistant"}, "invalid"])  # type: ignore[list-item]


def test_remove_state_only_after_explicit_call(tmp_path: Path) -> None:
    store = AgentStore(tmp_path / "state")
    store.create("agent", {"state": "reaped"}, [])
    history_path = store.history_path("agent")

    store.remove("agent")

    assert not history_path.exists()
    assert not (tmp_path / "state" / "agent").exists()
