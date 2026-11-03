"""Tests for the controller's model-completion proxy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from llm import LLMClient

from agent_controller import AgentController, AgentControllerError


class FakeStore:
    def __init__(self) -> None:
        self.metadata = {"max_model_calls": 1, "model": "chosen-model", "reasoning_effort": "high"}
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
        assert callback_requests == [{**request, "model": "chosen-model", "reasoning_effort": "high"}]
        assert runtime.model_calls == 1
        assert controller.store.updates[0]["model_calls"] == 1

        with pytest.raises(AgentControllerError, match="budget exhausted"):
            await controller._model_complete("test-agent", request)
        assert callback_requests == [{**request, "model": "chosen-model", "reasoning_effort": "high"}]

    asyncio.run(exercise())


def test_chat_provider_propagates_configured_model_and_reasoning_effort(monkeypatch) -> None:
    client = LLMClient.__new__(LLMClient)
    client.model = "o3-mini"
    client.reasoning_effort = "none"
    client.chat_completions_options = {}
    captured = {}
    class Response:
        def model_dump(self, **_kwargs):
            return {"choices": []}
    def create(**kwargs):
        captured.update(kwargs)
        return Response()
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("llm.asyncio.to_thread", lambda fn, **kwargs: _immediate(fn, kwargs))
    asyncio.run(client.chat_completions_callback({"messages": [], "reasoning_effort": "high"}))
    assert captured["model"] == "o3-mini"
    assert captured["reasoning_effort"] == "high"


def _immediate(function, kwargs):
    async def result():
        return function(**kwargs)
    return result()


def test_unsupported_model_rejects_requested_reasoning_effort() -> None:
    client = LLMClient.__new__(LLMClient)
    client.model = "compatible-provider/model"
    client.reasoning_effort = "none"
    client.chat_completions_options = {}
    client.client = SimpleNamespace()
    with pytest.raises(ValueError, match="does not support reasoning_effort"):
        asyncio.run(client.chat_completions_callback({"messages": [], "reasoning_effort": "medium"}))
