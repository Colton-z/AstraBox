"""An operator's session page carries both answers to "what state is this?".

`state` is the session's lifecycle, read from the `sessions` row: is the
session alive and able to accept work. `conversation_state` is what a user is
shown, derived from the conversation snapshot: does the last turn have proof
it finished.

They answer different questions, so they can disagree without either being
wrong. Measured: a turn whose terminal frame was lost left the session READY
(the box was fine) while every user-facing read said PROCESSING (no proof the
turn ended). Diagnosing that meant comparing two endpoints and guessing
whether the difference was a bug or a definition; the operator page now shows
both, so the difference is the diagnostic rather than the puzzle.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.admin_service import AdminService


def _service(snapshot: dict[str, Any] | None) -> AdminService:
    class _Sessions:
        async def get_session(self, session_id: str) -> dict[str, Any] | None:
            if session_id != "sess-1":
                return None
            return {
                "session_id": "sess-1",
                "agent_id": "agent-1",
                "state": "READY",
                "user_id": "owner",
            }

    class _Snapshots:
        async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
            _ = session_id
            return snapshot

    class _RuntimeManager:
        @staticmethod
        def get_runtime(session_id: str, *, sandbox_id: str | None = None) -> None:
            _ = (session_id, sandbox_id)
            return None

    class _AgentConfig:
        @staticmethod
        async def resolve_session_harness(
            session: dict[str, Any], *, require_enabled_environment: bool = True
        ) -> None:
            _ = session, require_enabled_environment
            return None

    service = AdminService(
        sessions_repo=_Sessions(),  # type: ignore[arg-type]
        message_view=object(),  # type: ignore[arg-type]
        session_events_repo=object(),  # type: ignore[arg-type]
        agent_config=_AgentConfig(),  # type: ignore[arg-type]
        runtime_manager=_RuntimeManager(),  # type: ignore[arg-type]
        sanitize_session=lambda doc: dict(doc),
    )
    service._session_snapshots_repo = _Snapshots()  # type: ignore[assignment]
    return service


def test_a_lost_terminal_shows_up_as_ready_and_processing_together() -> None:
    """The field case: the box was fine, the turn had no proof it ended."""
    conversation_state = _service(None)._derive_conversation_state(
        {
            "session_lifecycle_state": "ACTIVE",
            "conversation_state": "IDLE",
            # COMPLETED with no matching finish frame — the terminal was lost.
            "last_turn_status": "COMPLETED",
            "last_turn_id": "turn-1",
            "last_turn_terminal_frame": None,
        }
    )

    assert conversation_state == "PROCESSING"


def test_the_two_agree_once_the_turn_has_its_proof() -> None:
    conversation_state = _service(None)._derive_conversation_state(
        {
            "session_lifecycle_state": "ACTIVE",
            "conversation_state": "IDLE",
            "last_turn_status": "COMPLETED",
            "last_turn_id": "turn-1",
            "last_turn_command_id": "cmd-1",
            "last_turn_terminal_frame": {
                "type": "finish",
                "turn_id": "turn-1",
                "command_id": "cmd-1",
                "frame_seq": 7,
                "finish_reason": "stop",
            },
        }
    )

    assert conversation_state == "READY"


def test_a_session_with_no_snapshot_reports_no_conversation_state() -> None:
    """Absent is not the same as agreeing; a default would invent agreement."""
    assert _service(None)._derive_conversation_state(None) is None


def test_admin_runtime_identity_does_not_invent_claude_for_corrupt_state() -> None:
    versions = AdminService._persisted_runtime_versions(
        {"runtime_identity": {"status": "failed"}}
    )

    assert versions["engine_kind"] is None
    assert versions["runtime_identity_status"] == "failed"


@pytest.mark.asyncio
async def test_the_operator_page_carries_both_values() -> None:
    service = _service(
        {
            "session_lifecycle_state": "ACTIVE",
            "conversation_state": "IDLE",
            "last_turn_status": "COMPLETED",
            "last_turn_id": "turn-1",
            "last_turn_terminal_frame": None,
        }
    )

    async def _allow(_user: Any, _session: dict[str, Any]) -> None:
        return None

    service._assert_can_manage_session = _allow  # type: ignore[method-assign]
    service._apply_user_display = lambda *a, **k: None  # type: ignore[method-assign]
    service._nick_map_for_sessions = _nick_map  # type: ignore[method-assign]

    detail = await service.admin_get_session_detail(None, "sess-1")

    assert detail["state"] == "READY", "the lifecycle answer, from the sessions row"
    assert detail["conversation_state"] == "PROCESSING", "the user-facing answer"


async def _nick_map(_sessions: list[dict[str, Any]]) -> dict[str, str]:
    return {}
