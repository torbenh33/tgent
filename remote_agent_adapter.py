"""Thin local adapter for uploading a project and starting a remote agent."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import uuid
import warnings
from pathlib import Path

from remote_agent_config import RemoteAgentConfig
from remote_agent_protocol import RemoteAgentClient, RemoteProtocolError, validate_request


class RemoteAgentAdapterError(RuntimeError):
    """Raised when project upload or remote agent startup fails."""


def _project_name(project: Path) -> str:
    return str(project).replace(os.sep, "_")


class RemoteAgentAdapter:
    """Synchronize Git objects and working-tree files for remote agents."""

    def __init__(self, config: RemoteAgentConfig, *, client=None, rsync: str = "rsync") -> None:
        self.config = config
        self.client = client or RemoteAgentClient(config)
        self.rsync = rsync

    def _sync_project(self, project_root, *, base=None, expected_name=None, upload_id=None, upload_files=True):
        project = Path(project_root).expanduser().resolve(strict=True)
        git = ["git", "-C", str(project)]
        try:
            if base is None:
                base = subprocess.run([*git, "rev-parse", "--verify", "HEAD^{commit}"], capture_output=True, text=True, check=True).stdout.strip()
            if not isinstance(base, str) or len(base) not in (40, 64) or any(c not in "0123456789abcdef" for c in base):
                raise RemoteAgentAdapterError("Invalid recorded base commit")
            tree = subprocess.run([*git, "ls-tree", "-r", "-z", base], capture_output=True, check=True).stdout
            index = subprocess.run([*git, "ls-files", "--stage", "-z"], capture_output=True, check=True).stdout
            if any(entry.startswith(b"160000 ") for data in (tree, index) for entry in data.split(b"\0") if entry):
                raise RemoteAgentAdapterError("Remote project synchronization does not support submodules")
            name = _project_name(project)
            if expected_name is not None and name != expected_name:
                raise RemoteAgentAdapterError("Continuation project does not match the agent project")
            ensured = self._request("ensure_project", project_name=name, source_path=str(project))
            if ensured.get("collision"):
                warnings.warn(f"Project mirror name collision: {name}", RuntimeWarning)
            remote = str(self.config.project_root_dir / name)
            destination = f"{self.config.ssh_host}:{shlex.quote(remote)}"
            subprocess.run([*git, "push", destination, "+refs/*:refs/*", f"{base}:refs/agent-sync/{base}"], check=True, capture_output=True)
            if not upload_files:
                return name, base, None
            selected_upload = upload_id or uuid.uuid4().hex
            self._request("prepare_upload", upload_id=selected_upload)
            remote_path = str(self.config.uploads_dir / "incoming" / selected_upload)
            subprocess.run([self.rsync, "-a", "--checksum", "--delete", "--exclude", ".git", "--", str(project) + "/", f"{self.config.ssh_host}:{shlex.quote(remote_path)}/"], check=True)
            self._request("finalize_upload", upload_id=selected_upload)
            return name, base, selected_upload
        except (OSError, subprocess.CalledProcessError) as err:
            raise RemoteAgentAdapterError(f"Project synchronization failed: {err}") from err

    def start(self, project_root, request, *, request_id=None, upload_id=None):
        name, base, selected_upload = self._sync_project(project_root, upload_id=upload_id)
        message = validate_request({"schema_version": 1, "operation": "start", "request_id": request_id or uuid.uuid4().hex,
                                    "upload_id": selected_upload, "request": {**request, "_project_name": name, "_base_commit": base}})
        try:
            result = self.client.request(message)
        except RemoteProtocolError as err:
            raise RemoteAgentAdapterError(f"Remote start request failed: {err}") from err
        if not result["ok"]:
            raise RemoteAgentAdapterError("Remote service rejected agent start")
        return result

    def list_agents(self) -> list[dict[str, object]]:
        """List all agents registered by the remote controller."""
        return self._request("list_agents").get("agents", [])

    def status(self, agent_id: str) -> dict[str, object]:
        """Retrieve persisted remote agent status."""
        return self._request("status", agent_id=agent_id)

    def history(self, agent_id: str, *, limit: int = 10_000) -> dict[str, object]:
        """Retrieve the most recent bounded remote conversation messages."""
        return self._request("history", agent_id=agent_id, limit=limit)

    def summarize_history(self, agent_id: str) -> str:
        """Summarize a remote conversation using the remote service LLM."""
        result = self._request("summarize_history", agent_id=agent_id)
        summary = result.get("summary")
        if not isinstance(summary, str): raise RemoteAgentAdapterError("Remote history summary is invalid")
        return summary

    def review_replies(self, repo_path: str = ".", *, unacknowledged_only: bool = False) -> dict[str, object]:
        """Fetch review replies across all local series in one remote request."""
        return self._request("review_replies", repo_path=repo_path, unacknowledged_only=unacknowledged_only)

    def continue_agent(self, agent_id: str, messages: list[dict[str, object]], project_root: str | os.PathLike[str] | None = None) -> dict[str, object]:
        """Continue using the recorded base, optionally transferring current files."""
        if project_root is None:
            return self._request("continue", agent_id=agent_id, messages=messages)
        status = self._request("status", agent_id=agent_id)
        if status.get("state") in {"awaiting_review", "review_received"}:
            return self._request("continue", agent_id=agent_id, messages=messages)
        base, name = status.get("base_commit"), status.get("project_name")
        if not isinstance(base, str) or not isinstance(name, str):
            raise RemoteAgentAdapterError("Agent has no recorded mirror or base commit")
        self._sync_project(project_root, base=base, expected_name=name, upload_files=False)
        return self._request("continue", agent_id=agent_id, messages=messages)

    def get_agent_tools(self, agent_id: str) -> dict[str, object]:
        """Retrieve discovered remote project MCP tools and enabled status."""
        return self._request("agent_tools", agent_id=agent_id)

    def set_agent_tools(self, agent_id: str, tools: list[str]) -> dict[str, object]:
        """Set the remote controller's explicit MCP tool allowlist."""
        return self._request("set_tools", agent_id=agent_id, tools=tools)

    def stop(self, agent_id: str) -> dict[str, object]:
        """Stop a remote agent and wait for its worker to exit."""
        return self._request("stop", agent_id=agent_id)

    def logs(self, agent_id: str, *, max_bytes: int = 65_536) -> dict[str, object]:
        """Retrieve a bounded tail of the remote worker stderr log."""
        return self._request("logs", agent_id=agent_id, max_bytes=max_bytes)

    def acknowledge_patch_applied(self, agent_id: str) -> dict[str, object]:
        """Record explicit acknowledgement of an already-applied submitted patch."""
        return self._request("acknowledge_patch_applied", agent_id=agent_id)

    def reap(self, agent_id: str) -> dict[str, object]:
        """Reap one eligible remote agent."""
        return self._request("reap", agent_id=agent_id)

    def reap_agents(self, agent_ids: list[str]) -> dict[str, object]:
        """Bulk reap through one remote protocol request."""
        return self._request("reap_agents", agent_ids=agent_ids)

    def stop_agents(self) -> dict[str, object]:
        """Stop eligible remote workers in one request."""
        return self._request("stop_agents")

    def restart_agents(self, agent_ids: list[str]) -> dict[str, object]:
        """Bulk restart eligible agents through one request."""
        return self._request("restart_agents", agent_ids=agent_ids)

    def nack_patch(self, agent_id: str, reason: str | None = None) -> dict[str, object]:
        """Record operator rejection of an inactive submitted patch."""
        return self._request("nack_patch", agent_id=agent_id, reason=reason)

    def pending_patches(self) -> list[dict[str, object]]:
        """Enumerate pending patch metadata with one remote protocol call."""
        result = self._request("pending_patches")
        patches = result.get("patches")
        if not isinstance(patches, list):
            raise RemoteAgentAdapterError("Remote pending patches response is invalid")
        return patches

    def retrieve_patches(self, patches: list[dict[str, object]], destination: str | os.PathLike[str]) -> dict[str, Path | Exception]:
        """Transfer validated artifact set in one rsync invocation."""
        directory = Path(destination).expanduser().resolve()
        if not directory.is_dir():
            raise RemoteAgentAdapterError(f"Patch destination is not a directory: {directory}")
        valid = []
        results: dict[str, Path | Exception] = {}
        import re
        for item in patches:
            aid, name = item.get("agent_id"), item.get("artifact")
            if not isinstance(aid, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", aid) is None or name != f"{aid}.patch":
                if isinstance(aid, str): results[aid] = RemoteAgentAdapterError("Invalid artifact identity")
                continue
            size, digest = item.get("bytes"), item.get("sha256")
            if type(size) is not int or not 0 <= size <= 16 * 1024 * 1024 or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                results[aid] = RemoteAgentAdapterError("Invalid artifact metadata")
                continue
            valid.append(item)
        if not valid:
            return results
        sources = [f"{self.config.ssh_host}:{shlex.quote(str(self.config.patches_dir / str(item['artifact'])))}" for item in valid]
        try:
            subprocess.run((self.rsync, "-a", "--", *sources, os.fspath(directory) + "/"), check=True)
        except (OSError, subprocess.CalledProcessError) as err:
            for item in valid: results[str(item["agent_id"])] = RemoteAgentAdapterError(f"Patch batch transfer failed: {err}")
            return results
        for item in valid:
            aid = str(item["agent_id"]); target = directory / str(item["artifact"])
            if target.is_symlink() or not target.is_file() or target.stat().st_size != item["bytes"] or __import__("hashlib").sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                results[aid] = RemoteAgentAdapterError("Retrieved artifact does not match metadata")
            else: results[aid] = target
        return results

    def patches(self, agent_id: str) -> dict[str, object]:
        """Return metadata for the remote patch artifact, if one is available."""
        return self._request("patches", agent_id=agent_id)

    def retrieve_patch(self, agent_id: str, destination: str | os.PathLike[str] = "/tmp") -> Path | None:
        """Download an available patch artifact into an existing local directory.

        The destination must be a directory, not a filename. The fixed artifact
        name is generated from the validated remote agent ID.
        """
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
