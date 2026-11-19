"""Tests for the local remote-agent upload adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

import remote_agent_adapter as adapter_module
from remote_agent_adapter import RemoteAgentAdapter, RemoteAgentAdapterError
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
    calls = []
    base = "a" * 40
    def run(argv, **kwargs):
        calls.append(tuple(argv))
        output = base + "\n" if "rev-parse" in argv else b"" if "ls-tree" in argv or "ls-files" in argv else None
        return subprocess.CompletedProcess(argv, 0, output)
    monkeypatch.setattr(adapter_module.subprocess, "run", run)
    client = _FakeClient([{"ok": True, "result": {}}] * 3 + [{"ok": True}])
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", "/srv/tgent"), client=client)
    adapter.start(project, {"task": "implement feature"}, request_id="req-1", upload_id="upload-1")
    assert [m["operation"] for m in client.messages] == ["ensure_project", "prepare_upload", "finalize_upload", "start"]
    assert client.messages[-1]["request"]["_base_commit"] == base
    assert client.messages[-1]["request"]["_project_name"] == str(project).replace("/", "_")
    assert next(c for c in calls if "push" in c)[-2:] == ("+refs/*:refs/*", f"{base}:refs/agent-sync/{base}")
    rsync = next(c for c in calls if c[0] == "rsync")
    assert rsync[1:7] == ("-a", "--checksum", "--delete", "--exclude", ".git", "--")
    assert rsync[-2:] == (str(project) + "/", "beep:/srv/tgent/uploads/incoming/upload-1/")


def test_continuation_publishes_refs_without_upload(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    base = "b" * 40
    name = str(project).replace("/", "_")
    calls = []
    def run(argv, **kwargs):
        calls.append(tuple(argv))
        assert "rev-parse" not in argv
        output = b"" if "ls-tree" in argv or "ls-files" in argv else None
        return subprocess.CompletedProcess(argv, 0, output)
    monkeypatch.setattr(adapter_module.subprocess, "run", run)
    client = _FakeClient([{"ok": True, "result": {"base_commit": base, "project_name": name}}] + [{"ok": True, "result": {}}] * 4)
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", "/srv/tgent"), client=client)
    adapter.continue_agent("agent-1", [{"role": "user", "content": "continue"}], project_root=project)
    assert [m["operation"] for m in client.messages] == ["status", "ensure_project", "continue"]
    assert next(c for c in calls if "push" in c)[-2:] == ("+refs/*:refs/*", f"{base}:refs/agent-sync/{base}")
    assert not any(c[0] == "rsync" for c in calls)
    assert not any(m["operation"] in {"prepare_upload", "finalize_upload"} for m in client.messages)


def test_live_review_continuation_does_not_replace_agent_worktree():
    client = _FakeClient([{"ok": True, "result": {"state": "awaiting_review"}}, {"ok": True, "result": {"state": "review_received"}}])
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", "/srv/tgent"), client=client)
    assert adapter.continue_agent("agent-1", [{"role": "user", "content": "review"}], project_root="/does/not/exist")["state"] == "review_received"
    assert [m["operation"] for m in client.messages] == ["status", "continue"]


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
    base = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    client = _FakeClient([{"ok": True, "result": {"base_commit": base, "project_name": str(project).replace("/", "_")}}])
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", PurePosixPath("/srv/tgent")), client=client)

    with pytest.raises(RemoteAgentAdapterError, match="does not support submodules"):
        adapter.continue_agent("agent-1", [], project_root=project)

    assert [m["operation"] for m in client.messages] == ["status"]


@pytest.mark.parametrize("failure", ["push", "rsync"])
def test_start_reports_transfer_failure_without_start(tmp_path, monkeypatch, failure):
    project = tmp_path / "project"
    project.mkdir()
    def run(argv, **kwargs):
        if failure in argv:
            raise subprocess.CalledProcessError(1, argv)
        output = "a" * 40 if "rev-parse" in argv else b"" if "ls-tree" in argv or "ls-files" in argv else None
        return subprocess.CompletedProcess(argv, 0, output)
    monkeypatch.setattr(adapter_module.subprocess, "run", run)
    client = _FakeClient([{"ok": True, "result": {}}] * 3)
    adapter = RemoteAgentAdapter(RemoteAgentConfig("beep", "/srv/tgent"), client=client)
    with pytest.raises(RemoteAgentAdapterError, match="synchronization failed"):
        adapter.start(project, {"task": "work"})
    assert all(m["operation"] != "start" for m in client.messages)
