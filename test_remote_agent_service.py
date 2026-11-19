"""Tests for the persistent remote-agent controller service."""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import subprocess
from datetime import datetime, timezone

import pytest
from pathlib import Path, PurePosixPath

from agent_sandbox import RuntimeManifest, SandboxRuntimeLayout, write_runtime_manifest
from remote_agent_config import RemoteAgentConfig
from remote_agent_service import RemoteAgentService, _selected_runtime


def test_selects_latest_complete_runtime_manifest(tmp_path: Path) -> None:
    config = RemoteAgentConfig("beep", PurePosixPath(str(tmp_path / "remote")))
    versions = Path(str(config.runtime_dir / "versions"))
    for version in ("runtime-20260101", "runtime-20260201"):
        directory = versions / version
        directory.mkdir(parents=True)
        write_runtime_manifest(
            directory / "manifest.json",
            RuntimeManifest(
                version=version,
                rsync_completed_at=datetime.now(timezone.utc),
                layout=SandboxRuntimeLayout(()),
            ),
        )
    (versions / "runtime-20260301").mkdir()

    selected = _selected_runtime(config)

    assert selected.version == "runtime-20260201"


def test_dispatch_uses_registered_handler_and_returns_protocol_envelope() -> None:
    service = RemoteAgentService.__new__(RemoteAgentService)
    service.handlers = {"status": lambda request: {"agent_id": request["agent_id"], "state": "running"}}
    request = {
        "schema_version": 1,
        "operation": "status",
        "request_id": "req-1",
        "agent_id": "agent-1",
    }

    response = asyncio.run(service._dispatch(request))

    assert response == {
        "schema_version": 1,
        "request_id": "req-1",
        "ok": True,
        "result": {"agent_id": "agent-1", "state": "running"},
        "error": None,
    }


def test_main_uses_command_line_base_dir_for_stdio_proxy(monkeypatch) -> None:
    import remote_agent_service

    called = []
    monkeypatch.setattr(
        remote_agent_service,
        "proxy_stdio",
        lambda socket_path: called.append(socket_path) or 0,
    )

    result = remote_agent_service.main(["--stdio-v1", "--base-dir", "/srv/tgent"])

    assert result == 0
    assert called == [Path("/srv/tgent/state/remote-agent.sock")]
def test_service_removes_stale_socket_after_acquiring_lock(tmp_path) -> None:
    import remote_agent_service

    service = remote_agent_service.RemoteAgentService.__new__(remote_agent_service.RemoteAgentService)
    service.socket_path = tmp_path / "remote-agent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(service.socket_path))
    listener.close()
    service._service_lock_fd = None

    service._acquire_service_lock_and_clear_stale_socket()

    assert not service.socket_path.exists()
    assert service._service_lock_fd is not None
    os.close(service._service_lock_fd)


def test_service_refuses_active_socket_after_acquiring_lock(tmp_path) -> None:
    import remote_agent_service

    service = remote_agent_service.RemoteAgentService.__new__(remote_agent_service.RemoteAgentService)
    service.socket_path = tmp_path / "remote-agent.sock"
    service._service_lock_fd = None
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(service.socket_path))
    listener.listen()
    try:
        with pytest.raises(RuntimeError, match="Service socket is active"):
            service._acquire_service_lock_and_clear_stale_socket()
    finally:
        listener.close()


def test_stdio_proxy_exposes_service_socket_connection_failure(monkeypatch) -> None:
    import remote_agent_service

    class BrokenSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            raise ConnectionRefusedError("service socket unavailable")

    output = io.BytesIO()
    monkeypatch.setattr(remote_agent_service.socket, "socket", lambda *_args: BrokenSocket())
    monkeypatch.setattr(remote_agent_service.sys, "stdin", type("Input", (), {"buffer": io.BytesIO(b"{}" + bytes((10,)))})())
    monkeypatch.setattr(remote_agent_service.sys, "stdout", type("Output", (), {"buffer": output})())

    result = remote_agent_service.proxy_stdio(Path("/run/remote-agent.sock"))

    response = json.loads(output.getvalue())
    assert result == 0
    assert response["ok"] is False
    assert "ConnectionRefusedError: service socket unavailable" in response["error"]["message"]


