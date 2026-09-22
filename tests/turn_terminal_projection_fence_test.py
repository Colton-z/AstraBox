"""A same-turn worker replacement cannot fence out the durable terminal.

A parked turn can acquire a new process-local runtime and continuation worker
while its original worker is finishing. The replacement advances the snapshot
watermark and worker-command field but keeps the same turn id. A terminal frame
already durable for that turn must still become the snapshot's terminal proof;
only a different turn may fence it out.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_terminal,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._fencing import (
    _TurnFencedOut,
)

_SESSION = "session-1"
_TURN = "turn-1"
_COMMAND = "command-original"
_WATERMARK = "conversation_event_seq_applied"


class _Snapshots:
    def __init__(self, *, current_turn_id: str = _TURN) -> None:
        self.doc: dict[str, Any] = {
            "session_id": _SESSION,
            "conversation_state": "WAITING_FOR_INTERACTION",
            "current_turn_id": current_turn_id,
            "current_turn_worker_command_id": _COMMAND,
            _WATERMARK: 10,
            "last_turn_terminal_frame": None,
        }

    def replace_parked_worker(self) -> None:
        # Presence re-attaches the runtime; the continuation/replayed bridge is
        # the part that advances these projection fields. The turn identity is
        # deliberately unchanged.
        self.doc.update(
            {
                "conversation_state": "STREAMING",
                "current_turn_worker_command_id": "command-reattached",
                _WATERMARK: 12,
            }
        )

    async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        assert session_id == _SESSION
        return dict(self.doc)

    async def apply_channel_update(
        self,
        session_id: str,
        *,
        channel: str,
        event_seq: int,
        updates: dict[str, Any],
        extra_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        # The former terminal path: the replacement's newer watermark rejects
        # the original terminal event even though both belong to this turn.
        assert session_id == _SESSION
        assert channel == "conversation"
        if event_seq <= int(self.doc[_WATERMARK]):
            return None
        if extra_filter and any(
            self.doc.get(key) != value for key, value in extra_filter.items()
        ):
            return None
        self.doc.update(updates)
        self.doc[_WATERMARK] = event_seq
        return dict(self.doc)

    async def force_update_fields(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        extra_filter: dict[str, Any] | None = None,
    ) -> bool:
        assert session_id == _SESSION
        if extra_filter and any(
            self.doc.get(key) != value for key, value in extra_filter.items()
        ):
            return False
        self.doc.update(updates)
        return True


class _Journal:
    async def list_events(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert session_id == _SESSION
        assert kwargs["causation_id"] == _COMMAND
        return [
            {
                "session_id": _SESSION,
                "channel": "conversation",
                "turn_id": _TURN,
                "event_type": "turn.completed",
                "causation_id": _COMMAND,
                # The original terminal was journaled before the replacement
                # bridge advanced the snapshot watermark to 12.
                "event_seq": 11,
                "payload": {"command_id": _COMMAND},
            }
        ]

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"terminal event should already exist: {event}")


def _state() -> Any:
    return SimpleNamespace(
        effective_turn_id=_TURN,
        last_result_data=None,
        dispatch_confirmed=True,
        persisted_current_turn_remote_anchor=None,
        persisted_current_turn_engine_anchor=None,
        current_turn_engine_anchor=None,
        last_error_text="",
        last_assistant_text="",
        accumulated_tool_results={},
        accumulated_thinking_parts=[],
        last_event_seq=10,
        turn_settled=False,
    )


def _ctx(record_manifest: AsyncMock) -> Any:
    return SimpleNamespace(
        session_id=_SESSION,
        command_id=_COMMAND,
        correlation_id="correlation-1",
        user=SimpleNamespace(user_id="user-1"),
        build_terminal_assistant_blocks=lambda: [],
        record_background_task_manifest_if_needed=record_manifest,
    )


def _worker(snapshots: _Snapshots) -> Any:
    return SimpleNamespace(
        _session_snapshots_repo=snapshots,
        _session_events_repo=_Journal(),
        _sessions_repo=SimpleNamespace(update_session=AsyncMock()),
        _interaction_snapshots_repo=SimpleNamespace(),
        _session_title_service=None,
    )


def _finish_proof() -> dict[str, Any]:
    return {
        "turn_id": _TURN,
        "command_id": _COMMAND,
        "frame_seq": 7,
        "type": "finish",
        "finish_reason": "stop",
    }


@pytest.mark.asyncio
async def test_parked_turn_terminal_survives_same_turn_worker_replacement() -> None:
    snapshots = _Snapshots()
    snapshots.replace_parked_worker()
    record_manifest = AsyncMock()
    state = _state()

    await bridge_terminal._project_turn_terminal(
        _worker(snapshots),
        state,
        _ctx(record_manifest),
        SessionState.READY.value,
        terminal_frame=_finish_proof(),
    )

    assert snapshots.doc["current_turn_id"] is None
    assert snapshots.doc["current_turn_worker_command_id"] is None
    assert snapshots.doc["last_turn_id"] == _TURN
    assert snapshots.doc["last_turn_status"] == "COMPLETED"
    assert snapshots.doc["last_turn_terminal_frame"] == _finish_proof()
    assert snapshots.doc[_WATERMARK] == 12, "terminal commit must not roll back the newer watermark"
    assert state.turn_settled is True
    record_manifest.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_cannot_clear_a_different_current_turn() -> None:
    snapshots = _Snapshots(current_turn_id="turn-new")
    record_manifest = AsyncMock()

    with pytest.raises(_TurnFencedOut):
        await bridge_terminal._project_turn_terminal(
            _worker(snapshots),
            _state(),
            _ctx(record_manifest),
            SessionState.READY.value,
            terminal_frame=_finish_proof(),
        )

    assert snapshots.doc["current_turn_id"] == "turn-new"
    assert snapshots.doc["last_turn_terminal_frame"] is None
    record_manifest.assert_not_awaited()
