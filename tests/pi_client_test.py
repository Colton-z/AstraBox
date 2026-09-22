"""The pi client's turn slot, proven the way the platform actually drives it.

The turn pipeline stops iterating as soon as it has the terminal emission. A
client that releases its turn slot after yielding that frame therefore never
releases it at all — the generator is closed first, and the cleanup after the
yield does not run. A single-turn test cannot show that: the slot is only read
again by the next message, which is refused with "pi already has an active
turn".
"""

from __future__ import annotations

import asyncio

import json
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.base import EngineInputCommand
from astrabox.core.service.orchestrator.engine.emissions import TurnTerminal
from astrabox.core.service.orchestrator.engine.pi_client import PiEngineClient, _PiRelaySeam
from astrabox.core.service.orchestrator.engine.pi_events import PiProtocolError
from astrabox.core.service.orchestrator.engine.pi_pipe import PiWireRecord
from astrabox.core.service.orchestrator.engine.resident_relay import ResidentRelay
from astrabox.core.service.orchestrator.runtime.pty_terminal import ResolvedExecdEndpoint

_SESSION = "11111111-1111-4111-8111-111111111111"


def _turn_records() -> list[dict[str, Any]]:
    """One complete pi turn, in the order the vendor emits it."""

    return [
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_start", "contentIndex": 0},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": "hi",
            },
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "contentIndex": 0},
        },
        {
            "type": "message_end",
            "message": {"role": "assistant", "stopReason": "stop", "content": []},
        },
        {"type": "turn_end"},
        {"type": "agent_end", "willRetry": False},
        {"type": "agent_settled"},
    ]


