from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.engine.claude_code_runtime import (
    _runner_event_persister,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    DeliveryCoordinator,
    JournalDeliveryOutbox,
    command_input_id,
    confirm_engine_input_consumed,
    input_response_message_id,
    journal_input_rows,
)
from astrabox.core.service.orchestrator.engine.base import EngineInputCommand
from astrabox.core.service.orchestrator.engine.platform_events import (
    PlatformEngineEventSink,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)
from astrabox.core.service.orchestrator.session_message_view import (
    project_session_messages,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.bridge_loop import (
    _confirm_engine_input_consumed as confirm_projected_engine_input_consumed,
)
from astrabox.common.utils.user_context import UserContext


class _Journal:
    def __init__(
        self,
        events: list[dict[str, Any]],
        frames: list[dict[str, Any]] | None = None,
    ) -> None:
        self.events = [deepcopy(event) for event in events]
        self.frames = [deepcopy(frame) for frame in frames or []]

    async def list_frames(
        self,
        session_id: str,
        *,
        after_seq: int = -1,
        limit: int = 500,
        **_filters: Any,
    ) -> list[dict[str, Any]]:
        rows = [
            frame
            for frame in self.frames
            if frame["session_id"] == session_id
            and int(frame["frame_seq"]) > after_seq
        ]
        return deepcopy(sorted(rows, key=lambda row: int(row["frame_seq"]))[:limit])

    async def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        event_type: str | None = None,
        causation_id: str | None = None,
        limit: int = 500,
        **_filters: Any,
    ) -> list[dict[str, Any]]:
        rows = [
            event
            for event in self.events
            if event["session_id"] == session_id
            and int(event["event_seq"]) > after_seq
            and (event_type is None or event["event_type"] == event_type)
            and (
                causation_id is None
                or event.get("causation_id") == causation_id
            )
        ]
        return deepcopy(sorted(rows, key=lambda row: int(row["event_seq"]))[:limit])

    async def try_claim_event(
        self,
        event: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        existing = next(
            (
                row
                for row in self.events
                if row["session_id"] == event["session_id"]
                and row.get("causation_id") == event.get("causation_id")
                and row["event_type"] == event["event_type"]
            ),
            None,
        )
        if existing is not None:
            return deepcopy(existing), False
        claimed = {
            "event_seq": max(
                (int(row["event_seq"]) for row in self.events),
                default=0,
            )
            + 1,
            "occurred_at": "2026-08-13T00:00:02+00:00",
            **deepcopy(event),
        }
        self.events.append(claimed)
        return deepcopy(claimed), True

    async def find_input_command_by_input_id(
        self,
        session_id: str,
        *,
        input_id: str,
    ) -> dict[str, Any] | None:
        return deepcopy(
            next(
                (
                    row
                    for row in self.events
                    if row["session_id"] == session_id
                    and row["event_type"] == "command.accepted"
                    and row.get("payload", {}).get("input_id") == input_id
                ),
                None,
            )
        )


class _Gateway:
    def __init__(
        self,
        journal: _Journal,
        *,
        fail_command_id: str | None = None,
        assert_unmarked: bool = True,
    ) -> None:
        self.journal = journal
        self.fail_command_id = fail_command_id
        self.assert_unmarked = assert_unmarked
        self.commands: list[EngineInputCommand] = []

    async def deliver(self, command: EngineInputCommand) -> None:
        if self.assert_unmarked:
            assert not any(
                row["event_type"] == "input.delivered"
                and row.get("causation_id") == command.command_id
                for row in self.journal.events
            ), "the RunnerLink receipt must precede the durable delivery mark"
        if command.command_id == self.fail_command_id:
            raise ConnectionError("runner link disconnected before input_ack")
        self.commands.append(command)


def _accepted(
    *,
    event_seq: int,
    command_id: str,
    input_id: str,
    content: str,
) -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "channel": "command",
        "turn_id": "turn-1",
        "event_seq": event_seq,
        "event_type": "command.accepted",
        "causation_id": command_id,
        "correlation_id": command_id,
        "payload": {
            "command_type": "StartTurn" if event_seq == 1 else "SubmitInput",
            "client_message_id": f"client-{event_seq}",
            "input_id": input_id,
            "content": content,
        },
    }


