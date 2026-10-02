from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401  (self-registers)
from astrabox.core.service.orchestrator.engine.frame_scope import mark_engine_public_ui_frame
from astrabox.core.service.orchestrator.session_message_view import (
    SessionMessageView,
    project_session_messages,
)
from astrabox.persistence.repository.session_event_repository import (
    RESIDENT_OUTPUT_SOURCE_KIND, SessionEventRepository,
)


class _PagedSessionEvents(SessionEventRepository):
    def __init__(self, events: list[dict], frames: list[dict]) -> None:
        self.events = events
        self.frames = frames
        self.event_calls: list[dict] = []
        self.frame_calls: list[dict] = []

    async def list_events(self, session_id: str, **kwargs: object) -> list[dict]:
        assert session_id == "session-1"
        self.event_calls.append(dict(kwargs))
        rows = list(self.events)
        event_types = kwargs.get("event_types")
        if isinstance(event_types, (set, frozenset, tuple)):
            rows = [row for row in rows if row["event_type"] in event_types]
        turn_id = kwargs.get("turn_id")
        if isinstance(turn_id, str):
            rows = [row for row in rows if row["turn_id"] == turn_id]
        after_seq = int(kwargs.get("after_seq") or 0)
        before_seq = kwargs.get("before_seq")
        rows = [row for row in rows if int(row["event_seq"]) > after_seq]
        if isinstance(before_seq, int):
            rows = [row for row in rows if int(row["event_seq"]) < before_seq]
        rows.sort(
            key=lambda row: int(row["event_seq"]),
            reverse=bool(kwargs.get("newest_first")),
        )
        return rows[: int(kwargs.get("limit") or 500)]

    async def list_frames(self, session_id: str, **kwargs: object) -> list[dict]:
        assert session_id == "session-1"
        self.frame_calls.append(dict(kwargs))
        rows = list(self.frames)
        source_kind = kwargs.get("source_kind")
        if source_kind is not None:
            rows = [row for row in rows if row.get("source_kind") == source_kind]
        frame_types = kwargs.get("frame_types")
        if isinstance(frame_types, (set, frozenset, tuple)):
            rows = [row for row in rows if row["payload"]["type"] in frame_types]
        turn_id = kwargs.get("turn_id")
        turn_ids = kwargs.get("turn_ids")
        if isinstance(turn_id, str):
            rows = [row for row in rows if row["turn_id"] == turn_id]
        elif isinstance(turn_ids, (set, frozenset)):
            rows = [row for row in rows if row["turn_id"] in turn_ids]
        after_seq = int(kwargs.get("after_seq") or -1)
        rows = [row for row in rows if int(row["frame_seq"]) > after_seq]
        before_seq = kwargs.get("before_seq")
        if isinstance(before_seq, int):
            rows = [row for row in rows if int(row["frame_seq"]) < before_seq]
        rows.sort(key=lambda row: int(row["frame_seq"]), reverse=bool(kwargs.get("newest_first")))
        return rows[: int(kwargs.get("limit") or 500)]


def _event(
    seq: int,
    event_type: str,
    *,
    turn_id: str,
    payload: dict | None = None,
) -> dict:
    return {
        "session_id": "session-1",
        "event_seq": seq,
        "event_type": event_type,
        "turn_id": turn_id,
        "occurred_at": f"2026-08-13T00:00:{seq:02d}Z",
        "payload": payload or {},
    }


def _frame(seq: int, turn_id: str, payload: dict) -> dict:
    return {
        "session_id": "session-1",
        "frame_seq": seq,
        "turn_id": turn_id,
        "created_at": f"2026-08-13T00:00:{seq:02d}Z",
        "payload": payload,
    }


