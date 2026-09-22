"""A turn is in flight only while something is flying it.

When the bound sandbox dies out of band the platform answers the next request
with SANDBOX_GONE and the words "a replacement is being prepared — retry". The
retry asks for recovery, and recovery read the snapshot, saw a conversation
still marked RUNNING, and refused: "session state is PROCESSING, recovery only
applies to TERMINATED or READY sessions".

Nothing was going to move that conversation on. The runtime that would have
closed the turn went with the box, so PROCESSING described a dead process — and
the platform was refusing, on those grounds, the very retry it had just asked
for. Recovery is how a conversation reaches a runtime that can judge its own
turn; whether the turn continues stays that runtime's call, and this only stops
the platform from blocking the door.
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle import recover


class _Snapshots:
    def __init__(self, snapshot: dict[str, Any] | None) -> None:
        self._snapshot = snapshot

    async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        return self._snapshot


class _Interactions:
    async def get_active_interaction(self, session_id: str) -> dict[str, Any] | None:
        return None


def _derive(session: dict[str, Any], snapshot: dict[str, Any] | None) -> str:
    worker = recover._RecoverSessionMixin()
    worker._session_snapshots_repo = _Snapshots(snapshot)  # type: ignore[attr-defined]
    worker._interaction_snapshots_repo = _Interactions()  # type: ignore[attr-defined]
    return asyncio.run(
        worker._derive_effective_session_state_for_recover(
            session_id="s1", session=session
        )
    )


LIVE_TURN = {"conversation_state": "PROCESSING"}
STREAMING_TURN = {"conversation_state": "STREAMING"}
RUNNING_TERMINAL = {"terminal_state": "RUNNING"}


def test_a_live_turn_on_a_live_box_still_blocks_recovery() -> None:
    """The guard is not being removed: a turn that can still finish keeps it."""
    for snapshot in (LIVE_TURN, STREAMING_TURN, RUNNING_TERMINAL):
        assert _derive({"state": "READY"}, snapshot) == "PROCESSING", snapshot
        assert (
            _derive({"state": "READY", "runtime_unavailable": False}, snapshot)
            == "PROCESSING"
        ), snapshot


def test_the_same_turn_admits_recovery_once_its_box_is_gone() -> None:
    """`runtime_unavailable` is what the gone-sandbox path stamps on the row.

    Same snapshot, same conversation: the only difference is that the provider
    has reported the bound sandbox absent, which is exactly when the retry the
    platform asked for arrives.
    """
    for snapshot in (LIVE_TURN, STREAMING_TURN, RUNNING_TERMINAL):
        assert (
            _derive({"state": "READY", "runtime_unavailable": True}, snapshot)
            == "READY"
        ), snapshot


def test_a_gone_box_does_not_override_the_states_that_are_not_about_a_turn() -> None:
    """Lifecycle and interaction readings are unrelated to the runtime's health.

    A conversation being created, terminated, deleted, interrupting, or waiting
    on a person is in that state whether or not a sandbox answers, and recovery
    must keep treating them as it did.
    """
    gone = {"state": "READY", "runtime_unavailable": True}
    assert _derive(gone, {"session_lifecycle_state": "CREATING"}) == "CREATING"
    assert _derive(gone, {"session_lifecycle_state": "DELETED"}) == "DELETED"
    assert _derive(gone, {"conversation_state": "INTERRUPTING"}) == "INTERRUPTING"
    assert (
        _derive(gone, {"conversation_state": "WAITING_FOR_INTERACTION"})
        == "WAITING_INPUT"
    )
    # TERMINATED was already an accepted state for recovery; it stays one.
    assert _derive(gone, {"session_lifecycle_state": "TERMINATED"}) == "TERMINATED"
