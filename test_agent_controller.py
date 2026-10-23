"""Tests for agent controller request validation and state boundaries."""

from __future__ import annotations

import pytest

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


def test_start_request_rejects_non_message_history_entries() -> None:
    with pytest.raises(AgentControllerError, match="message objects"):
        AgentController._validate_start_request(
            {"schema_version": 1, "history": ["not a message"]}
        )
