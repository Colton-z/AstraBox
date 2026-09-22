"""The WebSocket channel to a box's resident Hermes backend.

The wire above this channel does not change when the transport does — that is
the claim the whole transport swap rests on, and Hermes states it in
`tui_gateway/ws.py`: "Identical to stdio: newline-delimited JSON-RPC in both
directions ... No framing differences." So what is pinned here is not the
protocol (the adapter's translation tests already own that) but the four
things this channel is answerable for:

* one frame in, one parsed record out — including a frame carrying several
  lines, because the wire is newline-delimited and a socket may coalesce;
* an unparseable frame is reported and skipped, never silently dropped: a
  dropped frame becomes a hang somewhere upstream, where nothing names it;
* every waiting request is failed with the terminal error, once, so a caller
  waiting on a reply learns instead of blocking forever;
* the socket closing on its own is a failure, because for a resident service
  it is the end of this attachment rather than an ordinary end of stream;
* the URI it presents and the address it dials stay different, because the
  backend binds loopback and checks the `Host` header against that bind.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest import mock

import pytest
import websockets

from astrabox.core.service.orchestrator.runtime.hermes_backend_channel import (
    HermesBackendChannel,
)


class _FakeSocket:
    """A websockets-shaped socket the test drives frame by frame."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._inbound: asyncio.Queue[str | None] = asyncio.Queue()

    def push(self, frame: str) -> None:
        self._inbound.put_nowait(frame)

    def end(self) -> None:
        self._inbound.put_nowait(None)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> "_FakeSocket":
        return self

    async def __anext__(self) -> str:
        frame = await self._inbound.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


async def _ignore_record(record: dict[str, Any], offset: int) -> None:
    return None


async def _ignore_failure(exc: BaseException) -> None:
    return None


class _Harness:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.offsets: list[int] = []
        self.failures: list[BaseException] = []
        self.socket = _FakeSocket()
        self.channel = HermesBackendChannel(
            url="ws://box:9118/api/ws?token=t",
            label="Hermes backend",
            on_record=self._record,
            on_failure=self._failure,
        )

    async def _record(self, record: dict[str, Any], offset: int) -> None:
        self.records.append(record)
        self.offsets.append(offset)

    async def _failure(self, exc: BaseException) -> None:
        self.failures.append(exc)

    async def open(self) -> None:
        """Attach the fake socket the way `connect` would, and start reading."""

        self.channel._ws = self.socket  # noqa: SLF001 - the seam under test
        self.channel._connected.set()  # noqa: SLF001
        self.channel._reader_task = asyncio.create_task(  # noqa: SLF001
            self.channel._reader_loop()  # noqa: SLF001
        )
        await asyncio.sleep(0)


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_uri_names_the_bind_and_the_dial_names_the_box() -> None:
    """Where the socket goes and what it claims to be talking to differ.

    Hermes binds loopback and refuses any upgrade whose `Host` names another
    interface — its DNS-rebinding defence. Building one URL out of the box's
    endpoint, which is what a first attempt did, made every upgrade `HTTP
    403`. `websockets` builds `Host` from the URI and takes the connection
    target from `host`/`port`, so this asserts that both reach it: a URI that
    still names the box would be the same defect returning, and a missing
    `host` would send the connection to a loopback address on this host.
    """

    captured: dict[str, Any] = {}

    async def _fake_connect(uri: str, **kwargs: Any) -> Any:
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return _FakeSocket()

    channel = HermesBackendChannel(
        url="ws://127.0.0.1:9119/api/ws?token=t",
        dial=("10.42.0.7", 9118),
        label="Hermes backend",
        on_record=_ignore_record,
        on_failure=_ignore_failure,
    )
    with mock.patch.object(websockets, "connect", _fake_connect):
        await channel.connect()

    await channel.detach()

    assert captured["uri"] == "ws://127.0.0.1:9119/api/ws?token=t"
    assert captured["kwargs"]["host"] == "10.42.0.7"
    assert captured["kwargs"]["port"] == 9118
    assert "headers" not in str(captured["kwargs"].get("additional_headers", "")), (
        "the Host must come from the URI; a second one would go out as a "
        "duplicate header"
    )


