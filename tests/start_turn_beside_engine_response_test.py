"""A turn a user starts beside an engine-owned response runs as its own turn.

Claude Code answers a background task's completion on its own, and the platform
publishes that response through PlatformResidentOutputSink, which holds the
conversation slot anchored on the response id. The dispatch gate lets a user's
input start its own turn beside it. The new turn's bridge seeds its engine
anchor from the snapshot its admission wrote, so the admission must not leave
the engine-owned response's anchor there. Otherwise the turn refuses its own
dispatch with "observed conflicting engine turn anchor for the active turn"
and fails while the engine goes on to run it.

The chain below is the real one: the sink's slot write, the StartTurn
admission, and the bridge's anchor observation, on the real repositories.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from astrabox.core.service.orchestrator.engine.platform_events import (
    PlatformResidentOutputSink,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    normalize_current_turn_engine_anchor,
    resident_engine_turn_id,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn import bridge_anchors
from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
    _BridgeRunState,
)
from astrabox.persistence.repository import SessionEventRepository, SessionSnapshotRepository


class _Admission(TurnDispatchStreamingMixin):
    def __init__(self, snapshots: SessionSnapshotRepository) -> None:
        self._session_snapshots_repo = snapshots


@pytest.mark.asyncio
async def test_a_turn_started_beside_an_engine_response_dispatches_under_its_own_anchor() -> None:
    session_id = str(uuid.uuid4())
    snapshots = SessionSnapshotRepository()
    await snapshots.apply_channel_update(
        session_id, channel="conversation", event_seq=1, updates={"conversation_state": "IDLE"}
    )
    sink = PlatformResidentOutputSink(
        session_id,
        broker=None,
        journal_repo=SessionEventRepository(),
        snapshots_repo=snapshots,
    )
    handle = await sink.open_resident_response(
        engine_kind="claude_code",
        response_id="engine-response-1",
        engine_session_key="native-conversation",
        causation_id=f"{session_id}:engine-prompt-1",
        native_message={"type": "user", "uuid": "engine-response-1"},
        runner_sequence=416,
    )
    assert handle is not None and handle.owns_slot
    held = await snapshots.get_snapshot(session_id)
    assert resident_engine_turn_id(held) == "engine-response-1"

    admitted_turn = "platform-turn-2"
    await _Admission(snapshots)._project_command_to_snapshot(
        session_id=session_id,
        turn_id=admitted_turn,
        command_seq=int(held["conversation_event_seq_applied"]) + 1,
        command_id=f"{session_id}:input-2",
    )
    admitted = await snapshots.get_snapshot(session_id)
    assert admitted["current_turn_id"] == admitted_turn
    assert admitted["current_turn_engine_anchor"] is None, (
        "the new turn must not inherit the engine-owned response's anchor"
    )
    assert resident_engine_turn_id(admitted) is None

    # The bridge seeds its anchor from this snapshot, then observes the anchor
    # of the input it dispatched.
    state = _BridgeRunState()
    state.effective_turn_id = admitted_turn
    state.current_turn_engine_anchor = normalize_current_turn_engine_anchor(
        admitted.get("current_turn_engine_anchor")
    )
    observed = await bridge_anchors._observe_engine_anchor(
        SimpleNamespace(_session_snapshots_repo=snapshots),
        state,
        SimpleNamespace(session_id=session_id),
        {
            "engine_kind": "claude_code",
            "engine_turn_id": f"{session_id}:input-2",
            "engine_session_key": "native-conversation",
        },
    )
    assert observed["engine_turn_id"] == f"{session_id}:input-2"
    dispatched = await snapshots.get_snapshot(session_id)
    assert dispatched["current_turn_engine_anchor"]["engine_turn_id"] == f"{session_id}:input-2"
