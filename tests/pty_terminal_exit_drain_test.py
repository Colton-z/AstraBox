"""``PtyTerminal.run`` drains output that arrives behind the exit notice.

The shell-exit control frame and the command's stdout travel the same
websocket but are produced by different execd internals, and the exit notice
can overtake stdout still queued behind it. Stopping at the exit frame would
lose that output while still reporting the command's exit status. These tests
use a real websocket server because the producer ordering is the contract under
test; a mocked receive loop cannot exercise it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from websockets.asyncio.server import serve

from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    PtyTerminal,
    _STDOUT,
)

_PTY_ID = "pty-1"


async def test_open_session_bootstraps_only_the_supplied_process_environment() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(201, json={"session_id": _PTY_ID})

    terminal = PtyTerminal(
        "http://execd.example.test",
        transport=httpx.MockTransport(handler),
    )
    opened = await terminal.open_session(
        cwd="/workspace",
        envs={
            "PRIVATE_API_TOKEN": "ASTRABOX-VAULT-CRED::credential-1::stable",
        },
    )

    assert opened == _PTY_ID
    assert requests == [
        {
            "cwd": "/workspace",
            "command": (
                "exec env -- "
                "PRIVATE_API_TOKEN=ASTRABOX-VAULT-CRED::credential-1::stable "
                "bash --norc --noprofile"
            ),
        }
    ]


def _http_faces() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/pty/{_PTY_ID}":
            return httpx.Response(200, json={"output_offset": 0})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@asynccontextmanager
async def _fake_execd(frames_after_input: list[str | bytes]) -> AsyncIterator[str]:
    """A real WS server: waits for the command input, then plays ``frames``."""

    async def handler(connection) -> None:
        await connection.recv()  # the sentinel-wrapped command
        for frame in frames_after_input:
            await connection.send(frame)
        await connection.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"


async def _collect(origin: str) -> list[dict]:
    terminal = PtyTerminal(origin, transport=_http_faces())
    return [event async for event in terminal.run(_PTY_ID, "echo HELLO && exit 7")]


async def test_stdout_behind_the_exit_notice_is_still_delivered() -> None:
    async with _fake_execd(
        [
            json.dumps({"type": "exit", "exit_code": 7}),
            bytes([_STDOUT]) + b"HELLO_STDOUT\n",
        ]
    ) as origin:
        events = await _collect(origin)

    stdout = "".join(e["text"] for e in events if e.get("type") == "stdout")
    assert "HELLO_STDOUT" in stdout, f"stdout behind the exit notice was dropped: {events}"
    done = events[-1]
    assert done["type"] == "__done__" and done["exit_code"] == 7


async def test_exit_with_nothing_behind_it_ends_promptly() -> None:
    # The drain must end on the socket CLOSING, not sit out a timeout: an exit
    # with no output behind it is the common case for every `exit`/Ctrl-D.
    import time

    async with _fake_execd([json.dumps({"type": "exit", "exit_code": 0})]) as origin:
        started = time.monotonic()
        events = await _collect(origin)
        elapsed = time.monotonic() - started

    assert events == [{"type": "__done__", "exit_code": 0, "cwd": ""}]
    assert elapsed < 2.0, f"drain did not end on socket close ({elapsed:.1f}s)"


async def test_output_before_exit_still_flushes_as_before() -> None:
    async with _fake_execd(
        [
            bytes([_STDOUT]) + b"EARLY\n",
            json.dumps({"type": "exit", "exit_code": 3}),
        ]
    ) as origin:
        events = await _collect(origin)

    stdout = "".join(e["text"] for e in events if e.get("type") == "stdout")
    assert "EARLY" in stdout
    assert events[-1]["exit_code"] == 3
