"""execd's wire, as :class:`ExecdProcesses` carries it for the process face.

These run against a WebSocket peer that behaves the way execd does, because the
properties are about that wire: execd answers a Close frame but never closes
its side of the TCP connection, and it says that a replay lost output only
through the replay frame's offset.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any

import pytest

from astrabox.core.service.orchestrator.runtime.process_json_lines import (
    ProcessJsonLineChannel,
)
from astrabox.providers.execd import processes as processes_module
from astrabox.providers.execd.processes import REPLAY, STDOUT, ExecdProcesses
from astrabox.seams.sandbox_data import SandboxProcessReplayGap


class _FakeExecd:
    """An execd PTY WebSocket, closing the way execd closes.

    It answers a Close frame with its own and then leaves the TCP connection
    open, which is what the in-box execd does. ``answer_close=False`` is a peer
    that never answers at all. Output keeps flowing to whichever connection
    took the pipe over last, as ``takeover=1`` does. ``replay`` is an
    ``(offset, bytes)`` replay frame sent before the ``connected`` notice, as
    execd sends one for a reattach with ``since``.
    """

    _GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(
        self, *, answer_close: bool = True, replay: tuple[int, bytes] | None = None
    ) -> None:
        self.answer_close = answer_close
        self.replay = replay
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
        if self.replay is not None:
            offset, data = self.replay
            writer.write(self._frame(0x2, bytes([REPLAY]) + offset.to_bytes(8, "big") + data))
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


async def _connected_channel(origin: str, records: list[dict[str, Any]]) -> ProcessJsonLineChannel:
    async def record(value: dict[str, Any], _offset: int) -> None:
        records.append(value)

    async def failure(_exc: BaseException) -> None:
        return None

    channel = ProcessJsonLineChannel(
        processes=ExecdProcesses.at_endpoint(origin, {}, sandbox_id="sb-1"),
        cwd="/workspace",
        command="pi --mode rpc",
        label="test engine",
        on_record=record,
        on_failure=failure,
        process_id="pty-1",
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
    every live pi conversation's delete ten seconds. The detach is
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

    monkeypatch.setattr(processes_module, "CLOSE_TIMEOUT_SECONDS", 1.0)
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


@pytest.mark.asyncio
async def test_a_replay_that_starts_past_the_cursor_refuses_the_attachment() -> None:
    """execd starts a replay at what it still holds; a later start is lost output."""

    execd = _FakeExecd(replay=(140, json.dumps({"n": 1}).encode("utf-8") + b"\n"))
    origin = await execd.start()
    try:
        processes = ExecdProcesses.at_endpoint(origin, {}, sandbox_id="sb-1")
        with pytest.raises(SandboxProcessReplayGap, match="requested=100 available=140"):
            await processes.attach("pty-1", since=100, takeover=True)
    finally:
        await execd.stop()