def test_command_input_identity_is_platform_owned() -> None:
    payload = _accepted(
        event_seq=1,
        command_id="command-1",
        input_id="sdk-input-1",
        content="hello",
    )["payload"]

    assert command_input_id(payload) == "sdk-input-1"
    payload.pop("input_id")
    assert command_input_id(payload) is None


async def test_delivery_coordinator_feeds_unconsumed_roots_in_journal_fifo() -> None:
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="first",
            ),
            {
                "session_id": "session-1",
                "event_seq": 2,
                "event_type": "turn.started",
            },
            _accepted(
                event_seq=3,
                command_id="command-2",
                input_id="00000000-0000-0000-0000-000000000002",
                content="second",
            ),
        ]
    )
    gateway = _Gateway(journal)

    delivered = await DeliveryCoordinator(
        JournalDeliveryOutbox(journal, session_id="session-1")
    ).deliver_pending("session-1", gateway)

    assert delivered == ["command-1", "command-2"]
    assert [command.command_id for command in gateway.commands] == delivered
    assert [command.sequence for command in gateway.commands] == [1, 3]
    assert [row["state"] for row in await journal_input_rows(journal, "session-1")] == [
        "DELIVERED",
        "DELIVERED",
    ], "input_ack is delivery evidence, not the FIFO dequeue"


def _turn_failed(*, event_seq: int, command_id: str, failure_phase: str) -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "channel": "conversation",
        "turn_id": "turn-1",
        "event_seq": event_seq,
        "event_type": "turn.failed",
        "causation_id": command_id,
        "correlation_id": command_id,
        "payload": {
            "command_id": command_id,
            "final_state": "FAILED",
            "error_text": "turn failed before dispatch",
            "failure_phase": failure_phase,
        },
    }


async def test_a_turn_that_died_before_dispatch_stops_owing_its_input() -> None:
    # Its box died before the engine saw it, so no turn will ever submit this
    # input again — and a turn start requires the FIFO head to be its own
    # command, so leaving it queued refuses every later turn instead of
    # delaying this one.
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="first",
            ),
            _turn_failed(event_seq=2, command_id="command-1", failure_phase="pre_dispatch"),
            _accepted(
                event_seq=3,
                command_id="command-2",
                input_id="00000000-0000-0000-0000-000000000002",
                content="retry",
            ),
        ]
    )

    rows = await journal_input_rows(journal, "session-1")

    assert [row["command_id"] for row in rows] == ["command-2"], (
        "the retry must be the FIFO head once the dead turn stops owing its input"
    )


async def test_a_dispatched_turn_that_failed_still_owes_its_input() -> None:
    # post_dispatch reached the engine: whether it consumed the input is a
    # question the consumption evidence answers, not one this failure closes.
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="first",
            ),
            _turn_failed(event_seq=2, command_id="command-1", failure_phase="post_dispatch"),
        ]
    )

    rows = await journal_input_rows(journal, "session-1")

    assert [row["command_id"] for row in rows] == ["command-1"]


async def test_engine_enqueue_receipt_cannot_dequeue_the_platform_fifo() -> None:
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="first",
            )
        ]
    )

    await DeliveryCoordinator(
        JournalDeliveryOutbox(journal, session_id="session-1")
    ).deliver_pending("session-1", _Gateway(journal))

    rows = await journal_input_rows(journal, "session-1")
    assert [row["state"] for row in rows] == ["DELIVERED"]
    assert not any(
        row["event_type"] == "input.consumed" for row in journal.events
    ), "adapter ownership is not proof that the engine consumed the FIFO head"


async def test_disconnect_during_feed_keeps_every_unconsumed_root_replayable() -> None:
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="first",
            ),
            _accepted(
                event_seq=2,
                command_id="command-2",
                input_id="00000000-0000-0000-0000-000000000002",
                content="second",
            ),
        ]
    )
    interrupted = _Gateway(journal, fail_command_id="command-2")
    with pytest.raises(ConnectionError, match="disconnected"):
        await DeliveryCoordinator(
            JournalDeliveryOutbox(journal, session_id="session-1")
        ).deliver_pending("session-1", interrupted)

    assert [
        row["command_id"] for row in await journal_input_rows(journal, "session-1")
    ] == ["command-1", "command-2"]

    # A new process cannot know whether the runner survived. It replays the
    # entire accepted-but-unconsumed prefix; command identity makes that safe.
    replay = _Gateway(journal, assert_unmarked=False)
    delivered = await DeliveryCoordinator(
        JournalDeliveryOutbox(journal, session_id="session-1")
    ).deliver_pending("session-1", replay)
    assert delivered == ["command-1", "command-2"]


