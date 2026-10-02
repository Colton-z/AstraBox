"""Opaque adapter resume cursors survive the output journal, not the UI wire."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.emissions import emission_from_translated_frame
from astrabox.core.service.orchestrator.engine.platform_events import PlatformEngineEventSink, PlatformResidentOutputSink
from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_assistant import (
    build_engine_output_checkpoint,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_checkpoint import (
    DurableRecoveryMaterializationMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn import bridge_journal
from astrabox.core.service.orchestrator.session_kernel.workers.turn._replay import _DurableSemanticFrameCoalescer
from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import _BridgeRunState
from astrabox.persistence.repository import SessionEventRepository, SessionSnapshotRepository


def _cursor(index: int) -> dict[str, Any]:
    return {"sessionId": "native", "seq": 12, "assistantStream": {
        "revision": index + 1,
        "activeAttempt": {"attemptId": "attempt", "startedAfterSeq": 12, "nextIndex": index},
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["foreground", "recovery"])
async def test_journal_writers_keep_cursor_private_and_return_it_to_the_adapter(writer: str) -> None:
    session_id = str(uuid.uuid4())
    journal = SessionEventRepository()
    coalescer = _DurableSemanticFrameCoalescer()
    emitted = []
    for index, text in enumerate(("first", " second"), 1):
        emitted.extend(coalescer.ingest({
            "type": "text-delta", "id": "block", "delta": text,
            "__engine_sequence_number": index, "__engine_output_cursor": _cursor(index),
        }))
    emitted.extend(coalescer.flush())
    assert len(emitted) == 1
    published = []

    async def mongo(_operation, callback, **_kwargs):
        return await callback()

    async def initialize(_turn):
        pass

    async def publish(event):
        published.append(event)

    if writer == "foreground":
        await bridge_journal._persist_frames(
            SimpleNamespace(_session_events_repo=journal), _BridgeRunState(),
            SimpleNamespace(session_id=session_id, command_id="command",
                            ensure_frame_seq_initialized=initialize,
                            run_live_frame_mongo_op=mongo, publish_broker_event=publish),
            [(frame, "turn") for frame in emitted],
        )
    else:
        service = DurableRecoveryMaterializationMixin()
        service._session_events_repo = journal
        await service._append_engine_anchor_recovered_frames(
            session_id=session_id, turn_id="turn", command_id="command", engine_kind="deepseek_harness",
            engine_turn_id="native-turn", frames=[emission_from_translated_frame(f) for f in emitted],
            starting_after=None,
        )
    frames = await journal.list_frames(session_id, turn_id="turn")
    assert len(frames) == 1
    assert frames[0]["payload"]["delta"] == "first second"
    assert frames[0]["engine_output_cursor"] == _cursor(2)
    assert "__engine_output_cursor" not in frames[0]["payload"]
    assert all("__engine_output_cursor" not in event["payload"] for event in published)
    checkpoint = build_engine_output_checkpoint(frames, after_sequence=2)
    assert checkpoint.output_cursor == _cursor(2)
    assert checkpoint.committed_frames[0]["engine_output_cursor"] == _cursor(2)


@pytest.mark.asyncio
async def test_resident_cursor_survives_open_and_settled_response_without_publication() -> None:
    session_id = str(uuid.uuid4())
    journal = SessionEventRepository()
    snapshots = SessionSnapshotRepository()
    await snapshots.apply_channel_update(session_id, channel="conversation", event_seq=1,
                                         updates={"conversation_state": "IDLE"})
    await journal.append_event({
        "session_id": session_id, "channel": "conversation", "event_type": "dispatch.confirmed",
        "payload": {"recovery_context": {"sandbox_id": "box", "engine_anchor": {
            "engine_kind": "deepseek_harness", "engine_session_key": "native", "engine_turn_id": "dispatch",
        }}},
    })
    published = []

    class Broker:
        async def publish(self, _session_id, event):
            published.append(event)

    sink = PlatformResidentOutputSink(session_id, broker=Broker(), journal_repo=journal, snapshots_repo=snapshots)
    handle = await sink.open_resident_response(
        engine_kind="deepseek_harness", response_id="native:2", engine_session_key="native",
        causation_id="native:2", native_message={"type": "turn/start"}, runner_sequence=10,
    )
    assert handle is not None
    for frame in [
        {"type": "text-start", "id": "block"},
        {"type": "text-delta", "id": "block", "delta": "answer"},
        {"type": "text-end", "id": "block"},
    ]:
        await sink.publish_resident_output(handle, [emission_from_translated_frame({
            **frame, "__engine_output_cursor": _cursor(2),
        })], engine_sequence_number=11)
    restored = await sink.restore_resident_output(engine_kind="deepseek_harness",
                                                  sandbox_id="box", engine_session_key="native")
    assert restored.output_cursor == _cursor(2)
    assert restored.open_response_id == "native:2"
    assert restored.high_water_sequence == 11
    terminal_cursor = {"sessionId": "native", "seq": 19}
    terminal = emission_from_translated_frame({
        "type": "result", "finishReason": "stop", "__engine_output_cursor": terminal_cursor,
    })
    await sink.close_resident_response(handle, terminal=terminal, engine_sequence_number=12)
    settled = await sink.restore_resident_output(engine_kind="deepseek_harness",
                                                 sandbox_id="box", engine_session_key="native")
    assert settled.open_response_id is None
    assert settled.replay_output_cursor == terminal_cursor
    assert settled.replay_after_sequence == 12
    event_sink = PlatformEngineEventSink(session_id, journal_repo=journal)
    await event_sink.persist_event(
        engine_kind="deepseek_harness", causation_id="deepseek_harness:idle:23:0",
        payload={"runner_sequence": 23, "message": {"child": "completed"}},
    )
    await event_sink.persist_event(
        engine_kind="other", causation_id="other:idle:900:0",
        payload={"runner_sequence": 900, "message": {}},
    )
    # Journal order does not imply reader order across earlier connections.
    await event_sink.persist_event(
        engine_kind="deepseek_harness", causation_id="old-reader:1",
        payload={"runner_sequence": 1, "message": {}},
    )
    after_idle = await sink.restore_resident_output(
        engine_kind="deepseek_harness", sandbox_id="box", engine_session_key="native",
    )
    assert after_idle.high_water_sequence == 23
    assert after_idle.replay_after_sequence == 12
    assert after_idle.replay_output_cursor == terminal_cursor
    from astrabox.core.service.orchestrator.engine.deepseek_harness_client import DeepSeekHarnessEngineClient
    client = DeepSeekHarnessEngineClient(
        session_id=session_id, native_session_id="native", link=None, output_checkpoint=after_idle,
    )
    assert await client._current_inbound_sequence() == 23
    assert all("__engine_output_cursor" not in event.get("payload", {}) for event in published)
    rows = await journal.list_frames(session_id)
    assert all("__engine_output_cursor" not in row["payload"] for row in rows)
    mismatched = await sink.restore_resident_output(engine_kind="deepseek_harness",
                                                    sandbox_id="other-box", engine_session_key="native")
    assert mismatched.replay_output_cursor is None


def test_dsh_cursor_keeps_native_durable_and_live_positions_separate() -> None:
    from astrabox.core.service.orchestrator.engine.deepseek_harness_client import DeepSeekHarnessEngineClient
    from astrabox.core.service.orchestrator.engine.deepseek_harness_events import DeepSeekHarnessTurnTranslator

    translator = DeepSeekHarnessTurnTranslator(session_id="native")
    client = DeepSeekHarnessEngineClient(session_id="platform", native_session_id="native", link=None)
    baseline = {"revision": 3, "activeAttempt": {
        "attemptId": "attempt", "startedAfterSeq": 12, "turn": 2, "step": 1, "nextIndex": 2,
        "stream": [
            {"type": "chunk", "chunk": {"type": "block-start", "index": 0, "blockType": "text"}},
            {"type": "chunk", "chunk": {"type": "text-delta", "index": 0, "text": "prefix"}},
        ],
    }}
    frames = client._translate_output_frame(translator, {
        "type": "session/assistant-stream-snapshot", "payload": {
            "sessionId": "native", "cursor": 12, "baseline": baseline,
        },
    })
    expected = {"sessionId": "native", "seq": 12, "assistantStream": {
        "revision": 3, "activeAttempt": {
            "attemptId": "attempt", "startedAfterSeq": 12, "turn": 2, "step": 1, "nextIndex": 2,
        },
    }}
    assert all(frame["__engine_output_cursor"] == expected for frame in frames)
    assert frames[-1]["delta"] == "prefix"
    live = client._translate_output_frame(translator, {
        "type": "session/assistant-stream", "payload": {"sessionId": "native", "frame": {
            "type": "chunk", "revision": 4, "attemptId": "attempt", "index": 2,
            "chunk": {"type": "text-delta", "index": 0, "text": " suffix"},
        }},
    })
    cursor = live[0]["__engine_output_cursor"]
    assert cursor["seq"] == 12
    assert cursor["assistantStream"]["revision"] == 4
    assert cursor["assistantStream"]["activeAttempt"]["nextIndex"] == 3
    assert "stream" not in cursor["assistantStream"]["activeAttempt"]
    assert frames[-1]["__engine_output_cursor"] == expected


def test_dsh_repair_replays_only_the_unfinished_native_turn_before_the_committed_cursor() -> None:
    from astrabox.core.service.orchestrator.engine.deepseek_harness_client import DeepSeekHarnessEngineClient

    client = DeepSeekHarnessEngineClient(session_id="platform", native_session_id="native", link=None)
    client._committed_replay = (3, 68)
    records = [
        ("native", 40, "turn/start", {"turn": 3}),
        ("native", 41, "message", {}),
        ("native", 61, "turn/end", {}),
        ("child", 1, "turn/start", {"turn": 1}),
        ("native", 62, "turn/start", {"turn": 4}),
        ("native", 65, "message", {}),
        ("native", 68, "turn/end", {}),
        ("native", 69, "turn/start", {"turn": 5}),
        ("native", 70, "message", {}),
    ]
    kept = [
        (session, sequence)
        for session, sequence, kind, data in records
        if not client._already_published_replay({
            "type": "session/event", "payload": {"sessionId": session,
                "event": {"seq": sequence, "type": kind, "data": data}},
        })
    ]
    assert kept == [("native", 40), ("native", 41), ("native", 61), ("child", 1), ("native", 69), ("native", 70)]
    assert client._committed_replay is None


def test_dsh_settling_an_older_gap_keeps_the_later_committed_resume_cursor() -> None:
    from astrabox.core.service.orchestrator.engine.base import ResidentOutputCheckpoint
    from astrabox.core.service.orchestrator.engine.deepseek_harness_client import DeepSeekHarnessEngineClient
    from astrabox.core.service.orchestrator.engine.deepseek_harness_events import DeepSeekHarnessTurnTranslator

    cursor = {"sessionId": "native", "seq": 68}
    client = DeepSeekHarnessEngineClient(
        session_id="platform", native_session_id="native", link=None,
        output_checkpoint=ResidentOutputCheckpoint(replay_output_cursor=cursor),
    )
    client._committed_replay = (3, 68)
    translator = DeepSeekHarnessTurnTranslator(session_id="native")
    for sequence, kind, data in [
        (43, "turn/start", {"turn": 3}),
        (61, "turn/end", {"turn": 3, "reason": {"kind": "completed"}}),
    ]:
        frames = client._translate_output_frame(translator, {
            "type": "session/event", "payload": {"sessionId": "native",
                "event": {"seq": sequence, "type": kind, "data": data}},
        })
    terminal = next(frame for frame in frames if frame["type"] == "result")
    assert terminal["__engine_output_cursor"] == cursor
