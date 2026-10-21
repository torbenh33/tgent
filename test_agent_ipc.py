"""Tests for the controller/worker Unix-socket JSON-RPC transport."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from agent_ipc import (
    AgentControlServer,
    AgentIPCError,
    AgentWorkerClient,
    JsonRpcRemoteError,
)


def test_worker_request_is_dispatched_and_replied_to(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "private" / "worker.sock"
        seen: list[dict[str, object]] = []

        async def model_complete(params: dict[str, object]) -> dict[str, object]:
            seen.append(params)
            return {"content": "answer"}

        server = AgentControlServer(socket_path, {"model.complete": model_complete})
        await server.start()
        try:
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            client = AgentWorkerClient(socket_path)
            await client.connect()
            try:
                await server.wait_for_worker(timeout_seconds=1)
                result = await client.request("model.complete", {"messages": [{"role": "user"}]})
                assert result == {"content": "answer"}
                assert seen == [{"messages": [{"role": "user"}]}]
            finally:
                await client.close()
        finally:
            await server.stop()
        assert not socket_path.exists()

    asyncio.run(scenario())


def test_unknown_method_returns_jsonrpc_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "ipc" / "worker.sock"
        server = AgentControlServer(socket_path, {})
        await server.start()
        client = AgentWorkerClient(socket_path)
        try:
            await client.connect()
            with pytest.raises(JsonRpcRemoteError) as error:
                await client.request("unregistered.method")
            assert error.value.code == -32601
        finally:
            await client.close()
            await server.stop()

    asyncio.run(scenario())


def test_handler_exception_is_returned_as_rpc_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "ipc" / "worker.sock"

        def fail(_params: dict[str, object]) -> None:
            raise ValueError("rejected request")

        server = AgentControlServer(socket_path, {"fail": fail})
        await server.start()
        client = AgentWorkerClient(socket_path)
        try:
            await client.connect()
            with pytest.raises(JsonRpcRemoteError, match="rejected request"):
                await client.request("fail")
        finally:
            await client.close()
            await server.stop()

    asyncio.run(scenario())


def test_client_rejects_oversized_request_before_sending(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "ipc" / "worker.sock"
        server = AgentControlServer(socket_path, {}, max_message_bytes=1024)
        await server.start()
        client = AgentWorkerClient(socket_path, max_message_bytes=1024)
        try:
            await client.connect()
            with pytest.raises(AgentIPCError, match="exceeds 1024 byte limit"):
                await client.request("large", {"payload": "x" * 2048})
        finally:
            await client.close()
            await server.stop()

    asyncio.run(scenario())


def test_server_refuses_insecure_socket_directory(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_dir = tmp_path / "public"
        socket_dir.mkdir(mode=0o755)
        socket_dir.chmod(0o755)
        server = AgentControlServer(socket_dir / "worker.sock", {})
        with pytest.raises(AgentIPCError, match="mode 0700"):
            await server.start()

    asyncio.run(scenario())


def test_server_refuses_to_replace_non_socket_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "ipc" / "worker.sock"
        socket_path.parent.mkdir(mode=0o700)
        socket_path.write_text("keep", encoding="utf-8")
        server = AgentControlServer(socket_path, {})
        with pytest.raises(AgentIPCError, match="non-socket"):
            await server.start()
        assert socket_path.read_text(encoding="utf-8") == "keep"

    asyncio.run(scenario())