def test_dispatch_does_not_expose_unregistered_operations() -> None:
    service = RemoteAgentService.__new__(RemoteAgentService)
    service.handlers = {}

    response = asyncio.run(service._dispatch({
        "schema_version": 1,
        "operation": "status",
        "request_id": "req-1",
        "agent_id": "agent-1",
    }))

    assert response["ok"] is False
    assert response["request_id"] == "req-1"
    assert response["error"]["code"] == "request_failed"


def test_remote_acknowledgement_delegates_state_validation_to_controller() -> None:
    from agent_controller import AgentControllerError

    class Controller:
        async def acknowledge_submitted_patch_applied(self, agent_id):
            assert agent_id == "agent-1"
            raise AgentControllerError("Agent has no submitted patch to acknowledge")

    service = RemoteAgentService.__new__(RemoteAgentService)
    service.controller = Controller()
    with pytest.raises(AgentControllerError, match="no submitted patch"):
        asyncio.run(service._acknowledge_patch_applied({"agent_id": "agent-1"}))


@pytest.mark.parametrize(
    ("patch_count", "subjects", "history_summary", "expected_blurb"),
    [
        (1, "change one\n", None, "This series contains 1 patch: change one."),
        (2, "change one\nchange two\n", "Agent history: implemented and verified the changes.", "Agent history: implemented and verified the changes."),
    ],
)
def test_submit_patch_generates_and_stores_mbox_without_local_review_export(
    tmp_path: Path, patch_count: int, subjects: str, history_summary: str | None, expected_blurb: str
) -> None:
    """Submission publishes Git refs and metadata, leaving review mail untouched."""
    from types import SimpleNamespace

    source = tmp_path / "agent-123"
    source.mkdir()
    def git(*args: str, cwd: Path = source) -> str:
        return subprocess.run(("git", "-C", str(cwd), *args), check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (source / "file.txt").write_text("base\n")
    git("add", "file.txt")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    for subject in subjects.splitlines():
        (source / "file.txt").write_text(f"{subject}\n")
        git("add", "file.txt")
        git("commit", "-qm", subject)
    head = git("rev-parse", "HEAD")
    mirror = tmp_path / "project-mirror"
    subprocess.run(("git", "init", "--bare", "-q", str(mirror)), check=True)
    git("push", str(mirror), f"{base}:refs/heads/base")
    registered = {"snapshot_project": str(source), "snapshot_root": str(tmp_path), "base_commit": base, "project_mirror": str(mirror)}
    class Store:
        def get_metadata(self, agent_id):
            assert agent_id == "agent-123"
            return registered
    state = tmp_path / "state"
    review_mail = tmp_path / "review.mbox"
    review_mail.write_bytes(b"operator review mail\n")
    service = RemoteAgentService.__new__(RemoteAgentService)
    service.config = SimpleNamespace(project_root_dir=tmp_path, state_dir=state, patches_dir=tmp_path / "patches")
    service.controller = SimpleNamespace(store=Store())

    def submit(commit: str, cover: str) -> dict:
        return asyncio.run(service._submit_patch_protocol({
            "agent_id": "agent-123", "base_commit": base, "head_commit": commit,
            "cover_letter_message": cover,
        }))

    first = submit(head, expected_blurb)
    assert first["status"] == "ok"
    assert first["ref"] == "refs/agent-submissions/agent-123/v1"
    assert first["version"] == 1
    saved = json.loads((state / "submissions" / "agent-123" / "v1.json").read_text())
    assert saved["cover_letter_message"] == expected_blurb
    assert saved["base"] == base and saved["commit_id"] == head
    assert git("rev-parse", first["ref"], cwd=mirror) == head

    # A subsequent submission advances to a distinct service-managed version.
    (source / "file.txt").write_text("follow-up\n")
    git("add", "file.txt")
    git("commit", "-qm", "follow-up")
    followup = git("rev-parse", "HEAD")
    second = submit(followup, "Follow-up cover letter")
    assert second["ref"] == "refs/agent-submissions/agent-123/v2"
    assert second["version"] == 2
    saved_v2 = json.loads((state / "submissions" / "agent-123" / "v2.json").read_text())
    assert saved_v2["cover_letter_message"] == "Follow-up cover letter"
    assert saved_v2["base"] == base and saved_v2["commit_id"] == followup
    assert git("rev-parse", second["ref"], cwd=mirror) == followup
    assert not service.config.patches_dir.exists()
    assert review_mail.read_bytes() == b"operator review mail\n"


def test_submit_patch_fetches_registered_repo_to_mirror_and_saves_metadata(tmp_path: Path) -> None:
    import asyncio
    import subprocess
    from types import SimpleNamespace

    source = tmp_path / "registered-work"
    source.mkdir()
    subprocess.run(("git", "-C", str(source), "init", "-q"), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.name", "Test"), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.email", "test@example.invalid"), check=True)
    (source / "file.txt").write_text("published\n")
    subprocess.run(("git", "-C", str(source), "add", "file.txt"), check=True)
    subprocess.run(("git", "-C", str(source), "commit", "-qm", "submission"), check=True)
    head = subprocess.run(("git", "-C", str(source), "rev-parse", "HEAD"), check=True, capture_output=True, text=True).stdout.strip()
    mirror = tmp_path / "managed-project-mirror"
    subprocess.run(("git", "init", "--bare", "-q", str(mirror)), check=True)
    registered = {
        "snapshot_project": str(source), "snapshot_root": str(source.parent),
        "base_commit": "base-sha", "project_mirror": str(mirror),
    }
    class Store:
        def get_metadata(self, agent_id):
            assert agent_id == "agent-123"
            return registered
    service = RemoteAgentService.__new__(RemoteAgentService)
    service.config = SimpleNamespace(
        project_root_dir=tmp_path, state_dir=tmp_path / "state",
    )
    service.controller = SimpleNamespace(store=Store())
    result = asyncio.run(service._submit_patch_protocol({
        "agent_id": "agent-123", "base_commit": "base-sha", "head_commit": head,
        "cover_letter_message": "Please review this change.",
        # Caller-supplied paths are ignored; only registered metadata is trusted.
        "source_path": str(tmp_path / "attacker-source"), "mirror_path": str(tmp_path / "attacker-mirror"),
    }))
    ref = "refs/agent-submissions/agent-123/v1"
    published = subprocess.run(("git", "--git-dir", str(mirror), "rev-parse", ref), check=True, capture_output=True, text=True).stdout.strip()
    assert published == head
    assert result["ref"] == ref and result["version"] == 1
    saved = json.loads((tmp_path / "state" / "submissions" / "agent-123" / "v1.json").read_text())
    assert saved["cover_letter_message"] == "Please review this change."

    service.controller.store.get_metadata = lambda _agent_id: registered
    with pytest.raises(subprocess.CalledProcessError):
        asyncio.run(service._submit_patch_protocol({
            "agent_id": "agent-fail", "base_commit": "base-sha", "head_commit": "f" * 40,
            "cover_letter_message": "will fail",
        }))
    absent = subprocess.run(("git", "--git-dir", str(mirror), "show-ref", "--verify", "refs/agent-submissions/agent-fail"), capture_output=True)
    assert absent.returncode != 0