class _FakeProcess:
    """A pi process that answers commands and replays one turn per prompt."""

    def __init__(self) -> None:
        self.is_connected = True
        self.fatal = None
        self.pty_session_id = "pty-1"
        self.sent: list[dict[str, Any]] = []
        self._queue: list[dict[str, Any]] = []
        self._arrived = asyncio.Event()
        self._offset = 0

    async def command(self, request_id: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.sent.append({**payload, "id": request_id})
        if payload.get("type") == "get_state":
            return {
                "id": request_id,
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": {"sessionId": "pi-session-1", "isStreaming": False},
            }
        if payload.get("type") == "prompt":
            records = _turn_records()
            if payload.get("streamingBehavior") == "followUp" and self._queue:
                # One active pi agent run drains the follow-up before its only
                # agent_end / agent_settled pair.
                assert [row.get("type") for row in self._queue[-2:]] == [
                    "agent_end",
                    "agent_settled",
                ]
                self._queue[-2:] = records[1:]
            else:
                self._queue.extend(records)
            self._arrived.set()
        return {
            "id": request_id,
            "type": "response",
            "command": payload.get("type"),
            "success": True,
        }

    async def next_record(self) -> PiWireRecord:
        # The wire waits when nothing has arrived, as the pipe does; the
        # relay reads it for the life of the process and must be able to sit
        # on an empty wire between turns.
        while not self._queue:
            self._arrived.clear()
            await self._arrived.wait()
        self._offset += 1
        return PiWireRecord(record=self._queue.pop(0), output_offset=self._offset)

    async def current_output_offset(self) -> int:
        return self._offset

    async def detach(self) -> None:
        self.is_connected = False


def _client(process: _FakeProcess) -> PiEngineClient:
    client = PiEngineClient(
        endpoint=ResolvedExecdEndpoint(origin="http://sandbox.test", headers={}),
        platform_session_id=_SESSION,
        cwd="/workspace",
        command="pi --mode rpc",
    )
    client._process = process
    client._engine_session_key = "pi-session-1"
    client._relay = ResidentRelay(
        seam=_PiRelaySeam(client),
        session_id=_SESSION,
        engine_session_key="pi-session-1",
        next_record=process.next_record,
        send_command=lambda payload: client._request(process, payload),
        current_sequence=process.current_output_offset,
        resident_output_sink=None,
        event_sink=None,
    )
    client._relay.start()
    return client


def _command(sequence: int, content: str) -> EngineInputCommand:
    return EngineInputCommand(
        command_id=f"cmd-{sequence}",
        session_id=_SESSION,
        sequence=sequence,
        input_id=f"2222222{sequence}-2222-4222-8222-222222222222",
        content=content,
    )


async def _drive_one_turn(client: PiEngineClient, command: EngineInputCommand) -> list[Any]:
    """Consume exactly as the platform does: stop at the terminal emission."""

    await client.deliver(command)
    receipt = await client.begin_delivery(command)
    emissions = []
    async for emission in client.iter_turn_events(receipt):
        emissions.append(emission)
        if isinstance(emission, TurnTerminal):
            break  # the turn pipeline does exactly this
    return emissions


@pytest.mark.asyncio
async def test_a_second_message_in_one_conversation_gets_its_own_turn() -> None:
    """The turn slot has to be free once the terminal is out.

    Releasing it after the terminal frame is yielded never happens: the
    consumer breaks, the generator closes, and the next message is refused.
    """

    process = _FakeProcess()
    client = _client(process)

    first = await _drive_one_turn(client, _command(1, "first"))
    assert isinstance(first[-1], TurnTerminal)
    assert first[-1].outcome == "completed"
    assert client.active_receipt is None, "the turn slot was not released"

    second = await _drive_one_turn(client, _command(2, "second"))
    assert isinstance(second[-1], TurnTerminal)

    prompts = [record for record in process.sent if record.get("type") == "prompt"]
    assert [record["message"] for record in prompts] == ["first", "second"]
    assert [record["id"] for record in prompts] == ["cmd-1", "cmd-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_only_an_acknowledged_abort_cancels_an_error_terminal(cancel: bool) -> None:
    process = _FakeProcess()
    client = _client(process)
    command = _command(1, "first")
    await client.deliver(command)
    receipt = await client.begin_delivery(command)
    for record in process._queue:
        if record.get("type") == "message_end":
            record["message"].update(stopReason="error", errorMessage="This operation was aborted")
    if cancel:
        assert await client.cancel_turn(receipt)
    emissions = [item async for item in client.iter_turn_events(receipt)]
    terminal = emissions[-1]
    assert isinstance(terminal, TurnTerminal)
    assert terminal.outcome == ("cancelled" if cancel else "failed")
    assert client._abort_request is None
    assert not await client.cancel_turn(receipt)
    second = await _drive_one_turn(client, _command(2, "second"))
    assert second[-1].outcome == "completed"


@pytest.mark.asyncio
async def test_a_rejected_abort_cannot_publish_a_cancelled_terminal() -> None:
    class RejectAbort(_FakeProcess):
        async def command(self, request_id: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if payload.get("type") == "abort":
                return {"success": False, "error": "abort rejected"}
            return await super().command(request_id, payload, **kwargs)

    client = _client(RejectAbort())
    command = _command(1, "first")
    await client.deliver(command)
    receipt = await client.begin_delivery(command)
    with pytest.raises(PiProtocolError, match="abort rejected"):
        await client.cancel_turn(receipt)
    with pytest.raises(PiProtocolError, match="abort rejected"):
        _ = [item async for item in client.iter_turn_events(receipt)]


@pytest.mark.asyncio
async def test_the_first_emission_reports_the_input_the_engine_accepted() -> None:
    """``data-input-consumed`` must precede any response frame for that input."""

    process = _FakeProcess()
    client = _client(process)

    emissions = await _drive_one_turn(client, _command(1, "hello"))

    first = emissions[0].as_frame()
    assert first["type"] == "data-input-consumed"
    assert first["data"]["content"] == "hello"


@pytest.mark.asyncio
async def test_a_prompt_sent_while_streaming_says_how_to_queue() -> None:
    """Pi refuses a mid-stream prompt that does not declare a behavior.

    The platform FIFO means "after the work in flight", which is followUp.
    """

    process = _FakeProcess()
    client = _client(process)
    client._streaming = True

    command = _command(1, "queued")
    await client.deliver(command)
    await client.begin_delivery(command)

    prompt = next(record for record in process.sent if record.get("type") == "prompt")
    assert prompt["streamingBehavior"] == "followUp"


@pytest.mark.asyncio
async def test_a_delivery_during_a_turn_joins_its_native_fifo_batch() -> None:
    """SubmitInput is delivered without a second platform turn consumer."""

    process = _FakeProcess()
    client = _client(process)
    first = _command(1, "first")
    second = _command(2, "second")

    await client.deliver(first)
    receipt = await client.begin_delivery(first)
    stream = client.iter_turn_events(receipt)
    emissions = [await anext(stream)]

    # This is the live platform's busy-turn path: it calls deliver() only.
    await client.deliver(second)
    async for emission in stream:
        emissions.append(emission)

    consumed = [
        frame["data"]["content"]
        for frame in (emission.as_frame() for emission in emissions)
        if frame.get("type") == "data-input-consumed"
    ]
    assert consumed == ["first", "second"]
    assert isinstance(emissions[-1], TurnTerminal)
    assert client.active_receipt is None

    prompts = [record for record in process.sent if record.get("type") == "prompt"]
    assert [record["message"] for record in prompts] == ["first", "second"]
    assert prompts[1]["streamingBehavior"] == "followUp"


@pytest.mark.asyncio
async def test_a_refused_prompt_fails_the_delivery_with_the_engine_s_reason() -> None:
    class _RefusingProcess(_FakeProcess):
        async def command(self, request_id: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
            if payload.get("type") == "prompt":
                return {
                    "id": request_id,
                    "type": "response",
                    "command": "prompt",
                    "success": False,
                    "error": "No API key found for astrabox.",
                }
            return await super().command(request_id, payload)

    client = _client(_RefusingProcess())
    command = _command(1, "hello")
    await client.deliver(command)

    with pytest.raises(Exception, match="No API key found"):
        await client.begin_delivery(command)


@pytest.mark.asyncio
async def test_the_command_carries_the_platform_command_id() -> None:
    """The response pi returns for that id is the consumption evidence."""

    process = _FakeProcess()
    client = _client(process)
    command = _command(1, "hello")

    await client.deliver(command)
    await client.begin_delivery(command)

    prompt = next(record for record in process.sent if record.get("type") == "prompt")
    assert prompt["id"] == command.command_id
    assert json.loads(json.dumps(prompt))["message"] == "hello"
