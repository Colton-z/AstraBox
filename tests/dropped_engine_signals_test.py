"""Two engine signals land where they belong instead of disappearing.

DEFER. The SDK's contract: a PreToolUse hook answering ``defer`` stops the run
and the result carries the call in ``deferred_tool_use``; resume re-issues it
under a fresh id. The call rides the result frame verbatim, and the bridge
closes the interaction where it reads the result. Routing it through a
separate ``deferred`` op is unreachable by construction: the runner sends it
AFTER the result, and the turn iterator returns AT the result, so nothing
would close the interaction the defer ended, and the console would keep
offering an approval whose only possible outcome is a 409.

ERRORS. The runner's event pump reports exceptions the SDK raises
(ProcessError — the CLI died; CLIJSONDecodeError — unparseable output) rather
than letting the pump task die silently and leaving the host to see a turn go
quiet with nothing to act on. The pump reports the SDK's own exception class
in-band before dying, and the engine client turns that report into a loud
error terminal carrying the class as its code — three different operator
actions arrive as three distinct codes rather than one bare string.
"""

from __future__ import annotations

import unittest
import uuid
from collections import deque
from typing import Any
from unittest.mock import AsyncMock

from astrabox.core.service.orchestrator.engine.claude_code_client import (
    ClaudeCodeEngineClient,
)
from astrabox.core.service.orchestrator.engine.base import EngineInputCommand
from astrabox.core.service.orchestrator.engine.frame_translator import (
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.sandbox_runner import (
    EnvelopeSender,
    RunnerSession,
)


class _FakeLink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True


class _ExplodingSdk:
    """An SDK whose message stream raises the way a dead CLI does."""

    class _ProcessError(Exception):
        pass

    _ProcessError.__name__ = "ProcessError"

    async def connect(self) -> None:  # pragma: no cover - not reached
        pass

    async def receive_messages(self):
        raise self._ProcessError("Command failed with exit code 1")
        yield  # pragma: no cover — make this an async generator

    async def disconnect(self) -> None:  # pragma: no cover - not reached
        pass


class PumpDeathIsReportedInBandTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_sdk_exception_class_reaches_the_wire(self) -> None:
        link = _FakeLink()
        core = RunnerSession.__new__(RunnerSession)
        core.sender = EnvelopeSender(link, "s-1")
        core._client = _ExplodingSdk()
        core._pending_prompt_consumptions = deque()
        core._consumed_prompt_ids = set()
        core._busy = False
        core._store = None

        with self.assertRaises(Exception):
            await core._pump_events()

        errors = [f for f in link.frames if f.get("op") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_class"], "ProcessError")
        self.assertIn("exit code 1", errors[0]["detail"])


class _QueueLink:
    """RunnerLink stand-in feeding a fixed frame list to the client."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = list(frames)
        self.is_live = True

    async def deliver(self, command: Any) -> dict[str, Any]:
        _ = command
        return {"seq": 0}

    async def next_frame(self) -> dict[str, Any] | None:
        return self._frames.pop(0) if self._frames else None


class ErrorOpBecomesALoudTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_error_class_is_the_terminal_code(self) -> None:
        link = _QueueLink(
            [
                {
                    "op": "error",
                    "seq": 5,
                    "error_class": "CLIJSONDecodeError",
                    "detail": "unparseable line",
                }
            ]
        )
        client = ClaudeCodeEngineClient(
            link,  # type: ignore[arg-type]
            session_id="s-1",
            transcript_store=AsyncMock(),
            workspace_dir="/workspace",
        )
        command = EngineInputCommand(
            command_id=uuid.uuid4().hex,
            session_id="s-1",
            sequence=1,
            input_id=str(uuid.uuid4()),
            content="hi",
        )
        await client.deliver(command)
        receipt = await client.begin_delivery(command)
        frames = [f async for f in client.iter_turn_events(receipt)]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["type"], "result")
        self.assertEqual(frames[0]["finishReason"], "error")
        self.assertEqual(frames[0]["error"]["code"], "CLIJSONDecodeError")
        self.assertIn("unparseable", frames[0]["error"]["message"])


class HiddenNoiseNeverBecomesAFrameTests(unittest.TestCase):
    def test_thinking_tokens_produces_no_frames(self) -> None:
        # Zero readers, hidden from rendering by declaration, information
        # duplicated on the result's usage — and it was one durable frame per
        # thinking delta (226 of a measured turn's 752).
        frames = list(
            translate_claude_sdk_message(
                {"__sdk_type": "SystemMessage", "subtype": "thinking_tokens", "tokens": 5},
                envelope_seq=1,
            )
        )
        self.assertEqual(frames, [])

    def test_init_still_flows_it_has_a_reader(self) -> None:
        frames = list(
            translate_claude_sdk_message(
                {"__sdk_type": "SystemMessage", "subtype": "init", "tools": []},
                envelope_seq=1,
            )
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["type"], "data-raw-event")


class DeferredRidesTheResultTests(unittest.TestCase):
    def test_the_result_frame_carries_the_deferred_call_verbatim(self) -> None:
        message = {
            "__sdk_type": "ResultMessage",
            "subtype": "success",
            "session_id": "sdk-1",
            "deferred_tool_use": {
                "__sdk_type": "DeferredToolUse",
                "id": "toolu_9",
                "name": "Bash",
                "input": {"command": "ls"},
            },
        }
        frames = list(translate_claude_sdk_message(message, envelope_seq=1))
        (result,) = [f for f in frames if f.get("type") == "result"]
        self.assertEqual(result["deferred_tool_use"]["id"], "toolu_9")
        self.assertEqual(result["deferred_tool_use"]["name"], "Bash")


if __name__ == "__main__":
    unittest.main()
