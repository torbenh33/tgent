"""Tests for serialized remote project upload handling."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remote_agent_upload import RemoteUploadError, RemoteUploadManager


def test_upload_snapshot_is_serialized_and_removed_after_success(tmp_path: Path) -> None:
    manager = RemoteUploadManager(tmp_path / "uploads")
    upload = manager.create_incoming("upload-1")
    (upload / ".git").mkdir()
    (upload / "tracked.txt").write_text("project", encoding="utf-8")
    manager.finalize_upload_handler()({"upload_id": "upload-1"})
    seen: list[Path] = []

    result = manager.publish_and_snapshot("upload-1", lambda path: seen.append(path) or "snapshot-1")

    assert result == "snapshot-1"
    assert seen == [(manager.completed / "upload-1").resolve()]
    assert not upload.exists()


def test_prepare_upload_handler_creates_private_upload_directory(tmp_path: Path) -> None:
    manager = RemoteUploadManager(tmp_path / "uploads")
    handler = manager.prepare_upload_handler()

    result = handler({"upload_id": "upload-1"})

    assert result == {"upload_id": "upload-1", "status": "ready"}
    upload = manager.incoming / "upload-1"
    assert upload.is_dir()
    assert upload.stat().st_mode & 0o777 == 0o700


def test_finalize_upload_requires_nonempty_real_upload(tmp_path: Path) -> None:
    manager = RemoteUploadManager(tmp_path / "uploads")
    upload = manager.create_incoming("upload-1")
    finalize = manager.finalize_upload_handler()

    with pytest.raises(RemoteUploadError, match="non-empty real directory"):
        finalize({"upload_id": "upload-1"})

    (upload / "project.txt").write_text("ready", encoding="utf-8")
    assert finalize({"upload_id": "upload-1"}) == {
        "upload_id": "upload-1",
        "status": "complete",
    }
    assert not upload.exists()
    assert (manager.completed / "upload-1").is_dir()
    with pytest.raises(RemoteUploadError, match="already finalized"):
        finalize({"upload_id": "upload-1"})


def test_start_handler_passes_upload_and_request_to_controller(tmp_path: Path) -> None:
    manager = RemoteUploadManager(tmp_path / "uploads")
    upload = manager.create_incoming("upload-1")
    (upload / "checkout").mkdir()
    manager.finalize_upload_handler()({"upload_id": "upload-1"})
    received: list[tuple[Path, dict[str, object]]] = []
    handler = manager.start_handler(
        lambda checkout, request: received.append((checkout, request)) or {"agent_id": "agent-1"}
    )

    result = handler(
        {
            "upload_id": "upload-1",
            "request": {"task": "do work"},
        }
    )

    assert result == {"agent_id": "agent-1"}
    assert received == [((manager.completed / "upload-1").resolve(), {"task": "do work"})]
    assert not upload.exists()


def test_upload_is_retained_when_snapshot_creation_fails(tmp_path: Path) -> None:
    manager = RemoteUploadManager(tmp_path / "uploads")
    upload = manager.create_incoming("upload-1")
    (upload / "project.txt").write_text("state", encoding="utf-8")
    manager.finalize_upload_handler()({"upload_id": "upload-1"})

    def fail(_path: Path) -> None:
        raise RuntimeError("snapshot failed")

    with pytest.raises(RuntimeError, match="snapshot failed"):
        manager.publish_and_snapshot("upload-1", fail)

    assert (manager.completed / "upload-1").is_dir()


def test_upload_ids_and_duplicates_are_rejected(tmp_path: Path) -> None:
    manager = RemoteUploadManager(tmp_path / "uploads")
    for identifier in ("../escape", "", "-bad"):
        with pytest.raises(RemoteUploadError, match="Invalid upload"):
            manager.create_incoming(identifier)
    manager.create_incoming("same-id")
    with pytest.raises(RemoteUploadError, match="already exists"):
        manager.create_incoming("same-id")


def test_abandoned_cleanup_removes_only_old_real_upload_directories(tmp_path: Path) -> None:
    manager = RemoteUploadManager(tmp_path / "uploads")
    old_upload = manager.create_incoming("old-upload")
    (old_upload / "partial").write_text("partial", encoding="utf-8")
    os.utime(old_upload, (100.0, 100.0))
    fresh_upload = manager.create_incoming("fresh-upload")
    (fresh_upload / "partial").write_text("partial", encoding="utf-8")

    removed = manager.cleanup_abandoned(older_than_seconds=60, now=200.0)

    assert removed == ("old-upload",)
    assert not old_upload.exists()
    assert fresh_upload.is_dir()


def test_snapshot_rejects_symlink_upload_path(tmp_path: Path) -> None:
    manager = RemoteUploadManager(tmp_path / "uploads")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "content").write_text("no", encoding="utf-8")
    (manager.incoming / "link-id").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RemoteUploadError, match="real directory"):
        manager.publish_and_snapshot("link-id", lambda _path: None)
