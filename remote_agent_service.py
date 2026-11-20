"""Persistent controller service and fixed SSH stdio proxy."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
from datetime import datetime, timezone
import inspect
import json
import mailbox
import hashlib
import email.message
import email.utils
import logging
import os
import secrets
import re
import warnings
import socket
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from agent_controller import AgentController
from agent_sandbox import RuntimeManifest, SandboxError, RuntimeMount, SandboxRuntimeLayout
from llm import LLMClient
from remote_agent_config import RemoteAgentConfig, load_remote_agent_config
from remote_agent_daemon import build_async_operation_handlers
from remote_agent_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    SCHEMA_VERSION,
    RemoteProtocolError,
    error_response,
    validate_request,
)
from remote_agent_upload import RemoteUploadManager


def _bounded_event_text(value: object, limit: int) -> str:
    """Normalize untrusted event text for durable metadata and plain-text mail."""
    text = str(value).replace("\\r", " ").replace("\\x00", "")
    return text if len(text) <= limit else text[:limit] + "\\n[truncated]"


def _json_line(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + bytes((10,))


def _managed_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise RuntimeError(f"Managed directory must be a real directory owned by this user: {path}")
    os.chmod(path, 0o700)


def _selected_runtime(config: RemoteAgentConfig) -> RuntimeManifest:
    """Describe the single deployed app as both service and worker runtime."""
    from agent_sandbox import read_runtime_manifest

    runtime_root = Path(str(config.runtime_dir))
    versions = runtime_root / "versions"
    candidates: list[RuntimeManifest] = []
    if versions.is_dir():
        for manifest_path in versions.glob("*/manifest.json"):
            try:
                candidates.append(read_runtime_manifest(manifest_path))
            except (OSError, ValueError, SandboxError):
                continue
    if candidates:
        return max(candidates, key=lambda manifest: manifest.rsync_completed_at)

    app = Path(str(config.remote_base_dir / "app"))
    python = app / "bin" / "python"
    if not python.is_file() or not (app / "pyvenv.cfg").is_file():
        raise SandboxError(f"No valid deployed app environment at {app}")
    return RuntimeManifest(
        version="app-current",
        rsync_completed_at=datetime.now(timezone.utc),
        layout=SandboxRuntimeLayout((RuntimeMount(app, app),)),
    )

def _runtime_worker_command(manifest: RuntimeManifest) -> tuple[str, ...]:
    """Use the sandbox path corresponding to the mounted Python environment."""
    for mount in manifest.layout.mounts:
        source = Path(str(mount.source))
        if not (source / "pyvenv.cfg").is_file():
            continue
        for executable in ("python", "python3"):
            if (source / "bin" / executable).is_file():
                target = Path(str(mount.target)) / "bin" / executable
                return (str(target), "-m", "agent_worker")
    raise SandboxError(f"Runtime {manifest.version} has no mounted Python virtual environment")


class RemoteAgentService:
    """Long-lived controller process; SSH invocations only proxy requests."""

    def __init__(self, config: RemoteAgentConfig) -> None:
        self.config = config
        for configured in (
            config.remote_base_dir,
            config.state_dir,
            config.snapshots_dir,
            config.logs_dir,
            config.uploads_dir,
            config.project_root_dir,
        ):
            _managed_directory(Path(str(configured)))
        self.runtime_manifest = _selected_runtime(config)
        self.uploads = RemoteUploadManager(config.uploads_dir)
        self._llm: LLMClient | None = None
        self.controller = AgentController(
            project_root=None,
            state_root=str(config.state_dir),
            snapshot_root=str(config.snapshots_dir),
            model_callback=self._get_llm().model_callback,
            patch_submit_callback=self._submit_patch_from_controller,
            runtime_layout=self.runtime_manifest.layout,
            runtime_version=self.runtime_manifest.version,
            runtime_app_path=str(config.remote_base_dir / "app"),
            worker_command=_runtime_worker_command(self.runtime_manifest),
            max_model_calls=int(os.environ.get("TGIT_AGENT_MAX_MODEL_CALLS", "200")),
            max_runtime_seconds=int(os.environ.get("TGIT_AGENT_MAX_RUNTIME_SECONDS", "43200")),
        )
        self.handlers = build_async_operation_handlers(
            self.uploads,
            start_agent=self._start_agent,
            lifecycle_handlers={
                "status": self._status,
                "history": self._history,
                "continue": self._continue,
                "ensure_project": self._ensure_project,
                "stop": self._stop,
                "stop_agents": self._stop_agents,
                "restart_agents": self._restart_agents,
                "logs": self._logs,
                "reap": self._reap,
                "reap_agents": self._reap_agents,
                "acknowledge_patch_applied": self._acknowledge_patch_applied,
                "nack_patch": self._nack_patch,
                "submit_patch": self._submit_patch_protocol,
                "fetch_pending_agent_mail": self._fetch_pending_agent_mail,
                "acknowledge_event_mail": self._acknowledge_event_mail,
                "integrate_submission": self._integrate_submission,
                "list_agents": self._list_agents,
                "set_tools": self._set_tools,
                "agent_tools": self._agent_tools,
                "summarize_history": self._summarize_history,
                "review_replies": self._review_replies,
            },
        )
        self.socket_path = Path(str(config.state_dir)) / "remote-agent.sock"
        self.server: asyncio.AbstractServer | None = None
        self._service_lock_fd: int | None = None

    def _acquire_service_lock_and_clear_stale_socket(self) -> None:
        """Lock service ownership and remove only a stale Unix socket."""
        lock_path = self.socket_path.with_suffix(".lock")
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("Service lock must be a regular file owned by this user")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                raise RuntimeError("Remote agent service is already running") from err

            try:
                socket_info = self.socket_path.lstat()
            except FileNotFoundError:
                socket_info = None
            if socket_info is not None:
                if not stat.S_ISSOCK(socket_info.st_mode):
                    raise RuntimeError(f"Refusing to replace non-socket service path: {self.socket_path}")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(os.fspath(self.socket_path))
                    except (ConnectionRefusedError, FileNotFoundError):
                        self.socket_path.unlink()
                    else:
                        raise RuntimeError(f"Service socket is active: {self.socket_path}")
            self._service_lock_fd = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(os.environ.get("TGIT_AGENT_LLM_CONFIG", "config.toml"))
        return self._llm

    async def _submit_patch_from_controller(self, snapshot, params: dict[str, Any]) -> dict[str, Any]:
        """Use the authoritative controller registration to resolve the work repo."""
        agent_id = params["agent_id"]
        metadata = self.controller.store.get_metadata(agent_id)
        if Path(metadata["snapshot_project"]).resolve() != Path(snapshot.project_root).resolve():
            raise RuntimeError("Submission snapshot does not match registered agent work repository")
        request = {**params, "agent_id": agent_id}
        return await self._submit_patch_request(request)

    async def _submit_patch_protocol(self, request: dict[str, Any]) -> dict[str, Any]:
        agent_id = request["agent_id"]
        metadata = self.controller.store.get_metadata(agent_id)
        head = request["head_commit"]
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
            raise RuntimeError("head_commit must be a full object id")
        if head.lower() == request["base_commit"].lower():
            raise RuntimeError("head_commit must differ from base_commit; submit at least one commit")
        if metadata.get("base_commit") != request["base_commit"]:
            raise RuntimeError("base_commit does not match registered agent")
        snapshot_path = Path(metadata["snapshot_project"])
        snapshot_root = Path(metadata["snapshot_root"])
        if snapshot_path.is_symlink() or snapshot_root.is_symlink() or snapshot_path.parent != snapshot_root:
            raise RuntimeError("Unsafe service-managed agent work repository path")
        snapshot = type("SubmissionSnapshot", (), {"root": snapshot_root, "project_root": snapshot_path, "base_commit": metadata["base_commit"]})()
        # Do not permit the caller to supply source or destination: both derive
        # solely from the registered agent and service project configuration.
        request = {**request, "_project_mirror": metadata.get("project_mirror")}
        return await self._submit_patch(snapshot, request)

    async def _submit_patch_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Resolve submission source from controller-managed agent metadata."""
        agent_id = request["agent_id"]
        metadata = self.controller.store.get_metadata(agent_id)
        snapshot_path = Path(metadata["snapshot_project"])
        snapshot_root = Path(metadata["snapshot_root"])
        if snapshot_path.is_symlink() or snapshot_root.is_symlink() or snapshot_path.parent != snapshot_root:
            raise RuntimeError("Unsafe service-managed agent work repository path")
        snapshot = type("SubmissionSnapshot", (), {
            "root": snapshot_root, "project_root": snapshot_path,
            "base_commit": metadata["base_commit"],
        })()
        request = {**request, "_project_mirror": metadata.get("project_mirror")}
        return await self._submit_patch(snapshot, request)

    async def _submit_patch(self, snapshot, params: dict[str, Any]) -> dict[str, Any]:
        """Publish a submitted commit into the project bare mirror."""
        return publish_submission({**params, "_work_repository": str(snapshot.project_root)}, state_dir=Path(str(self.config.state_dir)), project_root_dir=Path(str(self.config.project_root_dir)))




    def _repository(self, name):
        if not isinstance(name, str) or not name.startswith("_") or name in {"_", "_.."} or "/" in name or "\x00" in name:
            raise RemoteProtocolError("Invalid project mirror name")
        repository = Path(str(self.config.project_root_dir)) / name
        if repository.is_symlink():
            raise RemoteProtocolError("Project mirror must not be a symlink")
        return repository

    async def _ensure_project(self, request):
        name, source = request["project_name"], request["source_path"]
        repository = self._repository(name)
        if not isinstance(source, str) or not source.startswith("/") or source.replace("/", "_") != name:
            raise RemoteProtocolError("Invalid project source path")
        subprocess.run(["git", "init", "--bare", str(repository)], check=True, capture_output=True)
        marker = repository / "agent-source-path"
        collision = False
        try:
            with marker.open("x", encoding="utf-8") as stream:
                stream.write(source)
        except FileExistsError:
            if marker.read_text(encoding="utf-8") != source:
                collision = True
                warnings.warn(f"Project mirror name collision: {name}", RuntimeWarning)
        return {"status": "ready", "collision": collision}

    async def _start_agent(self, checkout: Path, request: dict[str, object]) -> dict[str, Any]:
        request = dict(request)
        task = request.get("task")
        name, base = request.pop("_project_name", None), request.pop("_base_commit", None)
        repository = self._repository(name)
        if not isinstance(base, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base) is None:
            raise RemoteProtocolError("Invalid project mirror base commit")
        work = repository.parent / (name + "-work-" + secrets.token_hex(8))
        subprocess.run(["git", "clone", "--no-local", "--no-checkout", "--reference", str(repository), str(repository), str(work)], check=True, capture_output=True)
        # Fetch refs under a temporary namespace first: writing directly to
        # refs/* can fail when that includes the currently checked-out branch.
        subprocess.run(["git", "-C", str(work), "fetch", str(repository), "+refs/*:refs/agent-mirror/*"], check=True, capture_output=True)
        mirrored_refs = subprocess.run(["git", "-C", str(work), "for-each-ref", "--format=%(refname) %(objectname)", "refs/agent-mirror/"], check=True, capture_output=True, text=True).stdout
        for line in mirrored_refs.splitlines():
            temporary_ref, object_id = line.split()
            original_ref = "refs/" + temporary_ref.removeprefix("refs/agent-mirror/")
            subprocess.run(["git", "-C", str(work), "update-ref", original_ref, object_id], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(work), "update-ref", "-d", temporary_ref], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work), "checkout", "--detach", base], check=True, capture_output=True)
        subprocess.run(["rsync", "-a", "--checksum", "--exclude", ".git", "--delete", str(checkout) + "/", str(work) + "/"], check=True, capture_output=True)
        result = await self.controller.start_agent(request, source_root=work, _base_commit=base, _mirror_path=repository, _project_name=name)
        agent_id = result.get("agent_id")
        if isinstance(agent_id, str):
            self._record_lifecycle_event("start", agent_id, {"task": _bounded_event_text(task, 4000), "base_commit": base})
        return result

    async def _status(self, request):
        return await self.controller.get_status(request["agent_id"])

    def _record_waiting_questions(self) -> None:
        """Reconcile persisted operator questions into stable mail events."""
        state_root = getattr(self.controller.store, "state_root", None)
        if state_root is None:
            return
        for metadata_path in Path(state_root).glob("*/metadata.json"):
            if metadata_path.is_symlink() or not metadata_path.is_file():
                continue
            agent_id = metadata_path.parent.name
            if not isinstance(agent_id, str):
                continue
            metadata = self.controller.store.get_metadata(agent_id)
            question = metadata.get("operator_question")
            if metadata.get("stop_reason") == "awaiting_command" and isinstance(question, str) and question.strip():
                self._record_lifecycle_event("operator_question", agent_id, {"question": _bounded_event_text(question, 4000)})

    async def _history(self, request):
        history = await self.controller.get_history(request["agent_id"])
        return {"agent_id": request["agent_id"], "messages": history[-request["limit"]:]}

    async def _summarize_history(self, request):
        history = await self.controller.get_history(request["agent_id"])
        encoded = json.dumps(history[-200:], ensure_ascii=False)[-100_000:]
        response = await self._get_llm().model_callback({"messages": [
            {"role": "system", "content": "Summarize this coding agent conversation for an operator: task, key actions and decisions, outcome, unresolved issues, and evidence. Be concise and distinguish facts from uncertainty."},
            {"role": "user", "content": encoded},
        ], "max_tokens": 1200})
        try:
            content = response["choices"][0]["message"]["content"]
            if not isinstance(content, str): raise TypeError("content is not text")
            return {"summary": content}
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM returned an invalid conversation summary response") from exc

    async def _review_replies(self, request):
        from mcp_tgit_review import get_review_replies

        return get_review_replies(
            repo_path=request["repo_path"],
            unacknowledged_only=request["unacknowledged_only"],
        )

    def _record_lifecycle_event(self, kind: str, agent_id: str, details: dict[str, Any]) -> None:
        details = {key: (_bounded_event_text(value, 4000) if isinstance(value, str) else value) for key, value in details.items()}
        events_dir = Path(str(self.config.state_dir)) / "lifecycle-events"
        events_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        event_id = hashlib.sha256(f"{kind}:{agent_id}:{json.dumps(details, sort_keys=True)}".encode()).hexdigest()
        path = events_dir / f"{event_id}.json"
        if path.exists():
            return
        status = self.controller.store.get_metadata(agent_id)
        event = {"event_id": event_id, "kind": kind, "agent_id": agent_id, "event_time": datetime.now(timezone.utc).isoformat(), "state": status.get("state"), "details": details}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def _acknowledge_event_mail(self, request):
        event_id = request["event_id"]
        path = Path(str(self.config.state_dir)) / "lifecycle-events" / f"{event_id}.json"
        event = json.loads(path.read_text(encoding="utf-8"))
        event["message_id"] = request["message_id"]
        event["materialized_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        return {"event_id": event_id, "materialized": True}

    async def _continue(self, request):
        agent_id = request["agent_id"]
        messages = [dict(message) for message in request["messages"]]
        result = await self.controller.continue_agent(agent_id, messages)
        self._record_lifecycle_event("continuation", agent_id, {})
        return result

    async def _stop(self, request):
        result = await self.controller.stop_agent(request["agent_id"])
        self._record_lifecycle_event("stop", request["agent_id"], {"result": result})
        return result

    async def _logs(self, request):
        return await self.controller.get_agent_log(request["agent_id"], max_bytes=request["max_bytes"])

    async def _stop_agents(self, request):
        return await self.controller.stop_agents()

    async def _restart_agents(self, request):
        return await self.controller.restart_agents(request["agent_ids"])

    async def _reap(self, request):
        result = await self.controller.reap_agents([request["agent_id"]])
        return result["results"][0]

    async def _reap_agents(self, request):
        return await self.controller.reap_agents(request["agent_ids"])

    async def _integrate_submission(self, request):
        """Integrate an explicitly approved version into a new controlled worktree."""
        if request.get("approved") is not True:
            raise RemoteProtocolError("Integration requires explicit approval")
        agent_id, version = request["agent_id"], request["version"]
        identity = re.sub(r"[^A-Za-z0-9_-]", "-", agent_id)
        metadata_path = Path(str(self.config.state_dir)) / "submissions" / identity / f"v{version}.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise RuntimeError("Submission version metadata is missing")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("agent_id") != agent_id or metadata.get("version") != version:
            raise RuntimeError("Submission version identity is invalid")
        registered = self.controller.store.get_metadata(agent_id)
        mirror = Path(registered.get("project_mirror", ""))
        if mirror.parent != Path(str(self.config.project_root_dir)) or mirror.is_symlink():
            raise RuntimeError("Unsafe registered project mirror")
        ref = f"refs/agent-submissions/{identity}/v{version}"
        if metadata.get("ref") != ref:
            raise RuntimeError("Published submission ref is invalid")
        head = subprocess.run(("git", "--git-dir", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}"), check=True, capture_output=True, text=True).stdout.strip()
        if head.lower() != str(metadata.get("commit_id", "")).lower():
            raise RuntimeError("Published submission ref does not match version metadata")
        base = metadata.get("base")
        if not isinstance(base, str) or subprocess.run(("git", "--git-dir", str(mirror), "cat-file", "-e", f"{base}^{{commit}}"), capture_output=True).returncode:
            raise RuntimeError("Submission base commit is unavailable")
        branch = f"agent-integrations/{identity}/v{version}"
        integration_ref = f"refs/heads/{branch}"
        if subprocess.run(("git", "--git-dir", str(mirror), "show-ref", "--verify", "--quiet", integration_ref)).returncode == 0:
            raise RuntimeError("Integration branch already exists; refusing to overwrite it")
        target = Path(str(self.config.project_root_dir)) / f"integration-{identity}-v{version}"
        subprocess.run(("git", "--git-dir", str(mirror), "worktree", "add", "-b", branch, str(target), base), check=True, capture_output=True, text=True)
        try:
            subprocess.run(("git", "-C", str(target), "merge", "--no-edit", "--no-ff", head), check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError:
            raise RuntimeError(f"Integration conflict; resolve explicitly in controlled worktree {target}")
        commit = subprocess.run(("git", "-C", str(target), "rev-parse", "HEAD"), check=True, capture_output=True, text=True).stdout.strip()
        audit_ref = f"refs/agent-integration-audit/{identity}/v{version}"
        subprocess.run(("git", "--git-dir", str(mirror), "update-ref", audit_ref, commit), check=True, capture_output=True)
        return {"status": "integrated", "agent_id": agent_id, "version": version, "source_ref": ref, "source_commit": head, "base": base, "integration_ref": integration_ref, "integration_commit": commit, "worktree": str(target), "audit_ref": audit_ref}

    async def _acknowledge_patch_applied(self, request):
        return await self.controller.acknowledge_submitted_patch_applied(request["agent_id"])

    async def _nack_patch(self, request):
        return await self.controller.reject_submitted_patch(request["agent_id"], request["reason"] )

    async def _agent_tools(self, request):
        return await self.controller.get_agent_tools(request["agent_id"])

    async def _set_tools(self, request):
        """Apply an explicit operator request to the controller allowlist."""
        return await self.controller.set_agent_tools(request["agent_id"], request["tools"])

    async def _list_agents(self, request):
        """Return metadata for all registered remote agents."""
        return {"agents": await self.controller.list_agents()}

    def _generate_review_patch(self, agent_id: str, version: int) -> dict[str, Any]:
        """Generate the compatibility mbox afresh from a published submission ref."""
        identity = re.sub(r"[^A-Za-z0-9_-]", "-", agent_id)
        metadata_dir = Path(str(self.config.state_dir)) / "submissions"
        metadata_path = metadata_dir / identity / f"v{version}.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise RuntimeError("Submission metadata is missing")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("agent_id") != agent_id:
            raise RuntimeError("Submission metadata identity is invalid")
        mirror_value = metadata.get("project_mirror")
        # Publication records use the registered project mirror; recover it from the
        # controller registration rather than trusting metadata paths.
        registered = self.controller.store.get_metadata(agent_id)
        mirror_value = registered.get("project_mirror")
        if not isinstance(mirror_value, str):
            raise RuntimeError("Registered project mirror is unavailable")
        mirror = Path(mirror_value)
        if mirror.parent != Path(str(self.config.project_root_dir)) or mirror.is_symlink():
            raise RuntimeError("Unsafe registered project mirror")
        ref = metadata.get("ref")
        if not isinstance(ref, str) or not ref.startswith("refs/agent-submissions/"):
            raise RuntimeError("Published submission ref is invalid")
        resolved = subprocess.run(("git", "--git-dir", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}"), capture_output=True, text=True)
        if resolved.returncode or resolved.stdout.strip().lower() != str(metadata.get("commit_id", "")).lower():
            raise RuntimeError("Published submission ref is missing or does not match metadata")
        base, head = metadata.get("base"), metadata.get("commit_id")
        if not isinstance(base, str) or not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", base) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
            raise RuntimeError("Submission base or commit metadata is invalid")
        submission_id = metadata.get("submission_id")
        if not isinstance(submission_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,132}", submission_id) or submission_id != f"{agent_id}-v{version}":
            raise RuntimeError("Submission ID metadata is invalid")

        generated = subprocess.run(("git", "--git-dir", str(mirror), "format-patch", "--stdout", "--thread=shallow", "--numbered", "--cover-letter", f"{base}..{head}"), capture_output=True, timeout=60)
        if generated.returncode or not generated.stdout:
            raise RuntimeError("Could not generate review mbox from submitted commit series")
        raw = generated.stdout
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError("Generated review mbox exceeds size limit")
        # Replace git's placeholder cover body with the exact submitted message.
        from email import policy
        import tempfile
        with tempfile.TemporaryDirectory(prefix="agent-review-mbox-") as temporary_directory:
            input_path = Path(temporary_directory) / "generated.mbox"
            input_path.write_bytes(raw)
            messages = list(mailbox.mbox(input_path))
        if not messages:
            raise RuntimeError("Generated review mbox is malformed")
        cover = metadata.get("cover_letter_message")
        if not isinstance(cover, str):
            raise RuntimeError("Submission cover-letter metadata is invalid")
        # Match the review server's generated series convention: a numbered,
        # useful cover subject, numbered patch subjects, and format-patch's
        # shallow threading (cover is the parent of every patch).
        commit_subjects = subprocess.run(
            ("git", "--git-dir", str(mirror), "log", "--no-merges", "--format=%s", "--reverse", f"{base}..{head}"),
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        commit_subjects = [subject.strip() for subject in commit_subjects if subject.strip()]
        summary = "; ".join(commit_subjects[:3]) or "patch series"
        if len(commit_subjects) > 3:
            summary += f"; and {len(commit_subjects) - 3} more"
        messages[0].replace_header("Subject", f"[PATCH v{version or 1} 0/{len(messages) - 1}] {summary}")
        import quopri
        messages[0].set_payload(quopri.encodestring(cover.encode("utf-8")).decode("ascii"))
        messages[0]["Content-Type"] = 'text/plain; charset="utf-8"'
        messages[0]["Content-Transfer-Encoding"] = "quoted-printable"

        # Commit-derived Message-IDs can repeat across agents and versions.
        # Namespace by agent and version; Reply-To remains the configured review
        # workflow address, and reply ownership is resolved from this persisted ID.
        message_ids = []
        for index, message in enumerate(messages):
            old_id = message.get("Message-ID", "")
            token = hashlib.sha256(f"{agent_id}:{version or 1}:{index}:{old_id}".encode()).hexdigest()[:32]
            new_id = f"<{token}@tgit-agent.local>"
            if message.get("Message-ID"):
                message.replace_header("Message-ID", new_id)
            else:
                message["Message-ID"] = new_id
            message_ids.append(new_id)
        for index, message in enumerate(messages[1:], 1):
            message.replace_header("Subject", f"[PATCH v{version or 1} {index}/{len(messages) - 1}] {message.get('Subject', '')}")
            for header in ("In-Reply-To", "References"):
                if message.get(header):
                    del message[header]
            message["In-Reply-To"] = message_ids[0]
            message["References"] = message_ids[0]
        reply_to = os.environ.get("TGIT_REVIEW_REPLY_TO", "agent <agent@localhost>").strip()
        if not reply_to or "\\n" in reply_to or "\\r" in reply_to:
            raise RuntimeError("TGIT_REVIEW_REPLY_TO must be a valid single-line identity")
        # Use the submitted head's commit time rather than fetch time. The mbox is
        # an artifact for a particular immutable submission: including wall-clock
        # time made its digest change on every poll, defeating importer idempotency.
        committed_at = subprocess.run(
            ("git", "--git-dir", str(mirror), "show", "-s", "--format=%ct", head),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        stable_date = email.utils.formatdate(int(committed_at), localtime=True)
        for message in messages:
            message["Reply-To"] = reply_to
            message.policy = policy.SMTP
            # Git's format-patch mbox omits Date; downstream Maildir clients such
            # as Mutt rely on it for message ordering and display.
            message._headers = [(name, value) for name, value in message._headers if name.lower() != "date"]
            message["Date"] = stable_date
        with tempfile.TemporaryDirectory(prefix="agent-review-mbox-") as temporary_directory:
            output_path = Path(temporary_directory) / "review.mbox"
            box = mailbox.mbox(output_path)
            try:
                for message in messages:
                    # mailbox.MboxMessage serialization uses an ASCII BytesGenerator
                    # regardless of the message policy. Write each message with
                    # SMTPUTF8 and an explicit UTF-8 BytesGenerator instead.
                    from email.generator import BytesGenerator
                    with output_path.open("ab") as stream:
                        BytesGenerator(stream, policy=policy.SMTPUTF8, mangle_from_=True).flatten(message, unixfrom=True)
                        stream.write(b"\n")
            finally:
                box.close()
            raw = output_path.read_bytes()
        return {"agent_id": agent_id, "submitted_head": head, "base_commit": base, "cover_letter_message": cover, "submission_id": submission_id, "ref": ref, "state": registered.get("state"), "mbox": raw.decode("utf-8", "surrogateescape")}

    async def _fetch_pending_agent_mail(self, request):
        """Return serialized submission and lifecycle messages for local Maildir import."""
        submissions = []
        metadata_dir = Path(str(self.config.state_dir)) / "submissions"
        entries = []
        for agent_dir in sorted(path for path in metadata_dir.iterdir() if path.is_dir() and not path.is_symlink()):
            for path in sorted(agent_dir.glob("v[0-9]*.json")):
                match = re.fullmatch(r"v([1-9][0-9]*)\.json", path.name)
                if match:
                    entries.append((path, int(match.group(1))))
        for path, version in entries:
            raw = None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                agent_id = raw.get("agent_id") if isinstance(raw, dict) else None
                if not isinstance(agent_id, str):
                    raise RuntimeError("Submission metadata identity is invalid")
                generated = self._generate_review_patch(agent_id, version)
                submissions.append({**generated, "version": version, "agent_id": agent_id})
            except Exception as err:
                submissions.append({"agent_id": raw.get("agent_id") if isinstance(raw, dict) else None, "version": version, "error": str(err)})
        self._record_waiting_questions()
        events = []
        events_dir = Path(str(self.config.state_dir)) / "lifecycle-events"
        if events_dir.is_dir():
            for event_path in sorted(events_dir.glob("*.json")):
                try:
                    event = json.loads(event_path.read_text(encoding="utf-8"))
                    if event.get("materialized_at"):
                        continue
                    if "message" not in event:
                        message = email.message.EmailMessage()
                        message["Message-ID"] = f"<{event['event_id']}@tgit-agent.local>"
                        message["From"] = "agent <agent@localhost>"
                        message["To"] = "operator"
                        message["Date"] = email.utils.formatdate(localtime=True)
                        message["Subject"] = f"[tgit-agent] {event['kind']}: {event['agent_id']}"
                        details = event.get('details', {})
                        if event.get('kind') == 'operator_question':
                            body = f"Agent {event['agent_id']} awaits your response.\n\nQuestion:\n{details.get('question', '')}\n\nReply using continue_agent for agent {event['agent_id']}; your message resumes the agent.\n"
                        else:
                            body = f"Event: {event['kind']}\nEvent ID: {event['event_id']}\nTime: {event['event_time']}\nAgent: {event['agent_id']}\nState: {event['state']}\n"
                            if event.get('kind') == 'start':
                                body += f"Base commit: {details.get('base_commit', 'unknown')}\nTask:\n{details.get('task', '(not provided)')}\n"
                            else:
                                body += f"Details: {json.dumps(details, ensure_ascii=False, default=str)[:8000]}\n"
                        message.set_content(body)
                        event["message"] = message.as_string()
                    events.append(event)
                except Exception as err:
                    events.append({"event_id": event_path.stem, "error": str(err)})
        return {"submissions": submissions, "events": events}

    async def _dispatch(self, message: object) -> dict[str, object]:
        request_id = message.get("request_id") if isinstance(message, dict) else None
        try:
            request = validate_request(message)
            handler = self.handlers.get(request["operation"])
            if not callable(handler):
                raise RemoteProtocolError(f"Remote operation is unavailable: {request['operation']}")
            result = handler(request)
            if inspect.isawaitable(result):
                result = await result
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            return {
                "schema_version": SCHEMA_VERSION,
                "request_id": request["request_id"],
                "ok": True,
                "result": result,
                "error": None,
            }
        except Exception as err:
            logging.getLogger(__name__).exception(
                "Remote operation failed (operation=%r, request_id=%r)",
                message.get("operation") if isinstance(message, dict) else None,
                request_id,
            )
            return error_response(request_id, err)

    async def _client_connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response: dict[str, object]
        try:
            raw = await reader.readline()
            if not raw:
                return
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(bytes((10,))):
                response = error_response(None, RemoteProtocolError("Request line is missing or exceeds the size limit"))
            elif await reader.read(1):
                response = error_response(None, RemoteProtocolError("Only one request is allowed per connection"))
            else:
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    response = error_response(None, RemoteProtocolError("Invalid request JSON"))
                else:
                    response = await self._dispatch(message)
            payload = _json_line(response)
            if len(payload) > MAX_RESPONSE_BYTES:
                payload = _json_line(error_response(response.get("request_id"), RemoteProtocolError("Response exceeds the size limit")))
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self) -> None:
        parent = self.socket_path.parent
        info = parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("Service socket directory must be private and owned by this user")
        self._acquire_service_lock_and_clear_stale_socket()
        self.server = await asyncio.start_unix_server(
            self._client_connected,
            path=os.fspath(self.socket_path),
            limit=MAX_REQUEST_BYTES + 1,
        )
        os.chmod(self.socket_path, 0o600)
        try:
            await self.server.serve_forever()
        finally:
            await self.controller.shutdown()
            self.server.close()
            await self.server.wait_closed()
            self.socket_path.unlink(missing_ok=True)
            if self._service_lock_fd is not None:
                os.close(self._service_lock_fd)
                self._service_lock_fd = None



def publish_submission(params: dict[str, Any], *, state_dir: Path, project_root_dir: Path) -> dict[str, Any]:
    """Fetch a registered commit to its immutable managed submission ref."""
    agent_id = params["agent_id"]
    if params.get("head_commit") == params.get("base_commit"):
        raise RuntimeError(
            "There are no committed changes to submit. Make and commit the intended changes first; "
            "committing is expected and safe in the private agent repository and does not modify the operator checkout."
        )
    identity = re.sub(r"[^A-Za-z0-9_-]", "-", agent_id)
    mirror_value = params.get("_project_mirror")
    if not isinstance(mirror_value, str):
        raise RuntimeError("Agent has no registered project mirror")
    mirror = Path(mirror_value)
    if mirror.parent != project_root_dir:
        raise RuntimeError("Registered project mirror is outside the managed project directory")
    metadata_dir = state_dir / "submissions"
    _managed_directory(metadata_dir)
    agent_dir = metadata_dir / identity
    _managed_directory(agent_dir)
    records = sorted(agent_dir.glob("v[0-9]*.json"), key=lambda path: int(path.stem[1:]) if path.stem[1:].isdigit() else 0)
    latest = None
    if records:
        latest_path = records[-1]
        if not re.fullmatch(r"v[1-9][0-9]*\.json", latest_path.name):
            raise RuntimeError("Invalid existing submission version metadata")
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        if latest.get("commit_id") == params["head_commit"] and latest.get("base") == params["base_commit"] and latest.get("cover_letter_message") == params["cover_letter_message"]:
            return {"status": "ok", **latest}
    version = (int(records[-1].stem[1:]) + 1) if records else 1
    ref = f"refs/agent-submissions/{identity}/v{version}"
    metadata_path = agent_dir / f"v{version}.json"
    metadata = {"agent_id": agent_id, "submission_id": f"{agent_id}-v{version}", "version": version,
                "commit_id": params["head_commit"], "base": params["base_commit"],
                "cover_letter_message": params["cover_letter_message"], "ref": ref}
    source = Path(params["_work_repository"])
    if source.is_symlink() or not source.is_dir() or not mirror.is_dir() or mirror.is_symlink():
        raise RuntimeError("Unsafe agent repository or project mirror")
    try:
        existing = subprocess.run(("git", "--git-dir", str(mirror), "rev-parse", "--verify", ref),
                                  capture_output=True, text=True)
        if existing.returncode == 0:
            raise RuntimeError("Submission ref is already occupied by a different or inconsistent version")
        # Fetch into a previously unused internally generated ref.
        subprocess.run(
            ["git", "--git-dir", str(mirror), "fetch", "--no-tags", str(source),
             f"{params['head_commit']}:{ref}"], check=True, capture_output=True, timeout=60)
        temporary = metadata_path.with_name(f".{metadata_path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, metadata_path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"status": "ok", **metadata}
    except BaseException:
        raise


def proxy_stdio(socket_path: Path) -> int:
    """Forward exactly one bounded SSH request to the persistent service."""
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 2)
    if not raw:
        return 1
    if len(raw) > MAX_REQUEST_BYTES + 1 or not raw.endswith(bytes((10,))):
        response = error_response(None, RemoteProtocolError("Request line is missing or exceeds the size limit"))
        sys.stdout.buffer.write(_json_line(response))
        sys.stdout.buffer.flush()
        return 0
    if sys.stdin.buffer.read(1):
        response = error_response(None, RemoteProtocolError("Only one request is allowed per SSH invocation"))
        sys.stdout.buffer.write(_json_line(response))
        sys.stdout.buffer.flush()
        return 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(60)
            client.connect(os.fspath(socket_path))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            response = bytearray()
            while len(response) <= MAX_RESPONSE_BYTES:
                chunk = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(response)))
                if not chunk:
                    break
                response.extend(chunk)
        if len(response) > MAX_RESPONSE_BYTES or not response.endswith(bytes((10,))):
            raise RemoteProtocolError("Remote service returned invalid response framing")
        sys.stdout.buffer.write(response)
    except (OSError, TimeoutError, RemoteProtocolError) as err:
        diagnostic = (
            err if isinstance(err, RemoteProtocolError)
            else RemoteProtocolError(f"Remote service socket request failed: {type(err).__name__}: {err}")
        )
        sys.stdout.buffer.write(_json_line(error_response(None, diagnostic)))
    sys.stdout.buffer.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serve", action="store_true", help="run the persistent service")
    group.add_argument("--stdio-v1", action="store_true", help="proxy one SSH request")
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument("--config", type=Path, help="deployment TOML path")
    config_group.add_argument(
        "--base-dir",
        help="managed service directory (for rendered service units and fixed SSH proxy calls)",
    )
    arguments = parser.parse_args(argv)
    config = (
        RemoteAgentConfig("localhost", PurePosixPath(arguments.base_dir))
        if arguments.base_dir is not None
        else load_remote_agent_config(arguments.config)
    )
    if arguments.stdio_v1:
        return proxy_stdio(Path(str(config.state_dir)) / "remote-agent.sock")
    try:
        asyncio.run(RemoteAgentService(config).run())
    except (OSError, RuntimeError, SandboxError) as err:
        print(f"remote agent service: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