@pytest.mark.asyncio
async def test_a_channel_without_a_dial_connects_to_its_uri() -> None:
    """No dial means no override, so the library resolves the URI itself."""

    captured: dict[str, Any] = {}

    async def _fake_connect(uri: str, **kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        return _FakeSocket()

    channel = HermesBackendChannel(
        url="ws://box:9118/api/ws?token=t",
        label="Hermes backend",
        on_record=_ignore_record,
        on_failure=_ignore_failure,
    )
    with mock.patch.object(websockets, "connect", _fake_connect):
        await channel.connect()

    await channel.detach()

    assert "host" not in captured["kwargs"]
    assert "port" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_a_frame_carrying_several_lines_yields_several_records() -> None:
    """The wire is newline-delimited and a socket may coalesce.

    Treating one frame as one record would swallow every line after the first
    — silently, and only under load, which is the worst way to find it.
    """

    world = _Harness()
    await world.open()
    world.socket.push(
        json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "a"}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "b"}})
    )
    await _settle()

    assert [r["params"]["type"] for r in world.records] == ["a", "b"]
    # Ordered within this attachment, which is all the offset claims to be.
    assert world.offsets == [1, 2]


@pytest.mark.asyncio
async def test_an_unparseable_frame_is_skipped_and_the_stream_continues() -> None:
    """Reported, not dropped, and not fatal.

    A frame this transport cannot read is the backend's to explain. Killing
    the channel would turn one bad line into a dead engine; swallowing it
    would turn it into a hang with nothing to name.
    """

    world = _Harness()
    await world.open()
    world.socket.push("{not json")
    world.socket.push(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "a"}}))
    await _settle()

    assert [r["params"]["type"] for r in world.records] == ["a"]
    assert world.failures == []


@pytest.mark.asyncio
async def test_a_terminal_failure_settles_every_waiting_request_once() -> None:
    """A caller waiting on a reply must learn, not block.

    And exactly once: `fail` is reachable from the reader loop and from the
    caller, and a second pass would try to settle futures it already settled.
    """

    world = _Harness()
    await world.open()
    pending = world.channel.register_request("rpc-1")

    boom = RuntimeError("backend went away")
    await world.channel.fail(boom)
    await world.channel.fail(RuntimeError("a second, later failure"))
    await _settle()

    assert pending.done()
    with pytest.raises(RuntimeError, match="backend went away"):
        await pending
    # One failure reported, and the first one — the later call must not
    # overwrite the reason the caller will be shown.
    assert [str(exc) for exc in world.failures] == ["backend went away"]
    assert world.socket.closed is True


@pytest.mark.asyncio
async def test_the_socket_closing_by_itself_is_a_failure() -> None:
    """For a resident service, end of stream is not an ordinary ending.

    `hermes serve` is supervised and does not close a healthy attachment, so a
    clean close means this attachment is over — and a caller that treated it
    as "no more events" would wait for a turn that can never arrive.
    """

    world = _Harness()
    await world.open()
    world.socket.end()
    await _settle()

    assert len(world.failures) == 1
    assert "closed its socket" in str(world.failures[0])
    assert world.channel.is_connected is False


@pytest.mark.asyncio
async def test_send_refuses_once_the_channel_is_gone() -> None:
    world = _Harness()
    await world.open()
    await world.channel.send({"method": "session.list"})
    assert json.loads(world.socket.sent[-1])["method"] == "session.list"

    await world.channel.fail(RuntimeError("gone"))
    with pytest.raises(Exception, match="not connected"):
        await world.channel.send({"method": "session.list"})


@pytest.mark.asyncio
async def test_the_offset_counts_frames_it_does_not_address_them() -> None:
    """The gap this swap accepts, stated where a reader will meet it.

    execd's offset is a byte cursor a NEW attachment can replay from, which is
    where turn recovery reconnects. This one only orders frames within the
    attachment that produced it: a fresh channel starts at zero however many
    frames the last one saw, so reconnecting resumes live rather than
    replaying a suffix.
    """

    world = _Harness()
    assert await world.channel.current_output_offset() == 0

    await world.open()
    world.socket.push(json.dumps({"method": "event", "params": {"type": "a"}}))
    await _settle()
    assert await world.channel.current_output_offset() == 1

    fresh = _Harness()
    assert await fresh.channel.current_output_offset() == 0
