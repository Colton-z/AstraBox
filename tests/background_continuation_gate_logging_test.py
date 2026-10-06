"""The gates in front of materializing a held background manifest.

A completed child materializes from its own projection without waiting for a
parent reply, and a malformed manifest row is refused before any repository is
touched.
"""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401
from astrabox.core.service.orchestrator.session_kernel.service_mixins.background_continuation import (
    BackgroundContinuationMixin,
)

_OPENED_EVENT: dict[str, Any] = {
    "session_id": "sess-1",
    "turn_id": "turn-1",
    "event_seq": 11,
    "payload": {"control_ids": ["task_a"]},
}


class _Snapshots:
    def __init__(self, state: str | None) -> None:
        self._state = state

    async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        return None if self._state is None else {"conversation_state": self._state}


class _Journal:
    async def list_events(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class _Harness(BackgroundContinuationMixin):
    """Only what the gates ahead of the first failure actually touch.

    Anything past the gate under test is absent rather than stubbed, so a
    silently-widened path fails here instead of being answered by a double.
    """

    def __init__(
        self,
        *,
        conversation_state: str | None = "IDLE",
    ) -> None:
        self._session_snapshots_repo = _Snapshots(conversation_state)
        self._sessions_repo = SimpleNamespace(get_session=AsyncMock(return_value=None))
        self._session_events_repo = _Journal()


async def _run(harness: _Harness, opened_event: dict[str, Any] | None = None) -> bool:
    return await harness._materialize_background_continuation_event(
        dict(opened_event or _OPENED_EVENT)
    )


async def test_a_completed_child_materializes_without_a_parent_reply() -> None:
    harness = _Harness()
    harness._sessions_repo.get_session.return_value = {
        "session_id": "sess-1", "session_kind": "agent_chat", "engine_kind": "claude_code",
    }
    harness._message_view = SimpleNamespace(get_assistant_message_for_turn=AsyncMock(
        side_effect=AssertionError("a lost parent's message is not child completion authority"),
    ))
    projection = {"blocks": [{"type": "subagent", "engine_ref": "native-child"}]}
    harness._collect_background_continuation_projection = AsyncMock(return_value=projection)
    claim = AsyncMock(return_value=({"event_seq": 12}, True))
    harness._session_events_repo.try_claim_event = claim
    assert await _run(harness) is True
    assert claim.call_args.args[0]["payload"]["blocks"] == projection["blocks"]
    harness._message_view.get_assistant_message_for_turn.assert_not_awaited()


async def test_a_malformed_manifest_row_says_so_without_touching_a_repository() -> None:
    """The first gate runs before any repository, so the harness has none."""
    harness = BackgroundContinuationMixin()

    result = await harness._materialize_background_continuation_event(
        {"session_id": "", "turn_id": "", "event_seq": 3, "payload": None}
    )

    assert result is False