def test_projects_user_and_assistant_from_events_without_message_store() -> None:
    events = [
        _event(
            1,
            "command.accepted",
            turn_id="turn-1",
            payload={
                "command_type": "StartTurn",
                "content": "hello",
                "client_message_id": "client-1",
                "author_user_id": "user-1",
            },
        ),
        _event(
            5,
            "turn.completed",
            turn_id="turn-1",
            payload={"assistant_text": "hello back"},
        ),
    ]
    frames = [
        _frame(3, "turn-1", {"type": "text-delta", "delta": "hello "}),
        _frame(4, "turn-1", {"type": "text-delta", "delta": "back"}),
    ]

    messages = project_session_messages(events=events, frames=frames)

    assert [(row["role"], row["content"]) for row in messages] == [
        ("user", "hello"),
        ("assistant", "hello back"),
    ]
    assert messages[1]["source_frame_seq_applied"] == 4


def test_active_input_becomes_visible_only_at_consumption_boundary() -> None:
    accepted = _event(
        1,
        "command.accepted",
        turn_id="platform-turn",
        payload={
            "command_type": "StartTurn",
            "content": "operator text",
            "input_id": "input-1",
        },
    )
    assert project_session_messages(events=[accepted], frames=[]) == []

    consumed = _event(
        2,
        "input.consumed",
        turn_id="platform-turn",
        payload={
            "input_id": "input-1",
            "response_message_id": "response-1",
            "client_message_id": "client-1",
            "content": "operator text",
        },
    )
    messages = project_session_messages(events=[accepted, consumed], frames=[])

    assert messages == [
        {
            "session_id": "session-1",
            "message_id": "input-1:user",
            "message_seq": 2,
            "turn_id": "platform-turn",
            "role": "user",
            "user_id": "",
            "client_message_id": "client-1",
            "content": "operator text",
            "blocks": [],
            "source_event_seq_applied": 2,
            "created_at": "2026-08-13T00:00:02Z",
        }
    ]


def test_failed_turn_is_a_message_derived_from_frames_and_terminal_fact() -> None:
    messages = project_session_messages(
        events=[
            _event(
                4,
                "turn.failed",
                turn_id="turn-1",
                payload={"error_text": "runner disconnected", "failure_phase": "post_dispatch"},
            )
        ],
        frames=[_frame(2, "turn-1", {"type": "text-delta", "delta": "partial"})],
    )

    assert messages[0]["content"] == "partial"
    assert messages[0]["blocks"][-1] == {
        "type": "turn_failure",
        "error": "runner disconnected",
        "failure_phase": "post_dispatch",
    }


@pytest.mark.asyncio
async def test_view_pages_the_derived_messages_and_has_no_write_api() -> None:
    events = [
        _event(
            1,
            "command.accepted",
            turn_id="turn-1",
            payload={"command_type": "StartTurn", "content": "first"},
        ),
        _event(3, "turn.completed", turn_id="turn-1", payload={"assistant_text": "one"}),
        _event(
            4,
            "command.accepted",
            turn_id="turn-2",
            payload={"command_type": "StartTurn", "content": "second"},
        ),
        _event(6, "turn.completed", turn_id="turn-2", payload={"assistant_text": "two"}),
    ]
    frames = [
        _frame(2, "turn-1", {"type": "text-delta", "delta": "one"}),
        _frame(5, "turn-2", {"type": "text-delta", "delta": "two"}),
    ]
    repo = _PagedSessionEvents(events, frames)
    view = SessionMessageView(repo)

    page, has_more = await view.list_page("session-1", limit=2)

    assert [row["content"] for row in page] == ["second", "two"]
    assert has_more is True
    assert not hasattr(view, "upsert_message")
    assert not hasattr(view, "delete_message")