async def test_prompt_consumption_is_the_only_durable_fifo_dequeue() -> None:
    input_id = "00000000-0000-0000-0000-000000000001"
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id=input_id,
                content="first",
            )
        ]
    )
    gateway = _Gateway(journal)
    await DeliveryCoordinator(
        JournalDeliveryOutbox(journal, session_id="session-1")
    ).deliver_pending("session-1", gateway)
    assert len(await journal_input_rows(journal, "session-1")) == 1

    consumed = await confirm_engine_input_consumed(
        journal,
        session_id="session-1",
        input_id=input_id,
        response_message_id=input_response_message_id(input_id),
        content="first",
    )

    assert consumed["event_type"] == "input.consumed"
    assert consumed["causation_id"] == "command-1"
    assert consumed["payload"]["input_id"] == input_id
    assert await journal_input_rows(journal, "session-1") == [], (
        "without the UserPromptSubmit confirmation the durable FIFO must stay queued"
    )


async def test_runner_hook_event_commits_the_durable_fifo_dequeue() -> None:
    input_id = "00000000-0000-0000-0000-000000000001"
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id=input_id,
                content="first",
            )
        ]
    )
    persist = _runner_event_persister(
        "session-1",
        event_sink=PlatformEngineEventSink("session-1", journal_repo=journal),
    )

    await persist(
        {
            "session_id": "session-1",
            "seq": 2,
            "message_type": "UserMessage",
            "message": {
                "__sdk_type": "UserMessage",
                "content": "first",
                "uuid": input_id,
                "parent_tool_use_id": None,
            },
        }
    )

    assert await journal_input_rows(journal, "session-1") == []


async def test_transformed_slash_command_consumes_and_projects_the_accepted_root() -> None:
    input_id = "00000000-0000-0000-0000-000000000001"
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id=input_id,
                content="/compact",
            )
        ]
    )
    sdk_content = (
        "<command-name>/compact</command-name>\n"
        "<command-message>compact</command-message>\n"
        "<command-args></command-args>"
    )
    frame = {
        "type": "data-input-consumed",
        "data": {
            "inputId": input_id,
            "responseMessageId": input_response_message_id(input_id),
            "content": sdk_content,
        },
    }
    state = SimpleNamespace(last_event_seq=0)
    await confirm_projected_engine_input_consumed(
        SimpleNamespace(
            _session_events_repo=journal,
        ),
        state,
        SimpleNamespace(
            session_id="session-1",
            user=SimpleNamespace(user_id="owner"),
        ),
        frame,
    )

    assert frame["data"] == {
        "inputId": input_id,
        "responseMessageId": input_response_message_id(input_id),
        "clientMessageId": "client-1",
        "content": "/compact",
    }, "the UI projection keeps the operator-authored command, not the SDK envelope"
    consumed = next(row for row in journal.events if row["event_type"] == "input.consumed")
    assert consumed["payload"]["content"] == "/compact"
    assert consumed["payload"]["sdk_content"] == sdk_content
    assert state.last_event_seq == consumed["event_seq"]
    assert await journal_input_rows(journal, "session-1") == []
    projected = project_session_messages(
        events=journal.events,
        frames=[],
        user_id="owner",
    )[0]
    assert projected == {
        "session_id": "session-1",
        "message_id": f"{input_id}:user",
        "message_seq": consumed["event_seq"],
        "turn_id": "turn-1",
        "role": "user",
        "user_id": "owner",
        "client_message_id": "client-1",
        "content": "/compact",
        "blocks": [],
        "source_event_seq_applied": consumed["event_seq"],
        "created_at": consumed["occurred_at"],
    }


async def test_sdk_owned_root_without_the_accepted_uuid_cannot_consume_input() -> None:
    accepted_input_id = "00000000-0000-0000-0000-000000000001"
    sdk_owned_input_id = "00000000-0000-0000-0000-000000000099"
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id=accepted_input_id,
                content="first",
            )
        ]
    )

    with pytest.raises(RuntimeError, match="unknown external input"):
        await confirm_engine_input_consumed(
            journal,
            session_id="session-1",
            input_id=sdk_owned_input_id,
            response_message_id=input_response_message_id(sdk_owned_input_id),
            content="an SDK-owned prompt",
        )

    assert [row["command_id"] for row in await journal_input_rows(journal, "session-1")] == [
        "command-1"
    ]


