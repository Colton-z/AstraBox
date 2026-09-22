"""The platform preserves optional engine-declared interaction ownership.

A preceding tool frame is not evidence that an independent native dialog
belongs to that tool. Only the interaction's declared binding is persisted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from astrabox.core.service.orchestrator.engine.emissions import (
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine_turn import iter_engine_client_events


class _GateEngineClient:
    """Emits a tool call, then a gate that omits its tool id."""

    def __init__(self, *, gate_tool_use_id: str = "") -> None:
        self._gate_tool_use_id = gate_tool_use_id

    @property
    def active_receipt(self) -> Any | None:
        return SimpleNamespace(
            engine_turn_id="engine-turn-1",
            engine_session_key="",
            input_id=None,
            input_consumed=True,
        )

    @property
    def engine_session_key(self) -> str | None:
        return "native-session-1"

    async def iter_turn_events(self, receipt: Any):
        yield emission_from_translated_frame(
            {"type": "tool-input-available", "toolCallId": "toolu_from_stream"}
        )
        yield emission_from_translated_frame(
            {
                "type": "interaction.request",
                "interactionId": "gate-1",
                "payload": {
                    "tool_name": "Bash",
                    "presentation": "tool_approval",
                    "prompt": "Allow Bash to continue?",
                    "raw_input": {"command": "ls"},
                    "tool_use_id": self._gate_tool_use_id,
                },
            }
        )


class _Repo:
    async def update_session(self, session_id: str, updates: dict[str, Any]) -> None:
        return None


async def _pending(**kwargs: Any) -> dict[str, Any]:
    """The durable record the seam built for the gate this client raised."""
    runtime = SimpleNamespace(
        lock=asyncio.Lock(),
        current_task=None,
        engine_client=_GateEngineClient(**kwargs),
        engine_kind="claude_code",
        sandbox_id="box-1",
        sandbox=None,
        interrupting=False,
        conversation_bound=True,
    )

    records: list[dict[str, Any]] = []
    async for event in iter_engine_client_events(
        session={"session_id": "s-1", "user_id": "u", "sandbox_id": "box-1"},
        session_id="s-1",
        effective_content="",
        turn_id="t-1",
        runtime=runtime,
        interaction_permission_mode=None,
        on_query_committed=None,
        emit_timing=lambda *a, **k: None,
        client_message_id=None,
        sessions_repo=_Repo(),  # type: ignore[arg-type]
        broker=SimpleNamespace(publish=lambda *a, **k: None),  # type: ignore[arg-type]
        answer_continuation=True,
        parked_engine_anchor={"engine_turn_id": "engine-turn-1"},
    ):
        if event.get("type") == "pending_interaction":
            records.append(event["pending_interaction"])
    assert len(records) == 1, f"expected exactly one gate record, got {records}"
    return records[0]


async def test_a_gate_without_an_id_stays_independent_of_the_preceding_call() -> None:
    record = await _pending()
    assert record.get("tool_call_id") is None


async def test_a_gate_that_carries_its_own_id_keeps_it() -> None:
    record = await _pending(gate_tool_use_id="toolu_from_gate")
    assert record.get("tool_call_id") == "toolu_from_gate"