@pytest.mark.asyncio
async def test_page_reads_only_the_tail_needed_for_a_long_session() -> None:
    events: list[dict] = []
    frames: list[dict] = []
    for index in range(1, 61):
        turn_id = f"turn-{index}"
        base_seq = (index - 1) * 3
        events.extend(
            [
                _event(
                    base_seq + 1,
                    "command.accepted",
                    turn_id=turn_id,
                    payload={"command_type": "StartTurn", "content": f"user {index}"},
                ),
                _event(
                    base_seq + 3,
                    "turn.completed",
                    turn_id=turn_id,
                    payload={"assistant_text": f"assistant {index}"},
                ),
            ]
        )
        frames.append(
            _frame(
                base_seq + 2,
                turn_id,
                {"type": "text-delta", "delta": f"assistant {index}"},
            )
        )
    repo = _PagedSessionEvents(events, frames)
    view = SessionMessageView(repo)

    page, has_more = await view.list_page("session-1", limit=2)

    assert [row["content"] for row in page] == ["user 60", "assistant 60"]
    assert has_more is True
    assert len(repo.event_calls) == 1
    assert repo.event_calls[0]["newest_first"] is True
    assert repo.event_calls[0]["limit"] == 20
    history_reads = [call for call in repo.frame_calls if call.get("turn_ids")]
    assert len(history_reads) == 1
    assert len(history_reads[0]["turn_ids"]) < 60


@pytest.mark.asyncio
async def test_assistant_lookup_reads_only_the_requested_turn() -> None:
    repo = _PagedSessionEvents(
        [
            _event(
                1,
                "command.accepted",
                turn_id="turn-1",
                payload={"command_type": "StartTurn", "content": "one"},
            ),
            _event(3, "turn.completed", turn_id="turn-1", payload={}),
            _event(
                4,
                "command.accepted",
                turn_id="turn-2",
                payload={"command_type": "StartTurn", "content": "two"},
            ),
            _event(6, "turn.completed", turn_id="turn-2", payload={}),
        ],
        [
            _frame(2, "turn-1", {"type": "text-delta", "delta": "answer one"}),
            _frame(5, "turn-2", {"type": "text-delta", "delta": "answer two"}),
        ],
    )
    view = SessionMessageView(repo)

    message = await view.get_assistant_message_for_turn(
        "session-1",
        turn_id="turn-2",
    )

    assert message is not None
    assert message["content"] == "answer two"
    assert {call.get("turn_id") for call in repo.event_calls} == {"turn-2"}
    assert {call.get("turn_id") for call in repo.frame_calls} == {"turn-2"}


@pytest.mark.asyncio
async def test_assistant_lookup_resolves_a_platform_turn_to_its_native_response() -> None:
    repo = _PagedSessionEvents(
        [_event(4, "turn.completed", turn_id="platform-turn", payload={})],
        [
            _frame(
                1,
                "platform-turn",
                {
                    "type": "data-input-consumed",
                    "data": {
                        "inputId": "input-1",
                        "responseMessageId": "response-1",
                        "content": "launch",
                    },
                },
            ),
            _frame(2, "platform-turn", {"type": "text-delta", "delta": "answer"}),
        ],
    )
    view = SessionMessageView(repo)

    message = await view.get_assistant_message_for_turn(
        "session-1",
        turn_id="platform-turn",
    )

    assert message is not None
    assert message["message_id"] == "response-1"
    assert message["turn_id"] == "platform-turn"
    assert message["content"] == "answer"


