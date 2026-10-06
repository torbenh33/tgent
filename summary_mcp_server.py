import os
from collections.abc import Iterable
from typing import Any

from mcp.server.fastmcp import FastMCP

from git_notes import get_git_note_for_path
from sumup import PROJECT_ROOT, summarize_file

mcp = FastMCP("tgent-summaries")


def _abs_path(path: str) -> str:
    candidate = os.path.abspath(os.path.join(PROJECT_ROOT, path))
    project_root_abs = os.path.abspath(PROJECT_ROOT)

    if candidate != project_root_abs and not candidate.startswith(project_root_abs + os.sep):
        raise ValueError(f"Path escapes project root: {path}")

    return candidate


def _rel_path(path: str) -> str:
    return os.path.relpath(path, PROJECT_ROOT)


def _summary_for_rel_path(relative_path: str, generate_missing: bool) -> dict[str, Any]:
    absolute_path = _abs_path(relative_path)

    if not os.path.exists(absolute_path):
        return {
            "path": relative_path,
            "exists": False,
            "is_file": False,
            "summary": None,
            "source": "missing",
        }

    if not os.path.isfile(absolute_path):
        return {
            "path": relative_path,
            "exists": True,
            "is_file": False,
            "summary": None,
            "source": "not-a-file",
        }

    note = get_git_note_for_path(relative_path)
    if note:
        return {
            "path": relative_path,
            "exists": True,
            "is_file": True,
            "summary": note,
            "source": "git-note",
        }

    if not generate_missing:
        return {
            "path": relative_path,
            "exists": True,
            "is_file": True,
            "summary": None,
            "source": "missing-note",
        }

    generated = summarize_file(absolute_path)
    return {
        "path": relative_path,
        "exists": True,
        "is_file": True,
        "summary": generated,
        "source": "generated",
    }


def _iter_files(root_abs: str, recursive: bool) -> Iterable[str]:
    if recursive:
        for dirpath, _, filenames in os.walk(root_abs):
            for filename in filenames:
                yield os.path.join(dirpath, filename)
        return

    for entry in os.scandir(root_abs):
        if entry.is_file():
            yield entry.path


@mcp.tool()
def get_summary(path: str, generate_missing: bool = False) -> dict[str, Any]:
    """
    Return summary metadata for one path.
    Path is relative to project root.
    """
    rel = _rel_path(_abs_path(path))
    return _summary_for_rel_path(rel, generate_missing)


@mcp.tool()
def get_summaries(paths: list[str], generate_missing: bool = False) -> dict[str, Any]:
    """
    Return summaries for multiple paths in one call.
    """
    results = []
    for path in paths:
        rel = _rel_path(_abs_path(path))
        results.append(_summary_for_rel_path(rel, generate_missing))

    return {
        "count": len(results),
        "results": results,
    }


@mcp.tool()
def get_directory_summaries(
    path: str = ".",
    recursive: bool = True,
    generate_missing: bool = False,
    include_without_summary: bool = False,
    max_files: int = 2000,
) -> dict[str, Any]:
    """
    Return summaries for all files under a directory.
    Use this to traverse summaries with a single tool call.
    """
    root_abs = _abs_path(path)
    if not os.path.isdir(root_abs):
        raise ValueError(f"Not a directory: {path}")

    files = []
    truncated = False
    for file_abs in _iter_files(root_abs, recursive):
        rel = _rel_path(file_abs)
        item = _summary_for_rel_path(rel, generate_missing)

        if include_without_summary or item["summary"] is not None:
            files.append(item)

        if len(files) >= max_files:
            truncated = True
            break

    return {
        "root": _rel_path(root_abs),
        "recursive": recursive,
        "generate_missing": generate_missing,
        "include_without_summary": include_without_summary,
        "count": len(files),
        "truncated": truncated,
        "results": files,
    }


if __name__ == "__main__":
    mcp.run()
