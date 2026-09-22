from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_journal,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
    _BridgeRunState,
)


class _Journal:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def append_event(self, row: dict[str, Any]) -> dict[str, Any]:
        self.rows.append(row)
        return row


async def test_private_engine_diagnostic_uses_a_non_browser_channel() -> None:
    journal = _Journal()
    worker = SimpleNamespace(_session_events_repo=journal)
    state = _BridgeRunState(effective_turn_id="turn-1")
    ctx = SimpleNamespace(
        session_id="session-1",
        command_id="command-1",
        correlation_id="correlation-1",
    )

    await bridge_journal._append_engine_diagnostic(
        worker,
        state,
        ctx,
        {
            "engine_kind": "deepseek_harness",
            "engine_turn_id": "native-turn-private",
            "event_type": "deepseek_harness.sdk",
            "subtype": "telemetry.added",
            "raw": {"nativeSessionId": "native-session-private"},
        },
    )

    assert journal.rows == [
        {
            "session_id": "session-1",
            "channel": "engine",
            "turn_id": "turn-1",
            "event_type": "engine.diagnostic",
            "causation_id": "command-1",
            "correlation_id": "correlation-1",
            "payload": {
                "engine_kind": "deepseek_harness",
                "engine_turn_id": "native-turn-private",
                "event_type": "deepseek_harness.sdk",
                "subtype": "telemetry.added",
                "raw": {"nativeSessionId": "native-session-private"},
            },
        }
    ]


async def test_malformed_private_diagnostic_fails_before_persistence() -> None:
    journal = _Journal()
    with pytest.raises(RuntimeError, match="malformed"):
        await bridge_journal._append_engine_diagnostic(
            SimpleNamespace(_session_events_repo=journal),
            _BridgeRunState(effective_turn_id="turn-1"),
            SimpleNamespace(
                session_id="session-1",
                command_id="command-1",
                correlation_id="correlation-1",
            ),
            {"engine_kind": "assistant", "event_type": "hermes.gateway"},
        )
    assert journal.rows == []