def test_settled_rendering_replaces_the_live_copy_of_turn_content() -> None:
    """The terminal payload owns a settled turn's content blocks.

    The live projection rendered the same text while it streamed, in a
    different interleaving (a subagent window and raw engine events between
    the segments). Sequence overlap cannot recognize that as containment, so
    without an ownership rule the settled turn renders its text twice — the
    launcher-message double body the background-agent e2e caught.
    """

    events = [
        _event(
            1,
            "command.accepted",
            turn_id="turn-1",
            payload={
                "command_type": "StartTurn",
                "content": "launch it",
                "client_message_id": "client-1",
            },
        ),
        _event(
            9,
            "turn.completed",
            turn_id="turn-1",
            payload={
                "assistant_text": "PARENT_LAUNCHED.",
                "blocks": [
                    {"type": "thinking", "thinking": "planning"},
                    {"type": "tool_use", "id": "call-1", "name": "Task"},
                    {"type": "tool_result", "tool_use_id": "call-1"},
                    {"type": "text", "text": "PARENT_LAUNCHED."},
                ],
            },
        ),
    ]
    frames = [
        _frame(2, "turn-1", {"type": "data-raw-event", "data": {"subtype": "init"}}),
        _frame(
            3,
            "turn-1",
            {
                "type": "data-subagent",
                "id": "subagent:lifecycle:task-1",
                "data": {
                    "kind": "lifecycle",
                    "phase": "started",
                    "childRunId": "child-1",
                    "controlId": "task-1",
                },
            },
        ),
        _frame(4, "turn-1", {"type": "text-delta", "id": "t0", "delta": "PARENT_LAUNCHED."}),
    ]

    messages = project_session_messages(events=events, frames=frames)
    assistant = next(row for row in messages if row["role"] == "assistant")
    blocks = assistant["blocks"]

    texts = [block["text"] for block in blocks if block.get("type") == "text"]
    assert texts == ["PARENT_LAUNCHED."]
    # Child-run lifecycle is a Session projection, never hidden inside a root
    # turn's message. The settled rendering still contributes the tool trace.
    assert [b["type"] for b in blocks].count("raw_event") == 1
    assert [b["type"] for b in blocks].count("subagent") == 0
    assert any(b.get("type") == "tool_use" and b.get("id") == "call-1" for b in blocks)
    assert assistant["content"] == "PARENT_LAUNCHED."


def test_live_blocks_the_settled_rendering_does_not_carry_survive() -> None:
    """An interaction's tool call is answered outside the engine stream.

    The settle payload re-renders streamed text, but it does not carry the
    AskUserQuestion tool_use the platform answered — dropping live blocks by
    type would erase the one canonical answered questionnaire from history,
    which is exactly what the answered-questionnaire e2e caught.
    """

    events = [
        _event(
            1,
            "command.accepted",
            turn_id="turn-1",
            payload={
                "command_type": "StartTurn",
                "content": "ask me",
                "client_message_id": "client-1",
            },
        ),
        _event(
            9,
            "turn.completed",
            turn_id="turn-1",
            payload={
                "assistant_text": "Thanks, noted.",
                "blocks": [{"type": "text", "text": "Thanks, noted."}],
            },
        ),
    ]
    frames = [
        _frame(
            2,
            "turn-1",
            {
                "type": "tool-input-available",
                "toolCallId": "ask-1",
                "toolName": "AskUserQuestion",
                "input": {"questions": []},
            },
        ),
        _frame(3, "turn-1", {"type": "reasoning-delta", "id": "r0", "delta": "a live-only aside"}),
        _frame(4, "turn-1", {"type": "text-delta", "id": "t0", "delta": "Thanks, noted."}),
    ]

    messages = project_session_messages(events=events, frames=frames)
    assistant = next(row for row in messages if row["role"] == "assistant")
    blocks = assistant["blocks"]

    assert any(
        block.get("type") == "tool_use" and block.get("id") == "ask-1" for block in blocks
    ), "the answered interaction tool call must survive the settle merge"
    texts = [block["text"] for block in blocks if block.get("type") == "text"]
    assert texts.count("Thanks, noted.") == 1
    assert any(block.get("type") == "thinking" for block in blocks), (
        "live-only streamed content the settle does not carry must survive"
    )


