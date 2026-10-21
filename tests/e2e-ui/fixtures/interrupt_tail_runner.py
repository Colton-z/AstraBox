"""Hold native completion blocks until the real runner journals interrupt ACK.

Only SDK delivery timing changes. The image's runner, SDK messages, response
translation and persistence remain real; the observation file is a source oracle.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, StreamEvent, TextBlock

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("interrupt_tail_runner", "/opt/astrabox/sandbox_runner.py")
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
ack = asyncio.Event()
state: dict = {"events": [], "streamed_text": "", "complete_text": "", "tail_parts": []}
blocks: dict[int, str] = {}
receive_messages = ClaudeSDKClient.receive_messages
send = runner.EnvelopeSender.send


def save() -> None:
    temporary = ROOT / "observation.tmp"
    temporary.write_text(json.dumps(state))
    temporary.replace(ROOT / "observation.json")


async def delayed_messages(self):
    async for message in receive_messages(self):
        selected = "result_seq" not in state
        root = getattr(message, "parent_tool_use_id", None) is None
        if selected and root and isinstance(message, StreamEvent):
            event = message.event
            if event["type"] == "content_block_start":
                kind = event["content_block"]["type"]
                if kind in {"text", "thinking"}:
                    blocks[event["index"]] = "text-end" if kind == "text" else "reasoning-end"
            if event["type"] == "content_block_stop":
                kind = blocks.pop(event["index"], None)
                if ack.is_set() and kind:
                    state["tail_parts"].append(kind)
            if ack.is_set() and event["type"] == "message_stop":
                state["tail_parts"].append("finish-step")
        if selected and root and isinstance(message, AssistantMessage):
            text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
            if text:
                state["complete_text"] += text
                state["complete_message_id"] = message.message_id
                save()
                await asyncio.wait_for(ack.wait(), timeout=30)
        if selected and isinstance(message, ResultMessage):
            state["native_terminal_reason"] = message.terminal_reason
            save()
            await asyncio.wait_for(ack.wait(), timeout=30)
        yield message


async def observed_send(self, op, **fields):
    sequence = await send(self, op, **fields)
    if "result_seq" in state:
        return sequence
    message = fields.get("message") or {}
    event = message.get("event") or {}
    if message.get("parent_tool_use_id") is None and event.get("type") == "message_start":
        state["stream_message_id"] = event["message"]["id"]
    if event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
        state["streamed_text"] += event["delta"]["text"]
    kind = fields.get("message_type")
    if op == "turn_interrupted":
        state["ack_seq"] = sequence
        ack.set()
    if kind in {"AssistantMessage", "ResultMessage"} or op == "turn_interrupted":
        state["events"].append({"op": op, "kind": kind, "seq": sequence,
                                "engine_boundary": fields.get("engine_boundary", False)})
    if kind == "ResultMessage":
        state["result_seq"] = sequence
    save()
    return sequence


save()
ClaudeSDKClient.receive_messages = delayed_messages
runner.EnvelopeSender.send = observed_send
asyncio.run(runner.main())
