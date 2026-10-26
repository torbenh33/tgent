"""Safe staging and serialized publication of per-request project uploads."""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import hashlib
import re
import subprocess
import os
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar


class RemoteUploadError(RuntimeError):
    """Raised when an uploaded project cannot be safely published."""


_T = TypeVar("_T")
_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _validate_upload_id(upload_id: str) -> None:
    if (
        not isinstance(upload_id, str)
        or not 1 <= len(upload_id) <= 128
        or upload_id[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        or any(character not in _ID_CHARACTERS for character in upload_id)
    ):
        raise RemoteUploadError("Invalid upload identifier")


class RemoteUploadManager:
    """Manage private incoming uploads and serialized snapshot creation."""

    def __init__(self, uploads_root: str | os.PathLike[str]) -> None:
        root = Path(uploads_root).absolute()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise RemoteUploadError(f"Uploads root is not a real directory: {root}")
        os.chmod(root, 0o700)
        self.root = root.resolve(strict=True)
        self._async_start_lock = asyncio.Lock()
        self.incoming = self.root / "incoming"
        self.incoming.mkdir(mode=0o700, exist_ok=True)
        self.completed = self.root / "completed"
        self.sync_root = self.root / "sync"
        self.sync_root.mkdir(mode=0o700, exist_ok=True)
        self.completed.mkdir(mode=0o700, exist_ok=True)
        if self.incoming.is_symlink() or not self.incoming.is_dir():
            raise RemoteUploadError(f"Incoming upload path is not a real directory: {self.incoming}")
        if self.completed.is_symlink() or not self.completed.is_dir():
            raise RemoteUploadError(f"Completed upload path is not a real directory: {self.completed}")
        os.chmod(self.incoming, 0o700)
        os.chmod(self.completed, 0o700)
        self.lock_path = self.root / ".publication.lock"
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)

    def prepare_sync(self, agent_id: str, sync_id: str) -> dict[str, str]:
        _validate_upload_id(agent_id)
        _validate_upload_id(sync_id)
        directory = self.sync_root / sync_id
        directory.mkdir(mode=0o700)
        (directory / "agent-id").write_text(agent_id, encoding="ascii")
        return {"sync_id": sync_id, "status": "ready"}

    def finalize_sync(self, agent_id: str, sync_id: str, size: int, digest: str, base_commit: str) -> dict[str, str]:
        _validate_upload_id(agent_id)
        _validate_upload_id(sync_id)
        directory = self.sync_root / sync_id
        bundle = directory / "repository.bundle"
        try:
            if directory.is_symlink() or directory.resolve(strict=True).parent != self.sync_root or (directory / "agent-id").read_text(encoding="ascii") != agent_id:
                raise RemoteUploadError("Invalid continuation sync directory")
            info = bundle.lstat()
            if not bundle.is_file() or bundle.is_symlink() or info.st_size != size or hashlib.sha256(bundle.read_bytes()).hexdigest() != digest:
                raise RemoteUploadError("Continuation bundle size or digest mismatch")
            verify = subprocess.run(["git", "bundle", "verify", str(bundle)], capture_output=True, text=True, check=True, timeout=60)
            refs = subprocess.run(["git", "bundle", "list-heads", str(bundle)], capture_output=True, text=True, check=True, timeout=30)
            if not any(line.split()[-1] == base_commit for line in refs.stdout.splitlines() if line.split()):
                raise RemoteUploadError("Bundle does not contain the declared base commit")
            return {"status": "staged", "sync_base": base_commit, "bundle_path": str(bundle)}
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def incoming_path(self, upload_id: str) -> Path:
        """Return the isolated rsync destination for a validated upload ID."""
        _validate_upload_id(upload_id)
        return self.incoming / upload_id

    def create_incoming(self, upload_id: str) -> Path:
        """Create a fresh upload directory, refusing duplicate IDs."""
        destination = self.incoming_path(upload_id)
        with self._publication_lock():
            if (self.completed / upload_id).exists() or (self.completed / upload_id).is_symlink():
                raise RemoteUploadError("Upload identifier already exists")
            try:
                destination.mkdir(mode=0o700)
            except FileExistsError as err:
                raise RemoteUploadError("Upload identifier already exists") from err
        return destination

    def publish_and_snapshot(self, upload_id: str, create_snapshot: Callable[[Path], _T]) -> _T:
        """Serialize snapshot creation and remove an upload only on success."""
        _validate_upload_id(upload_id)
        source = self.completed / upload_id
        with self._publication_lock():
            if (self.incoming / upload_id).is_symlink():
                raise RemoteUploadError("Upload path must be a real directory")
            try:
                info = source.lstat()
            except FileNotFoundError as err:
                raise RemoteUploadError("Upload directory does not exist") from err
            if not stat.S_ISDIR(info.st_mode):
                raise RemoteUploadError("Upload path must be a real directory")
            resolved = source.resolve(strict=True)
            if resolved.parent != self.completed:
                raise RemoteUploadError("Upload directory resolves outside managed storage")
            if not any(source.iterdir()):
                raise RemoteUploadError("Upload directory is empty")
            result = create_snapshot(resolved)
            shutil.rmtree(resolved)
            return result

    def prepare_upload_handler(self) -> Callable[[dict[str, object]], dict[str, str]]:
        """Create the protocol handler that allocates a private upload folder."""
        def handle(request: dict[str, object]) -> dict[str, str]:
            upload_id = request["upload_id"]
            if not isinstance(upload_id, str):
                raise RemoteUploadError("Invalid validated upload request")
            self.create_incoming(upload_id)
            return {"upload_id": upload_id, "status": "ready"}

        return handle

    def finalize_upload_handler(self) -> Callable[[dict[str, object]], dict[str, str]]:
        """Mark an upload complete after its rsync transfer succeeds."""
        def handle(request: dict[str, object]) -> dict[str, str]:
            upload_id = request["upload_id"]
            if not isinstance(upload_id, str):
                raise RemoteUploadError("Invalid validated upload request")
            source = self.incoming_path(upload_id)
            with self._publication_lock():
                sealed = self.completed / upload_id
                if sealed.exists() or sealed.is_symlink():
                    raise RemoteUploadError("Upload is already finalized")
                try:
                    info = source.lstat()
                except FileNotFoundError as err:
                    raise RemoteUploadError("Upload directory does not exist") from err
                if not stat.S_ISDIR(info.st_mode) or not any(source.iterdir()):
                    raise RemoteUploadError("Upload must be a non-empty real directory")
                sealed = self.completed / upload_id
                try:
                    os.rename(source, sealed)
                except FileExistsError as err:
                    raise RemoteUploadError("Upload is already finalized") from err
            return {"upload_id": upload_id, "status": "complete"}

        return handle

    def start_handler(self, start_agent: Callable[[Path, dict[str, object]], _T]) -> Callable[[dict[str, object]], _T]:
        """Create the protocol's ``start`` handler around a controller callback.

        The callback receives the completed uploaded checkout and the validated
        start request. Snapshot creation and registration must happen within the
        callback so the upload is retained if controller startup fails.
        """
        def handle(request: dict[str, object]) -> _T:
            upload_id = request["upload_id"]
            start_request = request["request"]
            if not isinstance(upload_id, str) or not isinstance(start_request, dict):
                raise RemoteUploadError("Invalid validated start request")
            return self.publish_and_snapshot(
                upload_id,
                lambda checkout: start_agent(checkout, start_request),
            )

        return handle

    def start_async_handler(
        self,
        start_agent: Callable[[Path, dict[str, object]], _T],
    ) -> Callable[[dict[str, object]], _T]:
        """Async start handler that retains uploads through awaited startup."""
        async def handle(request: dict[str, object]) -> _T:
            upload_id = request["upload_id"]
            start_request = request["request"]
            if not isinstance(upload_id, str) or not isinstance(start_request, dict):
                raise RemoteUploadError("Invalid validated start request")
            _validate_upload_id(upload_id)
            source = self.completed / upload_id
            # This daemon serializes controller starts in-process; the async
            # lock avoids blocking the event loop while a worker starts.
            async with self._async_start_lock:
                try:
                    info = source.lstat()
                except FileNotFoundError as err:
                    raise RemoteUploadError("Upload directory does not exist") from err
                resolved = source.resolve(strict=True)
                if not stat.S_ISDIR(info.st_mode) or resolved.parent != self.completed:
                    raise RemoteUploadError("Upload path must be a real directory")
                if not any(source.iterdir()):
                    raise RemoteUploadError("Upload directory is empty")
                result = start_agent(resolved, start_request)
                if inspect.isawaitable(result):
                    result = await result
                shutil.rmtree(resolved)
                return result

        return handle

    def cleanup_abandoned(self, *, older_than_seconds: int, now: float | None = None) -> tuple[str, ...]:
        """Remove stale incoming directories while holding the publication lock."""
        if (
            isinstance(older_than_seconds, bool)
            or not isinstance(older_than_seconds, int)
            or older_than_seconds < 60
        ):
            raise RemoteUploadError("older_than_seconds must be an integer of at least 60")
        cutoff = (now if now is not None else time.time()) - older_than_seconds
        removed: list[str] = []
        with self._publication_lock():
            for entry in (*self.incoming.iterdir(), *self.completed.iterdir()):
                try:
                    info = entry.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(info.st_mode) or info.st_mtime > cutoff:
                    continue
                if entry.parent not in {self.incoming, self.completed}:
                    continue
                if entry.resolve(strict=True).parent != entry.parent:
                    continue
                shutil.rmtree(entry)
                removed.append(entry.name)
        return tuple(removed)

    def _publication_lock(self) -> _FileLock:
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        return _FileLock(descriptor)


class _FileLock:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def __enter__(self) -> _FileLock:
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)
