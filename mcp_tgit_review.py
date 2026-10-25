#!/usr/bin/env python3
"""MCP tools for local LKML-style patch review through Mutt.

Run normally as an MCP server.  Run with ``--capture-mail`` as Mutt's local
sendmail command: stdin is stored in a private spool and is never delivered.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import hashlib
import json
import mailbox
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from email.message import Message
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("tgit-review")

@dataclass(frozen=True)
class ReviewPaths:
    """Filesystem locations used by the review server.

    Constructing an instance with temporary directories lets unit tests exercise
    storage and mail-matching behavior without touching the user's state.
    """

    state_root: Path
    series_root: Path
    mail_spool: Path

    @classmethod
    def from_environment(cls) -> ReviewPaths:
        state_root = Path(
            os.environ.get("TGIT_REVIEW_STATE_DIR")
            or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tgit-review"
        ).expanduser()
        return cls(
            state_root=state_root,
            series_root=state_root / "series",
            mail_spool=Path(os.environ.get("TGIT_REVIEW_MAILDIR") or state_root / "incoming").expanduser(),
        )


DEFAULT_PATHS = ReviewPaths.from_environment()
# Keep these names for callers that use the server's default locations.
STATE_ROOT = DEFAULT_PATHS.state_root
SERIES_ROOT = DEFAULT_PATHS.series_root
MAIL_SPOOL = DEFAULT_PATHS.mail_spool
MESSAGE_ID_RE = re.compile(r"<[^>\s]+>")
SERIES_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def _git(repo_path: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True, check=False)


def _git_bytes(repo_path: str, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run Git without decoding output, for RFC 5322 patch payloads."""
    return subprocess.run(["git", "-C", repo_path, *args], capture_output=True, check=False)


def _resolve_repo(repo_path: str) -> tuple[str | None, str | None]:
    path = os.path.realpath(os.path.abspath(os.path.expanduser(repo_path or ".")))
    proc = _git(path, ["rev-parse", "--show-toplevel"])
    if proc.returncode:
        return None, proc.stderr.strip() or "Path is not inside a Git repository."
    return os.path.realpath(proc.stdout.strip()), None

def _state_dirs(paths: ReviewPaths = DEFAULT_PATHS) -> None:
    """Create the state directories with private permissions."""
    for path in (paths.state_root, paths.series_root, paths.mail_spool):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The spool itself is a Maildir, so captured messages can be opened directly
    # by Mutt and new submissions appear in its new/ folder.
    for name in ("cur", "new", "tmp"):
        (paths.mail_spool / name).mkdir(mode=0o700, exist_ok=True)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    _atomic_write(path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())


def _safe_series_id(series_id: str) -> bool:
    return bool(SERIES_ID_RE.fullmatch(series_id))

def _load_manifest(series_id: str, paths: ReviewPaths = DEFAULT_PATHS) -> tuple[dict[str, Any] | None, Path]:
    """Load a series manifest from the selected review state directory."""
    path = paths.series_root / series_id / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, path
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(f"Could not read series manifest: {err}") from err
    if not isinstance(manifest, dict):
        raise ValueError("Invalid series manifest.")
    return manifest, path


