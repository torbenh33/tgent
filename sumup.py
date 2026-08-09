import os
import requests
from typing import List

# --- Configuration ---
OLLAMA_URL = "http://localhost:11434/api/generate"  # Adjust if Ollama is remote
MODEL_NAME = "gemma4:e2b"  # Replace with the model you are running in Ollama
PROJECT_ROOT = "/home/torbenh/cvs/tgent"  # Based on your workspace context

def get_ollama_summary(prompt: str) -> str:
    """
    Sends a prompt to the Ollama API to get a summary.
    """
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False
    }
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(OLLAMA_URL, json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
        # Assuming the response structure contains the generated text
        return result.get("response", "Error: No response received")
    except requests.exceptions.RequestException as e:
        return f"Error communicating with Ollama: {e}"

def add_git_note(sha1: str, note_content: str) -> bool:
    """
    Adds a git note (comment) to a specified commit SHA-1.
    Uses temporary files for safe interaction with 'git notes'.
    Returns True on success, False otherwise.
    """
    import subprocess
    import tempfile

    # Create a temporary file to hold the note content
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
        tmp.write(note_content)
        temp_filepath = tmp.name

    try:
        print(f"Attempting to add git note to {sha1}...")
        # Execute the command: git notes --file <temp_file> <sha1>
        result = subprocess.run(
            ['git', 'notes', '--file', temp_filepath, sha1], 
            check=True, 
            capture_output=True, 
            text=True
        )
        print("Successfully added git note.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error adding git note to {sha1}:")
        print(f"STDOUT: {e.stdout}")
        print(f"STDERR: {e.stderr}")
        return False
    finally:
        # Clean up the temporary file
        import os
        os.remove(temp_filepath)

def summarize_file(filepath: str) -> str:
    """
    Reads a file and sends its content to the LLM for summarization.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Construct the prompt for the LLM
        system_prompt = "You are an expert summarization assistant. Provide a concise summary of the following text."
        user_prompt = f"{system_prompt}\n\nFile Content:\n---\n{content}"
        
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
