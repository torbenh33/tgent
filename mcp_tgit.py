import re
import subprocess
from typing import Any

from fastmcp import FastMCP
from test_subprocess import run_subprocess

mcp = FastMCP("TestRunnerService")


@mcp.resource("gitdiff://context")
async def git_diff_context() -> dict:
    """Return current git diff output formatted with delta."""
    cmd = (
        "printf '### Unstaged changes\\n'; "
        "git diff | delta -n --hunk-header-style=omit; "
        "printf '\\n### Staged changes\\n'; "
        "git diff --cached | delta -n --hunk-header-style=omit"
    )
    return await run_subprocess("/bin/sh", "-lc", cmd)

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"

def _render_diff_with_delta(diff_text: str) -> tuple[str, str | None]:
    if not diff_text:
        return "", None

    delta_proc = subprocess.run(
        ["delta", "-n", "--hunk-header-style=omit", "--paging=never"],
        input=diff_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if delta_proc.returncode != 0:
        return diff_text, delta_proc.stderr
    return delta_proc.stdout, None


)


def _build_selected_patch(
    diff_text: str,
    selected_old_lines: set[int],
    selected_new_lines: set[int],
) -> str:
    lines = diff_text.splitlines()
    if not lines:
        return ""

    try:
        first_hunk_idx = next(i for i, line in enumerate(lines) if line.startswith("@@ "))
    except StopIteration:
        return ""

    preamble = lines[:first_hunk_idx]
    hunks: list[str] = []

    i = first_hunk_idx
    while i < len(lines):
        header_line = lines[i]
        match = _HUNK_HEADER_RE.match(header_line)
        if not match:
            i += 1
            continue

        old_cur = int(match.group("old_start"))
        new_cur = int(match.group("new_start"))
        i += 1

        group: list[dict[str, Any]] = []
        grouped_entries: list[list[dict[str, Any]]] = []

        while i < len(lines) and not lines[i].startswith("@@ "):
            raw = lines[i]
            prefix = raw[:1]

            if prefix == "-":
                entry = {
                    "raw": raw,
                    "kind": "-",
                    "old_ref": old_cur,
                    "new_ref": None,
                    "old_before": old_cur,
                    "new_before": new_cur,
                }
                old_cur += 1
            elif prefix == "+":
                entry = {
                    "raw": raw,
                    "kind": "+",
                    "old_ref": None,

    unstaged_rendered, unstaged_delta_err = _render_diff_with_delta(unstaged_proc.stdout)
    staged_rendered, staged_delta_err = _render_diff_with_delta(staged_proc.stdout)

    if unstaged_delta_err:
        errors.append(
            {
                "command": ["delta", "-n", "--hunk-header-style=omit"],
                "stderr": unstaged_delta_err,
                "context": "unstaged_diff",
            }
        )
    if staged_delta_err:
        errors.append(
            {
                "command": ["delta", "-n", "--hunk-header-style=omit"],
                "stderr": staged_delta_err,
                "context": "staged_diff",
            }
        )

    branch_name = branch_proc.stdout.strip() if branch_proc.returncode == 0 else ""
    detached = branch_name == "HEAD"

    result: dict[str, Any] = {
        "repo_path": repo_path,
        "branch": None if detached or not branch_name else branch_name,
        "detached": detached,
        "head_sha": head_proc.stdout.strip() if head_proc.returncode == 0 else None,
        "unstaged_diff": f"### Unstaged changes\n{unstaged_rendered}",
        "staged_diff": f"### Staged changes\n{staged_rendered}",
    }
    if submodule_state is not None:
                    "new_ref": new_cur,
                    "old_before": old_cur,
                    "new_before": new_cur,
                }
                new_cur += 1
            elif prefix == " ":
                old_cur += 1
                new_cur += 1
                if group:
                    grouped_entries.append(group)
                    group = []
                i += 1
                continue
            else:
                if group:
                    grouped_entries.append(group)
                    group = []
                i += 1
                continue

            include = (
                entry["kind"] == "-" and entry["old_ref"] in selected_old_lines
            ) or (
                entry["kind"] == "+" and entry["new_ref"] in selected_new_lines
            )

            if include:
                group.append(entry)
            elif group:
                grouped_entries.append(group)
                group = []

            i += 1

        if group:
            grouped_entries.append(group)

        for entries in grouped_entries:
            old_count = sum(1 for e in entries if e["kind"] == "-")
            new_count = sum(1 for e in entries if e["kind"] == "+")

            if old_count:
                old_start = next(e["old_ref"] for e in entries if e["kind"] == "-")
            else:
                old_start = entries[0]["old_before"]

            if new_count:
                new_start = next(e["new_ref"] for e in entries if e["kind"] == "+")
            else:
                new_start = entries[0]["new_before"]

            hunks.append(
                "\n".join(
                    [
                        f"@@ -{old_start},{old_count} +{new_start},{new_count} @@",
                        *(e["raw"] for e in entries),
                    ]
                )
            )

    if not hunks:
        return ""

    return "\n".join([*preamble, *hunks, ""])


