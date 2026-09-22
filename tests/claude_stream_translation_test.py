"""Token-level live streaming — StreamEvent → UI message stream parts.

Pins the projection constraints: every
contiguous block gets its own stable id (``claude-live:<uuid>:<index>``), the
complete AssistantMessage never double-renders what already streamed (text
and thinking suppressed; a started tool emits only its available frame), a
delta whose start was lost to a link gap self-heals by synthesizing the
start, and subagent stream events stay out of the main lanes.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    rebuild_claude_stream_cursor,
    translate_claude_sdk_message,
    translate_claude_stream_event,
)


def test_per_event_uuids_stream_one_lane_per_block() -> None:
    # The CLI can mint a fresh uuid per stream event; the block's stable identity
    # is its index. Keying by (uuid, index) would create one lane per delta and
    # leave starts without matching ends, so the fixture varies each event uuid.
    cursor = ClaudeStreamCursor()
    frames: list[dict[str, Any]] = []
    events = [
        _stream("e1", {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "thinking"}}),
        _stream("e2", {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "thinking_delta", "thinking": "a"}}),
        _stream("e3", {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "thinking_delta", "thinking": "b"}}),
        _stream("e4", {"type": "content_block_stop", "index": 0}),
        _stream("e5", {"type": "content_block_start", "index": 1,
                       "content_block": {"type": "tool_use", "id": "toolu_7", "name": "Bash"}}),
        _stream("e6", {"type": "content_block_delta", "index": 1,
                       "delta": {"type": "input_json_delta", "partial_json": "{\"c"}}),
        _stream("e7", {"type": "content_block_stop", "index": 1}),
    ]
    for event in events:
        frames.extend(translate_claude_stream_event(event, cursor=cursor))
    kinds = [f["type"] for f in frames]
    assert kinds == [
        "reasoning-start", "reasoning-delta", "reasoning-delta", "reasoning-end",
        "tool-input-start", "tool-input-delta",
    ]
    # Engine tools are dynamic to the client: without the flag the AI SDK
    # reducer builds a static typed part the console never renders.
    (tool_start,) = [f for f in frames if f["type"] == "tool-input-start"]
    assert tool_start["dynamic"] is True
    # One lane, stable id across the block's deltas and end.
    reasoning_ids = {f["id"] for f in frames if f["type"].startswith("reasoning")}
    assert len(reasoning_ids) == 1
    # The tool delta runs under the REAL tool id, not a synthetic lane id.
    assert [f["toolCallId"] for f in frames if "toolCallId" in f] == ["toolu_7", "toolu_7"]


def test_a_start_over_an_open_lane_closes_it_first() -> None:
    # A lost stop must not leave a lane that never ends: the superseding start
    # closes the translator's current lane before opening the new one.
    cursor = ClaudeStreamCursor()
    frames: list[dict[str, Any]] = []
    for event in [
        _stream("e1", {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "thinking"}}),
        _stream("e2", {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "thinking_delta", "thinking": "a"}}),
        # stop lost; next message reuses index 0 for text
        _stream("e3", {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "text"}}),
    ]:
        frames.extend(translate_claude_stream_event(event, cursor=cursor))
    assert [f["type"] for f in frames] == [
        "reasoning-start", "reasoning-delta", "reasoning-end", "text-start",
    ]


def _stream(uuid: str, event: dict[str, Any], parent: str | None = None) -> dict[str, Any]:
    return {
        "__sdk_type": "StreamEvent",
        "uuid": uuid,
        "event": event,
        "parent_tool_use_id": parent,
    }


def _drain(cursor: ClaudeStreamCursor, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        out.extend(
            translate_claude_sdk_message(message, envelope_seq=1, cursor=cursor)
        )
    return out


def test_full_live_cycle_streams_and_complete_message_does_not_double_render() -> None:
    cursor = ClaudeStreamCursor()
    _drain(cursor, _stream("start", {"type": "message_start", "message": {"id": "native-1"}}))
    frames = _drain(
        cursor,
        _stream("m1", {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "thinking"}}),
        _stream("m1", {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
        _stream("m1", {"type": "content_block_stop", "index": 0}),
        _stream("m1", {"type": "content_block_start", "index": 1,
                       "content_block": {"type": "text"}}),
        _stream("m1", {"type": "content_block_delta", "index": 1,
                       "delta": {"type": "text_delta", "text": "Sure"}}),
        _stream("m1", {"type": "content_block_stop", "index": 1}),
        _stream("m1", {"type": "content_block_start", "index": 2,
                       "content_block": {"type": "tool_use", "id": "toolu_9", "name": "Bash"}}),
        _stream("m1", {"type": "content_block_delta", "index": 2,
                       "delta": {"type": "input_json_delta", "partial_json": '{"comm'}}),
        _stream("m1", {"type": "content_block_stop", "index": 2}),
        {
            "__sdk_type": "AssistantMessage",
            "message_id": "native-1",
            "content": [
                {"__sdk_type": "ThinkingBlock", "thinking": "hmm"},
                {"__sdk_type": "TextBlock", "text": "Sure"},
                {"__sdk_type": "ToolUseBlock", "id": "toolu_9", "name": "Bash",
                 "input": {"command": "ls"}},
            ],
        },
    )
    kinds = [f["type"] for f in frames]
    assert kinds == [
        "reasoning-start", "reasoning-delta", "reasoning-end",
        "text-start", "text-delta", "text-end",
        "tool-input-start", "tool-input-delta",
        # complete message: ONLY the tool's available frame — text/thinking
        # already streamed, and the tool already announced its start.
        "tool-input-available",
    ]
    assert frames[3]["id"] == "claude-live:m1:1"
    assert frames[7]["toolCallId"] == "toolu_9"
    assert frames[8]["input"] == {"command": "ls"}


def test_delta_with_lost_start_self_heals() -> None:
    cursor = ClaudeStreamCursor()
    frames = _drain(
        cursor,
        _stream("m1", {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "after gap"}}),
    )
    assert [f["type"] for f in frames] == ["text-start", "text-delta"]
    assert frames[1]["delta"] == "after gap"


def test_subagent_stream_events_stay_out_of_main_lanes() -> None:
    cursor = ClaudeStreamCursor()
    (frame,) = list(
        translate_claude_stream_event(
            _stream("m1", {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": "sub"}},
                    parent="toolu_task"),
            cursor=cursor,
        )
    )
    assert frame["type"] == "data-raw-event"
    assert frame["data"]["subtype"] == "subagent_stream_event"
    assert frame["__engine_frame_scope"] == "session"
    assert frame["transient"] is True


def test_without_explicit_cursor_translates_streams_and_complete_blocks() -> None:
    # The public translator constructs its own cursor when omitted. A root
    # delta therefore reaches the text lane, while a complete-only call owns
    # its own step. Neither path may silently become a raw diagnostic.
    streamed = list(
        translate_claude_sdk_message(
            _stream("m1", {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": "x"}}),
            envelope_seq=1,
        )
    )
    assert streamed == [
        {"type": "text-start", "id": "claude-live:m1:0", "__engine_block_index": 0},
        {"type": "text-delta", "id": "claude-live:m1:0", "delta": "x"},
    ]
    full = list(
        translate_claude_sdk_message(
            {"__sdk_type": "AssistantMessage",
             "content": [{"__sdk_type": "TextBlock", "text": "hi"}]},
            envelope_seq=2,
        )
    )
    assert full == [
        {"type": "start-step"},
        {"type": "text-start", "id": "claude-text:2:0"},
        {"type": "text-delta", "id": "claude-text:2:0", "delta": "hi"},
        {"type": "text-end", "id": "claude-text:2:0"},
        {"type": "finish-step"},
    ]


def test_task_lifecycle_messages_translate_as_session_scoped_typed_facts() -> None:
    """Native lifecycle messages retain identity, status and usable controls."""
    from dataclasses import asdict

    from claude_agent_sdk.types import (
        TaskNotificationMessage,
        TaskProgressMessage,
        TaskStartedMessage,
        TaskUpdatedMessage,
    )

    usage = {"total_tokens": 12, "tool_uses": 1, "duration_ms": 20}
    cases = [
        (
            TaskStartedMessage(
                subtype="task_started", data={}, task_id="bg-1", description="work",
                uuid="started", session_id="sdk-sess",
            ),
            {"event": "opened", "operations": ["stop"], "description": "work"},
        ),
        (
            TaskProgressMessage(
                subtype="task_progress", data={}, task_id="bg-1", description="working",
                usage=usage, uuid="progress", session_id="sdk-sess", last_tool_name="Bash",
            ),
            {"event": "updated", "operations": ["stop"], "description": "working",
             "usage": usage, "lastToolName": "Bash"},
        ),
        (
            TaskUpdatedMessage(
                subtype="task_updated", data={}, task_id="bg-1", patch={"status": "paused"},
                uuid="updated", session_id="sdk-sess",
            ),
            {"event": "updated", "operations": ["stop"], "engineStatus": "paused"},
        ),
        (
            TaskNotificationMessage(
                subtype="task_notification", data={}, task_id="bg-1", status="completed",
                output_file="/private/task-output", summary="finished", uuid="notification",
                session_id="sdk-sess",
            ),
            {"event": "closed", "operations": [], "engineStatus": "completed",
             "summary": "finished"},
        ),
    ]
    for native, expected in cases:
        frames = list(
            translate_claude_sdk_message(
                {"__sdk_type": type(native).__name__, **asdict(native)},
                envelope_seq=7,
            )
        )
        assert frames == [{
            "type": "data-subagent",
            "id": f"subagent:lifecycle:{native.uuid}",
            "data": {
                "kind": "lifecycle", "engineRef": "bg-1", "controlRef": "bg-1",
                "engineEvent": native.subtype, "engineKind": "claude_code", **expected,
            },
            "__engine_frame_scope": "session",
            "transient": True,
        }]


def test_unregistered_sdk_message_still_fails_loud() -> None:
    # The gate itself must survive the registration above: a genuinely unknown
    # type keeps raising, never silently dropping.
    import pytest

    from astrabox.core.service.orchestrator.engine.frame_translator import (
        UnknownWireEvent,
    )

    with pytest.raises(UnknownWireEvent, match="no registered translation"):
        list(
            translate_claude_sdk_message(
                {"__sdk_type": "SomethingNewMessage"}, envelope_seq=1
            )
        )


def test_engine_message_boundaries_become_ui_steps() -> None:
    """The SDK's own message boundary is a step inside the turn's UI message.

    A platform turn is one UI message; the engine answers it in several of its
    own messages — think, call a tool, read the result, think again. The AI SDK
    stream vocabulary already has both levels (``start``/``finish`` for the
    message, ``start-step``/``finish-step`` for a step within it).
    ``message_start`` and ``message_stop`` mark the engine message boundaries.
    Treating them as informational leaves the whole turn as one flat run of
    blocks, and forces downstream consumers to invent boundaries from content.
    """
    cursor = ClaudeStreamCursor()
    frames = _drain(
        cursor,
        _stream("m1", {"type": "message_start"}),
        _stream("m1", {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "thinking"}}),
        _stream("m1", {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "thinking_delta", "thinking": "weigh it"}}),
        _stream("m1", {"type": "content_block_stop", "index": 0}),
        _stream("m1", {"type": "message_stop"}),
        _stream("m2", {"type": "message_start"}),
        _stream("m2", {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "text"}}),
        _stream("m2", {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "done"}}),
        _stream("m2", {"type": "content_block_stop", "index": 0}),
        _stream("m2", {"type": "message_stop"}),
    )

    kinds = [str(frame.get("type") or "") for frame in frames]
    assert kinds.count("start-step") == 2, kinds
    assert kinds.count("finish-step") == 2, kinds
    # Each step encloses its own content, so a reader never has to guess which
    # blocks belonged to which engine message.
    assert kinds.index("start-step") < kinds.index("reasoning-start"), kinds
    assert kinds.index("finish-step") > kinds.index("reasoning-end"), kinds


def _streamed_prefix(cursor: ClaudeStreamCursor, message_id: str = "native-1") -> list[dict[str, Any]]:
    return _drain(
        cursor,
        _stream("start", {"type": "message_start", "message": {"id": message_id}}),
        _stream("block", {"type": "content_block_start", "index": 0,
                          "content_block": {"type": "text"}}),
        _stream("delta", {"type": "content_block_delta", "index": 0,
                          "delta": {"type": "text_delta", "text": "Before tool"}}),
        _stream("stop", {"type": "content_block_stop", "index": 0}),
        _stream("end", {"type": "message_stop"}),
    )


def _complete(message_id: str | None, *, error: str | None = None) -> dict[str, Any]:
    return {"__sdk_type": "AssistantMessage", "message_id": message_id, "error": error,
            "content": [{"__sdk_type": "TextBlock", "text": "After tool"}]}


def test_complete_message_after_a_different_streamed_message_keeps_its_answer() -> None:
    cursor = ClaudeStreamCursor()
    _streamed_prefix(cursor)
    frames = _drain(cursor, _complete("native-2"))
    assert [frame["type"] for frame in frames] == [
        "start-step", "text-start", "text-delta", "text-end", "finish-step",
    ]
    assert frames[2]["delta"] == "After tool"


def test_message_start_without_content_does_not_hide_complete_answer() -> None:
    cursor = ClaudeStreamCursor()
    _streamed_prefix(cursor)
    _drain(cursor, _stream("start-2", {"type": "message_start", "message": {"id": "native-2"}}))
    frames = _drain(cursor, _complete("native-2"))
    assert [frame["type"] for frame in frames] == ["text-start", "text-delta", "text-end"]
    assert [frame["delta"] for frame in frames if frame["type"] == "text-delta"] == ["After tool"]


def test_absent_native_identity_cannot_prove_complete_content_was_streamed() -> None:
    for stream_id, complete_id in [("", "native-1"), ("native-1", None), ("", None)]:
        cursor = ClaudeStreamCursor()
        _streamed_prefix(cursor, stream_id)
        frames = _drain(cursor, _complete(complete_id))
        assert [frame["delta"] for frame in frames if frame["type"] == "text-delta"] == ["After tool"]


def test_provider_error_is_visible_even_when_it_names_the_streamed_message() -> None:
    cursor = ClaudeStreamCursor()
    _streamed_prefix(cursor)
    frames = _drain(cursor, _complete("native-1", error="rate_limit"))
    assert frames[0]["data"]["raw"]["error"] == "rate_limit"
    from astrabox.core.service.orchestrator.engine.frame_scope import public_engine_frame_payload

    assert public_engine_frame_payload(frames[0], frame_seq=1, scope="turn") is None
    assert [frame["delta"] for frame in frames if frame["type"] == "text-delta"] == ["After tool"]


def test_rebuilt_cursor_deduplicates_only_the_last_native_streamed_message() -> None:
    cursor = ClaudeStreamCursor()
    frames = _streamed_prefix(cursor)
    rows = [{"payload": frame} for frame in frames]
    resumed = rebuild_claude_stream_cursor(rows)
    assert _drain(resumed, _complete("native-1")) == []
    assert [frame["delta"] for frame in _drain(resumed, _complete("native-2"))
            if frame["type"] == "text-delta"] == ["After tool"]
    next_start = _drain(cursor, _stream("start-2", {"type": "message_start",
                                                 "message": {"id": "native-2"}}))
    resumed = rebuild_claude_stream_cursor(rows + [{"payload": frame} for frame in next_start])
    assert [frame["delta"] for frame in _drain(resumed, _complete("native-2"))
            if frame["type"] == "text-delta"] == ["After tool"]


def test_native_deduplication_identity_stays_private_in_public_projection() -> None:
    from astrabox.core.service.orchestrator.engine.frame_scope import public_engine_frame_payload

    cursor = ClaudeStreamCursor()
    frames = _streamed_prefix(cursor)
    assert frames[0]["__claude_stream_message_id"] == "native-1"
    assert public_engine_frame_payload(frames[0], frame_seq=1, scope="turn") == {
        "type": "start-step",
    }


def test_complete_only_step_resets_live_and_rebuilt_deduplication_state_equally() -> None:
    cursor = ClaudeStreamCursor()
    frames = _streamed_prefix(cursor)
    frames.extend(_drain(cursor, _complete("native-2")))
    rebuilt = rebuild_claude_stream_cursor([{"payload": frame} for frame in frames])
    assert rebuilt.stream_message_id == cursor.stream_message_id == ""
    assert rebuilt.streamed_text == cursor.streamed_text is False
    assert _drain(rebuilt, _complete("native-1")) == _drain(cursor, _complete("native-1"))
