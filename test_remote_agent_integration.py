"""Local integration coverage for remote upload and snapshot publication."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import remote_agent_adapter as adapter_module
from agent_sandbox import RuntimeManifest, RuntimeMount, SandboxRuntimeLayout, write_runtime_manifest
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

def _write_test_runtime(config: RemoteAgentConfig, version: str) -> None:
    from datetime import datetime, timezone

    from agent_sandbox import RuntimeManifest, RuntimeMount, SandboxRuntimeLayout, write_runtime_manifest

    version_root = Path(str(config.runtime_dir / "versions" / version))
    python_mount = version_root / "mounts" / "mount-000"
    (python_mount / "bin").mkdir(parents=True)
    (python_mount / "bin" / "python").write_text("test python", encoding="utf-8")
    (python_mount / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    write_runtime_manifest(
        version_root / "manifest.json",
        RuntimeManifest(
            version=version,
            rsync_completed_at=datetime.now(timezone.utc),
            layout=SandboxRuntimeLayout(
                (
                    RuntimeMount(Path("/usr"), Path("/usr")),
                    RuntimeMount(python_mount, Path("/opt/runtime-python")),
                )
            ),
        ),
    )


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


def test_remote_service_lifecycle_pins_runtime_version(tmp_path: Path, monkeypatch, request) -> None:
    """Exercise service start/status/logs/stop/reap using only local resources."""
    from pathlib import PurePosixPath

    from remote_agent_config import RemoteAgentConfig
    from remote_agent_service import RemoteAgentService

    remote_base = Path(tempfile.mkdtemp(prefix="tgent-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(remote_base, ignore_errors=True))
    config = RemoteAgentConfig("local-test", PurePosixPath(str(remote_base)))
    _write_test_runtime(config, "runtime-1")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_bwrap = fake_bin / "bwrap"
    fake_bwrap.write_text(
        "#!/bin/sh\necho fake worker stderr >&2\nexec sleep 300\n",
        encoding="utf-8",
    )
    fake_bwrap.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Integration Test")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "user.email")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "integration@example.invalid")

    project = _project(tmp_path / "source", "uploaded project\n")
    _git(project, "branch", "feature/test")
    _git(project, "tag", "v-test")
    _git(project, "update-ref", "refs/custom/test", "HEAD")
    (project / "deleted.txt").write_text("committed\n")
    _git(project, "add", "deleted.txt")
    _git(project, "commit", "-m", "add deleted file")
    (project / "deleted.txt").unlink()
    (project / ".venv").mkdir()
    (project / ".venv" / "marker").write_text("venv")
    service = RemoteAgentService(config)
    assert service.controller.worker_command == (
        "/opt/runtime-python/bin/python", "-m", "agent_worker"
    )
    startup_errors: list[Exception] = []
    original_start_agent = service.controller.start_agent

    async def record_start_error(*args, **kwargs):
        try:
            return await original_start_agent(*args, **kwargs)
        except Exception as err:
            startup_errors.append(err)
            raise

    service.controller.start_agent = record_start_error

    async def exercise_lifecycle() -> None:
        try:
            prepared = await service._dispatch({
                "schema_version": 1,
                "operation": "prepare_upload",
                "request_id": "prepare-lifecycle",
                "upload_id": "lifecycle-upload",
            })
            assert prepared["ok"] is True
            uploaded_checkout = service.uploads.incoming / "lifecycle-upload"
            shutil.copytree(project, uploaded_checkout, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
            name = str(project).replace("/", "_")
            await service._ensure_project({"project_name": name, "source_path": str(project)})
            base = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
            _git(project, "push", str(config.project_root_dir / name), "+refs/*:refs/*", f"{base}:refs/agent-sync/{base}")
            finalized = await service._dispatch({
                "schema_version": 1,
                "operation": "finalize_upload",
                "request_id": "finalize-lifecycle",
                "upload_id": "lifecycle-upload",
            })
            assert finalized["ok"] is True

            started = await service._dispatch({
                "schema_version": 1,
                "operation": "start",
                "request_id": "start-lifecycle",
                "upload_id": "lifecycle-upload",
                "request": {"schema_version": 1, "task": "run lifecycle test", "_project_name": name, "_base_commit": base},
            })
            assert started["ok"] is True, (started, startup_errors)
            agent_id = started["result"]["agent_id"]

            # A later runtime publication must not change the service's selected
            # runtime or the version recorded for this agent.
            _write_test_runtime(config, "runtime-2")
            status = await service._dispatch({
                "schema_version": 1,
                "operation": "status",
                "request_id": "status-lifecycle",
                "agent_id": agent_id,
            })
            assert status["ok"] is True
            assert status["result"]["runtime_version"] == "runtime-1"
            snapshot = Path(status["result"]["snapshot_project"])
            mirror = Path(str(config.project_root_dir / name))
            assert (snapshot / ".git/objects/info/alternates").read_text().strip() == str(mirror / "objects")
            assert subprocess.check_output(["git", "-C", str(snapshot), "rev-parse", "HEAD"], text=True).strip() == base
            for ref in ("refs/heads/feature/test", "refs/tags/v-test", "refs/custom/test"):
                expected = subprocess.check_output(["git", "--git-dir", str(mirror), "rev-parse", ref], text=True).strip()
                actual = subprocess.check_output(["git", "-C", str(snapshot), "rev-parse", ref], text=True).strip()
                assert actual == expected
            assert (snapshot / "tracked.txt").read_text() == "uploaded project\n"
            assert (snapshot / "local-change.txt").read_text() == "uploaded project\n"
            assert not (snapshot / "deleted.txt").exists()
            assert (snapshot / ".venv" / "marker").read_text() == "venv"

            log_path = Path(service.controller.store.get_metadata(agent_id)["worker_log"])
            for _ in range(100):
                if log_path.exists() and log_path.stat().st_size:
                    break
                await asyncio.sleep(0.01)

            logs = await service._dispatch({
                "schema_version": 1,
                "operation": "logs",
                "request_id": "logs-lifecycle",
                "agent_id": agent_id,
                "max_bytes": 1024,
            })
            assert logs["ok"] is True
            assert "fake worker stderr" in logs["result"]["log"]

            stopped = await service._dispatch({
                "schema_version": 1,
                "operation": "stop",
                "request_id": "stop-lifecycle",
                "agent_id": agent_id,
            })
            assert stopped["ok"] is True
            assert stopped["result"]["state"] == "stopped"

            # The local HEAD moves, but continuation keeps the original base
            # and re-transfers even when the agent already has that base.
            _git(project, "add", "-A")
            _git(project, "commit", "-m", "advance source")
            (project / "tracked.txt").write_text("new working tree\n")
            (project / "local-change.txt").unlink()
            _git(project, "push", str(mirror), "HEAD:refs/heads/moving")
            await service._dispatch({"schema_version": 1, "operation": "prepare_upload", "request_id": "prepare-cont", "upload_id": "continuation"})
            shutil.copytree(project, service.uploads.incoming / "continuation", dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
            await service._dispatch({"schema_version": 1, "operation": "finalize_upload", "request_id": "finalize-cont", "upload_id": "continuation"})
            continued = await service._dispatch({"schema_version": 1, "operation": "continue", "request_id": "continue", "agent_id": agent_id, "upload_id": "continuation", "messages": [{"role": "user", "content": "continue"}]})
            assert continued["ok"], continued
            child = continued["result"]["agent_id"]
            child_metadata = service.controller.store.get_metadata(child)
            child_snapshot = Path(child_metadata["snapshot_project"])
            assert child_metadata["base_commit"] == base
            assert child_metadata["project_mirror"] == str(mirror)
            assert subprocess.check_output(["git", "-C", str(child_snapshot), "rev-parse", "HEAD"], text=True).strip() == base
            assert (child_snapshot / "tracked.txt").read_text() == "new working tree\n"
            assert not (child_snapshot / "local-change.txt").exists()
            assert (child_snapshot / ".git/objects/info/alternates").read_text().strip() == str(mirror / "objects")
            await service.controller.stop_agent(child)
            reaped = await service._dispatch({
                "schema_version": 1,
                "operation": "reap",
                "request_id": "reap-lifecycle",
                "agent_id": agent_id,
            })
            assert reaped["ok"] is True
            assert reaped["result"]["state"] == "reaped"
        finally:
            await service.controller.shutdown()

    asyncio.run(exercise_lifecycle())


def test_patch_submission_and_retrieval_end_to_end(tmp_path: Path, monkeypatch, request) -> None:
    """Export a commit patch through the service and retrieve it through the adapter."""
    from pathlib import PurePosixPath

    from remote_agent_config import RemoteAgentConfig
    from remote_agent_service import RemoteAgentService

    remote_base = Path(tempfile.mkdtemp(prefix="tgent-patch-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(remote_base, ignore_errors=True))
    config = RemoteAgentConfig("beep", PurePosixPath(str(remote_base)))
    _write_test_runtime(config, "runtime-1")
    service = RemoteAgentService(config)

    project = _project(tmp_path / "patch-project", "before patch\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "uploaded project state")
    base_commit = subprocess.run(
        ("git", "-C", str(project), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot = _snapshot_or_skip(project, tmp_path / "patch-snapshot")
    (snapshot.project_root / "tracked.txt").write_text("after patch\n", encoding="utf-8")
    _git(snapshot.project_root, "add", "tracked.txt")
    _git(snapshot.project_root, "commit", "-m", "change tracked content")
    head_commit = subprocess.run(
        ("git", "-C", str(snapshot.project_root), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    agent_id = snapshot.root.name
    result = asyncio.run(
        service._submit_patch(snapshot, {"base_commit": base_commit, "head_commit": head_commit})
    )
    assert result["base"] == base_commit
    assert result["head"] == head_commit
    assert result["patch_artifact"] == f"{agent_id}.patch"
    assert result["patch_bytes"] > 0
    service.controller.store.create(
        agent_id,
        {"agent_id": agent_id, "state": "awaiting_review", "submitted_head": head_commit},
        [],
    )

    class ServiceProtocolClient:
        def request(self, message: dict[str, object]) -> dict[str, object]:
            metadata = asyncio.run(service._patches({"agent_id": message["agent_id"]}))
            return {"ok": True, "result": metadata}

    def local_rsync(argv, *, check):
        assert check is True
        source = Path(argv[-2].removeprefix("beep:"))
        Path(argv[-1]).write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(adapter_module.subprocess, "run", local_rsync)
    destination = tmp_path / "retrieved-patches"
    destination.mkdir()
    adapter = RemoteAgentAdapter(config, client=ServiceProtocolClient())

    retrieved = adapter.retrieve_patch(agent_id, destination)

    assert retrieved == destination / f"{agent_id}.patch"
    patch_content = retrieved.read_text(encoding="utf-8")
    assert "change tracked content" in patch_content
    assert "before patch" in patch_content
    assert "after patch" in patch_content
    assert result["patch_bytes"] == retrieved.stat().st_size