async def test_consumption_cannot_skip_the_durable_fifo_head() -> None:
    first_input_id = "00000000-0000-0000-0000-000000000001"
    second_input_id = "00000000-0000-0000-0000-000000000002"
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id=first_input_id,
                content="first",
            ),
            _accepted(
                event_seq=2,
                command_id="command-2",
                input_id=second_input_id,
                content="second",
            ),
        ]
    )

    with pytest.raises(RuntimeError, match="durable FIFO head"):
        await confirm_engine_input_consumed(
            journal,
            session_id="session-1",
            input_id=second_input_id,
            response_message_id=input_response_message_id(second_input_id),
            content="second",
        )

    assert [
        row["command_id"] for row in await journal_input_rows(journal, "session-1")
    ] == ["command-1", "command-2"]


async def test_declared_fifo_engine_dispatches_through_native_fifo() -> None:
    service = TurnDispatchStreamingMixin()
    session = {
        "session_id": "session-1",
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "state": "READY",
    }
    receipt = {
        "turn_id": "turn-1",
        "command_id": "command-1",
        "accepted": True,
    }
    user = UserContext(user_id="owner")
    service._must_get_projection_backed_session = AsyncMock(return_value=session)
    service._bound_runtime_lease_expired = lambda _session: False
    service._require_turn_eligible = lambda _session, *, channel: None
    service._get_kernel_session_snapshot = AsyncMock(return_value=None)
    service._dispatch_active_input_queue = AsyncMock(return_value=receipt)
    service._ensure_start_turn_allowed = AsyncMock(
        side_effect=AssertionError("agent_chat must not use legacy StartTurn dispatch")
    )

    result = await service.dispatch_turn_input(
        user,
        "session-1",
        "hello",
        client_message_id="client-1",
    )

    assert result == receipt
    service._dispatch_active_input_queue.assert_awaited_once_with(
        user=user,
        session=session,
        session_id="session-1",
        content="hello",
        content_blocks=None,
        permission_mode=None,
        client_message_id="client-1",
    )
    service._ensure_start_turn_allowed.assert_not_awaited()


async def test_assistant_turn_recovers_its_runtime_subject_before_dispatch() -> None:
    service = TurnDispatchStreamingMixin()
    unavailable = {
        "session_id": "session-1",
        "session_kind": "assistant_chat",
        "engine_kind": "assistant",
        "state": "READY",
        "runtime_unavailable": True,
    }
    ready = {
        **unavailable,
        "runtime_unavailable": False,
        "sandbox_id": "assistant-workspace-sandbox",
    }
    receipt = {
        "turn_id": "turn-1",
        "command_id": "command-1",
        "accepted": True,
    }
    user = UserContext(user_id="owner")
    service._must_get_projection_backed_session = AsyncMock(return_value=unavailable)
    service._bound_runtime_lease_expired = lambda _session: False
    service.recover_session = AsyncMock()
    service._await_runtime_subject_rebuild_ready = AsyncMock(return_value=ready)
    service._require_turn_eligible = lambda _session, *, channel: None
    service._get_kernel_session_snapshot = AsyncMock(return_value=None)
    service._dispatch_active_input_queue = AsyncMock(return_value=receipt)

    result = await service.dispatch_turn_input(user, "session-1", "hello")

    assert result == receipt
    service.recover_session.assert_awaited_once_with(user, "session-1")
    service._await_runtime_subject_rebuild_ready.assert_awaited_once_with(
        user, "session-1"
    )
    service._dispatch_active_input_queue.assert_awaited_once_with(
        user=user,
        session=ready,
        session_id="session-1",
        content="hello",
        content_blocks=None,
        permission_mode=None,
        client_message_id=None,
    )


