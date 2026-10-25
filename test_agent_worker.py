"""Tests for worker file tools and bounded model-context construction."""

from __future__ import annotations

from pathlib import Path

import asyncio

import pytest

from agent_worker import AgentWorker, SYSTEM_PROMPT, WorkerError, _edit_file, _read_file, _resolve_project_file, _search_files


def test_system_prompt_prioritizes_mcp_pytest_and_project_venv() -> None:
    prompt = SYSTEM_PROMPT

    assert "First check" in prompt
    assert "discovered `mcp_test_runner` pytest resource/tool" in prompt
    assert "`.venv/bin/pytest`" in prompt
    assert "`venv/bin/pytest`" in prompt
    assert "do not assume system `pytest` exists" in prompt
    assert "Do not install dependencies" in prompt
    assert "replace pytest with unittest" in prompt


def test_read_file_returns_requested_one_based_lines(tmp_path: Path) -> None:
    (tmp_path / "example.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = _read_file(tmp_path, {"path": "example.py", "start_line": 2, "end_line": 3})

    assert result == {
        "path": "example.py",
        "start_line": 2,
        "end_line": 3,
        "total_lines": 3,
        "content": "two\nthree",
    }


def test_read_and_search_reject_paths_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        with pytest.raises(WorkerError, match="outside the project"):
            _resolve_project_file(tmp_path.resolve(), str(outside), must_exist=True)
    finally:
        outside.unlink(missing_ok=True)


def test_edit_applies_ordered_exact_replacements_atomically(tmp_path: Path) -> None:
    source = tmp_path / "file.txt"
    source.write_text("alpha\nbeta\n", encoding="utf-8")

    result = _edit_file(
        tmp_path,
        {
            "path": "file.txt",
            "replacements": [
                {"search": "alpha", "replace": "first"},
                {"search": "beta", "replace": "second"},
            ],
        },
    )

    assert result["changed"] is True
    assert source.read_text(encoding="utf-8") == "first\nsecond\n"


def test_edit_uses_target_filename_for_atomic_temporary_file(tmp_path: Path) -> None:
    source = tmp_path / "file.txt"
    source.write_text("before\n", encoding="utf-8")

    result = _edit_file(
        tmp_path,
        {"path": "file.txt", "replacements": [{"search": "before", "replace": "after"}]},
    )

    assert result["changed"] is True
    assert source.read_text(encoding="utf-8") == "after\n"
    assert list(tmp_path.iterdir()) == [source]


def test_edit_rejects_missing_or_ambiguous_match_without_partial_write(tmp_path: Path) -> None:
    source = tmp_path / "file.txt"
    original = "alpha\nalpha\n"
    source.write_text(original, encoding="utf-8")

    with pytest.raises(WorkerError, match="expected one exact match"):
        _edit_file(
            tmp_path,
            {
                "path": "file.txt",
                "replacements": [
                    {"search": "alpha", "replace": "changed"},
                    {"search": "missing", "replace": "nope"},
                ],
            },
        )

    assert source.read_text(encoding="utf-8") == original


def test_search_files_returns_bounded_line_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\nnone\nneedle again\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("needle here\n", encoding="utf-8")

    result = _search_files(tmp_path, {"query": "needle", "max_results": 2})

    assert len(result["matches"]) == 2
    assert result["truncated"] is True
    assert all(item["path"] in {"a.py", "b.py"} for item in result["matches"])


def test_child_uses_isolated_snapshot_and_cannot_spawn() -> None:
    class Client:
        requests = []

        async def request(self, method, params):
            self.requests.append((method, params))
            return {"choices": [{"message": {"content": "summary"}}]}

    client = Client()
    worker = AgentWorker(client, Path("."), "agent")  # type: ignore[arg-type]
    worker.history = [{"role": "user", "content": "parent"}]

    result = asyncio.run(worker._run_child("summarize"))

    assert result == "summary"
    assert worker.history == [{"role": "user", "content": "parent"}]
    request = client.requests[0][1]
    assert request["messages"][-1]["content"].endswith("summarize")
    assert "delegate_context_task" not in [tool["function"]["name"] for tool in request["tools"]]
    assert not hasattr(worker, "_adopt_child_history")


    transcript = [
        {"role": "user", "content": "task", "id": "message-id", "model": "ui-model", "reasoning": "private"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
            "id": "assistant-id",
        },
    ]
    worker = AgentWorker(client=None, root=Path("."), agent_id="agent")  # type: ignore[arg-type]

    messages = worker._api_messages(transcript)

    assert messages[0]["role"] == "system"
    assert "Available tools:" in messages[0]["content"]
    assert "read_file" in messages[0]["content"]
    assert "submit_patch_queue" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "task"}
    assert messages[2]["tool_calls"] == transcript[1]["tool_calls"]
    assert transcript[0]["id"] == "message-id"
