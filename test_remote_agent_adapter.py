"""Tests for the local remote-agent upload adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

import remote_agent_adapter as adapter_module
from remote_agent_adapter import DEFAULT_RSYNC_EXCLUDES, RemoteAgentAdapter, RemoteAgentAdapterError
from remote_agent_config import RemoteAgentConfig


class _FakeClient:
    def __init__(self, responses=None):
        self.messages = []
        self.responses = list(responses or [{"ok": True}, {"ok": True}, {"ok": True}])

    def request(self, message):
        self.messages.append(message)
        return self.responses.pop(0)


def test_start_prepares_upload_rsyncs_and_requests_start(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".venv").mkdir()
    calls = []
    monkeypatch.setattr(
        adapter_module.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((tuple(argv), kwargs)),
    )
    client = _FakeClient()
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    response = adapter.start(project, {"task": "implement feature"}, request_id="req-1", upload_id="upload-1")

    assert response == {"ok": True}
    assert [message["operation"] for message in client.messages] == [
        "prepare_upload", "finalize_upload", "start"
    ]
    assert client.messages[0]["upload_id"] == "upload-1"
    assert client.messages[2]["request"] == {"task": "implement feature"}
    rsync_argv = calls[0][0]
    assert rsync_argv[0:2] == ("rsync", "-a")
    assert ".venv/" in rsync_argv
    assert all(".git" not in pattern for pattern in DEFAULT_RSYNC_EXCLUDES)
    assert rsync_argv[-2:] == (
        str(project) + "/",
        "beep:/srv/tgent/uploads/incoming/upload-1/",
    )


def test_lifecycle_methods_forward_operations_and_return_results() -> None:
    client = _FakeClient(
        [
            {"ok": True, "result": {"state": "running"}},
            {"ok": True, "result": {"messages": []}},
            {"ok": True, "result": {"state": "review_received"}},
            {"ok": True, "result": {"state": "stopped"}},
            {"ok": True, "result": {"log": "worker output"}},
            {"ok": True, "result": {"state": "reaped"}},
        ]
    )
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    assert adapter.status("agent-1") == {"state": "running"}
    assert adapter.history("agent-1") == {"messages": []}
    assert adapter.continue_agent("agent-1", [{"role": "user", "content": "continue"}]) == {
        "state": "review_received"
    }
    assert adapter.stop("agent-1") == {"state": "stopped"}
    assert adapter.logs("agent-1") == {"log": "worker output"}
    assert adapter.reap("agent-1") == {"state": "reaped"}
    assert [message["operation"] for message in client.messages] == [
        "status", "history", "continue", "stop", "logs", "reap"
    ]


def test_start_stops_if_upload_preparation_is_rejected(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    client = _FakeClient([{"ok": False}])
    monkeypatch.setattr(adapter_module.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unexpected rsync"))
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    with pytest.raises(RemoteAgentAdapterError, match="rejected upload"):
        adapter.start(project, {"task": "work"}, upload_id="upload-1")

    assert len(client.messages) == 1


def test_start_reports_rsync_failure_without_sending_start_request(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    client = _FakeClient()

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "rsync")

    monkeypatch.setattr(adapter_module.subprocess, "run", fail)
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    with pytest.raises(RemoteAgentAdapterError, match="upload failed"):
        adapter.start(project, {"task": "work"}, upload_id="upload-1")

    assert [message["operation"] for message in client.messages] == ["prepare_upload"]