async def test_start_turn_delivery_retry_restores_its_missing_producer() -> None:
    session_id = "10000000-0000-0000-0000-000000000001"
    client_message_id = "client-1"
    service = TurnDispatchStreamingMixin()
    command_id = service._input_command_id(session_id, client_message_id)
    input_id = service._input_id(session_id, client_message_id)
    service._session_events_repo = SimpleNamespace(
        get_command_event=AsyncMock(
            return_value={
                "session_id": session_id,
                "turn_id": "turn-1",
                "causation_id": command_id,
                "payload": {
                    "command_type": "StartTurn",
                    "client_message_id": client_message_id,
                    "input_id": input_id,
                    "content": "first",
                },
            }
        )
    )
    service._session_snapshots_repo = SimpleNamespace(
        get_snapshot=AsyncMock(
            return_value={
                "conversation_state": "PROCESSING",
                "current_turn_id": "turn-1",
            }
        )
    )
    service._turn_service = SimpleNamespace(
        deliver_pending_inputs=AsyncMock(return_value=[command_id])
    )
    service._sessions_repo = SimpleNamespace(mark_interaction=AsyncMock())
    service._accepted_turn_producers = {}
    wakeups: list[Any] = []

    class _Worker:
        def run(self, wakeup: Any) -> Any:
            wakeups.append(wakeup)
            return wakeup

    class _Producer:
        def add_done_callback(self, _callback: Any) -> None:
            return None

    service._build_turn_worker = _Worker
    service._spawn_background_task = lambda _work, *, name: _Producer()

    receipt = await service._dispatch_active_input_queue(
        user=UserContext(user_id="owner"),
        session={"session_id": session_id},
        session_id=session_id,
        content="first",
        content_blocks=None,
        permission_mode=None,
        client_message_id=client_message_id,
    )

    assert receipt["status"] == "delivered"
    service._sessions_repo.mark_interaction.assert_awaited_once()
    assert [wakeup.command_id for wakeup in wakeups] == [command_id], (
        "a replayed StartTurn still needs the platform worker that drains its "
        "already-delivered resident query"
    )


async def test_midrun_input_is_submit_input_without_another_platform_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "10000000-0000-0000-0000-000000000002"
    client_message_id = "client-2"
    service = TurnDispatchStreamingMixin()
    claimed: list[dict[str, Any]] = []

    async def try_claim_event(event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        claimed.append(deepcopy(event))
        return {
            **deepcopy(event),
            "event_seq": 9,
            "occurred_at": "2026-08-09T00:00:00Z",
        }, True

    service._session_events_repo = SimpleNamespace(
        get_command_event=AsyncMock(return_value=None),
        try_claim_event=try_claim_event,
    )
    service._session_snapshots_repo = SimpleNamespace(
        get_snapshot=AsyncMock(
            return_value={
                "conversation_state": "STREAMING",
                "current_turn_id": "turn-active",
            }
        )
    )
    service._turn_service = SimpleNamespace(
        deliver_pending_inputs=AsyncMock(return_value=["delivered"])
    )
    service._sessions_repo = SimpleNamespace(mark_interaction=AsyncMock())
    service._accepted_turn_producers = {}
    service._ensure_start_turn_allowed = AsyncMock()
    service._spawn_background_task = lambda *_args, **_kwargs: pytest.fail(
        "mid-run SDK input must not spawn a second platform worker"
    )
    monkeypatch.setattr(
        "astrabox.seams.admission.enforce_admission",
        AsyncMock(),
    )

    receipt = await service._dispatch_active_input_queue(
        user=UserContext(user_id="owner"),
        session={"session_id": session_id, "agent_id": "agent-1"},
        session_id=session_id,
        content="during the first response",
        content_blocks=None,
        permission_mode=None,
        client_message_id=client_message_id,
    )

    assert receipt["status"] == "delivered"
    assert claimed[0]["turn_id"] == "turn-active"
    assert claimed[0]["payload"]["command_type"] == "SubmitInput"
    service._ensure_start_turn_allowed.assert_not_awaited()


# ── carrier-loss redelivery ────────────────────────────────────────────────
#
# A consumed input may have no answer when its runtime dies. A replacement
# runtime must not treat the dead carrier's consumption as its own completed
# delivery; a receipt suppresses redelivery only for the matching carrier.

_DEAD_CARRIER = "box-a#iso-1"
_LIVE_CARRIER = "box-b#iso-2"


def _consumed(
    *,
    event_seq: int,
    command_id: str,
    input_id: str,
    content: str,
    consumer_carrier: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "input_id": input_id,
        "response_message_id": input_response_message_id(input_id),
        "client_message_id": "client-1",
        "content": content,
    }
    if consumer_carrier is not None:
        payload["consumer_carrier"] = consumer_carrier
    return {
        "session_id": "session-1",
        "channel": "delivery",
        "turn_id": "turn-1",
        "event_seq": event_seq,
        "event_type": "input.consumed",
        "causation_id": command_id,
        "correlation_id": command_id,
        "payload": payload,
    }


def _orphaned_consumption_journal(
    frames: list[dict[str, Any]] | None = None,
) -> _Journal:
    return _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="answer me",
            ),
            {
                "session_id": "session-1",
                "event_seq": 2,
                "event_type": "input.delivered",
                "causation_id": "command-1",
            },
            _consumed(
                event_seq=3,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="answer me",
                consumer_carrier=_DEAD_CARRIER,
            ),
        ],
        frames=frames,
    )


