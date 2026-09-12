import re
import subprocess
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("tgit")



def _git_command(repo_path: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    target_repo = repo_path.strip() if isinstance(repo_path, str) and repo_path.strip() else "."
    cmd.extend(["-C", target_repo])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )



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



def _list_submodules() -> list[dict[str, str]]:
    proc = _git_command(".", ["submodule", "status", "--recursive"])
    if proc.returncode != 0:
        return []

    submodules: list[dict[str, str]] = []
    for raw_line in proc.stdout.splitlines():
        if not raw_line.strip():
            continue
        state = raw_line[:1]
        payload = raw_line[1:].strip()
        parts = payload.split()
        if len(parts) < 2:
            continue
        submodules.append({"path": parts[1], "state": state})
    return submodules



def _repo_status(repo_path: str, submodule_state: str | None = None) -> dict[str, Any]:
    branch_proc = _git_command(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    head_proc = _git_command(repo_path, ["rev-parse", "HEAD"])
    unstaged_proc = _git_command(repo_path, ["diff", "--no-color", "-U0"])
    staged_proc = _git_command(repo_path, ["diff", "--cached", "--no-color", "-U0"])

    errors: list[dict[str, Any]] = []
    if branch_proc.returncode != 0:
        errors.append(
            {
                "command": ["rev-parse", "--abbrev-ref", "HEAD"],
                "stderr": branch_proc.stderr,
            }
        )
    if head_proc.returncode != 0:
        errors.append(
            {
                "command": ["rev-parse", "HEAD"],
                "stderr": head_proc.stderr,
            }
        )
    if unstaged_proc.returncode != 0:
        errors.append(
            {
                "command": ["diff", "--no-color", "-U0"],
                "stderr": unstaged_proc.stderr,
            }
        )
    if staged_proc.returncode != 0:
        errors.append(
            {
                "command": ["diff", "--cached", "--no-color", "-U0"],
                "stderr": staged_proc.stderr,
            }
        )

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

    has_unstaged_changes = bool(unstaged_proc.stdout.strip())
    has_staged_changes = bool(staged_proc.stdout.strip())

    result: dict[str, Any] = {
        "repo_path": repo_path,
        "branch": None if detached or not branch_name else branch_name,
        "detached": detached,
        "head_sha": head_proc.stdout.strip() if head_proc.returncode == 0 else None,
        "unstaged_diff": unstaged_rendered,
        "staged_diff": staged_rendered,
        "has_unstaged_changes": has_unstaged_changes,
        "has_staged_changes": has_staged_changes,
        "is_clean": not has_unstaged_changes and not has_staged_changes,
    }
    if submodule_state is not None:
        result["submodule_state"] = submodule_state
    if errors:
        result["errors"] = errors
    return result


@mcp.resource("gitdiff://context")
async def git_diff_context() -> dict:
    """Return staged/unstaged git diff context for the superproject and all submodules."""
    repos = [_repo_status(".")]
    for submodule in _list_submodules():
        repos.append(_repo_status(submodule["path"], submodule["state"]))

    return {
        "status": "ok",
        "repos": repos,
    }


_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
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
def stage_lines(
    path: str,
    old_line_numbers: list[int],
    new_line_numbers: list[int],
    repo_path: str = ".",
) -> dict[str, Any]:
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

    diff_proc = _git_command(repo_path, ["diff", "--no-color", "-U0", "--", path])
    if diff_proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to read git diff.",
            "stderr": diff_proc.stderr,
            "repo_path": repo_path,
        }

    patch = _build_selected_patch(diff_proc.stdout, selected_old_lines, selected_new_lines)
    if not patch:
        return {
            "status": "ok",
            "message": "No matching changed lines found to stage.",
            "repo_path": repo_path,
            "path": path,
            "staged_old_lines": [],
            "staged_new_lines": [],
        }

    apply_proc = subprocess.run(
        ["git", "-C", repo_path, "apply", "--cached", "--unidiff-zero", "-"],
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
            "repo_path": repo_path,
            "path": path,
        }

    return {
        "status": "ok",
        "repo_path": repo_path,
        "path": path,
        "staged_old_lines": sorted(selected_old_lines),
        "staged_new_lines": sorted(selected_new_lines),
    }


