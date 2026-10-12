"""Conformance for the platform's JSON-lines channel to a box process.

Every engine that embeds through a stdin/stdout pipe depends on these
properties, and each one here is a defect this transport has to make
impossible rather than a description of how it currently behaves: a lost
replay window must be loud, a record split across frames must arrive whole
and once, a Unicode separator inside a JSON string must not end a record, and
a dead pipe must fail every request waiting on it exactly once.

The channel reads no vendor vocabulary, and one test states that as a
property: a record carrying an ``id`` still reaches the adapter, because
"this record is a response" is the engine's meaning, not the transport's.

The frames are the process face's own (:mod:`astrabox.seams.sandbox_data`);
how execd puts them on the wire is proven in ``execd_processes_test.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from astrabox.core.service.orchestrator.runtime.process_json_lines import (
    ProcessJsonLineChannel,
)
from astrabox.seams.sandbox_data import (
    ProcessExit,
    ProcessOutput,
    ProcessReplay,
    SandboxProcessDetached,
    SandboxProcessReplayGap,
)


def _stdout(data: bytes) -> ProcessOutput:
    return ProcessOutput(stream="stdout", data=data)


def _stderr(data: bytes) -> ProcessOutput:
    return ProcessOutput(stream="stderr", data=data)


class _Attachment:
    """An attachment whose frames arrive when the test puts them."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.detached = False
        self._frames: asyncio.Queue[Any] = asyncio.Queue()

    async def frames(self) -> Any:
        while True:
            frame = await self._frames.get()
            if frame is None:
                return
            yield frame

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def detach(self) -> None:
        self.detached = True
        self._frames.put_nowait(None)


class _Processes:
    """A box's processes, recording what the channel asked of them."""

    def __init__(self, *, attach_error: BaseException | None = None) -> None:
        self.attachment = _Attachment()
        self.attach_error = attach_error
        self.closed: list[str] = []

    async def start(self, command: str, *, cwd: str | None) -> str:
        return "pty-1"

    async def exists(self, process_id: str) -> bool:
        return True

    async def output_offset(self, process_id: str) -> int:
        return 0

    async def attach(self, process_id: str, *, since: int, takeover: bool) -> _Attachment:
        if self.attach_error is not None:
            raise self.attach_error
        return self.attachment

    async def signal(self, process_id: str, name: str) -> None:
        return None

    async def close(self, process_id: str) -> None:
        self.closed.append(process_id)


class _Harness:
    """A channel plus the records and failure its adapter would have seen."""

    def __init__(self, processes: _Processes | None = None) -> None:
        self.records: list[tuple[dict[str, Any], int]] = []
        self.failures: list[BaseException] = []
        self.processes = processes or _Processes()
        self.channel = ProcessJsonLineChannel(
            processes=self.processes,
            cwd="/workspace",
            command="pi --mode rpc",
            label="test engine",
            on_record=self._record,
            on_failure=self._failure,
            process_id="pty-1",
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

    await harness.channel._handle_frame(_stdout(encoded + b"\n"))

    assert [record for record, _ in harness.records] == [payload]


@pytest.mark.asyncio
async def test_a_record_split_across_frames_arrives_once_and_whole() -> None:
    """Frames are byte chunks, not records; the tail waits for its LF."""

    harness = _Harness()
    encoded = json.dumps({"type": "message_update", "delta": "hello"}).encode("utf-8")
    head, tail = encoded[:9], encoded[9:]

    await harness.channel._handle_frame(_stdout(head))
    assert harness.records == []

    await harness.channel._handle_frame(_stdout(tail + b"\n"))

    assert [record for record, _ in harness.records] == [
        {"type": "message_update", "delta": "hello"}
    ]


@pytest.mark.asyncio
async def test_each_record_offset_points_past_its_own_terminator() -> None:
    """The offset is the resume cursor, so it must exclude nothing delivered."""

    harness = _Harness()
    first = json.dumps({"n": 1}).encode("utf-8")
    second = json.dumps({"n": 2}).encode("utf-8")

    await harness.channel._handle_frame(_stdout(first + b"\n" + second + b"\n"))

    assert [offset for _, offset in harness.records] == [
        len(first) + 1,
        len(first) + 1 + len(second) + 1,
    ]


@pytest.mark.asyncio
async def test_a_replay_that_lost_bytes_fails_instead_of_resuming() -> None:
    """Resuming past a gap silently drops records; the gap has to be loud."""

    gap = SandboxProcessReplayGap("pty-1", requested=100, available=140)
    harness = _Harness(_Processes(attach_error=gap))

    with pytest.raises(SandboxProcessReplayGap, match="requested=100 available=140"):
        await harness.channel.connect(since=100)

    assert harness.channel.fatal is gap
    assert harness.failures == [gap]


@pytest.mark.asyncio
async def test_a_replay_inside_the_window_rebases_the_cursor() -> None:
    harness = _Harness()

    await harness.channel._handle_frame(
        ProcessReplay(offset=80, data=json.dumps({"n": 1}).encode("utf-8") + b"\n")
    )

    assert [record for record, _ in harness.records] == [{"n": 1}]
    assert harness.records[0][1] == 80 + len(json.dumps({"n": 1})) + 1


@pytest.mark.asyncio
async def test_a_json_value_that_is_not_an_object_fails() -> None:
    harness = _Harness()

    with pytest.raises(RuntimeError, match="JSON record is not an object"):
        await harness.channel._handle_frame(_stdout(b"[1, 2, 3]\n"))


@pytest.mark.asyncio
async def test_non_json_stdout_fails_and_names_its_offset() -> None:
    harness = _Harness()

    with pytest.raises(RuntimeError, match="non-JSON stdout at offset 6"):
        await harness.channel._handle_frame(_stdout(b"oops!\n"))


@pytest.mark.asyncio
async def test_blank_lines_are_not_records() -> None:
    harness = _Harness()

    await harness.channel._handle_frame(_stdout(b"\n   \n" + b'{"n": 1}\n'))

    assert [record for record, _ in harness.records] == [{"n": 1}]


@pytest.mark.asyncio
async def test_stderr_is_kept_for_the_exit_that_will_need_it() -> None:
    """An exit notice carries no reason; the stderr tail is the only account."""

    harness = _Harness()
    await harness.channel._handle_frame(_stderr(b"ENOENT: pi not installed\n"))

    assert harness.channel.stderr_tail == "ENOENT: pi not installed\n"
    assert harness.records == []

    with pytest.raises(RuntimeError, match="exited with code 127.*pi not installed"):
        await harness.channel._handle_frame(ProcessExit(exit_code=127))


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

    assert isinstance(harness.channel.fatal, SandboxProcessDetached)


@pytest.mark.asyncio
async def test_sending_on_a_dead_channel_is_refused() -> None:
    harness = _Harness()

    with pytest.raises(SandboxProcessDetached, match="not connected"):
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

    await harness.channel._handle_frame(
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
async def test_detach_cancels_the_reader_and_closes_the_attachment() -> None:
    harness = _Harness()
    await harness.channel.connect(since=0)
    await asyncio.sleep(0)

    await harness.channel.detach()

    assert harness.processes.attachment.detached is True
    assert harness.channel._reader_task is None
    assert harness.channel.is_connected is False


@pytest.mark.asyncio
async def test_delete_stops_the_process() -> None:
    harness = _Harness()

    assert await harness.channel.delete() is True

    assert harness.processes.closed == ["pty-1"]
    assert harness.channel.process_id is None
    assert await harness.channel.delete() is False
