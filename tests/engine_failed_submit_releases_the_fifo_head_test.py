"""A delivery that never reached the engine must not own the queue forever.

Both resident adapters keep the conversation's undelivered inputs in order and
refuse to submit anything but the head — that ordering is the point. The head
was only released after a successful submit, so a submit that raised left its
command in front: the next turn arrived with its own command, found someone
else at the head, and was refused. One lost sandbox therefore silenced the
conversation permanently, since every later turn hit the same head.

Consumption is proven by the engine's own boundary frame, not by this call
returning, so a raised submit consumed nothing and the durable FIFO — which is
the authority on what the conversation is still owed — redelivers it.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.base import EngineInputCommand
from astrabox.core.service.orchestrator.engine.hermes_client import HermesTuiEngineClient
from astrabox.core.service.orchestrator.engine.pi_client import PiEngineClient


class _Boom(RuntimeError):
    """What a submit against a sandbox that is gone raises."""


def _command(command_id: str, sequence: int) -> EngineInputCommand:
    return EngineInputCommand(
        command_id=command_id,
        session_id="s1",
        sequence=sequence,
        # A real input id is a UUID; the delivery path derives the response
        # message identity from it and refuses anything else.
        input_id=str(uuid.uuid5(uuid.NAMESPACE_URL, command_id)),
        content=f"hello from {command_id}",
    )


def _hermes() -> HermesTuiEngineClient:
    client = HermesTuiEngineClient.__new__(HermesTuiEngineClient)
    client._input_lock = asyncio.Lock()  # type: ignore[attr-defined]
    client._pending_command_ids = __import__("collections").deque()  # type: ignore[attr-defined]
    client._commands = {}  # type: ignore[attr-defined]
    client._consumed_command_ids = set()  # type: ignore[attr-defined]
    client._active_command_id = None  # type: ignore[attr-defined]
    client._delivery_sequence = 0  # type: ignore[attr-defined]
    client._tui_session_id = "tui-1"  # type: ignore[attr-defined]
    client._platform_session_id = "s1"  # type: ignore[attr-defined]

    async def _ensure_session() -> None:
        raise _Boom("open_sandbox connect: sandbox no longer exists")

    client._ensure_session = _ensure_session  # type: ignore[attr-defined]
    return client


def _pi() -> PiEngineClient:
    client = PiEngineClient.__new__(PiEngineClient)
    client._input_lock = asyncio.Lock()  # type: ignore[attr-defined]
    client._pending_command_ids = __import__("collections").deque()  # type: ignore[attr-defined]
    client._commands = {}  # type: ignore[attr-defined]
    client._consumed_command_ids = set()  # type: ignore[attr-defined]
    client._active_command_id = None  # type: ignore[attr-defined]
    client._delivery_sequence = 0  # type: ignore[attr-defined]
    client._streaming = False  # type: ignore[attr-defined]
    client._platform_session_id = "s1"  # type: ignore[attr-defined]

    async def _ensure_process() -> Any:
        raise _Boom("pi process is gone")

    client._ensure_process = _ensure_process  # type: ignore[attr-defined]
    return client


@pytest.mark.parametrize(
    ("make_client", "submit"),
    [
        (_hermes, lambda c, cid: c._submit_fifo_head(expected_command_id=cid)),
        (_pi, lambda c, cid: c._submit_fifo_head(expected_command_id=cid)),
    ],
    ids=["hermes", "pi"],
)
def test_a_submit_that_raises_leaves_the_queue_empty(make_client, submit) -> None:
    async def run() -> None:
        client = make_client()
        first = _command("cmd-first", 1)
        client._commands[first.command_id] = first
        client._pending_command_ids.append(first.command_id)

        with pytest.raises(_Boom):
            await submit(client, first.command_id)

        assert list(client._pending_command_ids) == [], (
            "the command that never reached the engine is still the head, so "
            "every later turn will be refused for not being it"
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("make_client", "submit"),
    [
        (_hermes, lambda c, cid: c._submit_fifo_head(expected_command_id=cid)),
        (_pi, lambda c, cid: c._submit_fifo_head(expected_command_id=cid)),
    ],
    ids=["hermes", "pi"],
)
def test_the_next_turn_is_then_the_head_it_expects(make_client, submit) -> None:
    """The scene the lane hit: a lost sandbox, then a retry on a replacement."""

    async def run() -> None:
        client = make_client()
        first = _command("cmd-first", 1)
        client._commands[first.command_id] = first
        client._pending_command_ids.append(first.command_id)
        with pytest.raises(_Boom):
            await submit(client, first.command_id)

        retry = _command("cmd-retry", 2)
        client._commands[retry.command_id] = retry
        client._pending_command_ids.append(retry.command_id)
        # The retry now IS the head. Before this it raised "input consumption
        # is not the FIFO head", naming the command of a turn that had already
        # died with its box.
        assert client._pending_command_ids[0] == retry.command_id

    asyncio.run(run())


def _hermes_begin_delivery_client() -> HermesTuiEngineClient:
    client = _hermes()
    client._closed = False  # type: ignore[attr-defined]
    client._active_receipt = None  # type: ignore[attr-defined]
    return client


def _pi_begin_delivery_client() -> PiEngineClient:
    client = _pi()
    client._closed = False  # type: ignore[attr-defined]
    client._active_receipt = None  # type: ignore[attr-defined]
    return client


@pytest.mark.parametrize(
    "make_client",
    [_hermes_begin_delivery_client, _pi_begin_delivery_client],
    ids=["hermes", "pi"],
)
def test_begin_delivery_releases_its_command_when_the_box_is_gone(make_client) -> None:
    """The window the lane actually hit.

    `begin_delivery` queues the command and then reaches for the sandbox before
    submitting anything. Against a box that is gone, that reach is what raises —
    upstream of the submit, so releasing the head inside the submit never runs,
    and the command it had just queued stayed at the front.
    """

    async def run() -> None:
        client = make_client()
        first = _command("cmd-first", 1)

        with pytest.raises(_Boom):
            await client.begin_delivery(first)

        assert list(client._pending_command_ids) == [], (
            "the command queued by begin_delivery survived the failure and is "
            "now the head every later turn will be refused for not being"
        )

        # And the retry the platform asked for is then the head.
        retry = _command("cmd-retry", 2)
        with pytest.raises(_Boom):
            await client.begin_delivery(retry)
        assert list(client._pending_command_ids) == []

    asyncio.run(run())
