"""A settled response folds the same way on a reloaded page as on the open one.

The open page groups a response's parts with
``MessageParts.tsx::groupAssistantProcess``, where a data part the console
carries alongside the work (a Write call's ``data-file-changes``) joins the run
it sits in. A reloaded page reads :func:`project_record` instead, and there the
same part arrives as a ``ui_data`` block. These cases use the block order a
real Claude Code turn produced on the AWS testbed: two tool calls, their
results, the Write's file changes, then the model's thinking and its answer.
The thinking has to fold with the work on both pages, and the file changes
have to stay on the page, where the Diff panel reads them.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.history_blocks import (
    process_summary_input,
    project_record,
)

MESSAGE_ID = "message-1"


def _tool_work() -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_use",
            "id": "read-1",
            "name": "Read",
            "input": {"file_path": "/workspace/a.txt"},
        },
        {
            "type": "tool_use",
            "id": "write-1",
            "name": "Write",
            "input": {"file_path": "/workspace/b.txt"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "read-1",
            "content": "MARKER",
            "is_error": False,
            "tool_result_state": "output-available",
        },
        {
            "type": "tool_result",
            "tool_use_id": "write-1",
            "content": "File created",
            "is_error": False,
            "tool_result_state": "output-available",
        },
        {
            "type": "ui_data",
            "part": {
                "type": "data-file-changes",
                "id": "file-changes:write-1",
                "data": {"toolCallId": "write-1", "toolName": "Write", "files": []},
            },
        },
        {"type": "thinking", "thinking": "Both steps are done. Now reply."},
    ]


def _record(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "message_id": MESSAGE_ID,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "blocks": blocks,
    }


def _types(blocks: list[dict[str, Any]]) -> list[str]:
    return [str(block.get("type")) for block in blocks]


def test_thinking_after_a_writes_file_changes_folds_with_the_work() -> None:
    record = _record(
        [
            *_tool_work(),
            {"type": "text", "text": "ANSWER"},
            {"type": "result", "stop_reason": "end_turn", "num_turns": 3},
        ]
    )

    projected = project_record(record)

    shown = projected.record["blocks"]
    assert _types(shown) == ["process_block", "ui_data", "text", "result"], (
        "the thinking belongs to the folded work, the file changes and the answer stay on the page"
    )
    [(block_id, folded)] = projected.details.items()
    assert shown[0]["process_details"]["block_id"] == block_id
    assert shown[0]["process_details"]["tool_count"] == 2
    assert _types(folded) == ["tool_use", "tool_use", "tool_result", "tool_result", "thinking"]


def test_a_response_without_an_answer_keeps_one_header_across_file_changes() -> None:
    record = _record(
        [
            *_tool_work(),
            {"type": "result", "stop_reason": "end_turn", "num_turns": 2},
        ]
    )

    projected = project_record(record)

    shown = projected.record["blocks"]
    assert _types(shown) == ["process_block", "ui_data", "result"], (
        "one run of work is one header; file changes between its parts must not split it"
    )
    [folded] = projected.details.values()
    assert _types(folded) == ["tool_use", "tool_use", "tool_result", "tool_result", "thinking"]


def test_a_cancelled_result_folds_the_stopped_turn_and_names_its_work() -> None:
    # A user stop completes the turn; the platform result records that it was
    # cancelled. The retained e0827 record had this shape: a finished call, the
    # call the stop landed on (no result), and the cancelled result.
    record = _record(
        [
            {"type": "thinking", "thinking": "Run the first command."},
            {"type": "tool_use", "id": "first", "name": "Bash", "input": {"command": "echo a"}},
            {
                "type": "tool_result",
                "tool_use_id": "first",
                "content": "a",
                "is_error": False,
                "tool_result_state": "output-available",
            },
            {
                "type": "tool_use",
                "id": "stopped",
                "name": "Bash",
                "input": {"command": "sleep 300"},
            },
            {"type": "result", "finish_reason": "cancelled", "engine_kind": "claude_code"},
        ]
    )

    projected = project_record(record)

    shown = projected.record["blocks"]
    assert _types(shown) == ["process_block", "tool_use", "result"], (
        "the finished work folds; the call the stop landed on stays on the page"
    )
    assert shown[1]["id"] == "stopped"
    assert shown[0]["process_details"]["summarize"] is True, (
        "a stopped turn folds behind one labelled header, like the open page"
    )
    summary_input = process_summary_input(record)
    assert summary_input is not None
    assert summary_input.turn_completed is False
