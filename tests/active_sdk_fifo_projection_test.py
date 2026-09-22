from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.engine.claude_code import (
    ClaudeCodeEngineAdapter,
)
from astrabox.core.service.orchestrator.engine.registry import (
    register_engine_adapter,
)
from astrabox.core.service.orchestrator.session_kernel.active_turn_projection import (
    build_active_engine_fifo_messages,
    build_active_turn_message,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_read import (
    SessionReadRenderingMixin,
)


def _frame(frame_seq: int, payload: dict[str, object]) -> dict[str, object]:
    return {
        "frame_seq": frame_seq,
        "created_at": f"2026-08-09T00:00:0{frame_seq}Z",
        "payload": payload,
    }


def test_pre_result_native_fifo_projects_every_root_exchange_in_order() -> None:
    messages = build_active_engine_fifo_messages(
        session_id="session-1",
        turn_id="platform-turn-1",
        default_message_seq=10,
        frames=[
            _frame(
                1,
                {
                    "type": "data-input-consumed",
                    "data": {
                        "inputId": "00000000-0000-0000-0000-000000000001",
                        "responseMessageId": "response-1",
                        "clientMessageId": "client-1",
                        "content": "first",
                    },
                },
            ),
            _frame(2, {"type": "text-delta", "delta": "first answer"}),
            _frame(
                3,
                {
                    "type": "data-input-consumed",
                    "data": {
                        "inputId": "00000000-0000-0000-0000-000000000002",
                        "responseMessageId": "response-2",
                        "content": "second",
                    },
                },
            ),
            _frame(
                4,
                {
                    "type": "tool-input-available",
                    "toolCallId": "tool-2",
                    "toolName": "Bash",
                    "input": {"command": "printf second"},
                },
            ),
        ],
    )

    assert [(message["role"], message["message_id"]) for message in messages] == [
        ("user", "00000000-0000-0000-0000-000000000001:user"),
        ("assistant", "response-1"),
        ("user", "00000000-0000-0000-0000-000000000002:user"),
        ("assistant", "response-2"),
    ]
    assert [message["message_seq"] for message in messages] == [1, 2, 3, 4]
    assert {message["turn_id"] for message in messages} == {"platform-turn-1"}
    assert messages[0]["content"] == "first"
    assert messages[0]["client_message_id"] == "client-1"
    assert messages[1]["content"] == "first answer"
    assert messages[3]["blocks"] == [
        {
            "type": "tool_use",
            "id": "tool-2",
            "name": "Bash",
            "input": {"command": "printf second"},
        }
    ]


def test_consumed_root_without_assistant_output_keeps_an_empty_response_anchor() -> None:
    messages = build_active_engine_fifo_messages(
        session_id="session-1",
        turn_id="platform-turn-1",
        default_message_seq=1,
        frames=[
            _frame(
                1,
                {
                    "type": "data-input-consumed",
                    "data": {
                        "inputId": "00000000-0000-0000-0000-000000000001",
                        "responseMessageId": "response-1",
                        "content": "queued",
                    },
                },
            )
        ],
    )

    assert len(messages) == 2
    assert messages[1]["role"] == "assistant"
    assert messages[1]["message_id"] == "response-1"
    assert messages[1]["content"] == ""
    assert messages[1]["blocks"] == []


def test_adapter_public_data_part_survives_reload_with_its_open_payload() -> None:
    message = build_active_turn_message(
        session_id="session-1",
        turn_id="platform-turn-1",
        message_id="response-1",
        default_message_seq=1,
        existing_message=None,
        frames=[
            _frame(
                1,
                {
                    "type": "data-engine-metrics",
                    "id": "metrics-1",
                    "data": {"tokens": 1, "newVendorMetric": 7},
                    "providerMetadata": {"cacheReadTokens": 12},
                    "__engine_public_ui": True,
                },
            ),
            _frame(
                2,
                {
                    "type": "data-engine-metrics",
                    "id": "metrics-1",
                    "data": {"tokens": 2, "newVendorMetric": 9},
                    "providerMetadata": {"cacheReadTokens": 14},
                    "__engine_public_ui": True,
                },
            ),
        ],
    )

    assert message is not None
    assert message["blocks"] == [
        {
            "type": "ui_data",
            "part": {
                "type": "data-engine-metrics",
                "id": "metrics-1",
                "data": {"tokens": 2, "newVendorMetric": 9},
                "providerMetadata": {"cacheReadTokens": 14},
            },
        }
    ]


def test_platform_data_without_an_adapter_public_declaration_is_not_a_message_part() -> None:
    message = build_active_turn_message(
        session_id="session-1",
        turn_id="platform-turn-1",
        message_id="response-1",
        default_message_seq=1,
        existing_message=None,
        frames=[
            _frame(
                1,
                {
                    "type": "data-engine-control",
                    "data": {"controlRef": "private-control"},
                },
            )
        ],
    )

    assert message is None


def test_adapter_public_data_part_replaces_the_restored_part_by_type_and_id() -> None:
    message = build_active_turn_message(
        session_id="session-1",
        turn_id="platform-turn-1",
        message_id="response-1",
        default_message_seq=1,
        existing_message={
            "message_id": "response-1",
            "blocks": [
                {
                    "type": "ui_data",
                    "part": {
                        "type": "data-engine-metrics",
                        "id": "metrics-1",
                        "data": {"tokens": 1},
                    },
                }
            ],
            "source_frame_seq_applied": 4,
        },
        frames=[
            _frame(
                5,
                {
                    "type": "data-engine-metrics",
                    "id": "metrics-1",
                    "data": {"tokens": 2},
                    "__engine_public_ui": True,
                },
            )
        ],
        incremental=True,
    )

    assert message is not None
    assert message["blocks"] == [
        {
            "type": "ui_data",
            "part": {
                "type": "data-engine-metrics",
                "id": "metrics-1",
                "data": {"tokens": 2},
            },
        }
    ]


def test_active_projection_refuses_to_replace_an_engine_message_identity() -> None:
    with pytest.raises(
        ValueError,
        match="active message projection cannot replace an existing message identity",
    ):
        build_active_turn_message(
            session_id="session-1",
            turn_id="platform-turn-1",
            message_id="response-2",
            frames=[_frame(2, {"type": "text-delta", "delta": "answer"})],
            existing_message={
                "message_id": "response-1",
                "blocks": [],
            },
            default_message_seq=1,
        )


async def test_declared_fifo_engine_active_overlay_projects_native_fifo() -> None:
    register_engine_adapter("claude_code", ClaudeCodeEngineAdapter())
    frames = [
        _frame(
            1,
            {
                "type": "data-input-consumed",
                "data": {
                    "inputId": "00000000-0000-0000-0000-000000000001",
                    "responseMessageId": "response-1",
                    "clientMessageId": "client-1",
                    "content": "hello",
                },
            },
        ),
        _frame(2, {"type": "text-delta", "delta": "answer"}),
    ]
    renderer = SessionReadRenderingMixin()
    renderer._session_events_repo = SimpleNamespace(
        list_frames=AsyncMock(return_value=frames)
    )
    renderer._message_view = SimpleNamespace(
        get_assistant_message_for_turn=AsyncMock(return_value=None)
    )
    renderer._interaction_snapshots_repo = SimpleNamespace(
        get_active_interaction=AsyncMock(return_value=None)
    )

    overlay = await renderer._build_active_turn_overlay_message(
        "session-1",
        turn_id="turn-1",
        rows=[],
        session={
                "session_id": "session-1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
                "user_id": "owner",
        },
        snapshot={"conversation_state": "STREAMING"},
    )

    assert overlay is not None
    assert [message["role"] for message in overlay["__engine_fifo_messages"]] == [
        "user",
        "assistant",
    ]


async def test_active_overlay_appends_to_the_latest_native_message_for_one_turn() -> None:
    register_engine_adapter("claude_code", ClaudeCodeEngineAdapter())
    frames = [_frame(4, {"type": "text-delta", "delta": " continued"})]
    list_frames = AsyncMock(return_value=frames)
    renderer = SessionReadRenderingMixin()
    renderer._session_events_repo = SimpleNamespace(list_frames=list_frames)
    renderer._message_view = SimpleNamespace(
        get_assistant_message_for_turn=AsyncMock(return_value=None)
    )
    renderer._interaction_snapshots_repo = SimpleNamespace(
        get_active_interaction=AsyncMock(return_value=None)
    )

    overlay = await renderer._build_active_turn_overlay_message(
        "session-1",
        turn_id="platform-turn-1",
        rows=[
            {
                "session_id": "session-1",
                "message_id": "response-1",
                "message_seq": 1,
                "turn_id": "platform-turn-1",
                "role": "assistant",
                "content": "first",
                "blocks": [{"type": "text", "text": "first"}],
                "source_frame_seq_applied": 2,
            },
            {
                "session_id": "session-1",
                "message_id": "response-2",
                "message_seq": 3,
                "turn_id": "platform-turn-1",
                "role": "assistant",
                "content": "second",
                "blocks": [{"type": "text", "text": "second"}],
                "source_frame_seq_applied": 3,
            },
        ],
        session={
            "session_id": "session-1",
            "session_kind": "agent_chat",
            "engine_kind": "claude_code",
            "user_id": "owner",
        },
        snapshot={"conversation_state": "STREAMING"},
    )

    assert overlay is not None
    assert overlay["message_id"] == "response-2"
    assert overlay["turn_id"] == "platform-turn-1"
    assert overlay["content"] == "second continued"
    list_frames.assert_any_await(
        "session-1",
        turn_id="platform-turn-1",
        after_seq=3,
        limit=500,
    )