def test_a_thought_the_settled_rendering_omits_keeps_its_engine_position() -> None:
    """The frames are the turn's order; the settled payload is its content.

    A settled re-render does not always carry every thinking block the engine
    streamed (measured on a live turn: seven streamed, one settled). The blocks
    it omits are still the engine's, and they belong where the engine put them —
    between the tool call they preceded and the one they followed. Concatenating
    the leftovers ahead of the settled rendering hoists every one of them to the
    top of the message, which reads as the assistant thinking seven times before
    doing anything.
    """
    events = [
        _event(
            1,
            "command.accepted",
            turn_id="turn-1",
            payload={
                "command_type": "StartTurn",
                "content": "check the market",
                "client_message_id": "client-1",
                "author_user_id": "user-1",
            },
        ),
        _event(
            9,
            "turn.completed",
            turn_id="turn-1",
            payload={
                "assistant_text": "done",
                "blocks": [
                    {"type": "thinking", "thinking": "first thought"},
                    {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {}},
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"},
                    {"type": "text", "text": "done"},
                ],
            },
        ),
    ]
    frames = [
        _frame(2, "turn-1", {"type": "reasoning-delta", "delta": "first thought"}),
        _frame(
            3, "turn-1", {"type": "tool-input-start", "toolCallId": "call-1", "toolName": "Bash"}
        ),
        _frame(4, "turn-1", {"type": "tool-input-available", "toolCallId": "call-1", "input": {}}),
        _frame(
            5, "turn-1", {"type": "tool-output-available", "toolCallId": "call-1", "output": "ok"}
        ),
        _frame(6, "turn-1", {"type": "reasoning-delta", "delta": "second thought"}),
        _frame(7, "turn-1", {"type": "text-delta", "delta": "done"}),
    ]

    messages = project_session_messages(events=events, frames=frames)
    assistant = next(row for row in messages if row["role"] == "assistant")
    blocks = assistant["blocks"]
    positions = {
        str(block.get("thinking") or block.get("type")): index for index, block in enumerate(blocks)
    }

    assert positions["first thought"] < positions["tool_use"], blocks
    assert positions["tool_use"] < positions["second thought"], blocks


