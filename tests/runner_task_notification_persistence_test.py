"""A post-Result SDK task terminal survives outside the turn consumer."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401
import pytest
from claude_agent_sdk.types import (
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    UserMessage,
)

from astrabox.core.service.orchestrator.engine.base import ENGINE_MESSAGE_EVENT_TYPE
from astrabox.core.service.orchestrator.engine.claude_code_runtime import (
    _runner_event_persister,
)
from astrabox.core.service.orchestrator.engine.claude_code_background import (
    build_background_task_manifest,
)
from astrabox.core.service.orchestrator.engine.runner_link import RunnerLink
from astrabox.core.service.orchestrator.engine.platform_events import (
    PlatformEngineEventSink,
)
from astrabox.core.service.orchestrator.sandbox_runner import (
    EnvelopeSender,
    HistoryStoreSequence,
    HostLink,
    RunnerSession,
    RunnerWsServer,
    _jsonable,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.background_continuation import (
    BackgroundContinuationMixin,
)
from astrabox.core.service.orchestrator.session_child_run_view import SessionChildRunView


class _JournalRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def try_claim_event(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        for existing in self.events:
            if existing.get("event_type") == event.get("event_type") and existing.get(
                "causation_id"
            ) == event.get("causation_id"):
                return dict(existing), False
        persisted = {"event_seq": len(self.events) + 1, **event}
        self.events.append(persisted)
        return dict(persisted), True

    async def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        channel: str | None = None,
        turn_id: str | None = None,
        event_type: str | None = None,
        event_types: set[str] | frozenset[str] | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return [
            dict(event)
            for event in self.events
            if event.get("session_id") == session_id
            and int(event.get("event_seq") or 0) > after_seq
            and (channel is None or event.get("channel") == channel)
            and (turn_id is None or event.get("turn_id") == turn_id)
            and (event_type is None or event.get("event_type") == event_type)
            and (event_types is None or event.get("event_type") in event_types)
            and (correlation_id is None or event.get("correlation_id") == correlation_id)
            and (causation_id is None or event.get("causation_id") == causation_id)
        ][:limit]

    async def list_frames(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class _SdkSession:
    def __init__(self) -> None:
        self._messages: asyncio.Queue[Any] = asyncio.Queue()

    def emit(self, message: Any) -> None:
        self._messages.put_nowait(message)

    async def connect(self) -> None:
        pass

    async def query(self, prompt: Any) -> None:
        _ = prompt

    async def receive_messages(self):  # noqa: ANN201 - SDK iterator protocol
        while True:
            yield await self._messages.get()

    async def interrupt(self) -> None:
        pass

    async def stop_task(self, task_id: str) -> None:
        _ = task_id

    async def set_permission_mode(self, mode: str) -> None:
        _ = mode

    async def get_server_info(self) -> dict[str, Any]:
        return {}

    async def disconnect(self) -> None:
        pass


@dataclasses.dataclass
class ResultMessage:
    subtype: str = "success"
    session_id: str = "sdk-session"


class _SnapshotsRepo:
    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        assert session_id == "platform-session"
        return {"conversation_state": "IDLE"}


class _MessagesRepo:
    def __init__(self) -> None:
        self.message: dict[str, Any] = {
            "session_id": "platform-session",
            "message_id": "turn-1",
            "message_seq": 1,
            "turn_id": "turn-1",
            "role": "assistant",
            "user_id": "user-1",
            "content": "Agent launched.",
            "blocks": [],
            "source_event_seq_applied": 0,
            "source_frame_seq_applied": 4,
            "created_at": "2026-08-09T00:00:00Z",
        }

    async def get_assistant_message_for_turn(
        self, session_id: str, *, turn_id: str
    ) -> dict[str, Any] | None:
        if session_id != "platform-session" or turn_id != "turn-1":
            return None
        return dict(self.message)


class _SessionsRepo:
    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        if session_id != "platform-session":
            return None
        return {
            "session_id": session_id,
            "session_kind": "agent_chat",
            "user_id": "user-1",
            "engine_kind": "claude_code",
        }


class _TranscriptRepo:
    async def list_scopes_by_platform_session(
        self, platform_session_id: str
    ) -> list[dict[str, Any]]:
        assert platform_session_id == "platform-session"
        return []

    async def load_subpath_entries_by_platform_session(
        self,
        platform_session_id: str,
        *,
        subpath: str | None,
    ) -> list[dict[str, Any]]:
        assert platform_session_id == "platform-session"
        assert subpath in {None, "subagents/agent-sdk-task-aa"}
        return []


class _ProjectionHarness(BackgroundContinuationMixin):
    def __init__(self, journal: _JournalRepo) -> None:
        self._session_events_repo = journal
        self._child_run_view = SessionChildRunView(journal)
        self._session_snapshots_repo = _SnapshotsRepo()
        self._message_view = _MessagesRepo()
        self._sessions_repo = _SessionsRepo()
        self._transcript_entries_repo = _TranscriptRepo()


async def _next_message_type(frames: Any, message_type: str) -> dict[str, Any]:
    async for frame in frames:
        if frame.get("op") == "event" and frame.get("message_type") == message_type:
            return frame
    raise AssertionError(f"stream ended before {message_type}")


class _ConnectedLink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(dict(frame))
        return True


async def test_uncommitted_engine_message_cannot_be_compacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host outage cannot turn a store-covered Result into notification loss."""

    monkeypatch.setattr(EnvelopeSender, "_JOURNAL_COMPACTION_THRESHOLD", 1)
    sender = EnvelopeSender(_ConnectedLink(), "platform-session")
    await sender.send("status", state="busy")
    durable_sequence = await sender.send(
        "event",
        message_type="TaskNotificationMessage",
        message={"__sdk_type": "TaskNotificationMessage"},
        requires_persistence=True,
    )
    result_sequence = await sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(4),
        message_type="ResultMessage",
        message={"__sdk_type": "ResultMessage"},
    )

    await sender.compact_after_result(result_sequence, store_fully_flushed=True)
    assert sender.first_retained_seq == durable_sequence

    await sender.acknowledge_event_persistence(durable_sequence)
    await sender.compact_after_result(result_sequence, store_fully_flushed=True)
    assert sender.first_retained_seq == result_sequence


