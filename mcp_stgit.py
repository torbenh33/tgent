#!/usr/bin/env python3
"""Basic MCP tools for working with StGit patch stacks.

Run from an MCP client. Mutating tools use StGit's message options so the
caller can provide patch messages interactively, without opening an editor.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("stgit")


def _resolve_repo_path(repo_path: str | None) -> str:
    candidate = repo_path.strip() if isinstance(repo_path, str) else ""
    return os.path.abspath(os.path.expanduser(candidate or "."))


def _git_command(repo_path: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", _resolve_repo_path(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _stg_command(repo_path: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["stg", *args],
        cwd=_resolve_repo_path(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )


def _validate_repo(repo_path: str) -> tuple[str | None, dict[str, Any] | None]:
    target_repo = _resolve_repo_path(repo_path)
    if shutil.which("stg") is None:
        return None, {"status": "error", "message": "StGit executable 'stg' was not found."}
    proc = _git_command(target_repo, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return None, {
            "status": "error",
            "message": "Path is not inside a Git repository.",
            "repo_path": target_repo,
            "stderr": proc.stderr.strip(),
        }
    return os.path.realpath(proc.stdout.strip()), None


def _run_stg(repo_path: str, args: list[str], action: str) -> dict[str, Any]:
    repo, error = _validate_repo(repo_path)
    if error:
        return error
    assert repo is not None
    proc = _stg_command(repo, args)
    if proc.returncode != 0:
        return {
            "status": "error",
            "message": f"StGit {action} failed.",
            "repo_path": repo,
            "command": ["stg", *args],
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    return {
        "status": "ok",
        "repo_path": repo,
        "command": ["stg", *args],
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


@mcp.resource("stgit://status")
async def stgit_status_context() -> dict[str, Any]:
    """Return the current StGit status and patch series for this repository."""
    status = _run_stg(".", ["status"], "status")
    if status["status"] != "ok":
        return status
    series = _run_stg(".", ["series"], "series")
    if series["status"] != "ok":
        return series
    return {
        "status": "ok",
        "repo_path": status["repo_path"],
        "working_tree": status["stdout"],
        "series": series["stdout"],
    }


@mcp.tool()
def stgit_status(repo_path: str = ".") -> dict[str, Any]:
    """Return StGit's status output for the repository and its current stack."""
    return _run_stg(repo_path, ["status"], "status")


@mcp.tool()
def stgit_series(repo_path: str = ".") -> dict[str, Any]:
    """List patches in the current StGit stack, including applied and unapplied patches."""
    return _run_stg(repo_path, ["series"], "series")


@mcp.tool()
def stgit_commit(
    patches: list[str] | None = None,
    number: int | None = None,
    all_applied: bool = False,
    repo_path: str = ".",
) -> dict[str, Any]:
    """Permanently store applied patches in the stack base with ``stg commit``.

    Select patches by name, commit the first ``number`` applied patches, or
    commit all applied patches. If no selector is supplied, use StGit's default.
    The selectors are mutually exclusive.
    """
    selected = [item.strip() for item in (patches or []) if isinstance(item, str) and item.strip()]
    selector_count = bool(selected) + (number is not None) + bool(all_applied)
    if selector_count > 1:
        return {"status": "error", "message": "Choose only one of patches, number, or all_applied."}
    if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
        return {"status": "error", "message": "number must be a positive integer."}

    args = ["commit"]
    if selected:
        args.extend(["--", *selected])
    elif number is not None:
        args.extend(["--number", str(number)])
    elif all_applied:
        args.append("--all")
    return _run_stg(repo_path, args, "commit")


@mcp.tool()
def stgit_uncommit(
    commits: list[str] | None = None,
    number: int | None = None,
    prefix: str | None = None,
    to_ref: str | None = None,
    exclusive: bool = False,
    repo_path: str = ".",
) -> dict[str, Any]:
    """Turn regular Git commits into StGit patches using ``stg uncommit``.

    Select named patches with ``commits``, the newest ``number`` commits
    optionally limited by ``prefix``, or commits through ``to_ref``. These
    selection modes are mutually exclusive. ``exclusive`` applies only to
    ``to_ref`` and excludes that commit.
    """
    selected = [item.strip() for item in (commits or []) if isinstance(item, str) and item.strip()]
    clean_prefix = prefix.strip() if isinstance(prefix, str) else ""
    clean_to_ref = to_ref.strip() if isinstance(to_ref, str) else ""

    if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
        return {"status": "error", "message": "number must be a positive integer."}
    if exclusive and not clean_to_ref:
        return {"status": "error", "message": "exclusive requires to_ref."}
    selector_count = bool(selected) + (number is not None or bool(clean_prefix)) + bool(clean_to_ref)
    if selector_count > 1:
        return {"status": "error", "message": "Choose named commits, number/prefix, or to_ref."}
    if clean_prefix and number is None:
        return {"status": "error", "message": "prefix can only be used with number."}

    args = ["uncommit"]
    if clean_to_ref:
        args.extend(["--to", clean_to_ref])
        if exclusive:
            args.append("--exclusive")
    elif number is not None:
        args.extend(["--number", str(number)])
        if clean_prefix:
            args.extend(["--", clean_prefix])
    elif selected:
        args.extend(["--", *selected])
    return _run_stg(repo_path, args, "uncommit")


