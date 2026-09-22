"""One reader per engine process; a turn is attribution, not read scope.

The relay is driven here with a wire the tests write themselves, an engine
seam that speaks a three-word vocabulary (`start`, `text`, `settled`), and a
sink that records every call. What the cases pin down is the routing: which
records a platform turn receives, which open an engine-owned response, and
which are persisted for the child-run view because no run was open.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.base import (
    ResidentOutputCheckpoint,
    ResidentResponseHandle,
)
from astrabox.core.service.orchestrator.engine.emissions import TurnTerminal
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.engine.resident_relay import ResidentRelay


@dataclass(frozen=True)
class _Wire:
    seq: int
    record: dict[str, Any]


class _Translator:
    def __init__(self) -> None:
        self.seen: list[str] = []


class _Seam:
    """A toy engine: `start` opens a run, `text` is output, `settled` ends it."""

    engine_kind = "toy"

    def __init__(self) -> None:
        self.owed: list[dict[str, Any]] = []
        #: Children already announced; a repeated push says nothing new.
        self.reported: set[str] = set()
        self.natives: list[dict[str, Any]] = []

    @staticmethod
    def sequence(wire: _Wire) -> int:
        return wire.seq

    @staticmethod
    def record(wire: _Wire) -> dict[str, Any]:
        return wire.record

    @staticmethod
    def starts_run(record: dict[str, Any]) -> bool:
        return record.get("type") == "start"

    @staticmethod
    def settles_run(record: dict[str, Any]) -> bool:
        return record.get("type") == "settled"

    @staticmethod
    def response_id(record: dict[str, Any], sequence: int) -> str:
        return f"toy:{sequence}"

    @staticmethod
    def new_translator() -> _Translator:
        return _Translator()

    @staticmethod
    def translate(translator: _Translator, record: dict[str, Any]) -> list[dict[str, Any]]:
        kind = record.get("type")
        translator.seen.append(str(kind))
        if kind == "text":
            return [{"type": "text-delta", "id": "t", "delta": str(record.get("text"))}]
        if kind == "settled":
            return [{"type": "result", "finishReason": "stop"}]
        return []

    async def child_facts(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        if record.get("type") != "child":
            return []
        ref = str(record.get("ref"))
        if ref in self.reported:
            return []
        self.reported.add(ref)
        self.natives.append(dict(record))
        return [
            session_scoped_engine_frame(
                {
                    "type": "data-subagent",
                    "id": f"toy-child:{record.get('ref')}",
                    "data": {
                        "kind": "lifecycle",
                        "engineRef": str(record.get("ref")),
                        "engineKind": "toy",
                        "event": "opened",
                        "engineEvent": "child",
                        "operations": [],
                    },
                }
            )
        ]

    def owed_child_reads(self) -> list[dict[str, Any]]:
        owed, self.owed = self.owed, []
        return owed

    def native_records(self) -> list[dict[str, Any]]:
        natives, self.natives = self.natives, []
        return natives

    @staticmethod
    def carries_child_facts(record: dict[str, Any]) -> bool:
        return record.get("type") == "child"

    @staticmethod
    def interaction(record: dict[str, Any]) -> dict[str, Any] | None:
        return None


class _Sink:
    def __init__(self, checkpoint: ResidentOutputCheckpoint | None = None) -> None:
        self.checkpoint = checkpoint or ResidentOutputCheckpoint()
        self.opened: list[tuple[str, int]] = []
        self.published: list[tuple[str, int, list[str]]] = []
        self.frames: list[dict[str, Any]] = []
        self.closed: list[tuple[str, str, int]] = []
        self.heartbeats = 0

    async def restore_resident_output(self, *, engine_kind: str) -> ResidentOutputCheckpoint:
        return self.checkpoint

    async def open_resident_response(self, *, engine_kind, response_id, engine_session_key, causation_id, native_message, runner_sequence):
        self.opened.append((response_id, runner_sequence))
        return ResidentResponseHandle(response_id=response_id, owns_slot=True)

    async def publish_resident_output(self, handle, emissions, *, engine_sequence_number):
        self.frames.extend(e.as_frame() for e in emissions)
        self.published.append(
            (handle.response_id, engine_sequence_number, [e.as_frame()["type"] for e in emissions])
        )

    async def heartbeat_resident_response(self, handle) -> bool:
        self.heartbeats += 1
        return True

    async def open_resident_interaction(self, handle, **kwargs) -> bool:
        return True

    async def close_resident_response(self, handle, *, terminal: TurnTerminal, engine_sequence_number: int) -> None:
        self.closed.append((handle.response_id, terminal.outcome, engine_sequence_number))


class _EventSink:
    def __init__(self) -> None:
        self.persisted: list[dict[str, Any]] = []

    async def confirm_input_consumed(self, **kwargs) -> None:  # pragma: no cover - unused
        raise AssertionError("not part of these cases")

    async def persist_event(self, *, engine_kind: str, causation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.persisted.append({"engine_kind": engine_kind, "causation_id": causation_id, **payload})
        return {}


class _Harness:
    def __init__(self, *, checkpoint: ResidentOutputCheckpoint | None = None, floor: int = 0) -> None:
        self.wire: asyncio.Queue[Any] = asyncio.Queue()
        self.seam = _Seam()
        self.sink = _Sink(checkpoint)
        self.events = _EventSink()
        self.sent: list[dict[str, Any]] = []
        self._floor = floor
        self.relay = ResidentRelay(
            seam=self.seam,
            session_id="sess-1",
            engine_session_key="toy-session",
            next_record=self._next,
            send_command=self._send,
            current_sequence=self._current,
            resident_output_sink=self.sink,
            event_sink=self.events,
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def _next(self) -> Any:
        """The wire: a record, or the exception the pipe would raise."""

        item = await self.wire.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def _current(self) -> int:
        return self._floor

    async def feed(self, *records: tuple[int, dict[str, Any]]) -> None:
        for seq, record in records:
            await self.wire.put(_Wire(seq, record))
        # Let the relay drain everything it was handed.
        for _ in range(20):
            await asyncio.sleep(0)

    async def turn_records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.relay.turn_inbox.empty():
            item = self.relay.turn_inbox.get_nowait()
            out.append(item if isinstance(item, BaseException) else item.record)
        return out


@pytest.mark.asyncio
async def test_the_run_after_an_accepted_input_belongs_to_the_platform_turn() -> None:
    """Attribution comes from the input, not from who happens to be reading."""

    h = _Harness()
    h.relay.start()
    try:
        h.relay.platform_input_submitted()
        await h.feed(
            (10, {"type": "start"}),
            (11, {"type": "text", "text": "hi"}),
            (12, {"type": "settled"}),
        )

        assert [r["type"] for r in await h.turn_records()] == ["start", "text", "settled"]
        assert h.sink.opened == []
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_a_run_nobody_asked_for_is_the_engines_own_response() -> None:
    """Opened at its start, published as it streams, closed at its own terminal."""

    h = _Harness()
    h.relay.start()
    try:
        await h.feed(
            (10, {"type": "start"}),
            (11, {"type": "text", "text": "hi"}),
            (12, {"type": "settled"}),
        )

        assert h.sink.opened == [("toy:10", 10)]
        assert h.sink.published == [("toy:10", 11, ["text-delta"])]
        assert h.sink.closed == [("toy:10", "completed", 12)]
        assert await h.turn_records() == []
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_the_platform_turn_ends_where_the_engine_says_and_the_next_run_is_the_engines() -> None:
    h = _Harness()
    h.relay.start()
    try:
        h.relay.platform_input_submitted()
        await h.feed(
            (10, {"type": "start"}),
            (11, {"type": "settled"}),
            (20, {"type": "start"}),
            (21, {"type": "settled"}),
        )

        assert [r["type"] for r in await h.turn_records()] == ["start", "settled"]
        assert h.sink.opened == [("toy:20", 20)]
        assert h.sink.closed == [("toy:20", "completed", 21)]
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submit_before_restore",
    [True, False],
    ids=["submit-before-restore", "submit-after-restore"],
)
async def test_restored_platform_turn_consumes_its_pending_input_before_native_wakeup(
    submit_before_restore: bool,
) -> None:
    """Restored turn ownership cannot reserve the child's later parent response."""

    h = _Harness(checkpoint=ResidentOutputCheckpoint(external_turn_active=True))
    if submit_before_restore:
        h.relay.platform_input_submitted()
    h.relay.start()
    try:
        if not submit_before_restore:
            await h.feed()
            h.relay.platform_input_submitted()
        first_run = [
            {"type": "start"},
            {"type": "text", "text": "background child launched"},
            {"type": "settled"},
        ]
        await h.feed(*enumerate(first_run, start=10))

        assert await h.turn_records() == first_run
        assert h.sink.opened == []
        await h.feed(
            (20, {"type": "start"}),
            (21, {"type": "child", "ref": "child-1"}),
            (22, {"type": "text", "text": "child awakened the parent"}),
            (23, {"type": "settled"}),
        )

        assert h.relay.failure is None
        assert await h.turn_records() == []
        assert h.sink.opened == [("toy:20", 20)]
        assert h.sink.published == [
            ("toy:20", 21, ["data-subagent"]),
            ("toy:20", 22, ["text-delta"]),
        ]
        assert h.sink.frames[0]["data"]["engineRef"] == "child-1"
        assert h.sink.frames[1] == {
            "type": "text-delta", "id": "t", "delta": "child awakened the parent",
        }
        assert h.sink.closed == [("toy:20", "completed", 23)]
        assert h.events.persisted == []
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_platform_input_queued_after_restored_start_keeps_its_next_run() -> None:
    """Consuming the restored start must not consume an input submitted later."""

    h = _Harness(checkpoint=ResidentOutputCheckpoint(external_turn_active=True))
    h.relay.platform_input_submitted()
    h.relay.start()
    try:
        await h.feed((10, {"type": "start"}))
        assert await h.turn_records() == [{"type": "start"}]

        h.relay.platform_input_submitted()
        await h.feed(
            (11, {"type": "text", "text": "first platform answer"}),
            (12, {"type": "settled"}),
            (20, {"type": "start"}),
            (21, {"type": "text", "text": "queued platform answer"}),
            (22, {"type": "settled"}),
        )
        assert await h.turn_records() == [
            {"type": "text", "text": "first platform answer"},
            {"type": "settled"},
            {"type": "start"},
            {"type": "text", "text": "queued platform answer"},
            {"type": "settled"},
        ]
        assert h.sink.opened == []

        await h.feed(
            (30, {"type": "start"}),
            (31, {"type": "child", "ref": "child-2"}),
            (32, {"type": "text", "text": "autonomous answer after both inputs"}),
            (33, {"type": "settled"}),
        )
        assert h.relay.failure is None
        assert await h.turn_records() == []
        assert h.sink.opened == [("toy:30", 30)]
        assert h.sink.published == [
            ("toy:30", 31, ["data-subagent"]),
            ("toy:30", 32, ["text-delta"]),
        ]
        assert h.sink.frames[0]["data"]["engineRef"] == "child-2"
        assert h.sink.frames[1] == {
            "type": "text-delta", "id": "t", "delta": "autonomous answer after both inputs",
        }
        assert h.sink.closed == [("toy:30", "completed", 33)]
        assert h.events.persisted == []
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_wire_replay_from_before_the_relay_never_opens_a_response() -> None:
    """A reconnect replays the whole wire; history is journaled elsewhere."""

    h = _Harness(floor=100)
    h.relay.start()
    try:
        await h.feed(
            (10, {"type": "start"}),
            (11, {"type": "text", "text": "old"}),
            (12, {"type": "settled"}),
            (110, {"type": "start"}),
            (111, {"type": "settled"}),
        )

        assert h.sink.opened == [("toy:110", 110)]
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_a_restored_open_response_republishes_nothing_it_already_holds() -> None:
    """Replay up to the checkpoint rebuilds state; only what follows is new."""

    checkpoint = ResidentOutputCheckpoint(
        open_response_id="toy:10",
        boundary_sequence=10,
        after_sequence=11,
        committed_frames=(),
    )
    h = _Harness(checkpoint=checkpoint, floor=500)
    h.relay.start()
    try:
        await h.feed(
            (10, {"type": "start"}),
            (11, {"type": "text", "text": "already journaled"}),
            (12, {"type": "text", "text": "new"}),
            (13, {"type": "settled"}),
        )

        assert h.sink.opened == []
        assert h.sink.published == [("toy:10", 12, ["text-delta"])]
        assert h.sink.closed == [("toy:10", "completed", 13)]
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_a_child_fact_outside_any_run_is_persisted_for_the_durable_view() -> None:
    """Nothing is running, so there is no response to hang it on: the native
    record goes to the journal the child-run view already folds, and any read
    it made owed goes out now."""

    h = _Harness()
    h.seam.owed = [{"type": "prompt", "message": "/read child-1"}]
    h.relay.start()
    try:
        await h.feed((10, {"type": "child", "ref": "child-1"}))

        assert [p["message"] for p in h.events.persisted] == [{"type": "child", "ref": "child-1"}]
        assert h.events.persisted[0]["runner_sequence"] == 10
        assert h.sent == [{"type": "prompt", "message": "/read child-1"}]
        assert h.sink.published == []
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_a_repeated_push_that_changes_nothing_is_not_journaled() -> None:
    """The package repushes its status about once a second; measured, an
    idle session with one running child wrote 436 rows in 45 seconds when
    every push was persisted. Only a push that changed a child is a fact."""

    h = _Harness()
    h.relay.start()
    try:
        await h.feed(
            (10, {"type": "child", "ref": "child-1"}),
            (11, {"type": "child", "ref": "child-1"}),
            (12, {"type": "child", "ref": "child-1"}),
        )

        assert [p["runner_sequence"] for p in h.events.persisted] == [10]
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_a_child_fact_inside_a_response_is_published_with_it() -> None:
    h = _Harness()
    h.relay.start()
    try:
        await h.feed(
            (10, {"type": "start"}),
            (11, {"type": "child", "ref": "child-1"}),
            (12, {"type": "settled"}),
        )

        assert h.sink.published == [("toy:10", 11, ["data-subagent"])]
        assert h.events.persisted == []
    finally:
        await h.relay.stop()


@pytest.mark.asyncio
async def test_the_wires_failure_reaches_the_platform_turn_in_order() -> None:
    h = _Harness()
    h.relay.start()
    try:
        h.relay.platform_input_submitted()
        await h.feed((10, {"type": "start"}))
        await h.wire.put(RuntimeError("pipe died"))
        for _ in range(20):
            await asyncio.sleep(0)

        records = await h.turn_records()
        assert [r["type"] for r in records if isinstance(r, dict)] == ["start"]
        assert any(isinstance(r, RuntimeError) for r in records)
        assert isinstance(h.relay.failure, RuntimeError)
    finally:
        await h.relay.stop()