async def test_a_dead_carriers_consumption_reopens_the_row() -> None:
    journal = _orphaned_consumption_journal()

    rows = await journal_input_rows(
        journal, "session-1", current_carrier=_LIVE_CARRIER
    )

    assert [row["command_id"] for row in rows] == ["command-1"]

    gateway = _Gateway(journal, assert_unmarked=False)
    outbox = JournalDeliveryOutbox(
        journal, session_id="session-1", current_carrier=_LIVE_CARRIER
    )
    delivered = await DeliveryCoordinator(outbox).deliver_pending(
        "session-1", gateway
    )

    assert delivered == ["command-1"]
    assert [command.command_id for command in gateway.commands] == ["command-1"]


async def test_the_living_carriers_consumption_keeps_the_row_closed() -> None:
    journal = _orphaned_consumption_journal()

    rows = await journal_input_rows(
        journal, "session-1", current_carrier=_DEAD_CARRIER
    )

    assert rows == []


async def test_reads_without_a_carrier_keep_the_closed_projection() -> None:
    # Session reads and consumption head checks have no runtime in hand;
    # reopening is the delivery path's judgement alone.
    journal = _orphaned_consumption_journal()

    assert await journal_input_rows(journal, "session-1") == []


async def test_unsigned_consumption_evidence_keeps_the_closed_projection() -> None:
    # A receipt that does not say WHERE it was taken cannot be judged
    # orphaned; it keeps the closed reading it always had.
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="answer me",
            ),
            _consumed(
                event_seq=2,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="answer me",
                consumer_carrier=None,
            ),
        ]
    )

    assert (
        await journal_input_rows(
            journal, "session-1", current_carrier=_LIVE_CARRIER
        )
        == []
    )


async def test_an_answered_input_stays_closed_for_every_later_carrier() -> None:
    # The dead carrier consumed it, but the answer DID stream (terminal on
    # the input's own command). A third carrier must not re-run it.
    journal = _orphaned_consumption_journal(
        frames=[
            {
                "session_id": "session-1",
                "frame_seq": 4,
                "turn_id": "turn-1",
                "command_id": "command-1",
                "payload": {"type": "finish"},
            },
        ]
    )

    rows = await journal_input_rows(
        journal, "session-1", current_carrier=_LIVE_CARRIER
    )

    assert rows == []


async def test_a_carried_answer_also_closes_the_row() -> None:
    # Second answered shape: the input's frames never name its command, so
    # the consumed marker followed by a terminal on the same carrier turn is
    # the answer — including when a redelivery left an earlier, unanswered
    # marker behind on the turn that died.
    journal = _orphaned_consumption_journal(
        frames=[
            {
                "session_id": "session-1",
                "frame_seq": 4,
                "turn_id": "turn-dead",
                "command_id": "carrier-command",
                "payload": {
                    "type": "data-input-consumed",
                    "id": "input-consumed:00000000-0000-0000-0000-000000000001",
                },
            },
            {
                "session_id": "session-1",
                "frame_seq": 5,
                "turn_id": "turn-handoff",
                "command_id": "carrier-command",
                "payload": {
                    "type": "data-input-consumed",
                    "id": "input-consumed:00000000-0000-0000-0000-000000000001",
                },
            },
            {
                "session_id": "session-1",
                "frame_seq": 6,
                "turn_id": "turn-handoff",
                "command_id": "carrier-command",
                "payload": {"type": "finish"},
            },
        ]
    )

    rows = await journal_input_rows(
        journal, "session-1", current_carrier=_LIVE_CARRIER
    )

    assert rows == []


