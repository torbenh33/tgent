"""Thin local adapter for uploading a project and starting a remote agent."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path

from remote_agent_config import RemoteAgentConfig
from remote_agent_protocol import RemoteAgentClient, RemoteProtocolError, validate_request


class RemoteAgentAdapterError(RuntimeError):
    """Raised when project upload or remote agent startup fails."""


DEFAULT_RSYNC_EXCLUDES = (
    ".venv/", "venv/", "__pycache__/", ".pytest_cache/", ".mypy_cache/",
    ".ruff_cache/", ".tox/", "node_modules/", ".cache/", ".ssh/", ".aws/",
    ".azure/", ".config/gcloud/", ".env", ".env.*", "*.pem", "*.key",
    "id_rsa", "id_ed25519", "credentials", "secrets/",
)


class RemoteAgentAdapter:
    """Upload a checkout to a unique path and issue a fixed start request."""

    def __init__(self, config: RemoteAgentConfig, *, client=None, rsync: str = "rsync") -> None:
        self.config = config
        self.client = client or RemoteAgentClient(config)
        self.rsync = rsync

    def start(
        self,
        project_root: str | os.PathLike[str],
        request: dict[str, object],
        *,
        request_id: str | None = None,
        upload_id: str | None = None,
    ) -> dict[str, object]:
        """Upload project state and request a remote snapshot/start."""
        project = Path(project_root).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise RemoteAgentAdapterError(f"Project root is not a directory: {project}")
        selected_request_id = request_id or uuid.uuid4().hex
        selected_upload_id = upload_id or uuid.uuid4().hex
        message = validate_request({
            "schema_version": 1,
            "operation": "start",
            "request_id": selected_request_id,
            "upload_id": selected_upload_id,
            "request": request,
        })
        try:
            prepared = self.client.request(
                {
                    "schema_version": 1,
                    "operation": "prepare_upload",
                    "request_id": uuid.uuid4().hex,
                    "upload_id": selected_upload_id,
                }
            )
            if not prepared["ok"]:
                raise RemoteAgentAdapterError("Remote service rejected upload preparation")
        except RemoteProtocolError as err:
            raise RemoteAgentAdapterError(f"Could not prepare remote upload: {err}") from err

        remote_path = str(self.config.uploads_dir / "incoming" / selected_upload_id)
        rsync_argv = [self.rsync, "-a"]
        for pattern in DEFAULT_RSYNC_EXCLUDES:
            rsync_argv.extend(("--exclude", pattern))
        rsync_argv.extend(("--", os.fspath(project) + "/", f"{self.config.ssh_host}:{shlex.quote(remote_path)}/"))
        try:
            subprocess.run(tuple(rsync_argv), check=True)
        except (OSError, subprocess.CalledProcessError) as err:
            raise RemoteAgentAdapterError(f"Project upload failed: {err}") from err
        try:
            finalized = self.client.request(
                {
                    "schema_version": 1,
                    "operation": "finalize_upload",
                    "request_id": uuid.uuid4().hex,
                    "upload_id": selected_upload_id,
                }
            )
            if not finalized["ok"]:
                raise RemoteAgentAdapterError("Remote service rejected completed upload")
            result = self.client.request(message)
            if not result["ok"]:
                raise RemoteAgentAdapterError("Remote service rejected agent start")
            return result
        except RemoteProtocolError as err:
            raise RemoteAgentAdapterError(f"Remote start request failed: {err}") from err

    def list_agents(self) -> list[dict[str, object]]:
        """List all agents registered by the remote controller."""
        return self._request("list_agents").get("agents", [])

    def status(self, agent_id: str) -> dict[str, object]:
        """Retrieve persisted remote agent status."""
        return self._request("status", agent_id=agent_id)

    def history(self, agent_id: str, *, limit: int = 10_000) -> dict[str, object]:
        """Retrieve the most recent bounded remote conversation messages."""
        return self._request("history", agent_id=agent_id, limit=limit)

    def continue_agent(self, agent_id: str, messages: list[dict[str, object]]) -> dict[str, object]:
        """Append operator or review messages and resume a live remote agent."""
        return self._request("continue", agent_id=agent_id, messages=messages)

    def stop(self, agent_id: str) -> dict[str, object]:
        """Stop a remote agent and wait for its worker to exit."""
        return self._request("stop", agent_id=agent_id)

    def logs(self, agent_id: str, *, max_bytes: int = 65_536) -> dict[str, object]:
        """Retrieve a bounded tail of the remote worker stderr log."""
        return self._request("logs", agent_id=agent_id, max_bytes=max_bytes)

    def reap(self, agent_id: str) -> dict[str, object]:
        """Remove a stopped remote agent's snapshot and controller state."""
        return self._request("reap", agent_id=agent_id)

    def patches(self, agent_id: str) -> dict[str, object]:
        """Return metadata for the remote patch artifact, if one is available."""
        return self._request("patches", agent_id=agent_id)

    def retrieve_patch(self, agent_id: str, destination: str | os.PathLike[str]) -> Path | None:
        """Download an available patch artifact into a local directory."""
        result = self.patches(agent_id)
        if result.get("available") is not True:
            return None
        directory = Path(destination).expanduser().resolve()
        if not directory.is_dir():
            raise RemoteAgentAdapterError(f"Patch destination is not a directory: {directory}")
        artifact_name = f"{agent_id}.patch"
        if result.get("artifact") != artifact_name:
            raise RemoteAgentAdapterError("Remote service returned an invalid patch artifact name")
        size = result.get("bytes")
        if type(size) is not int or not 0 <= size <= 16 * 1024 * 1024:
            raise RemoteAgentAdapterError("Remote service returned an invalid patch artifact size")

        remote_path = str(self.config.patches_dir / artifact_name)
        target = directory / artifact_name
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{artifact_name}.", dir=directory)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            subprocess.run(
                (
                    self.rsync,
                    "-a",
                    "--",
                    f"{self.config.ssh_host}:{shlex.quote(remote_path)}",
                    os.fspath(temporary),
                ),
                check=True,
            )
            if temporary.is_symlink() or not temporary.is_file() or temporary.stat().st_size != size:
                raise RemoteAgentAdapterError("Retrieved patch artifact does not match its metadata")
            os.replace(temporary, target)
        except (OSError, subprocess.CalledProcessError) as err:
            raise RemoteAgentAdapterError(f"Patch retrieval failed: {err}") from err
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _request(self, operation: str, **fields: object) -> dict[str, object]:
        message = {
            "schema_version": 1,
            "operation": operation,
            "request_id": uuid.uuid4().hex,
            **fields,
        }
        try:
            response = self.client.request(message)
        except RemoteProtocolError as err:
            raise RemoteAgentAdapterError(f"Remote {operation} request failed: {err}") from err
        if not response["ok"]:
            error = response.get("error")
            detail = error.get("message", "Remote operation failed") if isinstance(error, dict) else "Remote operation failed"
            raise RemoteAgentAdapterError(str(detail))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RemoteAgentAdapterError(f"Remote {operation} returned a non-object result")
        return result