@mcp.tool()
def stgit_show(patch: str | None = None, repo_path: str = ".") -> dict[str, Any]:
    """Show a patch from the current stack, or the current patch when omitted."""
    args = ["show"]
    if isinstance(patch, str) and patch.strip():
        args.append(patch.strip())
    return _run_stg(repo_path, args, "show")


@mcp.tool()
def stgit_diff(patch: str | None = None, repo_path: str = ".") -> dict[str, Any]:
    """Show the diff for a patch, or the current worktree diff when patch is omitted."""
    args = ["diff"]
    if isinstance(patch, str) and patch.strip():
        args.append(patch.strip())
    return _run_stg(repo_path, args, "diff")


@mcp.tool()
def stgit_push(
    patches: list[str] | None = None,
    all_unapplied: bool = False,
    number: int | None = None,
    reverse: bool = False,
    set_tree: bool = False,
    keep: bool = False,
    merged: bool = False,
    repo_path: str = ".",
) -> dict[str, Any]:
    """Push patches onto the stack using ``stg push``.

    Select named patches/ranges, all unapplied patches, or a number of patches.
    These selectors are mutually exclusive. Optional flags control push order,
    tree handling, local changes, and checking for patches merged upstream.
    """
    selected = [item.strip() for item in (patches or []) if isinstance(item, str) and item.strip()]
    selector_count = bool(selected) + bool(all_unapplied) + (number is not None)
    if selector_count > 1:
        return {"status": "error", "message": "Choose only one of patches, all_unapplied, or number."}
    if number is not None and (not isinstance(number, int) or isinstance(number, bool) or number < 1):
        return {"status": "error", "message": "number must be a positive integer."}

    args = ["push"]
    if all_unapplied:
        args.append("--all")
    elif number is not None:
        args.extend(["--number", str(number)])
    if reverse:
        args.append("--reverse")
    if set_tree:
        args.append("--set-tree")
    if keep:
        args.append("--keep")
    if merged:
        args.append("--merged")
    if selected:
        args.extend(["--", *selected])
    return _run_stg(repo_path, args, "push")


@mcp.tool()
def stgit_pop(patch: str | None = None, repo_path: str = ".") -> dict[str, Any]:
    """Pop the top patch, or pop patches through the named patch, off the stack."""
    args = ["pop"]
    if isinstance(patch, str) and patch.strip():
        args.append(patch.strip())
    return _run_stg(repo_path, args, "pop")


@mcp.tool()
def stgit_goto(patch: str, repo_path: str = ".") -> dict[str, Any]:
    """Move the applied stack to the named patch."""
    clean_patch = patch.strip() if isinstance(patch, str) else ""
    if not clean_patch:
        return {"status": "error", "message": "Patch name must be provided."}
    return _run_stg(repo_path, ["goto", clean_patch], "goto")


@mcp.tool()
def stgit_new_patch(message: str, repo_path: str = ".") -> dict[str, Any]:
    """Create a new patch using the supplied commit-style message.

    Pass the message directly from the interactive client; StGit is invoked
    with -m and does not open an editor.
    """
    clean_message = message.strip() if isinstance(message, str) else ""
    if not clean_message:
        return {"status": "error", "message": "Patch message must be provided."}
    return _run_stg(repo_path, ["new", "-m", clean_message], "new")


@mcp.tool()
def stgit_refresh_patch(message: str | None = None, repo_path: str = ".") -> dict[str, Any]:
    """Refresh the current patch with worktree changes and optionally update its message.

    The message is passed via -m when provided, avoiding an editor prompt.
    """
    args = ["refresh"]
    if isinstance(message, str) and message.strip():
        args.extend(["-m", message.strip()])
    return _run_stg(repo_path, args, "refresh")


@mcp.tool()
def stgit_edit_patch(
    message: str,
    patch: str | None = None,
    sign: bool = False,
    ack: bool = False,
    review: bool = False,
    no_verify: bool = False,
    repo_path: str = ".",
) -> dict[str, Any]:
    """Edit a patch description using a supplied message, without opening an editor.

    Optionally target a patch by name and add Signed-off-by, Acked-by, or
    Reviewed-by trailers. ``no_verify`` disables the commit-msg hook.
    """
    clean_message = message.strip() if isinstance(message, str) else ""
    if not clean_message:
        return {"status": "error", "message": "Patch message must be provided."}

    args = ["edit", "--message", clean_message]
    if sign:
        args.append("--sign")
    if ack:
        args.append("--ack")
    if review:
        args.append("--review")
    if no_verify:
        args.append("--no-verify")
    clean_patch = patch.strip() if isinstance(patch, str) else ""
    if clean_patch:
        args.extend(["--", clean_patch])
    return _run_stg(repo_path, args, "edit")


if __name__ == "__main__":
    mcp.run()
