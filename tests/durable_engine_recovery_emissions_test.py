"""Durable engine reconnect preserves the typed engine seam.

The resident reconnect path runs after the ordinary turn worker is gone.  It
must therefore persist the same facts as the live path without turning private
engine data back into browser frames or re-interpreting vendor status words.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from astrabox.core.service.orchestrator.engine.emissions import (
    BackgroundTasksOpened,
    ChildResourceFact,
    InteractionRequested,
    PublicUIFrame,
    ResponseCompleted,
    TurnTerminal,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    public_engine_frame_payload,
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins import (
    durable_recovery_assistant,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_checkpoint import (
    DurableRecoveryMaterializationMixin,
)


class _SessionEvents:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    async def list_frames(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        after_seq: int = -1,
        limit: int = 500,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.frames
            if row["session_id"] == session_id
            and int(row["frame_seq"]) > after_seq
            and (turn_id is None or row.get("turn_id") == turn_id)
        ]
        return rows[:limit]

    async def allocate_session_frame_seq(self, _session_id: str, *, count: int) -> int:
        del count
        return len(self.frames)

    async def get_next_session_frame_seq(self, _session_id: str) -> int:
        return len(self.frames)

    async def append_frames(self, frames: list[dict[str, Any]]) -> None:
        self.frames.extend(dict(frame) for frame in frames)

    async def append_frame(self, frame: dict[str, Any]) -> None:
        self.frames.append(dict(frame))

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        stored = {**event, "event_seq": len(self.events) + 1}
        self.events.append(stored)
        return stored

    async def try_claim_event(
        self, event: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        for existing in self.events:
            if (
                existing.get("event_type") == event.get("event_type")
                and existing.get("causation_id") == event.get("causation_id")
            ):
                return dict(existing), False
        return await self.append_event(event), True


class _Snapshots:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = dict(snapshot)

    async def get_snapshot(self, _session_id: str) -> dict[str, Any]:
        return dict(self.snapshot)

    async def force_update_fields(
        self,
        _session_id: str,
        updates: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.snapshot.update(updates)
        return dict(self.snapshot)

    async def apply_channel_update(
        self,
        _session_id: str,
        *,
        updates: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.snapshot.update(updates)
        return dict(self.snapshot)


class _Sessions:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def update_session(
        self, _session_id: str, updates: dict[str, Any]
    ) -> None:
        self.updates.append(dict(updates))


class _Interactions:
    def __init__(self) -> None:
        self.deactivated_turns: list[tuple[str, str]] = []
        self.opened: list[dict[str, Any]] = []

    async def deactivate_active_for_turn(self, session_id: str, turn_id: str) -> int:
        self.deactivated_turns.append((session_id, turn_id))
        return 1

    async def project_open_interaction(self, **kwargs: Any) -> dict[str, Any]:
        projected = dict(kwargs["updates"])
        self.opened.append(projected)
        return projected

    async def get_interaction(
        self, _session_id: str, _interaction_id: str
    ) -> dict[str, Any] | None:
        return None

    async def project_answered_interaction(self, **_kwargs: Any) -> None:
        raise AssertionError("a fresh recovered interaction cannot already be answered")


class _ReconnectClient:
    def __init__(self, emissions: list[Any]) -> None:
        self._emissions = emissions

    async def _events(self):
        for emission in self._emissions:
            yield emission

    def iter_reconnected_turn_events(self, **_kwargs: Any):
        return self._events()


class _Harness(DurableRecoveryMaterializationMixin):
    def __init__(self, emissions: list[Any]) -> None:
        snapshot = {
            "conversation_state": "STREAMING",
            "current_turn_id": "turn-1",
            "current_turn_worker_command_id": "command-1",
            "current_turn_engine_anchor": {
                "engine_kind": "test_engine",
                "engine_turn_id": "native-turn-1",
                "engine_sequence_number": 0,
            },
        }
        self._session_events_repo = _SessionEvents()
        self._session_snapshots_repo = _Snapshots(snapshot)
        self._sessions_repo = _Sessions()
        self._interaction_snapshots_repo = _Interactions()
        runtime = SimpleNamespace(engine_client=_ReconnectClient(emissions))
        self._runtime_manager = SimpleNamespace(
            get_runtime=lambda *_args, **_kwargs: runtime
        )
        self._turn_service = SimpleNamespace()


def _child_fact() -> ChildResourceFact:
    return ChildResourceFact(
        session_scoped_engine_frame(
            {
                "type": "data-child-run",
                "data": {
                    "event": "opened",
                    "engineRef": "native-child-1",
                    "controlRef": "native-control-1",
                    "engineStatus": "running",
                },
                "__engine_sequence_number": 1,
            }
        )
    )


@pytest.mark.asyncio
async def test_reconnect_keeps_child_fact_private_and_cancelled_turn_completed() -> None:
    host = _Harness(
        [
            _child_fact(),
            PublicUIFrame(
                {
                    "type": "text-delta",
                    "id": "text-1",
                    "delta": "partial answer",
                    "__engine_sequence_number": 2,
                }
            ),
            TurnTerminal(
                {
                    "type": "result",
                    "finishReason": "cancelled",
                    "__engine_sequence_number": 3,
                },
                outcome="cancelled",
                finish_reason="cancelled",
                usage={"input_tokens": 7, "output_tokens": 3},
                native_reason="interrupted",
                private_data={"nativeSessionId": "secret-native-session"},
                closes_interaction=True,
            ),
        ]
    )

    with patch.object(
        durable_recovery_assistant,
        "get_engine_adapter",
        return_value=SimpleNamespace(),
    ):
        result = await host._recover_engine_via_anchor(
            session={
                "session_id": "session-1",
                "session_kind": "agent_chat",
                "sandbox_id": "sandbox-1",
                "engine_kind": "test_engine",
            },
            snapshot=host._session_snapshots_repo.snapshot,
        )

    assert result is not None
    assert result["last_turn_status"] == "COMPLETED"
    assert result["last_turn_terminal_reason"] == "interrupted"
    assert host._interaction_snapshots_repo.deactivated_turns == [
        ("session-1", "turn-1")
    ]

    child_docs = [
        frame
        for frame in host._session_events_repo.frames
        if (frame.get("payload") or {}).get("type") == "data-child-run"
    ]
    assert len(child_docs) == 1
    child_doc = child_docs[0]
    assert child_doc["turn_id"] is None
    assert child_doc["scope"] == "session"
    assert public_engine_frame_payload(
        child_doc["payload"],
        frame_seq=child_doc["frame_seq"],
        scope="session",
    ) == {
        "type": "data-child-runs-changed",
        "transient": True,
        "data": {"frameSeq": child_doc["frame_seq"]},
    }

    text_doc = next(
        frame
        for frame in host._session_events_repo.frames
        if (frame.get("payload") or {}).get("type") == "text-delta"
    )
    assert text_doc["payload"]["__engine_public_ui"] is True
    assert "__engine_public_ui" not in (
        public_engine_frame_payload(
            text_doc["payload"],
            frame_seq=text_doc["frame_seq"],
            scope="turn",
        )
        or {}
    )

    public_bytes = repr(host._session_events_repo.frames)
    assert "secret-native-session" not in public_bytes
    diagnostics = [
        event
        for event in host._session_events_repo.events
        if event.get("event_type") == "engine.diagnostic"
    ]
    assert diagnostics[0]["payload"]["raw"] == {
        "nativeSessionId": "secret-native-session"
    }

    result_frames = [
        frame.get("payload")
        for frame in host._session_events_repo.frames
        if (frame.get("payload") or {}).get("type") == "data-result"
    ]
    assert result_frames[-1]["data"]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 3,
    }


@pytest.mark.asyncio
async def test_reconnect_parks_typed_interaction_instead_of_publishing_control_data() -> None:
    host = _Harness(
        [
            PublicUIFrame(
                {
                    "type": "tool-input-available",
                    "toolCallId": "tool-call-1",
                    "toolName": "Bash",
                    "input": {"command": "pwd"},
                    "__engine_sequence_number": 1,
                }
            ),
            InteractionRequested(
                {
                    "type": "interaction.request",
                    "interactionId": "interaction-1",
                    "payload": {},
                    "__engine_sequence_number": 2,
                },
                interaction_id="interaction-1",
                contract={
                    "presentation": "tool_approval",
                    "tool_name": "Bash",
                    "prompt": "Allow this command?",
                    "raw_input": {"command": "pwd"},
                    "tool_use_id": "native-tool-use-1",
                },
            ),
        ]
    )

    with patch.object(
        durable_recovery_assistant,
        "get_engine_adapter",
        return_value=SimpleNamespace(),
    ):
        result = await host._recover_engine_via_anchor(
            session={
                "session_id": "session-1",
                "session_kind": "agent_chat",
                "sandbox_id": "sandbox-1",
                "engine_kind": "test_engine",
                "engine_session_key": "private-native-conversation",
            },
            snapshot=host._session_snapshots_repo.snapshot,
        )

    assert result is not None
    assert result["conversation_state"] == "WAITING_FOR_INTERACTION"
    assert result["active_interaction_id"] == "interaction-1"
    assert result["current_turn_engine_anchor"]["engine_sequence_number"] == 2
    assert host._interaction_snapshots_repo.opened[0]["tool_call_id"] == (
        "native-tool-use-1"
    )

    interaction_doc = next(
        frame
        for frame in host._session_events_repo.frames
        if (frame.get("payload") or {}).get("type") == "data-interaction"
    )
    public = public_engine_frame_payload(
        interaction_doc["payload"],
        frame_seq=interaction_doc["frame_seq"],
        scope="turn",
    )
    assert public is not None
    assert public["data"] == {
        "interaction_id": "interaction-1",
        "turn_id": "turn-1",
        "tool_call_id": "native-tool-use-1",
        "tool_name": "Bash",
        "presentation": "tool_approval",
        "prompt": "Allow this command?",
        "raw_input": {"command": "pwd"},
    }
    assert "engine_session_key" not in repr(public)


@pytest.mark.asyncio
async def test_reconnect_keeps_only_last_fifo_response_and_records_background_manifest() -> None:
    manifest = {
        "transcript_refs": ["private-transcript"],
        "engine_refs": ["private-engine-ref"],
        "transcript_to_engine_ref": {
            "private-transcript": "private-engine-ref"
        },
        "control_to_engine_ref": {"private-control": "private-engine-ref"},
    }
    host = _Harness(
        [
            PublicUIFrame(
                {
                    "type": "text-delta",
                    "id": "text-1",
                    "delta": "first response",
                    "__engine_sequence_number": 1,
                }
            ),
            ResponseCompleted(
                {
                    "type": "response-result",
                    "data": {},
                    "__engine_sequence_number": 2,
                },
                public_data={"usage": {"input_tokens": 1}},
            ),
            PublicUIFrame(
                {
                    "type": "text-delta",
                    "id": "text-2",
                    "delta": "second response",
                    "__engine_sequence_number": 3,
                }
            ),
            BackgroundTasksOpened(
                {
                    "type": "background-tasks-opened",
                    "manifest": manifest,
                    "__engine_sequence_number": 4,
                },
                manifest=manifest,
            ),
            TurnTerminal(
                {
                    "type": "result",
                    "finishReason": "stop",
                    "__engine_sequence_number": 5,
                },
                outcome="completed",
                finish_reason="stop",
                native_reason="completed",
            ),
        ]
    )

    with patch.object(
        durable_recovery_assistant,
        "get_engine_adapter",
        return_value=SimpleNamespace(),
    ):
        await host._recover_engine_via_anchor(
            session={
                "session_id": "session-1",
                "session_kind": "agent_chat",
                "sandbox_id": "sandbox-1",
                "engine_kind": "test_engine",
            },
            snapshot=host._session_snapshots_repo.snapshot,
        )

    terminal_event = next(
        event
        for event in host._session_events_repo.events
        if event.get("event_type") == "turn.completed"
    )
    assert terminal_event["payload"]["assistant_text"] == "second response"
    background_event = next(
        event
        for event in host._session_events_repo.events
        if event.get("event_type") == "turn.background_tasks_opened"
    )
    assert background_event["payload"] == {
        "command_id": "command-1",
        "source": "test_engine_background_task",
        **manifest,
    }
    assert "private-control" not in repr(host._session_events_repo.frames)