async def test_a_terminal_before_the_consumption_does_not_answer_it() -> None:
    # Order is part of the criterion: a terminal that precedes the marker
    # belongs to the response an interrupt ended, so the row reopens.
    journal = _orphaned_consumption_journal(
        frames=[
            {
                "session_id": "session-1",
                "frame_seq": 4,
                "turn_id": "turn-1",
                "command_id": "other-command",
                "payload": {"type": "finish"},
            },
            {
                "session_id": "session-1",
                "frame_seq": 5,
                "turn_id": "turn-1",
                "command_id": "other-command",
                "payload": {
                    "type": "data-input-consumed",
                    "id": "input-consumed:00000000-0000-0000-0000-000000000001",
                },
            },
        ]
    )

    rows = await journal_input_rows(
        journal, "session-1", current_carrier=_LIVE_CARRIER
    )

    assert [row["command_id"] for row in rows] == ["command-1"]


async def test_confirmation_signs_its_carrier_and_tolerates_a_replacement() -> None:
    journal = _Journal(
        [
            _accepted(
                event_seq=1,
                command_id="command-1",
                input_id="00000000-0000-0000-0000-000000000001",
                content="answer me",
            ),
        ]
    )

    first = await confirm_engine_input_consumed(
        journal,
        session_id="session-1",
        input_id="00000000-0000-0000-0000-000000000001",
        response_message_id=None,
        content="answer me",
        consumer_carrier=_DEAD_CARRIER,
    )
    assert first["payload"]["consumer_carrier"] == _DEAD_CARRIER

    # The replacement carrier re-consumes the redelivered input. The receipt
    # is evidence about WHERE, not part of WHAT was consumed: the original
    # event answers, and no identity collision is raised.
    second = await confirm_engine_input_consumed(
        journal,
        session_id="session-1",
        input_id="00000000-0000-0000-0000-000000000001",
        response_message_id=None,
        content="answer me",
        consumer_carrier=_LIVE_CARRIER,
    )
    assert second["event_seq"] == first["event_seq"]
    assert second["payload"]["consumer_carrier"] == _DEAD_CARRIER


async def test_the_stream_remakes_the_workers_delivery_judgement() -> None:
    """p89's red: the worker judged delivery against the runtime that existed
    at accept time; the stream's own ensure then replaced the box, and the
    stale ``consumption_confirmed=True`` starved the new engine. The stream
    must redeliver against ITS runtime and read the flag from the reopened
    row."""

    from astrabox.core.service.orchestrator.turn_service import TurnService

    journal = _orphaned_consumption_journal()
    gateway = _Gateway(journal, assert_unmarked=False)
    replacement_runtime = SimpleNamespace(
        sandbox_id="box-b", isolated_session_id="iso-2", engine_client=gateway
    )

    class _Service:
        deliver_calls: list[str] = []

        async def deliver_pending_inputs(self, **kwargs: Any) -> Any:
            self.deliver_calls.append(kwargs["requested_command_id"])
            outbox = JournalDeliveryOutbox(
                journal,
                session_id=kwargs["session_id"],
                current_carrier="box-b#iso-2",
            )
            await DeliveryCoordinator(outbox).deliver_pending(
                kwargs["session_id"], gateway
            )
            return replacement_runtime

        pending_input_rows = TurnService.pending_input_rows
        refresh_delivery_for_stream = TurnService.refresh_delivery_for_stream
        _session_events_repo = journal

    stale_command = {
        "command_id": "command-1",
        "session_id": "session-1",
        "sequence": 1,
        "input_id": "00000000-0000-0000-0000-000000000001",
        "content": "answer me",
        "client_message_id": "client-1",
        # The worker computed this against the dead carrier, whose own
        # consumption legitimately closed the row THEN.
        "consumption_confirmed": True,
    }

    refreshed = await _Service().refresh_delivery_for_stream(
        user=UserContext(user_id="owner"),
        session={"session_id": "session-1"},
        session_id="session-1",
        permission_mode=None,
        delivery_command=stale_command,
    )

    # The reopened row was redelivered to the replacement carrier...
    assert [command.command_id for command in gateway.commands] == ["command-1"]
    # ...and the stream's judgement now says the engine has NOT consumed it,
    # so the turn waits for the redelivered input's own consumption.
    assert refreshed["consumption_confirmed"] is False
    assert stale_command["consumption_confirmed"] is True, "input is not mutated"
