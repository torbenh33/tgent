import asyncio
import tempfile

from test_subprocess import run_subprocess_checked


def add_git_note(sha1: str, note_content: str) -> bool:
    """
    Adds a git note (comment) to a specified commit SHA-1.
    Uses a temporary file for safe interaction with `git notes`.

    >>> # Assuming a valid SHA-1 and note content are provided
    >>> # Note: This test requires git to be configured correctly in the environment.
    >>> add_git_note("2a755dced1a9967f5f50fd570960ee2b34c808dd", "Test note content")
    True
    """
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write(note_content)
        temp_filepath = tmp.name

    try:
        asyncio.run(
            run_subprocess_checked("git", "notes", "add", "-f", "-F", temp_filepath, sha1)
        )
        return True
    except Exception:
        return False
    finally:
        try:
            import os

            os.remove(temp_filepath)
        except OSError:
            pass


def git_note_exists(sha1: str) -> bool:
    """
    Checks whether a git note exists for a given object SHA-1.

    >>> # Assuming this object has a note.
    >>> git_note_exists("2a755dced1a9967f5f50fd570960ee2b34c808dd")
    True
    """
    try:
        asyncio.run(run_subprocess_checked("git", "notes", "show", sha1))
        return True
    except RuntimeError:
        return False
    except Exception:
        return False


def remove_git_note(sha1: str) -> bool:
    """
    Removes a git note for a given object SHA-1.
    Returns True on success, False if removal fails.
    """
    try:
        asyncio.run(run_subprocess_checked("git", "notes", "remove", sha1))
        return True
    except Exception:
        return False


def get_tracked_files() -> list[str]:
    """
    Returns all tracked file paths (repository-relative).
    Returns an empty list if retrieval fails.
    """
    try:
        result = asyncio.run(run_subprocess_checked("git", "ls-files"))
        return [line for line in result["stdout"].splitlines() if line.strip()]
    except Exception:
        return []


def get_sha1_for_path(pathname: str) -> str | None:
    """
    Retrieves the Git SHA-1 hash for a given file path relative to the repository root.
    Returns the SHA-1 string on success, or None if the file is not tracked or an error occurs.

    >>> # NOTE: These tests require the current directory to be a valid Git repository.
    >>> # Assuming 'README.md' exists and is tracked in the current git repo state.
    >>> isinstance(get_sha1_for_path("README.md"), (str, type(None)))
    True

    >>> # Assuming 'nonexistent/file.txt' is not tracked or does not exist.
    >>> get_sha1_for_path("nonexistent/file.txt") is None
    True
    """
    try:
        result = asyncio.run(
            run_subprocess_checked("git", "rev-parse", "--verify", ":" + pathname)
        )
        return result["stdout"].strip()
    except RuntimeError:
        return None
    except Exception:
        return None


def add_git_note_for_path(pathname: str, note_content: str) -> bool:
    """
    Adds a git note to the blob referenced by a repository-relative file path.
    Returns False if the path cannot be resolved.

    >>> # Assuming 'README.md' exists, is tracked, and git notes are enabled.
    >>> add_git_note_for_path("README.md", "Summary note")
    True

    >>> # Setting a note again replaces the previous one.
    >>> add_git_note_for_path("README.md", "Updated summary note")
    True
    >>> get_git_note_for_path("README.md")
    'Updated summary note'
    """
    sha1 = get_sha1_for_path(pathname)
    if sha1 is None:
        return False
    return add_git_note(sha1, note_content)


def git_note_exists_for_path(pathname: str) -> bool:
    """
    Checks whether a git note exists for the blob referenced by a repository-relative file path.

    >>> # Assuming 'README.md' exists and is tracked.
    >>> git_note_exists_for_path("README.md")
    True
    """
    sha1 = get_sha1_for_path(pathname)
    if sha1 is None:
        return False
    return git_note_exists(sha1)


def get_git_note_for_path(pathname: str) -> str | None:
    """
    Retrieves the git note for the blob referenced by a repository-relative file path.
    Returns the note content on success, or None if the path cannot be resolved
    or no note exists.

    >>> # Assuming 'README.md' exists and is tracked.
    >>> isinstance(get_git_note_for_path("README.md"), (str, type(None)))
    True

    >>> # Assuming this path is not tracked.
    >>> get_git_note_for_path("nonexistent/file.txt") is None
    True
    """
    sha1 = get_sha1_for_path(pathname)
    if sha1 is None:
        return None

    try:
        result = asyncio.run(run_subprocess_checked("git", "notes", "show", sha1))
        return result["stdout"].strip()
    except RuntimeError:
        return None
    except Exception:
        return None
