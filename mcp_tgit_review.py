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
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from email.message import Message
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


def _message_id(message: Message) -> str | None:
    value = message.get("Message-ID")
    return str(value).strip() if value else None


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
def export_patch_series(base: str, head: str = "HEAD", repo_path: str = ".", series_id: str | None = None) -> dict[str, Any]:
    """Export ``base..head`` as an mbox, creating or revising a review series.

    This only reads committed Git history and does not alter the worktree.  Pass
    the returned mbox_path to Mutt; pass its series_id again for a new revision.
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
    exported = _git_bytes(
        repo,
        ["format-patch", "--stdout", "--thread=shallow", "--numbered", "--no-cover-letter", f"{base_sha}..{head_sha}"],
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

    patch_ids = [_message_id(patch) for patch in patches]
    if len(patches) != commit_count or any(patch_id is None for patch_id in patch_ids):
        return {
            "status": "error",
            "message": "Generated mbox must contain one Message-ID per patch.",
            "commit_count": commit_count,
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
    output: list[dict[str, Any]] = []
    for path in sorted(paths.mail_spool.glob("*.eml")):
        try:
            raw = path.read_bytes()
            message = email.message_from_bytes(raw, policy=email.policy.default)
        except (OSError, email.errors.MessageError):
            continue
        thread_ids = _headers(message, "In-Reply-To", "References")
        match = next((entry for thread_id in thread_ids for entry in known if entry[0] == thread_id), None)
        review_id = _message_id(message) or f"sha256:{hashlib.sha256(raw).hexdigest()}"
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
        target = MAIL_SPOOL / f"{digest}.eml"
        if not target.exists():
            _atomic_write(target, raw)
        return 0
    except OSError as err:
        print(f"tgit review capture: {err}", file=sys.stderr)
        return 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Git patch review MCP server")
    parser.add_argument("--capture-mail", action="store_true", help="Capture sendmail stdin locally; do not deliver it")
    # Mutt appends normal sendmail flags and recipient addresses to this command.
    args, _mutt_sendmail_args = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.capture_mail:
        return capture_mail()
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