def test_a_settled_block_waits_for_its_own_live_counterpart() -> None:
    """The settled rendering carries fewer blocks than the stream, so the next
    settled block is not the block that belongs here.

    Measured on a live turn: the engine thought, called a tool, read the
    result, thought again, then answered — and the settled payload held only
    the SECOND thought. Flushing it at the first live block that reached its
    own counterpart rendered that thought twice: once hoisted ahead of the tool
    call it actually followed, once in its place.
    """
    from astrabox.core.service.orchestrator.message_blocks import (
        merge_settled_message_blocks,
    )

    live = [
        {"type": "thinking", "thinking": "first thought"},
        {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {}},
        {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"},
        {"type": "thinking", "thinking": "second thought"},
        {"type": "text", "text": "answer"},
    ]
    settled = [
        {"type": "thinking", "thinking": "second thought"},
        {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {}},
        {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"},
        {"type": "text", "text": "answer"},
        {"type": "result", "subtype": "success"},
    ]

    merged = merge_settled_message_blocks(live, settled)

    assert [block["type"] for block in merged] == [
        "thinking",
        "tool_use",
        "tool_result",
        "thinking",
        "text",
        "result",
    ], merged
    assert [block["thinking"] for block in merged if block["type"] == "thinking"] == [
        "first thought",
        "second thought",
    ], merged


def test_a_settled_block_that_concatenates_two_streamed_ones_adds_nothing() -> None:
    """A settled payload re-renders a turn without the engine's boundaries.

    Measured on the deployment: the engine streamed a 490-character thought,
    called a tool, then thought again — and the settled payload held ONE
    2615-character thinking block whose text is the two thoughts run together.
    Matching by equality finds no counterpart for it, so the concatenation
    renders a third time, ahead of the tool call it supposedly preceded.

    The live run carries the same characters with the boundaries intact, so it
    is what survives; the settled block adds nothing and must not enter.
    """
    from astrabox.core.service.orchestrator.message_blocks import (
        merge_settled_message_blocks,
    )

    live = [
        {"type": "thinking", "thinking": "first thought."},
        {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {}},
        {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"},
        {"type": "thinking", "thinking": "second thought."},
        {"type": "text", "text": "answer"},
    ]
    settled = [
        {"type": "thinking", "thinking": "first thought.second thought."},
        {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {}},
        {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"},
        {"type": "text", "text": "answer"},
        {"type": "result", "subtype": "success"},
    ]

    merged = merge_settled_message_blocks(live, settled)

    assert [block["type"] for block in merged] == [
        "thinking",
        "tool_use",
        "tool_result",
        "thinking",
        "text",
        "result",
    ], merged
    assert [block["thinking"] for block in merged if block["type"] == "thinking"] == [
        "first thought.",
        "second thought.",
    ], merged


def _resident_message_event(seq: int, text: str) -> dict:
    # A durable engine message with no turn: the SessionStart hook's system
    # message, projected through the Claude adapter's own reader.
    return _event(
        seq,
        "engine.message",
        turn_id="",
        payload={
            "engine_kind": "claude_code",
            "message": {
                "__sdk_type": "HookEventMessage",
                "subtype": "hook_response",
                "hook_event_name": "SessionStart",
                "uuid": f"hook-{seq}",
                "data": {"output": '{"systemMessage": "' + text + '"}'},
            },
        },
    )


@pytest.mark.asyncio
async def test_block_pages_never_settle_past_an_event_the_scan_has_not_read() -> None:
    # A native-queue turn answers two inputs; a resident engine message lands
    # after its second response and before its terminal; nineteen queued
    # inputs (commands that project to nothing until consumed) follow. The
    # newest batch of twenty holds those nineteen plus the terminal, whose
    # frames project both responses at once. With limit=1 the page would be
    # the second response — and the resident message between it and the
    # terminal, still unread, would sit past the cursor forever.
    frames = [
        _frame(2, "turn-1", {
            "type": "data-input-consumed",
            "data": {"inputId": "input-a", "responseMessageId": "response-a", "content": "first"},
        }),
        _frame(3, "turn-1", {"type": "text-delta", "delta": "answer a"}),
        _frame(4, "turn-1", {"type": "data-input-consumed", "data": {
            "inputId": "input-b", "responseMessageId": "response-b", "content": "second",
        }}),
        _frame(5, "turn-1", {"type": "text-delta", "delta": "answer b"}),
        _frame(6, "turn-1", {"type": "finish", "finishReason": "stop"}),
    ]
    events = [
        _event(1, "command.accepted", turn_id="turn-1", payload={
            "command_type": "StartTurn", "content": "first", "author_user_id": "u",
        }),
        _resident_message_event(7, "RESIDENT_NOTICE"),
        _event(8, "turn.completed", turn_id="turn-1", payload={}),
        *[
            _event(9 + index, "command.accepted", turn_id="turn-2", payload={
                "command_type": "StartTurn",
                "content": f"queued {index}",
                "input_id": f"queued-{index}",
            })
            for index in range(19)
        ],
    ]
    repo = _PagedSessionEvents(events, frames)
    view = SessionMessageView(repo)

    everything = await view._messages("session-1")
    expected_ids = [message["message_id"] for message in everything]
    assert expected_ids[-1] == "sdk-hook-system-message:hook-7"

    walked: list[str] = []
    before: str | None = None
    for _ in range(len(expected_ids) + 1):
        page = await view.list_history_blocks_page(
            "session-1", limit=1, through_seq=None, before_block_id=before,
        )
        walked = [record["message_id"] for record in page["records"]] + walked
        if not page["has_more"]:
            break
        before = page["next_before"]
    assert walked == expected_ids


def _file_part(call_id: str, path: str) -> dict:
    return {
        'type': 'data-file-changes', 'id': f'file-changes:{call_id}',
        'data': {
            'toolCallId': call_id, 'toolName': 'Write',
            'files': [{'path': path, 'diff': {'format': 'contents', 'before': '', 'after': 'kept\n'}}],
        },
    }


async def test_file_changes_include_early_history_and_only_reread_new_terminals() -> None:
    events = [_event(i * 10 + 5, 'turn.completed', turn_id=f'turn-{i}') for i in range(105)]
    frames = [
        _frame(1, 'turn-0', mark_engine_public_ui_frame(_file_part('early', '/workspace/early.txt'))),
        _frame(1041, 'turn-104', mark_engine_public_ui_frame(_file_part('late', '/workspace/late.txt'))),
    ]
    repo = _PagedSessionEvents(events, frames)
    view = SessionMessageView(repo)
    first = await view.file_changes('session-1')
    parts = [part for turn in first['turns'] for message in turn['messages'] for part in message['parts']]
    assert parts == [_file_part('early', '/workspace/early.txt'), _file_part('late', '/workspace/late.txt')]
    assert len(first['turns']) == 105
    assert [turn['turn_id'] for turn in first['turns']] == [f'turn-{i}' for i in range(105)]
    assert first['through_seq'] == 1045
    assert all(call['limit'] <= 100 for call in repo.event_calls if not call.get('newest_first'))

    repo.event_calls.clear()
    repo.frame_calls.clear()
    repo.events.append(_event(1055, 'turn.completed', turn_id='turn-105'))
    repo.frames.append(_frame(1051, 'turn-105', mark_engine_public_ui_frame(_file_part('next', '/workspace/next.txt'))))
    delta = await view.file_changes('session-1', after_seq=first['through_seq'])
    assert [turn['turn_id'] for turn in delta['turns']] == ['turn-105']
    assert delta['through_seq'] == 1055
    assert all(call['turn_ids'] == frozenset({'turn-105'}) for call in repo.frame_calls)
    assert all(call['after_seq'] >= 1045 for call in repo.event_calls if not call.get('newest_first'))


async def test_file_changes_use_canonical_recovery_blocks_and_replace_empty_turns() -> None:
    part = _file_part('native', '/workspace/recovered.txt')
    repo = _PagedSessionEvents([
        _event(5, 'turn.recovered', turn_id='turn-1', payload={
            'blocks': [{'type': 'ui_data', 'part': part}],
        }),
    ], [])
    view = SessionMessageView(repo)
    first = await view.file_changes('session-1')
    assert first['turns'] == [{'turn_id': 'turn-1', 'messages': [{'message_id': 'turn-1', 'parts': [part]}]}]
    repo.events.append(_event(9, 'turn.recovered', turn_id='turn-1', payload={'assistant_text': 'corrected'}))
    delta = await view.file_changes('session-1', after_seq=5)
    assert delta['turns'] == [{'turn_id': 'turn-1', 'messages': []}]


async def test_file_changes_do_not_invent_a_diff_from_failed_tool_arguments() -> None:
    repo = _PagedSessionEvents([
        _event(5, 'turn.failed', turn_id='turn-1', payload={'error_text': 'tool refused'}),
    ], [_frame(1, 'turn-1', {
        'type': 'tool-input-available', 'toolCallId': 'failed', 'toolName': 'Write',
        'input': {'file_path': '/workspace/not-written', 'content': 'not present'},
    })])
    assert (await SessionMessageView(repo).file_changes('session-1'))['turns'] == [
        {'turn_id': 'turn-1', 'messages': []},
    ]


async def test_file_change_read_keeps_one_checkpoint_while_new_work_arrives() -> None:
    class ArrivingEvents(_PagedSessionEvents):
        async def list_events(self, session_id: str, **kwargs: object) -> list[dict]:
            rows = await super().list_events(session_id, **kwargs)
            if kwargs.get('newest_first'):
                self.events.append(_event(15, 'turn.completed', turn_id='turn-2'))
                self.frames.append(_frame(11, 'turn-2', mark_engine_public_ui_frame(_file_part('later', '/workspace/later'))))
            return rows

    repo = ArrivingEvents([_event(5, 'turn.completed', turn_id='turn-1')], [
        _frame(1, 'turn-1', mark_engine_public_ui_frame(_file_part('first', '/workspace/first'))),
    ])
    result = await SessionMessageView(repo).file_changes('session-1')
    assert result['through_seq'] == 5
    assert [turn['turn_id'] for turn in result['turns']] == ['turn-1']
    assert result['turns'][0]['messages'][0]['parts'] == [_file_part('first', '/workspace/first')]
    assert all(call['before_seq'] == 6 for call in repo.frame_calls)


@pytest.mark.parametrize('cursor', [-1, 6])
async def test_file_change_cursor_outside_the_owned_history_is_refused(cursor: int) -> None:
    from astrabox.core.service.orchestrator.session_message_view import FileChangeCursorError
    repo = _PagedSessionEvents([_event(5, 'turn.completed', turn_id='turn-1')], [])
    with pytest.raises(FileChangeCursorError):
        await SessionMessageView(repo).file_changes('session-1', after_seq=cursor)
    assert repo.frame_calls == []


async def test_file_change_scan_refuses_a_nonadvancing_repository() -> None:
    repo = _PagedSessionEvents([_event(5, 'turn.completed', turn_id='turn-1')], [])
    original = repo.list_events

    async def stuck(session_id: str, **kwargs: object) -> list[dict]:
        if not kwargs.get('newest_first'):
            return [_event(0, 'turn.completed', turn_id='turn-1')]
        return await original(session_id, **kwargs)

    repo.list_events = stuck
    with pytest.raises(RuntimeError, match='did not advance'):
        await SessionMessageView(repo).file_changes('session-1')


async def test_file_change_read_checks_ownership_before_reading_history() -> None:
    from astrabox.common.utils.errors import APIError
    from astrabox.core.service.orchestrator.platform_service import AgentPlatformService
    target = SimpleNamespace(
        ensure_bootstrap=AsyncMock(),
        _session_service=SimpleNamespace(must_get_owned_session=AsyncMock(
            side_effect=APIError(code='SESSION_NOT_FOUND', message='not found', status_code=404),
        )),
        _session_message_view=SimpleNamespace(file_changes=AsyncMock()),
    )
    with pytest.raises(APIError):
        await AgentPlatformService.get_file_changes(target, object(), 'session-1')
    target._session_message_view.file_changes.assert_not_called()


def test_unfinished_resident_blocks_survive_a_later_input_and_settle_once() -> None:
    frames = [
        _frame(1, "native:3", {"type": "start", "messageId": "native:3"}),
        _frame(2, "native:3", {"type": "reasoning-delta", "id": "r", "delta": "checking"}),
        _frame(3, "native:3", {"type": "text-delta", "id": "t", "delta": "before tool"}),
        _frame(4, "native:3", {"type": "tool-input-available", "toolCallId": "call-1",
                               "toolName": "bash", "input": {"command": "pwd"}}),
    ]
    for frame in frames:
        frame["source_kind"] = RESIDENT_OUTPUT_SOURCE_KIND
    accepted = _event(5, "command.accepted", turn_id="next-input", payload={
        "command_type": "StartTurn", "input_id": "input-2", "content": "next question",
    })
    expected = [
        {"type": "thinking", "thinking": "checking"},
        {"type": "text", "text": "before tool"},
        {"type": "tool_use", "name": "bash", "input": {"command": "pwd"}, "id": "call-1"},
    ]
    partial = project_session_messages(events=[accepted], frames=frames)
    assert len(partial) == 1
    assert partial[0]["message_id"] == "native:3"
    assert partial[0]["blocks"] == expected

    consumed = _event(6, "input.consumed", turn_id="next-input", payload={
        "input_id": "input-2", "response_message_id": "native:4", "content": "next question",
    })
    later = _event(8, "turn.completed", turn_id="next-input", payload={"assistant_text": "next answer"})
    frames.append(_frame(7, "next-input", {"type": "text-delta", "id": "n", "delta": "next answer"}))
    messages = project_session_messages(events=[accepted, consumed, later], frames=frames)
    original = [row for row in messages if row["message_id"] == "native:3"]
    assert len(original) == 1
    assert original[0]["blocks"] == expected
    assert [row["content"] for row in messages] == ["before tool", "next question", "next answer"]

    terminal = _event(10, "turn.completed", turn_id="native:3", payload={
        "assistant_text": "before tool", "blocks": expected,
    })
    settled = project_session_messages(events=[accepted, consumed, later, terminal], frames=frames)
    assert len(settled) == 3
    assert [row["blocks"] for row in settled if row["message_id"] == "native:3"] == [expected]
