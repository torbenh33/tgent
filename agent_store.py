"""Durable per-agent metadata and CopilotChat-format conversation storage."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

AGENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
METADATA_FILENAME = "metadata.json"
HISTORY_FILENAME = "history.json"
SUMMARY_FILENAME = "history_summary.json"


class AgentStoreError(RuntimeError):
    """Raised when agent state is invalid or cannot be persisted safely."""


def validate_agent_id(agent_id: str) -> str:
    """Validate an externally supplied agent identifier before path use."""
    if not isinstance(agent_id, str) or AGENT_ID_RE.fullmatch(agent_id) is None:
        raise AgentStoreError("Invalid agent_id")
    return agent_id


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as err:
        raise AgentStoreError(f"Value is not JSON serializable: {err}") from err


class AgentStore:
    """Persist controller metadata separately from the conversation array.

    History messages are stored and returned as supplied; this class does not
    translate or normalize CopilotChat fields. The controller should serialize
    concurrent updates for a given agent (for example with an asyncio lock).
    """

    def __init__(self, state_root: str | os.PathLike[str]) -> None:
        self.state_root = Path(state_root).expanduser().absolute()

    def _agent_dir(self, agent_id: str) -> Path:
        return self.state_root / validate_agent_id(agent_id)

    def metadata_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / METADATA_FILENAME

    def history_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / HISTORY_FILENAME

    def summary_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / SUMMARY_FILENAME

    def get_cached_summary(self, agent_id: str) -> str | None:
        """Return a summary only if it matches the current transcript version."""
        path = self.summary_path(agent_id)
        if path.is_symlink() or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            stat = self.history_path(agent_id).stat()
            if data.get("history_mtime_ns") != stat.st_mtime_ns or data.get("history_size") != stat.st_size:
                return None
            return data["summary"] if isinstance(data.get("summary"), str) else None
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            return None

    def cache_summary(self, agent_id: str, summary: str) -> None:
        """Persist a summary tagged with the current history file version."""
        stat = self.history_path(agent_id).stat()
        _atomic_write(self.summary_path(agent_id), _json_bytes({"history_mtime_ns": stat.st_mtime_ns, "history_size": stat.st_size, "summary": summary}))

    def create(self, agent_id: str, metadata: dict[str, Any], history: list[dict[str, Any]]) -> None:
        """Create an agent record once; refuse to overwrite an existing ID."""
        agent_dir = self._agent_dir(agent_id)
        if agent_dir.exists():
            raise AgentStoreError(f"Agent already exists: {agent_id}")
        if not isinstance(metadata, dict) or not isinstance(history, list):
            raise AgentStoreError("metadata must be an object and history must be an array")
        if any(not isinstance(message, dict) for message in history):
            raise AgentStoreError("Every history entry must be a message object")
        agent_dir.mkdir(mode=0o700, parents=True)
        try:
            _atomic_write(agent_dir / METADATA_FILENAME, _json_bytes(metadata))
            _atomic_write(agent_dir / HISTORY_FILENAME, _json_bytes(history))
        except Exception:
            # Never leave a partially created record that a later start could
            # mistake for a successfully initialized agent.
            for child in agent_dir.iterdir():
                child.unlink(missing_ok=True)
            agent_dir.rmdir()
            raise

    def get_metadata(self, agent_id: str) -> dict[str, Any]:
        """Read an agent's controller metadata."""
        return self._read_object(self.metadata_path(agent_id), "metadata")

    def update_metadata(self, agent_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Atomically merge controller-owned fields into metadata."""
        if not isinstance(updates, dict):
            raise AgentStoreError("metadata updates must be an object")
        path = self.metadata_path(agent_id)
        current = self._read_object(path, "metadata")
        current.update(updates)
        current["updated_at"] = time.time()
        _atomic_write(path, _json_bytes(current))
        return current

    def get_history(self, agent_id: str) -> list[dict[str, Any]]:
        """Read the complete CopilotChat-format message array."""
        path = self.history_path(agent_id)
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as err:
            raise AgentStoreError(f"Unknown agent: {agent_id}") from err
        except (OSError, UnicodeError, json.JSONDecodeError) as err:
            raise AgentStoreError(f"Could not read agent history: {err}") from err
        if not isinstance(history, list) or any(not isinstance(message, dict) for message in history):
            raise AgentStoreError(f"Invalid history file for agent {agent_id}")
        return history

    def append_history(self, agent_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Append messages and durably persist the complete transcript."""
        if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
            raise AgentStoreError("messages must be an array of message objects")
        history = self.get_history(agent_id)
        history.extend(messages)
        _atomic_write(self.history_path(agent_id), _json_bytes(history))
        self.summary_path(agent_id).unlink(missing_ok=True)
        return history

    def remove(self, agent_id: str) -> None:
        """Remove stored agent state; caller must ensure resources are reaped."""
        agent_dir = self._agent_dir(agent_id)
        if agent_dir.is_symlink() or not agent_dir.is_dir():
            raise AgentStoreError(f"Refusing to remove invalid agent state directory: {agent_dir}")
        for path in agent_dir.iterdir():
            if path.is_file() and not path.is_symlink():
                path.unlink()
            else:
                raise AgentStoreError(f"Refusing to remove unexpected agent state entry: {path}")
        agent_dir.rmdir()

    @staticmethod
    def _read_object(path: Path, label: str) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as err:
            raise AgentStoreError(f"Unknown agent ({label} not found)") from err
        except (OSError, UnicodeError, json.JSONDecodeError) as err:
            raise AgentStoreError(f"Could not read agent {label}: {err}") from err
        if not isinstance(value, dict):
            raise AgentStoreError(f"Invalid agent {label}: expected an object")
        return value
