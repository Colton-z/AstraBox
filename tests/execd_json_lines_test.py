"""Conformance for the platform's execd JSON-lines channel.

Every engine that embeds through a stdin/stdout pipe depends on these
properties, and each one here is a defect this transport has to make
impossible rather than a description of how it currently behaves: a lost
replay window must be loud, a record split across frames must arrive whole
and once, a Unicode separator inside a JSON string must not end a record, and
a dead pipe must fail every request waiting on it exactly once.

The channel reads no vendor vocabulary, and the last test states that as a
property: a record carrying an ``id`` still reaches the adapter, because
"this record is a response" is the engine's meaning, not the transport's.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from astrabox.core.service.orchestrator.runtime.execd_json_lines import (
    REPLAY,
    STDERR,
    STDOUT,
    ExecdChannelDetached,
    ExecdChannelReplayGap,
    ExecdJsonLineChannel,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import ResolvedExecdEndpoint


def _stdout(data: bytes) -> bytes:
    return bytes([STDOUT]) + data


def _stderr(data: bytes) -> bytes:
    return bytes([STDERR]) + data


def _replay(offset: int, data: bytes) -> bytes:
    return bytes([REPLAY]) + offset.to_bytes(8, "big") + data


class _Harness:
    """A channel plus the records and failure its adapter would have seen."""

    def __init__(self, **kwargs: Any) -> None:
        self.records: list[tuple[dict[str, Any], int]] = []
        self.failures: list[BaseException] = []
        self.channel = ExecdJsonLineChannel(
            endpoint=ResolvedExecdEndpoint(origin="http://sandbox.test", headers={}),
            cwd="/workspace",
            command="pi --mode rpc",
            label="test engine",
            on_record=self._record,
            on_failure=self._failure,
            pty_session_id="pty-1",
            **kwargs,
        )

    async def _record(self, record: dict[str, Any], offset: int) -> None:
        self.records.append((record, offset))

    async def _failure(self, exc: BaseException) -> None:
        self.failures.append(exc)


@pytest.mark.asyncio
async def test_records_split_on_lf_only_so_a_separator_inside_one_stays_inside() -> None:
    """U+2028 / U+2029 are legal in a JSON string and must not end a record.

    ``JSON.stringify`` emits them raw, so they arrive as the bytes
    ``\xe2\x80\xa8`` / ``\xe2\x80\xa9`` rather than as escapes — which is
    why the split has to happen on the LF byte. A text-mode or Unicode-aware
    reader treats both as terminators and would cut this one record into
    three unparseable fragments.
    """

    harness = _Harness()
    payload = {"type": "prompt", "message": "line still one record"}
    # ensure_ascii would escape the separators away and leave this test
    # proving nothing but that ordinary JSON parses.
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert b"\xe2\x80\xa8" in encoded, "fixture lost its raw U+2028"
    assert b"\xe2\x80\xa9" in encoded, "fixture lost its raw U+2029"

    await harness.channel._handle_binary_frame(_stdout(encoded + b"\n"))

    assert [record for record, _ in harness.records] == [payload]


@pytest.mark.asyncio
async def test_a_record_split_across_frames_arrives_once_and_whole() -> None:
    """execd frames are byte chunks, not records; the tail waits for its LF."""

    harness = _Harness()
    encoded = json.dumps({"type": "message_update", "delta": "hello"}).encode("utf-8")
    head, tail = encoded[:9], encoded[9:]

    await harness.channel._handle_binary_frame(_stdout(head))
    assert harness.records == []

    await harness.channel._handle_binary_frame(_stdout(tail + b"\n"))

    assert [record for record, _ in harness.records] == [
        {"type": "message_update", "delta": "hello"}
    ]


@pytest.mark.asyncio
async def test_each_record_offset_points_past_its_own_terminator() -> None:
    """The offset is the resume cursor, so it must exclude nothing delivered."""

    harness = _Harness()
    first = json.dumps({"n": 1}).encode("utf-8")
    second = json.dumps({"n": 2}).encode("utf-8")

    await harness.channel._handle_binary_frame(_stdout(first + b"\n" + second + b"\n"))

    assert [offset for _, offset in harness.records] == [
        len(first) + 1,
        len(first) + 1 + len(second) + 1,
    ]


@pytest.mark.asyncio
async def test_a_replay_that_lost_bytes_fails_instead_of_resuming() -> None:
    """Resuming past a gap silently drops records; the gap has to be loud."""

    harness = _Harness()
    harness.channel._requested_since = 100

    with pytest.raises(ExecdChannelReplayGap, match="requested=100 available=140"):
        await harness.channel._handle_binary_frame(
            _replay(140, json.dumps({"n": 1}).encode("utf-8") + b"\n")
        )


@pytest.mark.asyncio
async def test_a_replay_inside_the_window_rebases_the_cursor() -> None:
    harness = _Harness()
    harness.channel._requested_since = 100

    await harness.channel._handle_binary_frame(
        _replay(80, json.dumps({"n": 1}).encode("utf-8") + b"\n")
    )

    assert [record for record, _ in harness.records] == [{"n": 1}]
    assert harness.records[0][1] == 80 + len(json.dumps({"n": 1})) + 1


@pytest.mark.asyncio
async def test_a_json_value_that_is_not_an_object_fails() -> None:
    harness = _Harness()

    with pytest.raises(RuntimeError, match="JSON record is not an object"):
        await harness.channel._handle_binary_frame(_stdout(b"[1, 2, 3]\n"))


@pytest.mark.asyncio
async def test_non_json_stdout_fails_and_names_its_offset() -> None:
    harness = _Harness()

    with pytest.raises(RuntimeError, match="non-JSON stdout at offset 6"):
        await harness.channel._handle_binary_frame(_stdout(b"oops!\n"))


@pytest.mark.asyncio
async def test_blank_lines_are_not_records() -> None:
    harness = _Harness()

    await harness.channel._handle_binary_frame(_stdout(b"\n   \n" + b'{"n": 1}\n'))

    assert [record for record, _ in harness.records] == [{"n": 1}]


@pytest.mark.asyncio
async def test_stderr_is_kept_for_the_exit_that_will_need_it() -> None:
    """An exit notice carries no reason; the stderr tail is the only account."""

    harness = _Harness()
    await harness.channel._handle_binary_frame(_stderr(b"ENOENT: pi not installed\n"))

    assert harness.channel.stderr_tail == "ENOENT: pi not installed\n"
    assert harness.records == []

    with pytest.raises(RuntimeError, match="exited with code 127.*pi not installed"):
        await harness.channel._handle_control_frame(
            json.dumps({"type": "exit", "exit_code": 127})
        )


@pytest.mark.asyncio
async def test_failure_fails_every_waiting_request_and_reports_once() -> None:
    harness = _Harness()
    first = harness.channel.register_request("a")
    second = harness.channel.register_request("b")

    await harness.channel.fail(RuntimeError("pipe died"))
    await harness.channel.fail(RuntimeError("and again"))

    assert len(harness.failures) == 1, "a consequence must not overwrite the cause"
    assert str(harness.failures[0]) == "pipe died"
    for future in (first, second):
        with pytest.raises(RuntimeError, match="pipe died"):
            await future
    assert harness.channel.fatal is not None


@pytest.mark.asyncio
async def test_an_unexpected_failure_becomes_a_detached_pipe() -> None:
    harness = _Harness()

    await harness.channel.fail(ValueError("something odd"))

    assert isinstance(harness.channel.fatal, ExecdChannelDetached)


@pytest.mark.asyncio
async def test_sending_on_a_dead_channel_is_refused() -> None:
    harness = _Harness()

    with pytest.raises(ExecdChannelDetached, match="not connected"):
        await harness.channel.send({"type": "prompt"})


@pytest.mark.asyncio
async def test_the_channel_never_settles_a_request_by_itself() -> None:
    """"This record answers that request" is the engine's meaning, not the transport's.

    Hermes drops an unmatched reply and pi reports it, so a transport that
    matched on ``id`` would be imposing one engine's protocol on the other.
    Every record reaches the adapter; only the adapter settles.
    """

    harness = _Harness()
    waiting = harness.channel.register_request("req-1")

    await harness.channel._handle_binary_frame(
        _stdout(json.dumps({"id": "req-1", "type": "response", "ok": True}).encode() + b"\n")
    )

    assert not waiting.done(), "the channel decided a record was a response"
    assert [record for record, _ in harness.records] == [
        {"id": "req-1", "type": "response", "ok": True}
    ]

    assert harness.channel.complete_request("req-1", {"id": "req-1", "ok": True}) is True
    assert await waiting == {"id": "req-1", "ok": True}


@pytest.mark.asyncio
async def test_completing_an_unregistered_request_reports_no_match() -> None:
    """The adapter decides what an unmatched reply means; it needs the fact."""

    harness = _Harness()

    assert harness.channel.complete_request("nobody", {"ok": True}) is False


@pytest.mark.asyncio
async def test_a_duplicate_request_id_is_refused() -> None:
    harness = _Harness()
    harness.channel.register_request("req-1")

    with pytest.raises(ValueError, match="already has a request registered"):
        harness.channel.register_request("req-1")


@pytest.mark.asyncio
async def test_detach_cancels_the_reader_and_closes_the_socket() -> None:
    class _Socket:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> "_Socket":
            return self

        async def __anext__(self) -> bytes:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        async def close(self) -> None:
            self.closed = True

    harness = _Harness()
    socket = _Socket()
    harness.channel._ws = socket
    harness.channel._reader_task = asyncio.create_task(harness.channel._reader_loop())
    await asyncio.sleep(0)

    await harness.channel.detach()

    assert socket.closed is True
    assert harness.channel._reader_task is None
    assert harness.channel.is_connected is False


@pytest.mark.asyncio
async def test_delete_removes_the_pty_resource() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    harness = _Harness(http_transport=httpx.MockTransport(handler))

    assert await harness.channel.delete() is True

    assert [(r.method, r.url.path) for r in requests] == [("DELETE", "/pty/pty-1")]
    assert harness.channel.pty_session_id is None
    assert await harness.channel.delete() is False


# ── closing the socket ───────────────────────────────────────────────────
class _FakeExecd:
    """An execd PTY WebSocket, closing the way execd closes.

    It answers a Close frame with its own and then leaves the TCP connection
    open, which is what the in-box execd does. ``answer_close=False`` is a peer
    that never answers at all. Output keeps flowing to whichever connection
    took the pipe over last, as ``takeover=1`` does.
    """

    _GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, *, answer_close: bool = True) -> None:
        self.answer_close = answer_close
        self.closes_received = 0
        self.eofs: list[asyncio.Event] = []
        self._active: asyncio.StreamWriter | None = None
        self._counter = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self._tasks.add(asyncio.create_task(self._produce()))
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        assert self._server is not None
        self._server.close()

    @staticmethod
    def _frame(opcode: int, payload: bytes) -> bytes:
        head = bytes([0x80 | opcode])
        if len(payload) < 126:
            return head + bytes([len(payload)]) + payload
        return head + bytes([126]) + len(payload).to_bytes(2, "big") + payload

    async def _produce(self) -> None:
        while True:
            await asyncio.sleep(0.02)
            writer = self._active
            if writer is None or writer.is_closing():
                continue
            self._counter += 1
            line = json.dumps({"n": self._counter}).encode("utf-8") + b"\n"
            writer.write(self._frame(0x2, bytes([STDOUT]) + line))

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        import base64
        import hashlib

        request = await reader.readuntil(b"\r\n\r\n")
        key = next(
            line.split(b":", 1)[1].strip()
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key:")
        )
        accept = base64.b64encode(hashlib.sha1(key + self._GUID).digest())
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        writer.write(self._frame(0x1, json.dumps({"type": "connected"}).encode()))
        self._active = writer
        eof = asyncio.Event()
        self.eofs.append(eof)
        try:
            while True:
                head = await reader.readexactly(2)
                opcode, length = head[0] & 0x0F, head[1] & 0x7F
                if length == 126:
                    length = int.from_bytes(await reader.readexactly(2), "big")
                elif length == 127:
                    length = int.from_bytes(await reader.readexactly(8), "big")
                mask = await reader.readexactly(4)
                payload = bytes(
                    b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(length))
                )
                if opcode == 0x8:
                    self.closes_received += 1
                    if self._active is writer:
                        self._active = None
                    if self.answer_close:
                        writer.write(self._frame(0x8, payload))
                    # Like execd: the TCP connection is left open.
        except (asyncio.IncompleteReadError, ConnectionError):
            eof.set()
            writer.close()


async def _connected_channel(origin: str, records: list[dict[str, Any]]) -> ExecdJsonLineChannel:
    async def record(value: dict[str, Any], _offset: int) -> None:
        records.append(value)

    async def failure(_exc: BaseException) -> None:
        return None

    channel = ExecdJsonLineChannel(
        endpoint=ResolvedExecdEndpoint(origin=origin, headers={}),
        cwd="/workspace",
        command="pi --mode rpc",
        label="test engine",
        on_record=record,
        on_failure=failure,
        pty_session_id="pty-1",
    )
    await channel.connect(since=0)
    return channel


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_detach_returns_once_execd_answers_close_and_a_reattach_keeps_reading() -> None:
    """A detach completes the closing handshake and does not wait on execd's FIN.

    execd never closes its side of the TCP connection, so waiting for it cost
    every live pi or Hermes conversation's delete ten seconds. The detach is
    reconnect-safe: the process keeps running, and a reattach that takes the
    pipe over reads its output as before.
    """

    execd = _FakeExecd()
    origin = await execd.start()
    try:
        first_records: list[dict[str, Any]] = []
        first = await _connected_channel(origin, first_records)
        await _until(lambda: len(first_records) >= 2)

        started = asyncio.get_running_loop().time()
        await first.detach()
        elapsed = asyncio.get_running_loop().time() - started

        assert execd.closes_received == 1, "the closing handshake was skipped"
        assert elapsed < 2.0, f"detach waited {elapsed:.1f}s for a TCP close execd never sends"
        await asyncio.wait_for(execd.eofs[0].wait(), timeout=2.0)

        second_records: list[dict[str, Any]] = []
        second = await _connected_channel(origin, second_records)
        await _until(lambda: len(second_records) >= 2)
        assert second_records[0]["n"] > first_records[-1]["n"]
        await second.detach()
    finally:
        await execd.stop()


@pytest.mark.asyncio
async def test_an_unanswered_close_keeps_its_bounded_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The socket is not dropped before the handshake: a silent peer is waited for."""

    from astrabox.core.service.orchestrator.runtime import execd_json_lines

    monkeypatch.setattr(execd_json_lines, "CLOSE_TIMEOUT_SECONDS", 1.0)
    execd = _FakeExecd(answer_close=False)
    origin = await execd.start()
    try:
        records: list[dict[str, Any]] = []
        channel = await _connected_channel(origin, records)
        await _until(lambda: len(records) >= 1)

        detaching = asyncio.create_task(channel.detach())
        await asyncio.sleep(0.5)
        assert not detaching.done(), "the socket was dropped before the peer answered Close"
        assert not execd.eofs[0].is_set()

        await asyncio.wait_for(detaching, timeout=3.0)
        assert execd.closes_received == 1
    finally:
        await execd.stop()
