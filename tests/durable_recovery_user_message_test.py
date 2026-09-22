from __future__ import annotations

from astrabox.core.service.orchestrator.session_message_view import (
    project_session_messages,
)


def _event(seq: int, event_type: str, payload: dict) -> dict:
    return {
        "session_id": "session-1",
        "event_seq": seq,
        "occurred_at": f"2026-08-11T00:00:{seq:02d}+00:00",
        "turn_id": "platform-turn-1",
        "event_type": event_type,
        "payload": payload,
    }


def test_recovery_does_not_publish_an_unconsumed_active_input() -> None:
    accepted = _event(
        13,
        "command.accepted",
        {
            "command_type": "StartTurn",
            "author_user_id": "user-1",
            "client_message_id": "client-1",
            "content": "hello",
            "input_id": "sdk-input-1",
        },
    )

    assert project_session_messages(events=[accepted], frames=[]) == []


def test_recovery_keeps_platform_turn_and_sdk_message_id_separate() -> None:
    accepted = _event(
        13,
        "command.accepted",
        {
            "command_type": "StartTurn",
            "author_user_id": "user-1",
            "client_message_id": "client-1",
            "content": "hello",
            "input_id": "sdk-input-1",
        },
    )
    consumed = _event(
        15,
        "input.consumed",
        {
            "input_id": "sdk-input-1",
            "response_message_id": "response-1",
            "client_message_id": "client-1",
            "content": "hello",
        },
    )

    messages = project_session_messages(
        events=[accepted, consumed],
        frames=[],
        user_id="user-1",
    )

    assert messages == [
        {
            "session_id": "session-1",
            "message_id": "sdk-input-1:user",
            "message_seq": 15,
            "turn_id": "platform-turn-1",
            "role": "user",
            "user_id": "user-1",
            "client_message_id": "client-1",
            "content": "hello",
            "blocks": [],
            "source_event_seq_applied": 15,
            "created_at": "2026-08-11T00:00:15+00:00",
        }
    ]
