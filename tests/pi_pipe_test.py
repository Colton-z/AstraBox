"""Pi's reading of the execd pipe.

The transport itself is proven in ``execd_json_lines_test.py``. What is
verified here is only what pi adds: which record answers which command, that
an unmatched response is reported rather than swallowed, and that a dead pipe
reaches the platform as an engine-seam failure rather than as a runtime type
the turn pipeline does not recognize.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.pi_pipe import PiRpcProcess
from astrabox.core.service.orchestrator.runtime.execd_json_lines import STDOUT
from astrabox.core.service.orchestrator.runtime.pty_terminal import ResolvedExecdEndpoint


class _Socket:
    """A connected pipe that records what was written to pi's stdin."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload: bytes) -> None:
        assert payload[0] == 0x00, "commands must be written as stdin frames"
        self.sent.append(json.loads(payload[1:].decode("utf-8")))

    async def close(self) -> None:
        return None


def _process(socket: Any = None) -> PiRpcProcess:
    process = PiRpcProcess(
        endpoint=ResolvedExecdEndpoint(origin="http://sandbox.test", headers={}),
        cwd="/workspace",
        command="pi --mode rpc",
        pty_session_id="pty-1",
    )
    if socket is not None:
        process._channel._ws = socket
    return process


async def _feed(process: PiRpcProcess, record: dict[str, Any]) -> None:
    await process._channel._handle_binary_frame(
        bytes([STDOUT]) + json.dumps(record).encode("utf-8") + b"\n"
    )


@pytest.mark.asyncio
async def test_a_command_carries_its_id_and_is_settled_by_the_matching_response() -> None:
    socket = _Socket()
    process = _process(socket)

    pending = asyncio.create_task(
        process.command("cmd-1", {"type": "prompt", "message": "hello"})
    )
    await asyncio.sleep(0)

    assert socket.sent == [{"type": "prompt", "message": "hello", "id": "cmd-1"}]

    await _feed(
        process,
        {"id": "cmd-1", "type": "response", "command": "prompt", "success": True},
    )

    assert await pending == {
        "id": "cmd-1",
        "type": "response",
        "command": "prompt",
        "success": True,
    }


@pytest.mark.asyncio
async def test_a_rejected_command_is_returned_rather_than_raised() -> None:
    """``success: false`` is pi's answer about the command, not a dead pipe.

    Pi rejects a prompt whose preflight fails — no credential, or streaming
    without a queueing behavior — and the caller has to see which, so the
    refusal is data. Raising here would turn a recoverable rejection into a
    transport failure and evict the runtime.
    """

    socket = _Socket()
    process = _process(socket)

    pending = asyncio.create_task(process.command("cmd-1", {"type": "prompt"}))
    await asyncio.sleep(0)
    await _feed(
        process,
        {
            "id": "cmd-1",
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "No API key found for fake-provider.",
        },
    )

    response = await pending
    assert response["success"] is False
    assert "No API key" in response["error"]
    assert process.fatal is None, "a rejected command must not kill the pipe"


@pytest.mark.asyncio
async def test_an_unmatched_response_is_reported_instead_of_dropped() -> None:
    """Pi answers every accepted command exactly once.

    So a response nobody awaits is evidence about a command this process has
    stopped tracking. Dropping it would hide that; the client decides.
    """

    process = _process()
    orphan = {"id": "gone", "type": "response", "command": "abort", "success": True}

    await _feed(process, orphan)

    assert (await process.next_record()).record == orphan


@pytest.mark.asyncio
async def test_events_and_extension_requests_reach_the_client() -> None:
    process = _process()

    await _feed(process, {"type": "message_update", "assistantMessageEvent": {"type": "start"}})
    await _feed(
        process,
        {"type": "extension_ui_request", "id": "ui-1", "method": "confirm", "title": "?"},
    )

    assert (await process.next_record()).record["type"] == "message_update"
    assert (await process.next_record()).record["type"] == "extension_ui_request"


@pytest.mark.asyncio
async def test_an_extension_ui_response_is_written_without_awaiting_a_reply() -> None:
    """It settles a request pi raised, so it carries that id and gets no response."""

    socket = _Socket()
    process = _process(socket)

    await process.send_untracked(
        {"type": "extension_ui_response", "id": "ui-1", "confirmed": True}
    )

    assert socket.sent == [
        {"type": "extension_ui_response", "id": "ui-1", "confirmed": True}
    ]


@pytest.mark.asyncio
async def test_a_dead_pipe_crosses_the_seam_as_an_engine_stream_detachment() -> None:
    """The platform channel speaks its own failure type; pi translates it.

    The turn pipeline acts on EngineStreamDetached specifically, so a runtime
    error leaking through would be handled as an unknown crash instead of a
    lost stream.
    """

    class _Broken:
        async def send(self, _payload: bytes) -> None:
            raise OSError("pipe closed")

        async def close(self) -> None:
            return None

    process = _process(_Broken())

    with pytest.raises(EngineStreamDetached, match="pi RPC transport detached"):
        await process.command("cmd-1", {"type": "prompt"})

    assert isinstance(process.fatal, EngineStreamDetached)
    with pytest.raises(EngineStreamDetached):
        await process.next_record()


@pytest.mark.asyncio
async def test_a_command_on_a_disconnected_pipe_is_refused() -> None:
    process = _process()

    with pytest.raises(EngineStreamDetached, match="pi RPC is not connected"):
        await process.command("cmd-1", {"type": "prompt"})
