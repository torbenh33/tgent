import asyncio
import os

from llm import LLMClient
from test_subprocess import run_subprocess_checked

PROJECT_ROOT = "/home/torbenh/cvs/tgent"  # Based on your workspace context

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
    """
    messages = [
        {"role": "system", "content": "You are an expert summarization assistant. Provide a concise summary of the given text."},
        {"role": "user", "content": prompt},
    ]

    try:
        response = get_llm_client().chat(messages)
        if response and getattr(response, "choices", None):
            return response.choices[0].message.content or "Error: Empty response received"
        return "Error: No response received"
    except Exception as e:
        return f"Error communicating with LLM backend: {e}"

def add_git_note(sha1: str, note_content: str) -> bool:
    """
    Adds a git note (comment) to a specified commit SHA-1.
    Uses temporary files for safe interaction with 'git notes'.

    >>> # Assuming a valid SHA-1 and note content are provided
    >>> # Note: This test requires git to be configured correctly in the environment.
    >>> add_git_note("2a755dced1a9967f5f50fd570960ee2b34c808dd", "Test note content")
    True
    """
    import tempfile

    # Create a temporary file to hold the note content
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
        tmp.write(note_content)
        temp_filepath = tmp.name

    try:
        # Execute the command: git notes add -F <temp_file> <sha1>
        asyncio.run(
            run_subprocess_checked('git', 'notes', 'add', '-f', '-F', temp_filepath, sha1)
        )
        return True
    except Exception:
        return False
    finally:
        # Clean up the temporary file
        import os
        os.remove(temp_filepath)

def get_sha1_for_path(pathname: str) -> str | None:
    """
    Retrieves the Git SHA-1 hash for a given file path relative to the repository root.
    Returns the SHA-1 string on success, or None if the file is not tracked or an error occurs.

    >>> # NOTE: These tests require the current directory to be a valid Git repository 
    >>> # with tracked files for successful execution.
    >>> # Assuming 'README.md' exists and is tracked in the current git repo state.
    >>> get_sha1_for_path("README.md")
    '2a755dced1a9967f5f50fd570960ee2b34c808dd'

    >>> # Assuming 'nonexistent/file.txt' is not tracked or does not exist.
    >>> # The function prints a warning and returns None.
    >>> get_sha1_for_path("nonexistent/file.txt")
    Warning: Could not find SHA-1 for path 'nonexistent/file.txt'. Is it tracked by Git?
    """
    try:
        # Use git rev-parse --verify to get the full commit SHA of the current version of the file
        result = asyncio.run(
            run_subprocess_checked('git', 'rev-parse', '--verify', ':' + pathname)
        )
        return result["stdout"].strip()
    except RuntimeError:
        # This usually means the file is not tracked or does not exist in the index/repo
        print(f"Warning: Could not find SHA-1 for path '{pathname}'. Is it tracked by Git?")
        return None
    except Exception:
        # git command itself might not be found
        print("Error: 'git' command not found. Ensure Git is installed and in PATH.")
        return None

def summarize_file(filepath: str) -> str:
    """
    Reads a file and sends its content to the LLM for summarization.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Construct the prompt for the LLM
        user_prompt = f"File path: {filepath}\n\nFile Content:\n---\n{content}"

        print(f"Summarizing: {filepath}...")
        summary = get_ollama_summary(user_prompt)
        return summary
    except FileNotFoundError:
        return f"Error: File not found at {filepath}"
    except Exception as e:
        return f"An unexpected error occurred while processing {filepath}: {e}"

def process_directory(root_dir: str) -> dict:
    """
    Recursively traverses the directory, summarizes files, and aggregates directory summaries.
    """
    file_summaries = {}
    directory_summaries = {}

    for dirpath, dirnames, filenames in os.walk(root_dir):
        # 1. Process file summaries
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            summary = summarize_file(filepath)
            
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
    print(f"Starting project traversal in: {PROJECT_ROOT}")
    
    results = process_directory(PROJECT_ROOT)
    
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