def test_empty_submission_rejected_before_publication_or_metadata(tmp_path: Path) -> None:
    from remote_agent_service import publish_submission

    mirror = tmp_path / "mirror"
    subprocess.run(("git", "init", "--bare", "-q", str(mirror)), check=True)
    source = tmp_path / "work"
    source.mkdir()
    state = tmp_path / "state"
    message = "There are no committed changes to submit. Make and commit the intended changes first; committing is expected and safe in the private agent repository and does not modify the operator checkout."
    with pytest.raises(RuntimeError, match="no committed changes to submit"):
        publish_submission({"agent_id": "empty", "base_commit": "a" * 40, "head_commit": "a" * 40,
            "cover_letter_message": "empty", "_work_repository": str(source), "_project_mirror": str(mirror)},
            state_dir=state, project_root_dir=tmp_path)
    assert subprocess.run(("git", "--git-dir", str(mirror), "show-ref", "--verify", "refs/agent-submissions/empty/v1"), capture_output=True).returncode != 0
    assert not (state / "submissions" / "empty.json").exists()
    assert message.startswith("There are no committed changes to submit")


def _published_review_fixture(tmp_path: Path, agent_id="agent-review"):
    from types import SimpleNamespace

    host = tmp_path / "host"
    host.mkdir()
    def git(*args):
        return subprocess.run(("git", "-C", str(host), *args), check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (host / "tracked.txt").write_text("base\n")
    git("add", "tracked.txt")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("update-ref", "refs/heads/base", base)
    (host / "tracked.txt").write_text("submitted\n")
    git("commit", "-qam", "submitted change")
    (host / "tracked.txt").write_text("submitted again\n")
    git("commit", "-qam", "follow-up change")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/heads/submitted", head)
    mirror = tmp_path / "managed-project.git"
    subprocess.run(("git", "init", "--bare", "-q", str(mirror)), check=True)
    subprocess.run(("git", "--git-dir", str(mirror), "config", "user.name", "Test"), check=True)
    subprocess.run(("git", "--git-dir", str(mirror), "config", "user.email", "test@example.invalid"), check=True)
    ref = f"refs/agent-submissions/{agent_id}/v1"
    subprocess.run(("git", "--git-dir", str(mirror), "fetch", str(host), "refs/heads/base:refs/test/base", f"refs/heads/submitted:{ref}"), check=True, capture_output=True)
    state, patches = tmp_path / "state", tmp_path / "patches"
    (state / "submissions" / agent_id).mkdir(parents=True)
    patches.mkdir()
    cover = "Review the important fix — naïve café.\nPreserve this exact text: 日本語."
    (state / "submissions" / agent_id / "v1.json").write_text(json.dumps({
        "agent_id": agent_id, "submission_id": f"{agent_id}-v1", "version": 1, "ref": ref,
        "commit_id": head, "base": base, "cover_letter_message": cover,
    }))
    registration = {"project_mirror": str(mirror), "state": "submitted"}
    service = RemoteAgentService.__new__(RemoteAgentService)
    service.config = SimpleNamespace(state_dir=state, project_root_dir=tmp_path, patches_dir=patches)
    service.controller = SimpleNamespace(store=SimpleNamespace(get_metadata=lambda _id: registration))
    return service, host, cover, base, head


def test_pending_review_mbox_is_generated_stably_from_managed_ref_without_touching_host(tmp_path):
    from mcp_tgit_review import _parse_mbox

    service, host, cover, base, head = _published_review_fixture(tmp_path)
    before = (subprocess.run(("git", "-C", str(host), "status", "--porcelain=v2", "--branch"), check=True, capture_output=True).stdout,
              subprocess.run(("git", "-C", str(host), "rev-parse", "HEAD"), check=True, capture_output=True).stdout,
              (host / "tracked.txt").read_bytes())
    one = service._generate_review_patch("agent-review", 1)
    first_bytes = (service.config.patches_dir / one["artifact"]).read_bytes()
    two = service._generate_review_patch("agent-review", 1)
    assert one == two
    assert (service.config.patches_dir / one["artifact"]).read_bytes() == first_bytes
    messages = _parse_mbox(first_bytes)
    assert len(messages) == 3
    assert messages[0].get_content().rstrip("\n") == cover
    assert messages[0]["Subject"].startswith("[PATCH v1 0/2] submitted change; follow-up change")
    assert messages[1]["Subject"].startswith("[PATCH v1 1/2] [PATCH 1/2] submitted change")
    assert messages[2]["Subject"].startswith("[PATCH v1 2/2] [PATCH 2/2] follow-up change")
    cover_id = messages[0]["Message-ID"]
    assert cover_id and all(message["Message-ID"] for message in messages[1:])
    assert len({message["Message-ID"] for message in messages}) == 3
    assert all(message["In-Reply-To"] == cover_id for message in messages[1:])
    assert all(cover_id in message["References"] for message in messages[1:])
    assert one["cover_letter_message"] == cover
    assert messages[0].get_content_charset() == "utf-8"
    assert messages[0]["Content-Transfer-Encoding"].lower() == "quoted-printable"
    assert b"Review the important fix" in first_bytes
    assert messages[0].get_payload(decode=True).decode("utf-8").rstrip("\n") == cover
    assert "日本語" in messages[0].get_content()
    assert one["ref"] == "refs/agent-submissions/agent-review/v1"
    assert one["base_commit"] == base and one["submitted_head"] == head
    after = (subprocess.run(("git", "-C", str(host), "status", "--porcelain=v2", "--branch"), check=True, capture_output=True).stdout,
             subprocess.run(("git", "-C", str(host), "rev-parse", "HEAD"), check=True, capture_output=True).stdout,
             (host / "tracked.txt").read_bytes())
    assert after == before


def test_remote_submission_versions_have_independent_stable_threads(tmp_path):
    from mcp_tgit_review import _parse_mbox

    service, host, cover_one, base, _head = _published_review_fixture(tmp_path)
    first = service._generate_review_patch("agent-review", 1)
    first_raw = (service.config.patches_dir / first["artifact"]).read_bytes()

    (host / "tracked.txt").write_text("version two\n")
    subprocess.run(("git", "-C", str(host), "add", "tracked.txt"), check=True)
    subprocess.run(("git", "-C", str(host), "commit", "-qm", "version two change"), check=True)
    head_two = subprocess.run(("git", "-C", str(host), "rev-parse", "HEAD"), check=True, capture_output=True, text=True).stdout.strip()
    ref_two = "refs/agent-submissions/agent-review/v2"
    mirror = Path(service.controller.store.get_metadata("agent-review")["project_mirror"])
    subprocess.run(("git", "--git-dir", str(mirror), "fetch", str(host), f"{head_two}:{ref_two}"), check=True, capture_output=True)
    metadata_path = service.config.state_dir / "submissions" / "agent-review" / "v2.json"
    cover_two = "Second version\nDistinct cover letter."
    metadata_path.write_text(json.dumps({
        "agent_id": "agent-review", "submission_id": "agent-review-v2", "version": 2,
        "ref": ref_two, "commit_id": head_two, "base": base,
        "cover_letter_message": cover_two,
    }))
    second = service._generate_review_patch("agent-review", 2)
    second_raw = (service.config.patches_dir / second["artifact"]).read_bytes()
    assert first_raw == (service.config.patches_dir / first["artifact"]).read_bytes()
    assert second["artifact"] != first["artifact"]
    messages_one, messages_two = _parse_mbox(first_raw), _parse_mbox(second_raw)
    ids_one = {message["Message-ID"] for message in messages_one}
    ids_two = {message["Message-ID"] for message in messages_two}
    assert len(ids_one) == len(messages_one) and len(ids_two) == len(messages_two)
    assert ids_one.isdisjoint(ids_two)
    assert cover_one == messages_one[0].get_content().rstrip("\n")
    assert cover_two == messages_two[0].get_content().rstrip("\n")
    assert all(message["Subject"].startswith("[PATCH v1 ") for message in messages_one)
    assert all(message["Subject"].startswith("[PATCH v2 ") for message in messages_two)
    assert all("agent-review" not in message["Subject"] for message in messages_one + messages_two)
    assert all(message.get("Reply-To") for message in messages_one + messages_two)
    import email.utils
    assert all(email.utils.parsedate_tz(str(message.get("Date", ""))) for message in messages_one + messages_two)
    for messages in (messages_one, messages_two):
        parent = messages[0]["Message-ID"]
        assert all(message["In-Reply-To"] == parent for message in messages[1:])
        assert all(parent in message["References"] for message in messages[1:])


def test_remote_versions_import_idempotently_with_version_scoped_reply_association(tmp_path):
    import hashlib
    import email
    from email import policy
    from email.message import EmailMessage
    from mcp_tgit_review import ReviewPaths, _parse_mbox, _reviews, import_patch_series

    service, host, _cover_one, base, head_one = _published_review_fixture(tmp_path)
    first = service._generate_review_patch("agent-review", 1)
    first_raw = (service.config.patches_dir / first["artifact"]).read_bytes()
    (host / "tracked.txt").write_text("version two\n")
    subprocess.run(("git", "-C", str(host), "add", "tracked.txt"), check=True)
    subprocess.run(("git", "-C", str(host), "commit", "-qm", "version two change"), check=True)
    head_two = subprocess.run(("git", "-C", str(host), "rev-parse", "HEAD"), check=True, capture_output=True, text=True).stdout.strip()
    ref_two = "refs/agent-submissions/agent-review/v2"
    mirror = Path(service.controller.store.get_metadata("agent-review")["project_mirror"])
    subprocess.run(("git", "--git-dir", str(mirror), "fetch", str(host), f"{head_two}:{ref_two}"), check=True, capture_output=True)
    cover_two = "Second version cover letter."
    (service.config.state_dir / "submissions" / "agent-review" / "v2.json").write_text(json.dumps({
        "agent_id": "agent-review", "submission_id": "agent-review-v2", "version": 2,
        "ref": ref_two, "commit_id": head_two, "base": base,
        "cover_letter_message": cover_two,
    }))
    second = service._generate_review_patch("agent-review", 2)
    second_raw = (service.config.patches_dir / second["artifact"]).read_bytes()
    paths = ReviewPaths(tmp_path / "review-state", tmp_path / "review-series", tmp_path / "review-mail")
    def import_version(raw, version, head):
        return import_patch_series(raw, base=base, submitted_head=head, repo_path=str(host),
            series_id=f"remote-agent-review-v{version}", artifact_sha256=hashlib.sha256(raw).hexdigest(), paths=paths)
    import mailbox
    one = import_version(first_raw, 1, head_one)
    assert one["status"] == "ok"
    # The cover and each patch are individual messages in the review Maildir.
    box = mailbox.Maildir(paths.mail_spool, create=False)
    try:
        first_ids = [box.get_message(key).get("Message-ID") for key in box.iterkeys()]
    finally:
        box.close()
    assert set(first_ids) == {message["Message-ID"] for message in _parse_mbox(first_raw)}
    # Lifecycle and submission mail share the exact incoming Maildir.
    lifecycle = EmailMessage()
    lifecycle["Message-ID"] = "<event-1@tgit-agent.local>"
    lifecycle["Subject"] = "[tgit-agent] stop"
    lifecycle.set_content("Event: stop")
    from mcp_tgit_review import _import_patches_to_maildir
    _import_patches_to_maildir([lifecycle], paths)
    _import_patches_to_maildir([lifecycle], paths)
    box = mailbox.Maildir(paths.mail_spool, create=False)
    try:
        combined_ids = [box.get_message(key).get("Message-ID") for key in box.iterkeys()]
    finally:
        box.close()
    assert set(combined_ids) == set(first_ids) | {"<event-1@tgit-agent.local>"}
    retry_one = import_version(first_raw, 1, head_one)
    assert retry_one["status"] == "ok" and retry_one["idempotent"]
    box = mailbox.Maildir(paths.mail_spool, create=False)
    try:
        retried_ids = [box.get_message(key).get("Message-ID") for key in box.iterkeys()]
    finally:
        box.close()
    assert set(retried_ids) == set(first_ids) | {"<event-1@tgit-agent.local>"}
    two = import_version(second_raw, 2, head_two)
    assert two["status"] == "ok"
    records = _reviews(paths)
    assert {record["review_id"] for record in records} == {"<event-1@tgit-agent.local>"}
    assert len(list(paths.series_root.glob("*/manifest.json"))) == 2
    for version in (1, 2):
        messages = _parse_mbox(first_raw if version == 1 else second_raw)
        reply = EmailMessage()
        reply["Message-ID"] = f"<review-v{version}@example.invalid>"
        reply["In-Reply-To"] = messages[1]["Message-ID"]
        reply["References"] = f"{messages[0]['Message-ID']} {messages[1]['Message-ID']}"
        reply["Subject"] = f"Re: {messages[1]['Subject']}"
        reply.set_content(f"Review for v{version}")
        with paths.mail_spool.joinpath(f"reply-v{version}.eml").open("wb") as output:
            output.write(reply.as_bytes(policy=policy.SMTP))
    records = _reviews(paths)
    assert len(records) == 3
    assert {record["series_id"] for record in records if record["series_id"]} == {"remote-agent-review-v1", "remote-agent-review-v2"}
    assert {record["revision"] for record in records if record["series_id"]} == {1}
    again = import_version(first_raw, 1, head_one)
    final_records = _reviews(paths)
    assert again["idempotent"] and len(final_records) == 3
    assert {record["review_id"] for record in final_records if record["series_id"]} == {
        "<review-v1@example.invalid>", "<review-v2@example.invalid>"
    }
    assert {record["series_id"] for record in final_records if record["series_id"]} == {
        "remote-agent-review-v1", "remote-agent-review-v2"
    }
    assert {record["review_id"] for record in final_records if not record["series_id"]} == {
        "<event-1@tgit-agent.local>"
    }
    assert len(list(paths.series_root.glob("*/manifest.json"))) == 2


def test_halted_patch_notification_has_real_newlines(tmp_path, monkeypatch):
    import mailbox
    from types import SimpleNamespace
    from mcp_agent import _notify_halted_patch

    spool = tmp_path / "mail"
    monkeypatch.setenv("USER", "operator")
    _notify_halted_patch({"name": "Worker", "state": "halted"},
                         {"agent_id": "agent-1", "series_id": "remote-agent-1"},
                         SimpleNamespace(DEFAULT_PATHS=SimpleNamespace(mail_spool=spool)))
    message = next(iter(mailbox.Maildir(spool)))
    body = message.get_payload(decode=True).decode(message.get_content_charset() or "utf-8")
    assert "\\n" not in body
    assert "Agent: Worker (agent-1)\nState: halted\n" in body
    assert body.count("\n") > 5


def test_review_mbox_artifact_identity_uses_submission_version(tmp_path):
    service, _host, _cover, _base, _head = _published_review_fixture(tmp_path)
    metadata_path = service.config.state_dir / "submissions" / "agent-review" / "v1.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["submission_id"] = "agent-review-v1"
    metadata_path.write_text(json.dumps(metadata))
    result = service._generate_review_patch("agent-review", 1)
    assert result["artifact"] == "agent-review-v1.patch"
    assert (service.config.patches_dir / result["artifact"]).is_file()


def test_legacy_submission_without_explicit_id_is_supported(tmp_path):
    service, _host, _cover, _base, _head = _published_review_fixture(tmp_path)
    versioned = service.config.state_dir / "submissions" / "agent-review" / "v1.json"
    legacy = json.loads(versioned.read_text())
    legacy.pop("submission_id", None)
    legacy_path = service.config.state_dir / "submissions" / "agent-review.json"
    legacy_path.write_text(json.dumps(legacy))
    result = service._generate_review_patch("agent-review")
    assert result["submission_id"] == "agent-review-v1"


def test_pending_review_missing_and_malformed_metadata_errors(tmp_path):
    service, _host, _cover, _base, _head = _published_review_fixture(tmp_path)
    with pytest.raises(RuntimeError, match="metadata is missing"):
        service._generate_review_patch("absent")
    metadata = service.config.state_dir / "submissions" / "agent-review" / "v1.json"
    metadata.write_text("{")
    with pytest.raises(json.JSONDecodeError):
        service._generate_review_patch("agent-review")
    metadata.write_text(json.dumps({"agent_id": "agent-review", "submission_id": "agent-review-v1", "ref": "refs/agent-submissions/agent-review/v1"}))
    with pytest.raises(RuntimeError, match="missing or does not match"):
        service._generate_review_patch("agent-review", 1)


def test_pending_review_bulk_returns_success_and_per_item_errors(tmp_path):
    service, _host, _cover, _base, _head = _published_review_fixture(tmp_path)
    (service.config.state_dir / "submissions" / "broken.json").write_text("not json")
    result = asyncio.run(service._pending_patches({}))
    assert len(result["patches"]) == 2
    success = next(item for item in result["patches"] if item.get("agent_id") == "agent-review")
    failure = next(item for item in result["patches"] if item.get("error"))
    assert success["submitted_head"]
    assert "error" in failure


def test_fetch_pending_agent_mail_serializes_submissions_and_events(tmp_path):
    service, _host, _cover, _base, _head = _published_review_fixture(tmp_path)
    events = service.config.state_dir / "lifecycle-events"
    events.mkdir()
    (events / "event-1.json").write_text(json.dumps({"event_id": "event-1", "kind": "stop", "agent_id": "agent-review", "event_time": "now", "state": "stopped", "details": {}}))
    result = asyncio.run(service._fetch_pending_agent_mail({}))
    assert result["submissions"][0]["mbox"]
    first_mbox = result["submissions"][0]["mbox"]
    assert first_mbox.encode("utf-8", "surrogateescape") == (service.config.patches_dir / result["submissions"][0]["artifact"]).read_bytes()
    assert result["events"][0]["message"]
    assert "Message-ID" in result["events"][0]["message"]
    assert result["submissions"][0]["mbox"]
    email = __import__("email")
    event_message = email.message_from_string(result["events"][0]["message"], policy=email.policy.default)
    assert "Event: stop\nEvent ID: event-1\n" in event_message.get_content()
    repeated = asyncio.run(service._fetch_pending_agent_mail({}))
    assert repeated["submissions"][0]["mbox"] == result["submissions"][0]["mbox"]


def test_project_mirror_collision_warns_and_allows_both_paths(tmp_path):
    import asyncio
    from remote_agent_service import RemoteAgentService
    from remote_agent_config import RemoteAgentConfig
    service = RemoteAgentService.__new__(RemoteAgentService)
    service.config = RemoteAgentConfig("beep", str(tmp_path))
    Path(str(service.config.project_root_dir)).mkdir()
    one, two = "/a_b/c", "/a/b_c"
    name = one.replace("/", "_")
    async def check():
        assert (await service._ensure_project({"project_name": name, "source_path": one}))["collision"] is False
        assert (await service._ensure_project({"project_name": name, "source_path": one}))["collision"] is False
        with pytest.warns(RuntimeWarning, match="collision"):
            assert (await service._ensure_project({"project_name": name, "source_path": two}))["collision"] is True
    asyncio.run(check())
