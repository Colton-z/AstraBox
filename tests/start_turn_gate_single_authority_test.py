"""The start-turn gate judges turn liveness from the snapshot alone.

The session DOC's state is a projection mirror written after the terminal
snapshot CAS. A send can therefore be admissible from the settled snapshot
while the document still carries the active state. Consulting both would reject
that send from stale data, so the snapshot is the sole authority.
"""

from __future__ import annotations

import unittest
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)

_SETTLED_SNAPSHOT = {
    "conversation_state": "IDLE",
    "last_turn_status": "COMPLETED",
    "last_turn_id": "t-1",
    "last_turn_command_id": "c-1",
    "last_turn_terminal_frame": {
        "turn_id": "t-1",
        "command_id": "c-1",
        "frame_seq": 41,
        "type": "finish",
        "finish_reason": "stop",
    },
}


class _FakeSnapshots:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return dict(self.snapshot)


class _FakeInteractions:
    async def get_active_interaction(self, session_id: str) -> None:
        return None


class _Harness(TurnDispatchStreamingMixin):
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._session_snapshots_repo = _FakeSnapshots(snapshot)
        self._interaction_snapshots_repo = _FakeInteractions()


class StartTurnGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_settled_snapshot_admits_despite_stale_doc_mirror(self) -> None:
        h = _Harness(_SETTLED_SNAPSHOT)
        # The doc mirror still says PROCESSING — the snapshot has settled, so
        # the send is admissible. This is the exact no-gap race the e2e
        # launcher spec pins end to end.
        await h._ensure_start_turn_allowed(
            "s-1", session={"state": "PROCESSING", "session_id": "s-1"}
        )

    async def test_mid_turn_snapshot_still_rejects(self) -> None:
        h = _Harness({"conversation_state": "STREAMING", "current_turn_id": "t-1"})
        with pytest.raises(APIError) as excinfo:
            await h._ensure_start_turn_allowed(
                "s-1", session={"state": "READY", "session_id": "s-1"}
            )
        assert excinfo.value.code == "SESSION_BUSY"


if __name__ == "__main__":
    unittest.main()
