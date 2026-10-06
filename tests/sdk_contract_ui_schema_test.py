from __future__ import annotations

import json
import subprocess
from pathlib import Path

from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    translate_assistant_response_event,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.bridge_loop import (
    _build_ack_control_frames,
)


ROOT = Path(__file__).resolve().parents[1]


def test_every_platform_frame_matches_the_installed_ai_sdk_schema(
    node_toolchain_env: dict[str, str],
) -> None:
    cursor = ClaudeStreamCursor()
    frames: list[dict] = []
    for sequence, message in enumerate(
        [
            {
                "__sdk_type": "AssistantMessage",
                "content": [
                    {"__sdk_type": "ThinkingBlock", "thinking": "thinking"},
                    {"__sdk_type": "TextBlock", "text": "hello"},
                    {
                        "__sdk_type": "ToolUseBlock",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"cmd": "pwd"},
                    },
                ],
            },
            {
                "__sdk_type": "UserMessage",
                "content": [
                    {
                        "__sdk_type": "ToolResultBlock",
                        "tool_use_id": "tool-1",
                        "content": "/workspace",
                    }
                ],
            },
            {
                "__sdk_type": "UserMessage",
                "content": [
                    {
                        "__sdk_type": "ToolResultBlock",
                        "tool_use_id": "tool-2",
                        "content": "failed",
                        "is_error": True,
                    }
                ],
            },
            {
                "__sdk_type": "TaskStartedMessage",
                "subtype": "task_started",
                "tool_use_id": "child-run-1",
                "task_id": "task-1",
                "description": "research",
                "uuid": "subagent-lifecycle-1",
            },
            {
                "__sdk_type": "ResultMessage",
                "subtype": "success",
                "session_id": "sdk-session",
            },
        ],
        start=1,
    ):
        frames.extend(
            translate_claude_sdk_message(
                message,
                envelope_seq=sequence,
                cursor=cursor,
            )
        )
    for event in (
        {"type": "response.output_text.delta", "item_id": "out-1", "delta": "hi"},
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "call_id": "call-1", "name": "tool"},
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call-1",
                "name": "tool",
                "arguments": '{"x":1}',
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "done",
            },
        },
        {"type": "response.completed", "response": {"id": "response-1"}},
    ):
        frames.extend(translate_assistant_response_event(event))
    frames.extend(_build_ack_control_frames("turn-1", "client-1"))
    frames.extend(
        [
            {"type": "data-result", "data": {"stop_reason": "end_turn"}},
            {"type": "finish", "finishReason": "stop"},
            {"type": "error", "errorText": "turn failed"},
        ]
    )
    frames = [frame for frame in frames if frame.get("type") != "result"]
    start = next(frame for frame in frames if frame.get("type") == "start")
    assert start == {
        "type": "start",
        "messageId": "turn-1",
        "messageMetadata": {"turn_id": "turn-1"},
    }

    _assert_frames_match_ui_schema(frames, node_toolchain_env)


_UI_SCHEMA_SCRIPT = """
const fs = require('node:fs');
const { safeValidateTypes } = require('@ai-sdk/provider-utils');
const { uiMessageChunkSchema } = require('ai');
const frames = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  for (const frame of frames) {
    const result = await safeValidateTypes({
      value: frame,
      schema: uiMessageChunkSchema,
    });
    if (!result.success) {
      throw new Error(`${JSON.stringify(frame)}: ${String(result.error)}`);
    }
  }
})().catch(error => {
  process.stderr.write(String(error));
  process.exitCode = 1;
});
"""


def _assert_frames_match_ui_schema(
    frames: list[dict], node_toolchain_env: dict[str, str]
) -> None:
    result = subprocess.run(
        ["node", "-e", _UI_SCHEMA_SCRIPT],
        cwd=ROOT / "frontend",
        input=json.dumps(frames),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=node_toolchain_env,
    )

    assert result.returncode == 0, result.stderr
