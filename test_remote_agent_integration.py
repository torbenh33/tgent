"""Local integration coverage for remote upload and snapshot publication."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import remote_agent_adapter as adapter_module
from agent_snapshot import SnapshotError, create_project_snapshot
from remote_agent_adapter import RemoteAgentAdapter
from remote_agent_config import RemoteAgentConfig
from remote_agent_daemon import build_operation_handlers, serve_stdio
from remote_agent_protocol import dispatch_request
from remote_agent_upload import RemoteUploadManager


def _git(project: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(project), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _project(root: Path, content: str) -> Path:
    root.mkdir()
    subprocess.run(("git", "-C", str(root), "init", "-q"), check=True)
    _git(root, "config", "user.name", "Integration Test")
    _git(root, "config", "user.email", "integration@example.invalid")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "initial")
    # Include uncommitted local state in the transfer, as the adapter does.
    (root / "tracked.txt").write_text(content, encoding="utf-8")
    (root / "local-change.txt").write_text(content, encoding="utf-8")
    return root


def _transfer(manager: RemoteUploadManager, upload_id: str, project: Path) -> None:
    incoming = manager.incoming_path(upload_id)
    assert incoming.is_dir()
    shutil.copytree(project, incoming, dirs_exist_ok=True)
    manager.finalize_upload_handler()({"upload_id": upload_id})


def _snapshot_or_skip(checkout: Path, destination: Path):
    try:
        return create_project_snapshot(checkout, destination)
    except SnapshotError as err:
        if "reflink" in str(err).lower() or "operation not supported" in str(err).lower():
            pytest.skip(f"Filesystem does not support snapshot copies: {err}")
        raise


def test_protocol_uploads_create_isolated_snapshots_concurrently(tmp_path: Path) -> None:
    """The real protocol, upload manager, and Git snapshot code preserve each upload."""
    uploads = RemoteUploadManager(tmp_path / "uploads")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    projects = {
        "upload-one": _project(tmp_path / "project-one", "first upload\n"),
        "upload-two": _project(tmp_path / "project-two", "second upload\n"),
    }

    def start_agent(checkout: Path, request: dict[str, object]) -> dict[str, str]:
        agent_id = str(request["agent_id"])
        snapshot = _snapshot_or_skip(checkout, snapshots / agent_id)
        return {"agent_id": agent_id, "snapshot": str(snapshot.project_root)}

    handlers = build_operation_handlers(
        uploads,
        start_agent=start_agent,
        lifecycle_handlers={},
    )

    for upload_id, project in projects.items():
        prepared = dispatch_request(
            {
                "schema_version": 1,
                "operation": "prepare_upload",
                "request_id": f"prepare-{upload_id}",
                "upload_id": upload_id,
            },
            handlers,
        )
        assert prepared["ok"] is True
        _transfer(uploads, upload_id, project)

    gate = threading.Barrier(2)

    def start(upload_id: str) -> dict[str, object]:
        gate.wait(timeout=5)
        return dispatch_request(
            {
                "schema_version": 1,
                "operation": "start",
                "request_id": f"start-{upload_id}",
                "upload_id": upload_id,
                "request": {"agent_id": upload_id},
            },
            handlers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(start, projects))

    assert all(response["ok"] is True for response in responses)
    assert not any((uploads.completed / upload_id).exists() for upload_id in projects)
    for upload_id, expected in (
        ("upload-one", "first upload\n"),
        ("upload-two", "second upload\n"),
    ):
        snapshot_project = snapshots / upload_id / "project"
        assert (snapshot_project / "tracked.txt").read_text(encoding="utf-8") == expected
        assert (snapshot_project / "local-change.txt").read_text(encoding="utf-8") == expected

    # Changes to a later source checkout cannot affect an already published snapshot.
    (projects["upload-one"] / "tracked.txt").write_text("changed later\n", encoding="utf-8")
    assert (snapshots / "upload-one" / "project" / "tracked.txt").read_text(encoding="utf-8") == "first upload\n"


def test_interrupted_upload_cannot_start_and_is_reaped_by_cleanup(tmp_path: Path) -> None:
    uploads = RemoteUploadManager(tmp_path / "uploads")
    handlers = build_operation_handlers(
        uploads,
        start_agent=lambda _checkout, _request: pytest.fail("incomplete upload reached controller"),
        lifecycle_handlers={},
    )
    dispatch_request(
        {
            "schema_version": 1,
            "operation": "prepare_upload",
            "request_id": "prepare-interrupted",
            "upload_id": "interrupted",
        },
        handlers,
    )
    partial = uploads.incoming / "interrupted"
    (partial / "partial-file").write_text("partial transfer", encoding="utf-8")

    output = io.StringIO()
    result = serve_stdio(
        handlers,
        stdin=io.StringIO(json.dumps({
            "schema_version": 1,
            "operation": "start",
            "request_id": "start-interrupted",
            "upload_id": "interrupted",
            "request": {"agent_id": "agent-interrupted"},
        }) + chr(10)),
        stdout=output,
    )

    response = json.loads(output.getvalue())
    assert result == 0
    assert response["ok"] is False
    assert partial.is_dir()
    os.utime(partial, (100.0, 100.0))
    assert uploads.cleanup_abandoned(older_than_seconds=60, now=200.0) == ("interrupted",)
    assert not partial.exists()


def test_adapter_upload_protocol_and_git_snapshot_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """Exercise the local adapter through upload publication and snapshot creation."""
    project = _project(tmp_path / "source-project", "transferred working tree\n")
    uploads = RemoteUploadManager(tmp_path / "managed-uploads")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()

    def start_agent(checkout: Path, request: dict[str, object]) -> dict[str, str]:
        assert request == {"schema_version": 1, "task": "inspect this checkout"}
        snapshot = _snapshot_or_skip(checkout, snapshots / "adapter-agent")
        return {"agent_id": "adapter-agent", "snapshot": str(snapshot.project_root)}

    handlers = build_operation_handlers(
        uploads,
        start_agent=start_agent,
        lifecycle_handlers={},
    )

    class InProcessProtocolClient:
        def request(self, message: dict[str, object]) -> dict[str, object]:
            return dispatch_request(message, handlers)

    def local_rsync(argv, *, check):
        assert check is True
        assert argv[0:2] == ("rsync", "-a")
        source = Path(argv[-2].removesuffix("/"))
        remote_destination = argv[-1].removeprefix("beep:").removesuffix("/")
        assert remote_destination == "/srv/tgent/uploads/incoming/adapter-upload"
        destination = uploads.incoming / "adapter-upload"
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(adapter_module, "subprocess", SimpleNamespace(run=local_rsync))
    adapter = RemoteAgentAdapter(
        RemoteAgentConfig("beep", "/srv/tgent"),
        client=InProcessProtocolClient(),
    )

    result = adapter.start(
        project,
        {"schema_version": 1, "task": "inspect this checkout"},
        request_id="adapter-start",
        upload_id="adapter-upload",
    )

    assert result["ok"] is True
    assert result["result"]["agent_id"] == "adapter-agent"
    snapshot_project = snapshots / "adapter-agent" / "project"
    assert (snapshot_project / "tracked.txt").read_text(encoding="utf-8") == "transferred working tree\n"
    assert (snapshot_project / "local-change.txt").read_text(encoding="utf-8") == "transferred working tree\n"
    assert not (uploads.incoming / "adapter-upload").exists()
    assert not (uploads.completed / "adapter-upload").exists()
