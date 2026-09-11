import argparse
import os
import subprocess

from git_notes import add_git_note_for_path, get_git_note_for_path
from llm import LLMClient

PROJECT_ROOT = os.getcwd()

_llm_client: LLMClient | None = None

def get_llm_client() -> LLMClient:
    """Returns a cached LLM client initialized from config.ini."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def get_ollama_summary(prompt: str) -> str:
    """
    Sends a prompt through llm.py (OpenAI-compatible client configured via config.ini).
    Raises RuntimeError on backend/config/API errors.
    """
    messages = [
        {"role": "system", "content": "You are an expert summarization assistant. Provide a concise summary of the given text."},
        {"role": "user", "content": prompt},
    ]

    try:
        response = get_llm_client().chat(messages)
        if not response or not getattr(response, "choices", None):
            raise RuntimeError("No response received")

        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Empty response received")
        return content
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Error communicating with LLM backend: {e}") from e


def remove_git_note_for_path(relative_path: str) -> str:
    """Removes a git note for the blob at HEAD:<relative_path>."""
    try:
        blob = subprocess.run(
            ["git", "rev-parse", f"HEAD:{relative_path}"],
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

        subprocess.run(
            ["git", "notes", "remove", blob],
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        return "removed"
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        if "no note found" in stderr.lower():
            return "none"
        return f"error: {stderr or e}"
    except Exception as e:
        return f"error: {e}"


def summarize_file(filepath: str, ignore_old_notes: bool = False) -> str:
    """
    Reads a file and summarizes it with the LLM unless a git note already exists
    for the current blob of the path. Summaries are cached in git notes.
    """
    try:
        relative_path = os.path.relpath(filepath, PROJECT_ROOT)

        # Reuse summary if a git note already exists for the current blob.
        if not ignore_old_notes:
            existing_note = get_git_note_for_path(relative_path)
            if existing_note:
                print(f"Reusing git note summary: {relative_path}")
                return existing_note

        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        user_prompt = f"File path: {relative_path}\n\nFile Content:\n---\n{content}"

        print(f"Summarizing: {relative_path}...")
        summary = get_ollama_summary(user_prompt)
        add_git_note_for_path(relative_path, summary)
        return summary
    except FileNotFoundError:
        return f"Error: File not found at {filepath}"
    except RuntimeError:
        raise
    except Exception as e:
        return f"An unexpected error occurred while processing {filepath}: {e}"

def process_directory(root_dir: str, ignore_old_notes: bool = False, clear_notes_only: bool = False) -> dict:
    """
    Recursively traverses the directory, summarizes files, and aggregates directory summaries.
    """
    file_summaries = {}
    directory_summaries = {}

    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Ignore hidden directories and files
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        visible_filenames = [f for f in filenames if not f.startswith('.')]

        # 1. Process file summaries
        for filename in visible_filenames:
            filepath = os.path.join(dirpath, filename)
            relative_path = os.path.relpath(filepath, PROJECT_ROOT)

            if clear_notes_only:
                status = remove_git_note_for_path(relative_path)
                print(f"Clear note [{status}]: {relative_path}")
                continue

            summary = summarize_file(filepath, ignore_old_notes=ignore_old_notes)

            # Store file-level summary (optional, but useful)
            file_summaries[filepath] = summary

            # 2. Aggregate summaries for the directory
            if dirpath not in directory_summaries:
                directory_summaries[dirpath] = []
            directory_summaries[dirpath].append(f"--- Summary for {filename} ---\n{summary}\n")

    return {
        "file_summaries": file_summaries,
        "directory_summaries": directory_summaries
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize project files and cache summaries in git notes.")
    parser.add_argument(
        "--ignore-old-notes",
        action="store_true",
        help="Do not reuse existing git notes; regenerate summaries.",
    )
    parser.add_argument(
        "--clear-notes-only",
        action="store_true",
        help="Remove git notes for traversed files and exit without summarization.",
    )
    args = parser.parse_args()

    print(f"Starting project traversal in: {PROJECT_ROOT}")

    try:
        results = process_directory(
            PROJECT_ROOT,
            ignore_old_notes=args.ignore_old_notes,
            clear_notes_only=args.clear_notes_only,
        )
    except RuntimeError as e:
        print(f"Fatal: {e}")
        raise SystemExit(1)

    if args.clear_notes_only:
        print("Done clearing notes.")
    else:
        print("\n" + "="*50)
        print("FILE SUMMARIES:")
        print("="*50)
        for path, summary in results["file_summaries"].items():
            print(f"\n[FILE]: {path}")
            print("-" * 20)
            print(summary[:500] + "..." if len(summary) > 500 else summary)

        print("\n" + "="*50)
        print("DIRECTORY SUMMARIES (Recursive):")
        print("="*50)
        for directory, summaries in results["directory_summaries"].items():
            print(f"\n[DIRECTORY]: {directory}")
            print("=" * 30)
            for summary_block in summaries:
                print(summary_block)
            print("\n" + "#" * 40)
