from __future__ import annotations

import re
from typing import Any

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def normalize_selected_line_numbers(
    old_line_numbers: list[int],
    new_line_numbers: list[int],
) -> tuple[set[int], set[int], str | None]:
    """Return positive old/new line selections or an error message."""
    try:
        selected_old_lines = {int(line) for line in old_line_numbers if int(line) > 0}
        selected_new_lines = {int(line) for line in new_line_numbers if int(line) > 0}
    except (TypeError, ValueError):
        return set(), set(), "Line numbers must be integers."

    if not selected_old_lines and not selected_new_lines:
        return set(), set(), "At least one positive old or new line number is required."

    return selected_old_lines, selected_new_lines, None


def build_selected_patch(
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

        entries: list[dict[str, Any]] = []
        while i < len(lines) and not lines[i].startswith("@@ "):
            raw = lines[i]
            prefix = raw[:1]

            if prefix == " ":
                entries.append(
                    {
                        "raw": raw,
                        "kind": " ",
                        "old_ref": old_cur,
                        "new_ref": new_cur,
                        "old_before": old_cur,
                        "new_before": new_cur,
                    }
                )
                old_cur += 1
                new_cur += 1
            elif prefix == "-":
                entries.append(
                    {
                        "raw": raw,
                        "kind": "-",
                        "old_ref": old_cur,
                        "new_ref": None,
                        "old_before": old_cur,
                        "new_before": new_cur,
                    }
                )
                old_cur += 1
            elif prefix == "+":
                entries.append(
                    {
                        "raw": raw,
                        "kind": "+",
                        "old_ref": None,
                        "new_ref": new_cur,
                        "old_before": old_cur,
                        "new_before": new_cur,
                    }
                )
                new_cur += 1
            else:
                break

            i += 1

        selected_indexes = {
            idx
            for idx, entry in enumerate(entries)
            if (entry["kind"] == "-" and entry["old_ref"] in selected_old_lines)
            or (entry["kind"] == "+" and entry["new_ref"] in selected_new_lines)
        }

        if not selected_indexes:
            continue

        included_indexes = set(selected_indexes)
        for idx in selected_indexes:
            prev_idx = idx - 1
            next_idx = idx + 1

            if prev_idx >= 0 and entries[prev_idx]["kind"] == " ":
                included_indexes.add(prev_idx)

            if next_idx < len(entries) and entries[next_idx]["kind"] == " ":
                included_indexes.add(next_idx)

        sorted_indexes = sorted(included_indexes)
        segments: list[list[dict[str, Any]]] = []
        current_segment: list[dict[str, Any]] = []
        previous_idx: int | None = None

        for idx in sorted_indexes:
            if previous_idx is not None and idx != previous_idx + 1 and current_segment:
                segments.append(current_segment)
                current_segment = []

            current_segment.append(entries[idx])
            previous_idx = idx

        if current_segment:
            segments.append(current_segment)

        for segment in segments:
            old_count = sum(1 for e in segment if e["kind"] in {" ", "-"})
            new_count = sum(1 for e in segment if e["kind"] in {" ", "+"})

            old_start_entry = next((e for e in segment if e["kind"] in {" ", "-"}), None)
            new_start_entry = next((e for e in segment if e["kind"] in {" ", "+"}), None)

            old_start = old_start_entry["old_ref"] if old_start_entry else segment[0]["old_before"]
            new_start = new_start_entry["new_ref"] if new_start_entry else segment[0]["new_before"]

            hunks.append(
                "\n".join(
                    [
                        f"@@ -{old_start},{old_count} +{new_start},{new_count} @@",
                        *(e["raw"] for e in segment),
                    ]
                )
            )

    if not hunks:
        return ""

    return "\n".join([*preamble, *hunks, ""])

