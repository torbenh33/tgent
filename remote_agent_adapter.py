"""Thin local adapter for uploading a project and starting a remote agent."""

from __future__ import annotations

import os
import shlex
import subprocess
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