async def test_post_result_task_notification_persists_and_settles_manifest() -> None:
    """The resident SDK pump, not a parent transcript line, closes the task."""

    journal = _JournalRepo()
    sdk = _SdkSession()
    store_bindings: list[tuple[str, dict[str, Any] | None]] = []

    def factory(opening: dict[str, Any], link: HostLink) -> RunnerSession:
        return RunnerSession(
            session_id=str(opening["slot_id"]),
            link=link,
            client_factory=lambda _broker: sdk,
            activation_callback=lambda target, store: store_bindings.append((target, store)),
        )

    server = RunnerWsServer(host="127.0.0.1", port=0, session_factory=factory)
    await server.start()
    link = RunnerLink(
        f"ws://127.0.0.1:{server.port}/",
        persistent_event_handler=_runner_event_persister(
            "platform-session",
            event_sink=PlatformEngineEventSink(
                "platform-session", journal_repo=journal
            ),
        ),
    )
    await link.__aenter__()
    try:
        await link.configure("platform-session")
        assert store_bindings == [("platform-session", None)]
        frames = link.frames()

        launch = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="call_00_a",
                    content="Async agent launched.",
                )
            ],
            uuid="launch-uuid",
            tool_use_result={
                "status": "async_launched",
                "agentId": "agent-session-aa",
                "isAsync": True,
                "description": "compute the marker",
            },
        )
        sdk.emit(launch)
        launch_frame = await _next_message_type(frames, "UserMessage")
        sdk.emit(ResultMessage())
        result_frame = await _next_message_type(frames, "ResultMessage")

        manifest = build_background_task_manifest([launch_frame["message"]])
        assert manifest == {
            "transcript_refs": ["agent-session-aa"],
            "engine_refs": ["agent-session-aa"],
            "transcript_to_engine_ref": {"agent-session-aa": "agent-session-aa"},
            "control_to_engine_ref": {"agent-session-aa": "agent-session-aa"},
            "activation_to_engine_ref": {"call_00_a": "agent-session-aa"},
        }
        opened_event, created = await journal.try_claim_event(
            {
                "session_id": "platform-session",
                "channel": "conversation",
                "turn_id": "turn-1",
                "event_type": "turn.background_tasks_opened",
                "causation_id": "command-1:background-continuation",
                "correlation_id": "command-1",
                "payload": {"command_id": "command-1", **manifest},
            }
        )
        assert created is True

        sdk_task_id = "agent-session-aa"
        sdk.emit(
            TaskStartedMessage(
                subtype="task_started",
                data={
                    "task_id": sdk_task_id,
                    "description": "compute the marker",
                    "uuid": "started-uuid",
                    "session_id": "sdk-session",
                    "tool_use_id": "call_00_a",
                    "task_type": "local_agent",
                },
                task_id=sdk_task_id,
                description="compute the marker",
                uuid="started-uuid",
                session_id="sdk-session",
                tool_use_id="call_00_a",
                task_type="local_agent",
            )
        )
        started_frame = await _next_message_type(frames, "TaskStartedMessage")
        sdk.emit(
            TaskProgressMessage(
                subtype="task_progress",
                data={
                    "task_id": sdk_task_id,
                    "description": "working",
                    "usage": {
                        "total_tokens": 4,
                        "tool_uses": 1,
                        "duration_ms": 20,
                    },
                    "uuid": "progress-uuid",
                    "session_id": "sdk-session",
                    "tool_use_id": "call_00_a",
                    "last_tool_name": "Bash",
                },
                task_id=sdk_task_id,
                description="working",
                usage={"total_tokens": 4, "tool_uses": 1, "duration_ms": 20},
                uuid="progress-uuid",
                session_id="sdk-session",
                tool_use_id="call_00_a",
                last_tool_name="Bash",
            )
        )
        progress_frame = await _next_message_type(frames, "TaskProgressMessage")
        harness = _ProjectionHarness(journal)
        background = await harness._get_background_task_state("platform-session")
        assert background is not None
        assert background["pending_task_count"] == 1

        update_data = {
            "task_id": sdk_task_id,
            "patch": {"status": "completed", "end_time": 1786320313958},
            "uuid": "updated-uuid",
            "session_id": "sdk-session",
        }
        update = TaskUpdatedMessage(
            subtype="task_updated",
            data=dict(update_data),
            task_id=sdk_task_id,
            patch=dict(update_data["patch"]),
            status="completed",
            uuid=update_data["uuid"],
            session_id=update_data["session_id"],
        )
        sdk.emit(update)
        update_frame = await _next_message_type(frames, "TaskUpdatedMessage")

        data = {
            "task_id": sdk_task_id,
            "status": "completed",
            "output_file": "/tmp/sdk-task-aa.output",
            "summary": "BACKGROUND COMPLETION MARKER 42",
            "uuid": "notification-uuid",
            "session_id": "sdk-session",
            "tool_use_id": None,
        }
        notification = TaskNotificationMessage(
            subtype="task_notification",
            data=dict(data),
            task_id=data["task_id"],
            status="completed",
            output_file=data["output_file"],
            summary=data["summary"],
            uuid=data["uuid"],
            session_id=data["session_id"],
            tool_use_id=data["tool_use_id"],
            usage=None,
        )
        sdk.emit(notification)
        notification_frame = await _next_message_type(frames, "TaskNotificationMessage")

        assert (
            result_frame["seq"]
            < started_frame["seq"]
            < progress_frame["seq"]
            < update_frame["seq"]
            < notification_frame["seq"]
        ), "the contract must cover lifecycle that starts after the launching turn's Result"
        assert notification_frame["requires_persistence"] is True
        engine_events = [
            event for event in journal.events if event["event_type"] == ENGINE_MESSAGE_EVENT_TYPE
        ]
        assert len(engine_events) == 4, (
            "started, progress, update, and notification must use one durable channel"
        )
        persisted = engine_events[-1]
        assert persisted["event_type"] == ENGINE_MESSAGE_EVENT_TYPE
        assert persisted["payload"] == {
            "engine_kind": "claude_code",
            "runner_sequence": notification_frame["seq"],
            "message": _jsonable(notification),
        }, "the persistent event must keep every SDK field and status unchanged"

        assert await harness._get_background_task_state("platform-session") is None
        assert await harness._materialize_background_continuation_event(opened_event)

        materialized_events = [
            event
            for event in journal.events
            if event["event_type"] == "turn.background_tasks_materialized"
        ]
        assert len(materialized_events) == 1
        materialized = materialized_events[0]
        assert "assistant_text" not in materialized["payload"]
        lifecycle = next(
            block
            for block in materialized["payload"]["blocks"]
            if block.get("data", {}).get("kind") == "lifecycle"
        )
        assert lifecycle["data"]["summary"] == "BACKGROUND COMPLETION MARKER 42"
        assert lifecycle["data"]["engineRef"] == "agent-session-aa"
        assert lifecycle["data"]["controlRef"] == sdk_task_id
        assert lifecycle["data"]["event"] == "closed"
        assert lifecycle["data"]["engineStatus"] == "completed"
        assert "controlId" not in lifecycle["data"]
        assert "childRunId" not in lifecycle["data"]
        assert await harness._get_background_task_state("platform-session") is None
    finally:
        await link.close()
        await server.stop()
