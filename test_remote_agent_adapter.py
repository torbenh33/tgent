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
            {"ok": True, "result": {"patch_applied_acknowledgement": {"submitted_head": "abc"}}},
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
    assert adapter.acknowledge_patch_applied("agent-1")["patch_applied_acknowledgement"]["submitted_head"] == "abc"
    assert adapter.reap("agent-1") == {"state": "reaped"}
    assert [message["operation"] for message in client.messages] == [
        "status", "history", "continue", "stop", "logs", "acknowledge_patch_applied", "reap"
    ]


def test_review_replies_batches_multiple_messages_in_one_remote_call() -> None:
    result = {"reviews": [{"review_id": "r-1"}, {"review_id": "r-2"}]}
    client = _FakeClient([{"ok": True, "result": result}])
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    assert adapter.review_replies("/repo", unacknowledged_only=True) == result
    assert len(client.messages) == 1
    assert client.messages[0]["operation"] == "review_replies"
    assert client.messages[0]["repo_path"] == "/repo"
    assert client.messages[0]["unacknowledged_only"] is True


def test_continue_with_project_rejects_submodule_gitlinks_before_staging(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "base"], check=True)
    # A mode-160000 tree entry is the superproject's submodule pointer; its
    # object is not the submodule commit and cannot carry that repository's history.
    subprocess.run(["git", "-C", str(project), "update-index", "--add", "--cacheinfo", "160000", "0123456789abcdef0123456789abcdef01234567", "vendor/submodule"], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "add gitlink"], check=True)
    client = _FakeClient()
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    with pytest.raises(RemoteAgentAdapterError, match="does not support submodules"):
        adapter.continue_agent("agent-1", [], project_root=project)

    assert client.messages == []


def test_retrieve_patch_downloads_verified_artifact_atomically(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "patches"
    destination.mkdir()
    client = _FakeClient([{
        "ok": True,
        "result": {
            "agent_id": "agent-1",
            "available": True,
            "artifact": "agent-1.patch",
            "bytes": len(b"patch contents"),
        },
    }])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        Path(argv[-1]).write_bytes(b"patch contents")

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    retrieved = adapter.retrieve_patch("agent-1", destination)

    assert retrieved == destination / "agent-1.patch"
    assert retrieved.read_bytes() == b"patch contents"
    assert calls[0][0][-2] == "beep:/srv/tgent/patches/agent-1.patch"
    assert calls[0][0][-1] != str(retrieved)
    assert not list(destination.glob(".agent-1.patch.*"))


def test_retrieve_patch_rejects_file_destination(tmp_path) -> None:
    destination = tmp_path / "not-a-directory"
    destination.write_text("keep", encoding="utf-8")
    client = _FakeClient([{"ok": True, "result": {"available": True}}])
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    with pytest.raises(RemoteAgentAdapterError, match="destination is not a directory"):
        adapter.retrieve_patch("agent-1", destination)

    assert destination.read_text(encoding="utf-8") == "keep"


def test_retrieve_patch_defaults_to_tmp_directory(tmp_path, monkeypatch) -> None:
    client = _FakeClient([{"ok": True, "result": {"available": False}}])
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    assert adapter.retrieve_patch("agent-1") is None


def test_retrieve_patch_returns_none_when_artifact_is_unavailable(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "patches"
    destination.mkdir()
    client = _FakeClient([{"ok": True, "result": {"available": False}}])
    monkeypatch.setattr(
        adapter_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unexpected rsync"),
    )
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    assert adapter.retrieve_patch("agent-1", destination) is None


def test_retrieve_patch_rejects_artifact_size_mismatch_without_replacing_target(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "patches"
    destination.mkdir()
    target = destination / "agent-1.patch"
    target.write_text("previous", encoding="utf-8")
    client = _FakeClient([{
        "ok": True,
        "result": {
            "available": True,
            "artifact": "agent-1.patch",
            "bytes": 100,
        },
    }])

    def fake_run(argv, **kwargs):
        Path(argv[-1]).write_bytes(b"short")

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    with pytest.raises(RemoteAgentAdapterError, match="does not match"):
        adapter.retrieve_patch("agent-1", destination)

    assert target.read_text(encoding="utf-8") == "previous"
    assert not list(destination.glob(".agent-1.patch.*"))


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


def test_batch_patch_enumeration_and_transfer_use_one_call_each(tmp_path, monkeypatch):
    import hashlib
    config = RemoteAgentConfig("beep", PurePosixPath("/srv/tgent"))
    patches = [b"patch one", b"patch two"]
    metadata = [{"agent_id": f"agent-{i}", "artifact": f"agent-{i}.patch", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} for i, raw in enumerate(patches)]
    client = _FakeClient([{"ok": True, "result": {"patches": metadata}}])
    adapter = RemoteAgentAdapter(config, client=client)
    assert adapter.pending_patches() == metadata
    assert len(client.messages) == 1
    destination = tmp_path / "patches"; destination.mkdir()
    calls = []
    def rsync(argv, **kwargs):
        calls.append(argv)
        for item, raw in zip(metadata, patches):
            (destination / item["artifact"]).write_bytes(raw)
    monkeypatch.setattr(adapter_module.subprocess, "run", rsync)
    result = adapter.retrieve_patches(metadata, destination)
    assert len(calls) == 1
    assert len([arg for arg in calls[0] if str(arg).startswith("beep:")]) == 2
    assert all(isinstance(value, Path) for value in result.values())


def test_batch_patch_empty_and_partial_metadata_failures(tmp_path, monkeypatch):
    config = RemoteAgentConfig("beep", PurePosixPath("/srv/tgent"))
    client = _FakeClient([{"ok": True, "result": {"patches": []}}])
    adapter = RemoteAgentAdapter(config, client=client)
    assert adapter.pending_patches() == []
    destination = tmp_path / "patches"; destination.mkdir()
    assert adapter.retrieve_patches([], destination) == {}
    items = [
        {"agent_id": "agent-good", "artifact": "agent-good.patch", "bytes": 1, "sha256": "0" * 64},
        {"agent_id": "../bad", "artifact": "../bad.patch", "bytes": 1, "sha256": "0" * 64},
    ]
    calls = []
    monkeypatch.setattr(adapter_module.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    result = adapter.retrieve_patches(items, destination)
    assert len(calls) == 1
    assert isinstance(result["../bad"], Exception)
    assert isinstance(result["agent-good"], Exception)