@mcp.tool()
def stage_lines(path: str, old_line_numbers: list[int], new_line_numbers: list[int]) -> dict[str, Any]:
    """Stage selected old/new changed line numbers from one file using git apply --cached."""
    if not path:
        return {"status": "error", "message": "Path must be provided."}

    selected_old_lines = {int(line) for line in old_line_numbers if int(line) > 0}
    selected_new_lines = {int(line) for line in new_line_numbers if int(line) > 0}
    if not selected_old_lines and not selected_new_lines:
        return {
            "status": "error",
            "message": "At least one positive old or new line number is required.",
        }

    diff_proc = subprocess.run(
        ["git", "diff", "--no-color", "-U0", "--", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if diff_proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to read git diff.",
            "stderr": diff_proc.stderr,
        }

    patch = _build_selected_patch(diff_proc.stdout, selected_old_lines, selected_new_lines)
    if not patch:
        return {
            "status": "ok",
            "message": "No matching changed lines found to stage.",
            "path": path,
            "staged_old_lines": [],
            "staged_new_lines": [],
        }

    apply_proc = subprocess.run(
        ["git", "apply", "--cached", "--unidiff-zero", "-"],
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if apply_proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to apply patch to index.",
            "stderr": apply_proc.stderr,
            "path": path,
        }

    return {
        "status": "ok",
        "path": path,
        "staged_old_lines": sorted(selected_old_lines),
        "staged_new_lines": sorted(selected_new_lines),
    }


@mcp.tool()
def unstage_lines(path: str, old_line_numbers: list[int], new_line_numbers: list[int]) -> dict[str, Any]:
    """Unstage selected old/new changed line numbers from one file using reverse git apply --cached."""
    if not path:
        return {"status": "error", "message": "Path must be provided."}

    selected_old_lines = {int(line) for line in old_line_numbers if int(line) > 0}
    selected_new_lines = {int(line) for line in new_line_numbers if int(line) > 0}
    if not selected_old_lines and not selected_new_lines:
        return {
            "status": "error",
            "message": "At least one positive old or new line number is required.",
        }

    diff_proc = subprocess.run(
        ["git", "diff", "--cached", "--no-color", "-U0", "--", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if diff_proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to read staged git diff.",
            "stderr": diff_proc.stderr,
        }

    patch = _build_selected_patch(diff_proc.stdout, selected_old_lines, selected_new_lines)
    if not patch:
        return {
            "status": "ok",
            "message": "No matching staged lines found to unstage.",
            "path": path,
            "unstaged_old_lines": [],
            "unstaged_new_lines": [],
        }

    apply_proc = subprocess.run(
        ["git", "apply", "-R", "--cached", "--unidiff-zero", "-"],
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if apply_proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to unstage patch from index.",
            "stderr": apply_proc.stderr,
            "path": path,
        }

    return {
        "status": "ok",
        "path": path,
        "unstaged_old_lines": sorted(selected_old_lines),
        "unstaged_new_lines": sorted(selected_new_lines),
    }


@mcp.tool()
def stage_files(paths: list[str]) -> dict[str, Any]:
    """Stage entire files using git add for a provided list of file paths."""
    selected_paths = [p for p in paths if isinstance(p, str) and p.strip()]
    if not selected_paths:
        return {"status": "error", "message": "At least one file path must be provided."}

    proc = subprocess.run(
        ["git", "add", "--", *selected_paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to stage files.",
            "stderr": proc.stderr,
            "stdout": proc.stdout,
            "paths": selected_paths,
        }

    return {
        "status": "ok",
        "message": "Files staged.",
        "paths": selected_paths,
    }


@mcp.tool()
def unstage_files(paths: list[str]) -> dict[str, Any]:
    """Unstage entire files using git restore --staged for a provided list of file paths."""
    selected_paths = [p for p in paths if isinstance(p, str) and p.strip()]
    if not selected_paths:
        return {"status": "error", "message": "At least one file path must be provided."}

    proc = subprocess.run(
        ["git", "restore", "--staged", "--", *selected_paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to unstage files.",
            "stderr": proc.stderr,
            "stdout": proc.stdout,
            "paths": selected_paths,
        }

    return {
        "status": "ok",
        "message": "Files unstaged.",
        "paths": selected_paths,
    }


@mcp.tool()
def stash_changes(message: str = "", include_untracked: bool = False) -> dict[str, Any]:
    """Stash local changes using git stash push."""
    has_changes = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if has_changes.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to inspect working tree state.",
            "stderr": has_changes.stderr,
        }
    if not has_changes.stdout.strip():
        return {"status": "ok", "message": "No local changes to stash."}

    cmd = ["git", "stash", "push"]
    if include_untracked:
        cmd.append("--include-untracked")
    if message and message.strip():
        cmd.extend(["-m", message.strip()])

    stash_proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if stash_proc.returncode != 0:
        return {
            "status": "error",
            "message": "git stash failed.",
            "stdout": stash_proc.stdout,
            "stderr": stash_proc.stderr,
        }

    return {
        "status": "ok",
        "message": "Changes stashed.",
        "stdout": stash_proc.stdout,
        "stderr": stash_proc.stderr,
    }


@mcp.tool()
def commit_staged(message: str) -> dict[str, Any]:
    """Commit currently staged changes using git commit."""
    if not message or not message.strip():
        return {"status": "error", "message": "Commit message must be provided."}

    has_staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if has_staged.returncode == 0:
        return {"status": "error", "message": "No staged changes to commit."}
    if has_staged.returncode not in (0, 1):
        return {
            "status": "error",
            "message": "Failed to inspect staged changes.",
            "stderr": has_staged.stderr,
        }

    commit_proc = subprocess.run(
        ["git", "commit", "-m", message.strip()],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_proc.returncode != 0:
        return {
            "status": "error",
            "message": "git commit failed.",
            "stdout": commit_proc.stdout,
            "stderr": commit_proc.stderr,
        }

    return {
        "status": "ok",
        "message": "Commit created.",
        "stdout": commit_proc.stdout,
        "stderr": commit_proc.stderr,
    }


if __name__ == "__main__":
    mcp.run()
