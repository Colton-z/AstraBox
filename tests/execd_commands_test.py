"""The commands face over execd reads execd's event stream the way execd writes it.

Each case drives :class:`astrabox.providers.execd.ExecdCommands` through the real
OpenSandbox SDK client against a fake execd answering ``/command`` with the
server-sent events execd sends, so the SDK's parsing and dispatch are the ones
in use. What is pinned is execd's wire rules and their translation: output
lines reassembled per :data:`EXECD_LINE_EVENT_MODEL` (measured, not published,
so a change upstream turns these red), the exit status read from the first
numeric error value and never assumed, the box's refusal and an unreachable
box told apart, and a deadline the caller's side keeps.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint

from astrabox.providers.execd import ExecdCommands
from astrabox.providers.execd.commands import EXECD_LINE_EVENT_MODEL
from astrabox.seams.sandbox import SandboxUnreachable
from astrabox.seams.sandbox_data import (
    SandboxCommandOutcomeUnknown,
    SandboxCommandRefused,
)

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _commands(handler: Handler) -> ExecdCommands:
    config = ConnectionConfig(transport=httpx.MockTransport(handler))
    adapter = AdapterFactory(config).create_command_service(
        SandboxEndpoint(endpoint="execd.test:44772", headers={})
    )
    return ExecdCommands.over(adapter, sandbox_id="sb-1")


def _answer(*events: dict[str, Any], requests: list[dict[str, Any]] | None = None) -> Handler:
    async def handle(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(json.loads(request.content))
        body = "".join(f"data: {json.dumps({'timestamp': 0, **event})}\n\n" for event in events)
        return httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "text/event-stream"}
        )

    return handle


def _lines(stream: str, texts: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{"type": stream, "text": text} for text in texts]


_COMPLETE = {"type": "execution_complete", "execution_time": 1}


def _exit(code: int) -> dict[str, Any]:
    return {"type": "error", "error": {"ename": "CommandExecError", "evalue": str(code)}}


@pytest.mark.parametrize(
    ("command", "events", "expected"),
    EXECD_LINE_EVENT_MODEL,
    ids=[row[0] for row in EXECD_LINE_EVENT_MODEL],
)
async def test_output_is_reassembled_from_every_measured_execd_shape(
    command: str, events: tuple[str, ...], expected: str
) -> None:
    """Each measured shape, on both streams.

    Three rows are faithful. Two assert a loss on purpose: execd sends no event
    for a trailing newline, so ``printf 'only\\n'`` and ``printf 'no-newline'``
    reassemble alike, and a carriage return before a newline is consumed with
    it. A caller that needs exact bytes reads a file instead.
    """
    _ = command
    commands = _commands(
        _answer(*_lines("stdout", events), *_lines("stderr", events), _COMPLETE)
    )

    result = await commands.run("x")

    assert (result.exit_code, result.stdout, result.stderr) == (0, expected, expected)


async def test_the_command_env_and_the_box_deadline_reach_execd() -> None:
    requests: list[dict[str, Any]] = []
    commands = _commands(_answer(_COMPLETE, requests=requests))

    await commands.run("printf %s \"$VALUE\"", env={"VALUE": "a b$c"}, timeout=10.0)

    (body,) = requests
    assert body["command"] == "printf %s \"$VALUE\""
    assert body["envs"] == {"VALUE": "a b$c"}
    # The box stops the command a little after the caller stops waiting.
    assert body["timeout"] == 15_000


async def test_the_exit_status_is_the_first_number_execd_names() -> None:
    """A drain error after the status describes the pipe, not the process."""
    commands = _commands(
        _answer(
            {"type": "stdout", "text": "partial"},
            _exit(7),
            {"type": "error", "error": {"ename": "RuntimeError", "evalue": "read stdout: file already closed"}},
        )
    )

    result = await commands.run("x")

    assert (result.exit_code, result.stdout) == (7, "partial")


@pytest.mark.parametrize(
    "events",
    [
        (),
        ({"type": "error", "error": {"ename": "RuntimeError", "evalue": "session vanished"}},),
    ],
    ids=["no-events", "no-number"],
)
async def test_no_exit_status_is_never_read_as_success(events: tuple[dict[str, Any], ...]) -> None:
    commands = _commands(_answer(*events))

    with pytest.raises(SandboxCommandOutcomeUnknown):
        await commands.run("x")


async def test_a_refusal_and_an_unreachable_box_are_told_apart() -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "token rejected"})

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(SandboxCommandRefused, match="token rejected"):
        await _commands(refuse).run("x")
    with pytest.raises(SandboxUnreachable):
        await _commands(unreachable).run("x")


async def test_a_command_past_its_deadline_raises_timeout() -> None:
    """execd's stream has no read timeout of its own, so the caller's side keeps one."""

    async def never_ends(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    with pytest.raises(TimeoutError, match="did not complete within"):
        await _commands(never_ends).run("sleep 3600", timeout=0.05)
