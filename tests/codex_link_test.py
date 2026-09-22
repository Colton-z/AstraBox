"""The link's half of the app-server wire contract.

Run against a real WebSocket server rather than a stub, because the two things
that matter here are properties of a socket: that the four JSON-RPC message
kinds are told apart on one connection, and that the socket ending is what
ends the turn stream. A stub that returned instead of closing would exercise
neither.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import websockets

from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.codex_link import (
    CodexAppServerLink,
    CodexRpcError,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    ResolvedExecdEndpoint,
)


class FakeAppServer:
    """Answers `initialize`, then whatever the test scripted."""

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self.script = script or []
        self.received: list[dict[str, Any]] = []
        self.server: Any = None

    async def start(self) -> int:
        self.server = await websockets.serve(self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, websocket: Any) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            self.received.append(message)
            if message.get("method") == "initialize":
                await websocket.send(
                    json.dumps(
                        {
                            "id": message["id"],
                            "result": {
                                "userAgent": "astrabox/0.147.0",
                                "codexHome": "/home/gem/.codex",
                                "platformOs": "linux",
                            },
                        }
                    )
                )
                for frame in self.script:
                    await websocket.send(json.dumps(frame))
                continue
            if message.get("method") == "boom":
                await websocket.send(
                    json.dumps(
                        {
                            "id": message["id"],
                            "error": {"code": -32600, "message": "no such thread"},
                        }
                    )
                )
                continue
            if "id" in message and "method" in message:
                await websocket.send(json.dumps({"id": message["id"], "result": {}}))


async def _link(port: int) -> CodexAppServerLink:
    link = CodexAppServerLink(
        endpoint=ResolvedExecdEndpoint(origin=f"http://127.0.0.1:{port}", headers={})
    )
    await link._attach()
    return link


@pytest.mark.asyncio
async def test_the_handshake_is_first_and_acknowledged() -> None:
    """`initialize` then `initialized`, or every later call is refused."""

    server = FakeAppServer()
    port = await server.start()
    try:
        link = await _link(port)
        await asyncio.sleep(0.05)
        await link.close()
    finally:
        await server.stop()

    assert [m.get("method") for m in server.received[:2]] == ["initialize", "initialized"]
    assert server.received[0]["params"]["clientInfo"]["name"] == "astrabox"
    assert server.received[0]["params"]["capabilities"] == {
        "experimentalApi": True,
        "requestAttestation": False,
    }
    # The acknowledgement is a notification: an id on it would make the server
    # answer something nobody is waiting for.
    assert "id" not in server.received[1]


@pytest.mark.asyncio
async def test_what_initialize_answered_is_kept_for_the_manifest() -> None:
    server = FakeAppServer()
    port = await server.start()
    try:
        link = await _link(port)
        info = link.server_info
        await link.close()
    finally:
        await server.stop()

    assert info is not None
    assert info["userAgent"] == "astrabox/0.147.0"


@pytest.mark.asyncio
async def test_an_error_object_raises_rather_than_returning_none() -> None:
    """A JSON-RPC error is a result-shaped failure; treating it as a value
    would carry "no such thread" onward as an empty answer."""

    server = FakeAppServer()
    port = await server.start()
    try:
        link = await _link(port)
        with pytest.raises(CodexRpcError) as excinfo:
            await link.call("boom", {})
        await link.close()
    finally:
        await server.stop()

    assert excinfo.value.rpc_code == -32600
    assert "no such thread" in excinfo.value.rpc_message


@pytest.mark.asyncio
async def test_notifications_and_server_requests_arrive_and_responses_do_not() -> None:
    """One socket carries four kinds; only two of them are the turn's."""

    server = FakeAppServer(
        script=[
            {"method": "turn/started", "params": {"turnId": "t1"}},
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"itemId": "c1"},
            },
        ]
    )
    port = await server.start()
    try:
        link = await _link(port)
        inbound = link.iter_inbound()
        first = await asyncio.wait_for(anext(inbound), timeout=5)
        second = await asyncio.wait_for(anext(inbound), timeout=5)
        # A call's own response is routed to its waiter, never onto this
        # stream: a turn that saw its own answers would translate them.
        assert await link.call("thread/read", {}) == {}
        await link.close()
    finally:
        await server.stop()

    assert first["method"] == "turn/started"
    assert second["method"] == "item/commandExecution/requestApproval"
    assert second["id"] == 7


@pytest.mark.asyncio
async def test_the_socket_ending_is_what_ends_the_turn_stream() -> None:
    server = FakeAppServer()
    port = await server.start()
    link = await _link(port)
    inbound = link.iter_inbound()
    await server.stop()

    with pytest.raises(EngineStreamDetached):
        await asyncio.wait_for(anext(inbound), timeout=5)

    assert link.is_live is False
    await link.close()