def _commit(repo: str, ref: str, name: str) -> tuple[str | None, str | None]:
    proc = _git(repo, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"])
    if proc.returncode:
        return None, f"{name} does not resolve to a commit: {ref}"
    return proc.stdout.strip(), None


def _parse_mbox(raw: bytes) -> list[Message]:
    with tempfile.NamedTemporaryFile() as handle:
        handle.write(raw)
        handle.flush()
        box = mailbox.mbox(
            handle.name,
            factory=lambda source: email.message_from_binary_file(source, policy=email.policy.default),
        )
        try:
            return list(box)
        finally:
            box.close()

def _apply_reply_to(raw: bytes, reply_to: str) -> bytes:
    """Set Reply-To on every message while preserving Message-IDs and threading."""
    if not reply_to or "\n" in reply_to or "\r" in reply_to:
        raise ValueError("Reply-To must be a valid single-line identity")
    messages = _parse_mbox(raw)
    for message in messages:
        if message.get("Reply-To"):
            message.replace_header("Reply-To", reply_to)
        else:
            message["Reply-To"] = reply_to
    return _serialize_mbox(messages)


def _serialize_mbox(messages: list[Message]) -> bytes:
    """Serialize messages as an mbox after any header/body edits."""
    with tempfile.NamedTemporaryFile() as handle:
        box = mailbox.mbox(handle.name)
        try:
            for message in messages:
                box.add(message)
            box.flush()
        finally:
            box.close()
        handle.seek(0)
        return handle.read()


def _message_id(message: Message) -> str | None:
    value = message.get("Message-ID")
    return str(value).strip() if value else None


def _import_patches_to_maildir(patches: list[Message], paths: ReviewPaths = DEFAULT_PATHS) -> None:
    """Add exported patches to the shared Maildir, without duplicate Message-IDs."""
    _state_dirs(paths)
    maildir = mailbox.Maildir(paths.mail_spool, create=True)
    try:
        existing_ids = {
            message_id
            for key in maildir.iterkeys()
            if (message := maildir.get_message(key)) is not None
            and (message_id := _message_id(message)) is not None
        }
        for patch in patches:
            message_id = _message_id(patch)
            if message_id is not None and message_id not in existing_ids:
                maildir.add(patch)
                existing_ids.add(message_id)
    finally:
        maildir.close()


def _headers(message: Message, *names: str) -> list[str]:
    return [item for name in names for item in MESSAGE_ID_RE.findall(str(message.get(name, "")))]


def _body(message: Message) -> str:
    if message.is_multipart():
        part = next(
            (candidate for candidate in message.walk() if candidate.get_content_type() == "text/plain" and not candidate.get_filename()),
            None,
        )
        if part is None:
            return ""
    else:
        part = message
    try:
        content = part.get_content()
        return content if isinstance(content, str) else str(content)
    except (LookupError, UnicodeError, AttributeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


@mcp.tool()
def export_patch_series(
    base: str,
    head: str = "HEAD",
    repo_path: str = ".",
    series_id: str | None = None,
    cover_letter: str = "",
) -> dict[str, Any]:
    """Export ``base..head`` as an mbox, creating or revising a review series.

    Multi-patch mboxes include a numbered cover letter (patch 0); single-patch
    mboxes contain only the patch.
    Git generates patch 0 with the series summary and diffstat. If supplied,
    ``cover_letter`` uses its first line as the subject and remaining lines as
    introductory text in place of Git's blurb placeholder, preserving the
    generated summary and diffstat. This only reads committed Git
    history and does not alter the worktree. Pass the returned mbox_path to Mutt;
    pass its series_id again for a new revision.
    """
    repo, error = _resolve_repo(repo_path)
    if error:
        return {"status": "error", "message": error, "repo_path": repo_path}
    assert repo is not None
    base_sha, error = _commit(repo, base, "base")
    if error:
        return {"status": "error", "message": error, "repo_path": repo}
    head_sha, error = _commit(repo, head, "head")
    if error:
        return {"status": "error", "message": error, "repo_path": repo}

    count = _git(repo, ["rev-list", "--no-merges", "--count", f"{base_sha}..{head_sha}"])
    if count.returncode:
        return {"status": "error", "message": "Failed to inspect commit range.", "stderr": count.stderr}
    commit_count = int(count.stdout.strip() or "0")
    if commit_count < 1:
        return {"status": "error", "message": "The selected range contains no non-merge commits."}

    _state_dirs()
    if series_id is None or not series_id.strip():
        series_id = f"review-{os.urandom(5).hex()}"
        manifest: dict[str, Any] = {
            "series_id": series_id,
            "repo_path": repo,
            "created_at": int(time.time()),
            "revisions": [],
            "acknowledged_review_ids": [],
        }
        series_dir = SERIES_ROOT / series_id
        series_dir.mkdir(mode=0o700)
        manifest_path = series_dir / "manifest.json"
    else:
        series_id = series_id.strip()
        if not _safe_series_id(series_id):
            return {"status": "error", "message": "Invalid series_id."}
        try:
            manifest, manifest_path = _load_manifest(series_id)
        except ValueError as err:
            return {"status": "error", "message": str(err)}
        if manifest is None:
            return {"status": "error", "message": "Unknown series_id.", "series_id": series_id}
        if manifest.get("repo_path") != repo:
            return {"status": "error", "message": "Series belongs to a different repository."}
        series_dir = manifest_path.parent

    revisions = manifest.get("revisions")
    if not isinstance(revisions, list):
        return {"status": "error", "message": "Series manifest has invalid revisions."}
    revision_number = len(revisions) + 1
    reply_to_address = os.environ.get("TGIT_REVIEW_REPLY_TO", "agent <agent@localhost>").strip()
    if not reply_to_address or "\n" in reply_to_address or "\r" in reply_to_address:
        return {
            "status": "error",
            "message": "Set TGIT_REVIEW_REPLY_TO to this agent's reply address.",
        }

    exported = _git_bytes(
        repo,
        [
            "format-patch", "--stdout", "--thread=shallow", "--numbered",
            *( ["--cover-letter"] if commit_count > 1 else [] ),
            f"{base_sha}..{head_sha}",
        ],
    )
    if exported.returncode:
        return {
            "status": "error",
            "message": "git format-patch failed.",
            "stderr": exported.stderr.decode("utf-8", errors="replace"),
        }
    raw = exported.stdout
    if not raw.strip():
        return {"status": "error", "message": "git format-patch produced an empty mbox."}
    try:
        patches = _parse_mbox(raw)
    except (OSError, mailbox.Error, email.errors.MessageError) as err:
        return {"status": "error", "message": f"Could not parse generated mbox: {err}"}

    # --add-header is not reliably applied to Git’s generated cover letter.
    # Explicitly set it on every message so replies to the summary are captured.
    for patch in patches:
        if patch.get("Reply-To"):
            patch.replace_header("Reply-To", reply_to_address)
        else:
            patch["Reply-To"] = reply_to_address

    if commit_count > 1 and cover_letter:
        title, separator, body = cover_letter.strip().partition("\n")
        if not title.strip():
            return {"status": "error", "message": "The cover letter must start with a subject line."}
        patches[0].replace_header("Subject", f"[PATCH 0/{commit_count}] {title.strip()}")
        generated_body = _body(patches[0])
        introduction = body.strip() if separator else ""
        generated_body = generated_body.replace("*** BLURB HERE ***", introduction, 1)
        patches[0].set_content(generated_body)
    elif commit_count > 1:
        generated_body = _body(patches[0]).replace(
            "*** BLURB HERE ***",
            f"This series contains {commit_count} patches.",
            1,
        )
        patches[0].set_content(generated_body)

    # Serialize the modified messages unconditionally: format-patch's raw mbox
    # does not include headers added to the parsed Message objects above.
    raw = _serialize_mbox(patches)

    patch_ids = [_message_id(patch) for patch in patches]
    expected_messages = commit_count + (1 if commit_count > 1 else 0)
    if len(patches) != expected_messages or any(patch_id is None for patch_id in patch_ids):
        return {
            "status": "error",
            "message": "Generated mbox must contain one Message-ID per patch.",
            "commit_count": commit_count,
            "expected_patch_count": expected_messages,
            "patch_count": len(patches),
        }

    mbox_path = series_dir / f"v{revision_number}.mbox"
    _atomic_write(mbox_path, raw)
    revision = {
        "revision": revision_number,
        "base": base_sha,
        "head": head_sha,
        "commit_count": commit_count,
        "patch_message_ids": patch_ids,
        "mbox_path": str(mbox_path),
        "created_at": int(time.time()),
    }
    revisions.append(revision)
    _write_manifest(manifest_path, manifest)
    try:
        _import_patches_to_maildir(patches)
    except (OSError, mailbox.Error) as err:
        return {"status": "error", "message": f"Could not add patches to the review Maildir: {err}", "series_id": series_id}
    return {"status": "ok", "series_id": series_id, **revision}

def import_patch_series(
    raw: bytes,
    *,
    base: str,
    submitted_head: str,
    repo_path: str = ".",
    series_id: str,
    artifact_sha256: str,
    paths: ReviewPaths = DEFAULT_PATHS,
) -> dict[str, Any]:
    """Validate and import a complete remote mbox into the normal review store.

    The supplied cover letter is retained verbatim (and is therefore authored
    by the remote agent); patch Message-IDs are not regenerated.
    """
    if len(raw) > 16 * 1024 * 1024:
        return {"status": "error", "message": "Mbox exceeds 16 MiB limit."}
    if not isinstance(submitted_head, str) or re.fullmatch(r"[0-9a-fA-F]{40,64}", submitted_head) is None:
        return {"status": "error", "message": "Invalid submitted head identity."}
    if not _safe_series_id(series_id) or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        return {"status": "error", "message": "Invalid series identity or artifact digest."}
    if hashlib.sha256(raw).hexdigest() != artifact_sha256:
        return {"status": "error", "message": "Artifact digest mismatch."}
    repo, error = _resolve_repo(repo_path)
    if error or repo is None:
        return {"status": "error", "message": error or "Invalid repository."}
    base_sha, error = _commit(repo, base, "base")
    if error:
        return {"status": "error", "message": error}
    try:
        messages = _parse_mbox(raw)
    except (OSError, mailbox.Error, email.errors.MessageError) as err:
        return {"status": "error", "message": f"Malformed mbox: {err}"}
    if len(messages) < 2 or not all(_message_id(message) for message in messages):
        return {"status": "error", "message": "Mbox must contain a cover letter and patches with Message-IDs."}
    ids = [_message_id(message) for message in messages]
    if len(set(ids)) != len(ids):
        return {"status": "error", "message": "Mbox contains duplicate Message-IDs."}
    # Reconstruct in a private temporary clone, never touching the operator worktree.
    with tempfile.TemporaryDirectory(prefix="tgit-remote-review-") as temporary:
        clone = Path(temporary) / "repo"
        proc = subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", repo, str(clone)], capture_output=True)
        if proc.returncode:
            return {"status": "error", "message": "Could not create isolated validation clone."}
        for args in (["checkout", "--quiet", "--detach", base_sha],):
            checkout = _git(str(clone), list(args))
        patches = _serialize_mbox(messages[1:])
        patch_file = Path(temporary) / "patches.mbox"
        patch_file.write_bytes(patches)
        applied = subprocess.run(["git", "-C", str(clone), "-c", "user.name=Review Import", "-c", "user.email=review@localhost", "am", "--committer-date-is-author-date", str(patch_file)], capture_output=True)
        if checkout.returncode or applied.returncode:
            return {"status": "error", "message": "Mbox does not apply cleanly to the declared base."}
        reconstructed = _git(str(clone), ["rev-parse", "HEAD"])
        if reconstructed.returncode or reconstructed.stdout.strip() == base_sha:
            return {"status": "error", "message": "Mbox produced no reconstructed commits."}
    _state_dirs(paths)
    manifest, manifest_path = _load_manifest(series_id, paths)
    if manifest is None:
        directory = paths.series_root / series_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        manifest_path = directory / "manifest.json"
        manifest = {"series_id": series_id, "repo_path": repo, "created_at": int(time.time()), "revisions": [], "acknowledged_review_ids": [], "remote_provenance": []}
    if manifest.get("repo_path") != repo:
        return {"status": "error", "message": "Series belongs to a different repository."}
    provenance = manifest.setdefault("remote_provenance", [])
    if any(item.get("artifact_sha256") == artifact_sha256 for item in provenance):
        return {"status": "ok", "series_id": series_id, "revision": next((item["revision"] for item in manifest["revisions"] if item.get("artifact_sha256") == artifact_sha256), None), "idempotent": True}
    revision_number = len(manifest["revisions"]) + 1
    directory = manifest_path.parent
    mbox_path = directory / f"v{revision_number}.mbox"
    _atomic_write(mbox_path, raw)
    revision = {"revision": revision_number, "base": base_sha, "head": submitted_head, "commit_count": len(messages)-1, "patch_message_ids": ids, "mbox_path": str(mbox_path), "artifact_sha256": artifact_sha256, "created_at": int(time.time())}
    manifest["revisions"].append(revision)
    provenance.append({"artifact_sha256": artifact_sha256, "submitted_head": submitted_head, "base": base_sha})
    _write_manifest(manifest_path, manifest)
    try:
        _import_patches_to_maildir(messages, paths)
    except (OSError, mailbox.Error) as err:
        return {"status": "error", "message": f"Could not import mbox messages: {err}"}
    return {"status": "ok", "series_id": series_id, **revision}


def _known_patches(paths: ReviewPaths = DEFAULT_PATHS) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Return patch Message-IDs and their owning series/revision records."""
    result: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for path in paths.series_root.glob("*/manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        for revision in manifest.get("revisions", []):
            if not isinstance(revision, dict):
                continue
            for patch_id in revision.get("patch_message_ids", []):
                if isinstance(patch_id, str):
                    result.append((patch_id, manifest, revision))
    return result

def _reviews(paths: ReviewPaths = DEFAULT_PATHS) -> list[dict[str, Any]]:
    """Read captured mail and associate replies with known patch messages.

    The paths argument keeps this filesystem-backed behavior isolated and
    straightforward to test using a temporary directory.
    """
    _state_dirs(paths)
    known = _known_patches(paths)
    known_patch_ids = {patch_id for patch_id, _manifest, _revision in known}
    output: list[dict[str, Any]] = []
    mail_paths = [
        *paths.mail_spool.glob("*.eml"),  # Read messages from the previous flat-spool format.
        *paths.mail_spool.joinpath("new").iterdir(),
        *paths.mail_spool.joinpath("cur").iterdir(),
    ]
    for path in sorted(mail_paths):
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
            message = email.message_from_bytes(raw, policy=email.policy.default)
        except (OSError, email.errors.MessageError):
            continue
        message_id = _message_id(message)
        # Outbound patch messages share this Maildir with replies, but are not reviews.
        if message_id in known_patch_ids:
            continue
        thread_ids = _headers(message, "In-Reply-To", "References")
        match = next((entry for thread_id in thread_ids for entry in known if entry[0] == thread_id), None)
        review_id = message_id or f"sha256:{hashlib.sha256(raw).hexdigest()}"
        patch_id, manifest, revision = match if match is not None else (None, None, None)
        acknowledged = bool(manifest) and review_id in manifest.get("acknowledged_review_ids", [])
        output.append({
            "review_id": review_id,
            "series_id": manifest.get("series_id") if manifest else None,
            "revision": revision.get("revision") if revision else None,
            "patch_message_id": patch_id,
            "from": str(message.get("From", "")),
            "to": str(message.get("To", "")),
            "date": str(message.get("Date", "")),
            "subject": str(message.get("Subject", "")),
            "in_reply_to": str(message.get("In-Reply-To", "")),
            "references": str(message.get("References", "")),
            "body": _body(message),
            "raw_path": str(path),
            "acknowledged": acknowledged,
        })
    return output


@mcp.tool()
def list_review_series(repo_path: str = ".") -> dict[str, Any]:
    """List review series and captured-reply counts for one Git repository."""
    repo, error = _resolve_repo(repo_path)
    if error:
        return {"status": "error", "message": error}
    _state_dirs()
    reviews = _reviews()
    result = []
    for path in sorted(SERIES_ROOT.glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("repo_path") != repo:
            continue
        series_reviews = [item for item in reviews if item["series_id"] == manifest.get("series_id")]
        result.append({
            "series_id": manifest.get("series_id"),
            "revisions": manifest.get("revisions", []),
            "review_count": len(series_reviews),
            "unacknowledged_count": sum(not item["acknowledged"] for item in series_reviews),
        })
    return {"status": "ok", "repo_path": repo, "series": result}


def _get_review_replies(repo_path: str = ".", unacknowledged_only: bool = False) -> dict[str, Any]:
    """Return captured replies across all review series for one Git repository.

    Reporting is read-only: it does not acknowledge replies or mark comments
    resolved. Each reply includes its series, revision, and patch Message-ID.
    """
    repo, error = _resolve_repo(repo_path)
    if error:
        return {"status": "error", "message": error}
    series_ids: set[str] = set()
    for path in SERIES_ROOT.glob("*/manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        series_id = manifest.get("series_id")
        if manifest.get("repo_path") == repo and isinstance(series_id, str):
            series_ids.add(series_id)
    reviews = [item for item in _reviews() if item["series_id"] in series_ids]
    if unacknowledged_only:
        reviews = [item for item in reviews if not item["acknowledged"]]
    return {"status": "ok", "repo_path": repo, "review_count": len(reviews),
            "unacknowledged_count": sum(not item["acknowledged"] for item in reviews),
            "reviews": reviews}


@mcp.tool()
def get_review_replies(repo_path: str = ".", unacknowledged_only: bool = False) -> dict[str, Any]:
    """Return captured replies across all review series for one Git repository."""
    return _get_review_replies(repo_path, unacknowledged_only)


@mcp.tool()
def get_reviews(series_id: str, unacknowledged_only: bool = False) -> dict[str, Any]:
    """Return captured review replies threaded to patches in a series."""
    if not _safe_series_id(series_id):
        return {"status": "error", "message": "Invalid series_id."}
    try:
        manifest, _ = _load_manifest(series_id)
    except ValueError as err:
        return {"status": "error", "message": str(err)}
    if manifest is None:
        return {"status": "error", "message": "Unknown series_id.", "series_id": series_id}
    reviews = [item for item in _reviews() if item["series_id"] == series_id]
    if unacknowledged_only:
        reviews = [item for item in reviews if not item["acknowledged"]]
    return {"status": "ok", "series_id": series_id, "reviews": reviews}


@mcp.tool()
def get_unmatched_reviews() -> dict[str, Any]:
    """Return captured mail that does not reference an exported patch Message-ID."""
    return {"status": "ok", "reviews": [item for item in _reviews() if item["series_id"] is None]}


def _maildir_messages(paths: ReviewPaths) -> list[tuple[Path, bytes]]:
    """Read message files from both Maildir arrival and seen folders."""
    messages: list[tuple[Path, bytes]] = []
    for folder in ("new", "cur"):
        for path in (paths.mail_spool / folder).iterdir():
            if not path.is_file():
                continue
            try:
                messages.append((path, path.read_bytes()))
            except OSError:
                continue
    return messages


@mcp.tool()
def wait_for_mail(timeout_seconds: int = 300) -> dict[str, Any]:
    """Wait for a newly arrived Maildir message addressed to this agent.

    The agent's address is taken from ``TGIT_REVIEW_REPLY_TO``. A message is
    eligible when that address appears in its To or Cc header.
    """
    if timeout_seconds < 0:
        return {"status": "error", "message": "timeout_seconds must not be negative."}
    reply_to = os.environ.get("TGIT_REVIEW_REPLY_TO", "agent <agent@localhost>").strip()
    agent_addresses = {
        address.casefold()
        for _name, address in getaddresses([reply_to])
        if address
    }
    if not agent_addresses:
        return {"status": "error", "message": "Set TGIT_REVIEW_REPLY_TO to this agent's reply address."}

    _state_dirs()
    # Existing mail must not immediately satisfy a wait; compare content so a
    # Maildir rename from new/ to cur/ does not appear to be a fresh message.
    existing = {hashlib.sha256(raw).digest() for _path, raw in _maildir_messages(DEFAULT_PATHS)}
    deadline = time.monotonic() + timeout_seconds
    while True:
        for path, raw in _maildir_messages(DEFAULT_PATHS):
            digest = hashlib.sha256(raw).digest()
            if digest in existing:
                continue
            existing.add(digest)
            try:
                message = email.message_from_bytes(raw, policy=email.policy.default)
            except email.errors.MessageError:
                continue
            recipients = {
                address.casefold()
                for _name, address in getaddresses([
                    str(message.get("To", "")),
                    str(message.get("Cc", "")),
                ])
                if address
            }
            if not agent_addresses.intersection(recipients):
                continue
            return {
                "status": "ok",
                "message": {
                    "message_id": _message_id(message),
                    "from": str(message.get("From", "")),
                    "to": str(message.get("To", "")),
                    "cc": str(message.get("Cc", "")),
                    "date": str(message.get("Date", "")),
                    "subject": str(message.get("Subject", "")),
                    "body": _body(message),
                    "raw_path": str(path),
                },
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "timeout", "message": "No new mail addressed to this agent arrived before the timeout."}
        time.sleep(min(1.0, remaining))


@mcp.tool()
def acknowledge_reviews(series_id: str, review_ids: list[str]) -> dict[str, Any]:
    """Mark review messages as seen by the agent, without marking comments fixed."""
    if not _safe_series_id(series_id):
        return {"status": "error", "message": "Invalid series_id."}
    try:
        manifest, path = _load_manifest(series_id)
    except ValueError as err:
        return {"status": "error", "message": str(err)}
    if manifest is None:
        return {"status": "error", "message": "Unknown series_id."}
    requested = {item for item in review_ids if isinstance(item, str)}
    available = {item["review_id"] for item in _reviews() if item["series_id"] == series_id}
    unknown = sorted(requested - available)
    if unknown:
        return {"status": "error", "message": "Some review IDs do not belong to this series.", "unknown_review_ids": unknown}
    acknowledged = set(manifest.get("acknowledged_review_ids", []))
    acknowledged.update(requested)
    manifest["acknowledged_review_ids"] = sorted(acknowledged)
    _write_manifest(path, manifest)
    return {"status": "ok", "series_id": series_id, "acknowledged_review_ids": sorted(requested)}


def capture_mail() -> int:
    """Save sendmail stdin in the spool and return success without delivery."""
    try:
        raw = sys.stdin.buffer.read()
        if not raw.strip():
            print("tgit review capture: empty message", file=sys.stderr)
            return 1
        _state_dirs()
        digest = hashlib.sha256(raw).hexdigest()
        # A stable content-addressed filename makes repeated Mutt submissions idempotent.
        target = MAIL_SPOOL / "new" / digest
        temporary = MAIL_SPOOL / "tmp" / f"{digest}.{os.getpid()}.{os.urandom(4).hex()}"
        already_captured = target.exists() or any((MAIL_SPOOL / "cur").glob(f"{digest}*"))
        if not already_captured:
            _atomic_write(temporary, raw)
            try:
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        return 0
    except OSError as err:
        print(f"tgit review capture: {err}", file=sys.stderr)
        return 1


def _install_mutt_sendmail(path: str) -> int:
    """Install an executable wrapper for Mutt's local sendmail command."""
    target = Path(path).expanduser().absolute()
    python = shlex.quote(os.path.abspath(sys.executable))
    script = shlex.quote(os.path.realpath(__file__))
    default_maildir = shlex.quote(str(DEFAULT_PATHS.mail_spool))
    mutt_sendmail = str(target).replace("\\", "\\\\").replace('"', '\\"')
    sendmail_setting = shlex.quote(f'set sendmail="{mutt_sendmail}"')
    smtp_setting = shlex.quote('set smtp_url=""')
    contents = (
        "#!/bin/sh\n"
        "# Mutt calls this with sendmail flags; no flags launches the review Maildir.\n"
        "if [ \"$#\" -gt 0 ]; then\n"
        "    case \"$1\" in\n"
        f"        -*) exec {python} {script} --capture-mail \"$@\" ;;\n"
        "    esac\n"
        "    maildir=$1\n"
        "    shift\n"
        "else\n"
        "    maildir=${TGIT_REVIEW_MAILDIR:-}\n"
        f"    [ -n \"$maildir\" ] || maildir={default_maildir}\n"
        "fi\n"
        f"exec mutt -e {sendmail_setting} -e {smtp_setting} -f \"$maildir\" \"$@\"\n"
    )
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o700)
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError as err:
        print(f"Could not install Mutt sendmail wrapper: {err}", file=sys.stderr)
        return 1

    mutt_path = str(target).replace("\\\\", "\\\\\\\\").replace('"', '\\"')
    print(f"Installed Mutt sendmail/launcher wrapper: {target}")
    print(f'Run `{target}` to open the review Maildir in Mutt.')
    print(f'Run `{target} /path/to/Maildir` to open a specific Maildir.')
    print("Mutt sendmail setting: handled by the wrapper when invoked with sendmail flags.")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Git patch review MCP server")
    parser.add_argument("--capture-mail", action="store_true", help="Capture sendmail stdin locally; do not deliver it")
    parser.add_argument(
        "--install-mutt-sendmail",
        metavar="PATH",
        help="write an executable Mutt sendmail wrapper at PATH",
    )
    # Mutt appends normal sendmail flags and recipient addresses to this command.
    args, _mutt_sendmail_args = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.install_mutt_sendmail:
        return _install_mutt_sendmail(args.install_mutt_sendmail)
    if args.capture_mail:
        return capture_mail()
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