@mcp.tool()
def unstage_lines(
    path: str,
    old_line_numbers: list[int],
    new_line_numbers: list[int],
    repo_path: str = ".",
) -> dict[str, Any]:
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

    diff_proc = _git_command(repo_path, ["diff", "--cached", "--no-color", "-U0", "--", path])
    if diff_proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to read staged git diff.",
            "stderr": diff_proc.stderr,
            "repo_path": repo_path,
        }

    patch = _build_selected_patch(diff_proc.stdout, selected_old_lines, selected_new_lines)
    if not patch:
        return {
            "status": "ok",
            "message": "No matching staged lines found to unstage.",
            "repo_path": repo_path,
            "path": path,
            "unstaged_old_lines": [],
            "unstaged_new_lines": [],
        }

    apply_proc = subprocess.run(
        ["git", "-C", repo_path, "apply", "-R", "--cached", "--unidiff-zero", "-"],
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
            "repo_path": repo_path,
            "path": path,
        }

    return {
        "status": "ok",
        "repo_path": repo_path,
        "path": path,
        "unstaged_old_lines": sorted(selected_old_lines),
        "unstaged_new_lines": sorted(selected_new_lines),
    }


@mcp.tool()
def stage_files(paths: list[str], repo_path: str = ".") -> dict[str, Any]:
    """Stage entire files using git add for a provided list of file paths."""
    selected_paths = [p for p in paths if isinstance(p, str) and p.strip()]
    if not selected_paths:
        return {"status": "error", "message": "At least one file path must be provided."}

    proc = _git_command(repo_path, ["add", "--", *selected_paths])
    if proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to stage files.",
            "stderr": proc.stderr,
            "stdout": proc.stdout,
            "repo_path": repo_path,
            "paths": selected_paths,
        }

    return {
        "status": "ok",
        "message": "Files staged.",
        "repo_path": repo_path,
        "paths": selected_paths,
    }


@mcp.tool()
def unstage_files(paths: list[str], repo_path: str = ".") -> dict[str, Any]:
    """Unstage entire files using git restore --staged for a provided list of file paths."""
    selected_paths = [p for p in paths if isinstance(p, str) and p.strip()]
    if not selected_paths:
        return {"status": "error", "message": "At least one file path must be provided."}

    proc = _git_command(repo_path, ["restore", "--staged", "--", *selected_paths])
    if proc.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to unstage files.",
            "stderr": proc.stderr,
            "stdout": proc.stdout,
            "repo_path": repo_path,
            "paths": selected_paths,
        }

    return {
        "status": "ok",
        "message": "Files unstaged.",
        "repo_path": repo_path,
        "paths": selected_paths,
    }


@mcp.tool()
def stash_changes(message: str = "", include_untracked: bool = False, repo_path: str = ".") -> dict[str, Any]:
    """Stash local changes using git stash push."""
    has_changes = _git_command(repo_path, ["status", "--porcelain"])
    if has_changes.returncode != 0:
        return {
            "status": "error",
            "message": "Failed to inspect working tree state.",
            "stderr": has_changes.stderr,
            "repo_path": repo_path,
        }
    if not has_changes.stdout.strip():
        return {"status": "ok", "message": "No local changes to stash.", "repo_path": repo_path}

    cmd = ["stash", "push"]
    if include_untracked:
        cmd.append("--include-untracked")
    if message and message.strip():
        cmd.extend(["-m", message.strip()])

    stash_proc = _git_command(repo_path, cmd)
    if stash_proc.returncode != 0:
        return {
            "status": "error",
            "message": "git stash failed.",
            "stdout": stash_proc.stdout,
            "stderr": stash_proc.stderr,
            "repo_path": repo_path,
        }

    return {
        "status": "ok",
        "message": "Changes stashed.",
        "repo_path": repo_path,
        "stdout": stash_proc.stdout,
        "stderr": stash_proc.stderr,
    }


@mcp.tool()
def commit_staged(message: str, repo_path: str = ".") -> dict[str, Any]:
    """Commit currently staged changes using git commit."""
    if not message or not message.strip():
        return {"status": "error", "message": "Commit message must be provided."}

    has_staged = _git_command(repo_path, ["diff", "--cached", "--quiet"])
    if has_staged.returncode == 0:
        return {"status": "error", "message": "No staged changes to commit.", "repo_path": repo_path}
    if has_staged.returncode not in (0, 1):
        return {
            "status": "error",
            "message": "Failed to inspect staged changes.",
            "stderr": has_staged.stderr,
            "repo_path": repo_path,
        }

    commit_proc = _git_command(repo_path, ["commit", "-m", message.strip()])
    if commit_proc.returncode != 0:
        return {
            "status": "error",
            "message": "git commit failed.",
            "stdout": commit_proc.stdout,
            "stderr": commit_proc.stderr,
            "repo_path": repo_path,
        }

    return {
        "status": "ok",
        "message": "Commit created.",
        "repo_path": repo_path,
        "stdout": commit_proc.stdout,
        "stderr": commit_proc.stderr,
    }


if __name__ == "__main__":
    mcp.run()
