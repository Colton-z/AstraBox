"""Withhold later root partial events while forwarding the real SDK messages.

The native client, tool execution and transcript store are unchanged. A complete
text block from a later native message pauses before runner delivery, allowing
the browser to observe the earlier live content before the test releases it.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolUseBlock

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "astrabox_complete_message_probe_runner", "/opt/astrabox/sandbox_runner.py"
)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
real_receive = ClaudeSDKClient.receive_messages


async def observed_receive(self):
    state = {
        "stage": "receiving", "stream_message_id": "", "first_tool_message_id": "",
        "forwarded_text": "", "suppressed_events": 0, "suppressed_message_ids": [],
        "complete_messages": [], "errors": [],
    }

    def save():
        temporary = ROOT / "evidence.tmp"
        temporary.write_text(json.dumps(state))
        temporary.replace(ROOT / "evidence.json")

    held = False
    async for message in real_receive(self):
        if (ROOT / "disarmed").exists():
            yield message
            continue
        if isinstance(message, StreamEvent) and message.parent_tool_use_id is None:
            if message.event.get("type") == "message_start":
                state["stream_message_id"] = str(message.event.get("message", {}).get("id") or "")
            native_id = state["stream_message_id"]
            suppress = state["first_tool_message_id"] and native_id != state["first_tool_message_id"]
            if suppress:
                if not native_id:
                    raise RuntimeError("real SDK stream lacks a native message ID")
                state["suppressed_events"] += 1
                if native_id not in state["suppressed_message_ids"]:
                    state["suppressed_message_ids"].append(native_id)
                save()
                continue
            delta = message.event.get("delta", {})
            if delta.get("type") == "text_delta":
                state["forwarded_text"] += delta["text"]
        if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
            text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
            tools = [block.id for block in message.content if isinstance(block, ToolUseBlock)]
            state["complete_messages"].append({
                "message_id": message.message_id, "text": text, "tool_ids": tools,
                "error": message.error,
            })
            if message.error is not None:
                state["errors"].append(message.error)
            if tools and not state["first_tool_message_id"]:
                if not message.message_id or message.message_id != state["stream_message_id"]:
                    raise RuntimeError("first real tool message must match the observed stream")
                state["first_tool_message_id"] = message.message_id
            if text and message.message_id in state["suppressed_message_ids"] and not held:
                held = True
                state["stage"] = "complete-awaiting-release"
                save()
                deadline = time.monotonic() + 45
                while not (ROOT / "release").exists():
                    if (ROOT / "disarmed").exists():
                        break
                    if time.monotonic() >= deadline:
                        state["errors"].append("complete message release deadline expired")
                        save()
                        raise TimeoutError("complete message release deadline expired")
                    await asyncio.sleep(0.05)
                state["stage"] = "released"
        if isinstance(message, ResultMessage):
            state["stage"] = "finished"
            state["result_is_error"] = message.is_error
        save()
        yield message


ClaudeSDKClient.receive_messages = observed_receive
asyncio.run(runner.main())
