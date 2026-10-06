#!/usr/bin/env python3
import argparse

from git_notes import (
    get_git_note_for_path,
    get_sha1_for_path,
    get_tracked_files,
    remove_git_note,
)


BROKEN_NOTE_MARKERS: tuple[str, ...] = (
    "Error communicating with LLM backend:",
    "Error: No response received",
    "Error: Empty response received",
    "An unexpected error occurred while processing",
    "Error: File not found at",
)


def is_broken_note(note: str) -> bool:
    content = note.strip()
    return any(marker in content for marker in BROKEN_NOTE_MARKERS)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove broken git notes while keeping valid summaries."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove notes. Without this flag, only print what would be removed.",
    )
    args = parser.parse_args()

    scanned = 0
    broken = 0
    removed = 0

    paths = get_tracked_files()

    for path in paths:
        sha = get_sha1_for_path(path)
        if sha is None:
            continue

        scanned += 1
        note = get_git_note_for_path(path)
        if not note:
            continue

        if is_broken_note(note):
            broken += 1
            if args.apply:
                ok = remove_git_note(sha)
                if ok:
                    removed += 1
                    print(f"Removed broken note: {sha} ({path})")
                else:
                    print(f"Failed to remove note: {sha} ({path})")
            else:
                print(f"Would remove broken note: {sha} ({path})")

    print("\nDone.")
    print(f"Scanned tracked paths: {scanned}")
    print(f"Broken notes found: {broken}")
    if args.apply:
        print(f"Broken notes removed: {removed}")
    else:
        print("Dry run only. Use --apply to remove notes.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
