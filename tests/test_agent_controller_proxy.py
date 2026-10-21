"""Tests for the controller's model-completion proxy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agent_controller import AgentController, AgentControllerError


class FakeStore:
    def __init__(self) -> None:
        self.metadata = {"max_model_calls": 1}
        self.updates: list[dict[str, Any]] = []

    def get_metadata(self, _agent_id: str) -> dict[str, Any]:
        return dict(self.metadata)

    def update_metadata(self, _agent_id: str, values: dict[str, Any]) -> dict[str, Any]:
        self.updates.append(dict(values))
        self.metadata.update(values)
        return dict(self.metadata)


def test_model_complete_forwards_request_and_enforces_call_limit() -> None:
    request = {"messages": [{"role": "user", "content": "hello"}], "tools": []}
    response = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
    callback_requests: list[dict[str, Any]] = []

    def model_callback(params: dict[str, Any]) -> dict[str, Any]:
        callback_requests.append(params)
        return response

    controller = AgentController.__new__(AgentController)
    controller.model_callback = model_callback
    controller.store = FakeStore()
    runtime = SimpleNamespace(model_calls=0, lock=asyncio.Lock())
    controller._runtimes = {"test-agent": runtime}

    async def exercise() -> None:
        actual = await controller._model_complete("test-agent", request)
        assert actual == response
        assert callback_requests == [request]
        assert runtime.model_calls == 1
        assert controller.store.updates[0]["model_calls"] == 1

        with pytest.raises(AgentControllerError, match="budget exhausted"):
            await controller._model_complete("test-agent", request)
        assert callback_requests == [request]

    asyncio.run(exercise())
