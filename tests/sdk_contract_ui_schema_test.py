from __future__ import annotations

import json
import subprocess
from pathlib import Path

from deepseek_harness_fixtures import current_settlement_frames

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


def test_deepseek_harness_frames_match_the_installed_ai_sdk_schema(
    node_toolchain_env: dict[str, str],
) -> None:
    """The dsh client's output rides the same UI chunk contract.

    Two vendor recordings drive the real client after their old durable chunk
    envelopes are repackaged into the current compact settlement contract:
    the recorded downlink of a live ``dsh --profile web`` server (which is
    what carries the consumption boundary, because only the product echoes the
    caller's prompt id), and the vendor's SDK snapshots for the tool and
    subagent turns those two prompts did not cover. ``result`` is the
    platform-internal terminal frame the turn worker consumes; it is not an AI
    SDK UI chunk and is excluded here exactly as the claude leg excludes it."""
    import asyncio
    from collections.abc import AsyncIterator

    from astrabox.core.service.orchestrator.engine.base import EngineInputCommand
    from astrabox.core.service.orchestrator.engine.deepseek_harness_client import (
        DeepSeekHarnessEngineClient,
    )
    from astrabox.core.service.orchestrator.engine.emissions import EngineEmission

    session_id = "dsh-schema-session"
    data_dir = ROOT / "tests" / "data" / "deepseek_harness"
    recorded_prompt_rpc_id = "astrabox-input:test-input-1:deadbeef"

    class _GoldenLink:
        is_live = True

        def __init__(self, frames: list[dict]) -> None:
            self._frames = frames

        async def call(
            self, method: str, payload: dict, *, rpc_id: str | None = None
        ) -> dict:
            _ = (payload, rpc_id)
            if method == "subagents/list":
                assert payload == {"args": {"parentSessionId": session_id}}
                return {"entries": [], "parentAvailable": True}
            assert method == "session/prompt", method
            assert payload["args"]["request"]["sessionId"] == session_id
            return {"accepted": True}

        async def respond(self, rpc_id: str, result: dict) -> bool:
            _ = (rpc_id, result)
            return True

        async def iter_frames(self) -> AsyncIterator[dict]:
            for frame in self._frames:
                yield frame
            # A completed turn does not close the resident product connection.
            # The client's close() cancels this reader in _collect's finally.
            await asyncio.Event().wait()

        async def close(self) -> None: ...

    def _product_frames() -> list[dict]:
        raw = (data_dir / "product-two-turns.mux.jsonl").read_text()
        raw = raw.replace("{{sessionId}}", session_id)
        messages = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return current_settlement_frames([
            {
                "rpcId": str(message.get("rpcId") or ""),
                "type": str(message["payload"].get("type") or ""),
                "payload": message["payload"],
            }
            for message in messages
        ], session_id=session_id)

    def _snapshot_frames(case: str) -> list[dict]:
        raw = (data_dir / f"{case}.notifications.jsonl").read_text()
        raw = raw.replace("{{sessionId}}", session_id)
        frames = []
        child_active = False
        children_finished = 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            message = json.loads(line)
            # This fixed, single-child snapshot redacts both native identities
            # to {{sessionId}}. Its explicit lifecycle notifications bracket
            # the child's contiguous events; restore a distinct fixture address
            # so the child's terminal cannot settle the root's tool call.
            if message.get("method") == "subagent.started":
                assert case == "subagent-spawn-in-process" and not child_active
                assert children_finished == 0
                child_active = True
                continue
            if message.get("method") == "subagent.finished":
                assert child_active
                assert message["params"]["status"] == "ok"
                assert message["params"]["stopReason"] == "completed"
                child_active = False
                children_finished += 1
                continue
            if message.get("method") != "session.event":
                continue
            frames.append(
                {
                    "rpcId": "",
                    "type": "session/event",
                    "payload": {
                        "sessionId": (
                            f"{session_id}-child" if child_active
                            else message["params"]["sessionId"]
                        ),
                        "type": "session/event",
                        "event": message["params"]["event"],
                    },
                }
            )
        assert not child_active
        assert children_finished == int(case == "subagent-spawn-in-process")
        return current_settlement_frames(frames, session_id=session_id)

    async def _collect(frames: list[dict], *, pin_prompt: bool) -> list[EngineEmission]:
        client = DeepSeekHarnessEngineClient(
            session_id=session_id,
            link=_GoldenLink(frames),
            native_session_id=session_id,
        )
        command = EngineInputCommand(
            command_id="cmd-golden",
            session_id=session_id,
            sequence=1,
            input_id="11111111-2222-4333-8444-555555555555",
            content="golden",
        )
        # The SDK snapshots predate the product wire and carry no prompt id on
        # their echo, so their consumption boundary cannot be recognised — it
        # is asserted instead, through the platform's own recovery entry
        # point, which is exactly what that flag means: this input is already
        # known to have been consumed. The product recording drives the
        # boundary for real.
        try:
            receipt = await client.begin_delivery(
                command, consumption_confirmed=not pin_prompt
            )
            if pin_prompt:
                client._prompted = {recorded_prompt_rpc_id: command}  # noqa: SLF001
            async with asyncio.timeout(10):
                emissions = [frame async for frame in client.iter_turn_events(receipt)]
            result = emissions[-1].as_frame()
            assert result["type"] == "result" and result["finishReason"] == "stop"
            assert sum(frame.as_frame()["type"] == "result" for frame in emissions) == 1
            return emissions
        finally:
            await client.close()

    emissions = asyncio.run(_collect(_product_frames(), pin_prompt=True))
    frames = [emission.as_frame() for emission in emissions]
    assert sum(frame.get("type") == "data-input-consumed" for frame in frames) == 1
    assert any(frame.get("type") == "text-delta" for frame in frames)
    for case in ("text-turn", "bash-tool", "subagent-spawn-in-process"):
        case_frames = [
            emission.as_frame()
            for emission in asyncio.run(
                _collect(_snapshot_frames(case), pin_prompt=False)
            )
        ]
        assert {"text-delta", "reasoning-delta"} <= {
            frame["type"] for frame in case_frames
        }, case
        if case != "text-turn":
            assert {"tool-input-start", "tool-input-available", "tool-output-available"} <= {
                frame["type"] for frame in case_frames
            }, case
        if case == "subagent-spawn-in-process":
            outputs = [frame for frame in case_frames if frame["type"] == "tool-output-available"]
            assert outputs == [{
                "type": "tool-output-available",
                "toolCallId": "call_00_oHPNQ1nLoakoaAGXIxCM7404",
                "output": {
                    "content": [{"type": "text", "text": "child answer 42."}],
                    "isError": False,
                },
            }]
            assert "".join(
                frame["delta"] for frame in case_frames if frame["type"] == "text-delta"
            ) == "child answer 42."
        frames.extend(case_frames)

    frames = [frame for frame in frames if frame.get("type") != "result"]
    _assert_frames_match_ui_schema(frames, node_toolchain_env)
