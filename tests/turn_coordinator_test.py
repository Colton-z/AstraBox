from __future__ import annotations

import unittest
from datetime import datetime, timezone

from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.session_kernel.workers.turn_coordinator import (
    TurnCoordinator,
)
from astrabox.core.service.orchestrator.session_message_view import (
    project_session_messages,
)
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_OK,
    SandboxLifecycleProbeResult,
)


class _FakeJournal:
    def __init__(self) -> None:
        self.claimed: list[dict] = []
        self.events: list[dict] = []
        self.frames: list[dict] = []
        self.next_frame_seq: dict[str, int] = {}

    async def try_claim_event(self, event: dict):
        session_id = str(event.get("session_id") or "").strip()
        causation_id = str(event.get("causation_id") or "").strip()
        event_type = str(event.get("event_type") or "").strip()
        for existing in self.events:
            if (
                str(existing.get("session_id") or "").strip() == session_id
                and str(existing.get("causation_id") or "").strip() == causation_id
                and str(existing.get("event_type") or "").strip() == event_type
            ):
                return dict(existing), False
        stored = {**event, "event_seq": len(self.events) + 1}
        self.claimed.append(stored)
        self.events.append(stored)
        return stored, True

    async def list_events(self, *args, **kwargs):
        session_id = args[0] if args else None
        rows = []
        for event in self.events:
            if session_id is not None and event.get("session_id") != session_id:
                continue
            if kwargs.get("turn_id") is not None and event.get("turn_id") != kwargs["turn_id"]:
                continue
            if kwargs.get("event_type") is not None and event.get("event_type") != kwargs["event_type"]:
                continue
            if kwargs.get("channel") is not None and event.get("channel") != kwargs["channel"]:
                continue
            rows.append(dict(event))
        return rows[: int(kwargs.get("limit") or len(rows) or 500)]

    async def allocate_session_frame_seq(self, session_id: str, *, count: int = 1):
        if session_id not in self.next_frame_seq:
            rows = [item for item in self.frames if item.get("session_id") == session_id]
            self.next_frame_seq[session_id] = (
                max(int(item.get("frame_seq") or 0) for item in rows) + 1
                if rows
                else 0
            )
        start = self.next_frame_seq[session_id]
        self.next_frame_seq[session_id] += count
        return start

    async def get_next_session_frame_seq(self, session_id: str):
        return await self.allocate_session_frame_seq(session_id)

    async def append_frame(self, frame: dict):
        self.frames.append(dict(frame))

    async def list_frames(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        after_seq: int = -1,
        limit: int = 500,
        **kwargs,
    ):
        command_id = kwargs.get("command_id")
        rows = [
            dict(item)
            for item in self.frames
            if item.get("session_id") == session_id
            and int(item.get("frame_seq") or 0) > after_seq
            and (turn_id is None or item.get("turn_id") == turn_id)
            and (command_id is None or item.get("command_id") == command_id)
        ]
        rows.sort(key=lambda item: int(item.get("frame_seq") or 0))
        return rows[:limit]


class _FakeSnapshots:
    def __init__(self) -> None:
        self.applied: list[dict] = []

    async def apply_channel_update(self, session_id: str, **kwargs):
        self.applied.append({"session_id": session_id, **kwargs})
        return {"session_id": session_id, "last_turn_status": kwargs["updates"]["last_turn_status"]}

    async def get_snapshot(self, session_id: str):
        return {"session_id": session_id}


class _FakeSessions:
    def __init__(self) -> None:
        self.updated: list[tuple[str, dict]] = []

    async def update_session(self, session_id: str, updates: dict):
        self.updated.append((session_id, dict(updates)))


class _FakeMessageView:
    def __init__(self) -> None:
        self.messages: dict[tuple[str, str], dict] = {}

    async def upsert_message(self, message: dict):
        self.messages[(message["session_id"], message["message_id"])] = dict(message)

    async def get_message(self, session_id: str, message_id: str):
        return self.messages.get((session_id, message_id))

    async def get_assistant_message_for_turn(self, session_id: str, *, turn_id: str):
        return self.messages.get((session_id, turn_id))


class _FakeEngineFrames(_FakeJournal):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict] = []
        self.next_frame_seq: dict[str, int] = {}

    async def allocate_session_frame_seq(self, session_id: str, *, count: int = 1):
        # Session-scoped allocation: every frame of the session shares one
        # sequence, whichever turn it belongs to.
        if session_id not in self.next_frame_seq:
            rows = [
                item for item in self.frames
                if item.get("session_id") == session_id
            ]
            self.next_frame_seq[session_id] = (
                max(int(item.get("frame_seq") or 0) for item in rows) + 1
                if rows
                else 0
            )
        start = self.next_frame_seq[session_id]
        self.next_frame_seq[session_id] += count
        return start

    async def get_next_session_frame_seq(self, session_id: str):
        return await self.allocate_session_frame_seq(session_id)

    async def append_frame(self, frame: dict):
        self.frames.append(dict(frame))

    async def list_frames(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        after_seq: int = -1,
        limit: int = 500,
        **kwargs,
    ):
        _ = kwargs
        rows = [
            dict(item)
            for item in self.frames
            if item.get("session_id") == session_id
            and int(item.get("frame_seq") or 0) > after_seq
            and (turn_id is None or item.get("turn_id") == turn_id)
        ]
        rows.sort(key=lambda item: int(item.get("frame_seq") or 0))
        return rows[:limit]


class _FakeTranscriptEntries:
    def __init__(self, entries: list[dict] | None = None) -> None:
        self.entries = list(entries or [])

    async def load_recovery_entries_by_platform_session(
        self, platform_session_id: str
    ) -> list[dict]:
        _ = platform_session_id
        return [dict(item) for item in self.entries]


def _prompt_entry(text: str) -> dict:
    """The CLI JSONL user entry the coordinator slices the turn tail from."""
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "uuid": "u-prompt",
    }


_PROMPT = "run the long thing"


def _prompted(
    message_view: "_FakeMessageView",
    session_id: str,
    turn_id: str,
) -> "_FakeMessageView":
    """Give the turn its durable user row — the slice anchor recovery reads."""
    message_view.messages[(session_id, f"{turn_id}:user")] = {
        "session_id": session_id,
        "message_id": f"{turn_id}:user",
        "content": _PROMPT,
    }
    return message_view


class _FakeInteractionSnapshots:
    def __init__(self) -> None:
        self.deactivated: list[tuple[str, str]] = []

    async def deactivate_active_for_turn(self, session_id: str, turn_id: str) -> int:
        self.deactivated.append((session_id, turn_id))
        return 1


class _FakeRuntimeManager(RemoteAgentRuntimeManager):
    def __init__(self, *, terminal: bool = False) -> None:
        self.terminal = terminal
        self.probed: list[str] = []

    async def resolve_enhanced_server_endpoint(self, *, session_id: str, sandbox_id: str):
        _ = session_id
        return f"{sandbox_id}.example"

    # Inherit the product's probe classifier so this fake cannot make a call to
    # a missing runtime-manager method look valid.
    async def get_sandbox_lifecycle_probe(
        self, sandbox_id: str,
    ) -> SandboxLifecycleProbeResult:
        self.probed.append(sandbox_id)
        return SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_OK,
            sandbox_state="terminated" if self.terminal else "running",
        )


def _coordinator(
    *,
    journal: _FakeJournal | None = None,
    snapshots: _FakeSnapshots | None = None,
    message_view: _FakeMessageView | None = None,
    runtime_manager=None,
    transcript_entries: _FakeTranscriptEntries | None = None,
) -> TurnCoordinator:
    return TurnCoordinator(
        session_events_repo=journal or _FakeJournal(),
        session_snapshots_repo=snapshots or _FakeSnapshots(),
        sessions_repo=_FakeSessions(),
        message_view=message_view or _FakeMessageView(),
        runtime_manager=runtime_manager or _FakeRuntimeManager(),
        transcript_entries_repo=transcript_entries,
    )


def _with_frames(journal: _FakeJournal, frames: _FakeEngineFrames) -> _FakeJournal:
    journal.frames = frames.frames
    journal.next_frame_seq = frames.next_frame_seq
    return journal


def _dispatch_confirmed(
    session_id: str,
    turn_id: str,
    sandbox_id: str,
    *,
    remote_anchor: dict | None = None,
) -> dict:
    return {
        "session_id": session_id,
        "channel": "conversation",
        "turn_id": turn_id,
        "event_type": "dispatch.confirmed",
        "causation_id": "cmd-1",
        "event_seq": 1,
        "payload": {
            "recovery_context": {"sandbox_id": sandbox_id, "remote_anchor": remote_anchor}
        },
    }


def _pending_snapshot(turn_id: str) -> dict:
    return {
        "conversation_state": "IDLE",
        "last_turn_status": "FAILED",
        "turn_recovery_phase": "TRANSCRIPT_PENDING",
        "last_turn_id": turn_id,
        "last_turn_command_id": "cmd-1",
    }


class TurnCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_identity_locates_the_recovery_prompt(self):
        journal = _FakeJournal()
        journal.events.append(
            {
                "session_id": "session-1",
                "channel": "command",
                "turn_id": "platform-turn-1",
                "event_type": "command.accepted",
                "event_seq": 1,
                "payload": {
                    "command_type": "StartTurn",
                    "content": _PROMPT,
                    "input_id": "sdk-input-1",
                },
            }
        )
        message_view = _FakeMessageView()
        message_view.messages[("session-1", "sdk-input-1:user")] = {
            "session_id": "session-1",
            "message_id": "sdk-input-1:user",
            "content": _PROMPT,
        }
        coordinator = _coordinator(journal=journal, message_view=message_view)

        prompt = await coordinator._initiating_user_prompt_text(
            "session-1",
            "platform-turn-1",
        )

        self.assertEqual(prompt, _PROMPT)

    async def test_accepted_command_anchors_recovery_before_ui_projection(self):
        journal = _FakeJournal()
        journal.events.append(
            {
                "session_id": "session-1",
                "channel": "command",
                "turn_id": "platform-turn-1",
                "event_type": "command.accepted",
                "event_seq": 1,
                "payload": {
                    "command_type": "StartTurn",
                    "content": _PROMPT,
                    "input_id": "sdk-input-1",
                },
            }
        )
        coordinator = _coordinator(journal=journal)

        prompt = await coordinator._initiating_user_prompt_text(
            "session-1",
            "platform-turn-1",
        )

        self.assertEqual(prompt, _PROMPT)

    async def test_recovery_attempt_time_is_normalized_to_utc(self):
        journal = _FakeJournal()
        coordinator = _coordinator(journal=journal)
        cases = (
            ("2026-08-09T03:15:00-07:00", "2026-08-09T10:15:00+00:00"),
            ("2026-08-09T10:15:00", "2026-08-09T10:15:00+00:00"),
        )

        for attempted_at, expected in cases:
            with self.subTest(attempted_at=attempted_at):
                journal.events = [
                    {
                        "session_id": "session-1",
                        "turn_id": "turn-1",
                        "event_type": "turn.recovery_attempt_failed",
                        "payload": {"attempted_at": attempted_at},
                    }
                ]

                parsed = await coordinator._get_first_attempt_time(
                    "session-1", "turn-1"
                )

                self.assertIsInstance(parsed, datetime)
                self.assertIs(parsed.tzinfo, timezone.utc)
                self.assertEqual(parsed.isoformat(), expected)

    async def test_interrupted_projection_settles_failed_with_partial_text(self):
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        sessions = _FakeSessions()
        message_view = _FakeMessageView()
        coordinator = TurnCoordinator(
            session_events_repo=journal,
            session_snapshots_repo=snapshots,
            sessions_repo=sessions,
            message_view=message_view,
            runtime_manager=object(),
        )

        result = await coordinator._complete_recovery(
            session_id="session-1",
            turn_id="turn-1",
            session={
                "session_id": "session-1",
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            snapshot={
                "conversation_state": "IDLE",
                "last_turn_status": "FAILED",
                "turn_recovery_phase": "TRANSCRIPT_PENDING",
                "last_turn_id": "turn-1",
                "last_turn_command_id": "cmd-1",
            },
            projection={
                "done": True,
                "interrupted": True,
                "has_result": False,
                "assistant_text": "1 - this is line 1\n2 - this is line 2",
                "blocks": [{"type": "text", "text": "1 - this is line 1\n2 - this is line 2"}],
            },
        )

        self.assertEqual(result["last_turn_status"], "FAILED")
        self.assertEqual(journal.claimed[0]["event_type"], "turn.failed")
        self.assertEqual(journal.claimed[0]["payload"]["reason"], "interrupted")
        stored_message = project_session_messages(
            events=journal.events,
            frames=journal.frames,
            user_id="u1",
        )[-1]
        self.assertEqual(stored_message["content"], "1 - this is line 1\n2 - this is line 2")
        self.assertEqual(stored_message["blocks"][0]["type"], "text")
        self.assertEqual(stored_message["blocks"][-1]["type"], "turn_failure")
        self.assertEqual(stored_message["blocks"][-1]["error"], "Request interrupted by user")
        self.assertEqual(
            snapshots.applied[0]["updates"]["last_turn_status"],
            "FAILED",
        )
        self.assertEqual(
            sessions.updated,
            [("session-1", {"interrupt_requested": False})],
        )

    async def test_turn_local_sandbox_comes_from_dispatch_not_from_the_session(self):
        # The session's CURRENT box is not evidence about THIS turn: a re-boxed
        # session would have recovery probe a box that never carried it.
        journal = _FakeJournal()
        coordinator = _coordinator(journal=journal)

        self.assertIsNone(
            await coordinator._turn_local_sandbox_id("session-1", "turn-1")
        )

        journal.events.append(_dispatch_confirmed("session-1", "turn-1", "sandbox-turn"))
        self.assertEqual(
            await coordinator._turn_local_sandbox_id("session-1", "turn-1"),
            "sandbox-turn",
        )

    async def test_a_live_box_with_an_incomplete_mirror_leaves_the_turn_pending(self):
        # Recovery must not demand a remote anchor: it reads nothing with one,
        # and settling FAILED because the dispatch recorded none kills a turn
        # whose producer is still streaming and which completes correctly
        # seconds later.
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        message_view = _FakeMessageView()
        journal.events.append(
            _dispatch_confirmed("session-1", "turn-1", "sandbox-1", remote_anchor=None)
        )
        coordinator = _coordinator(
            journal=journal,
            snapshots=snapshots,
            message_view=message_view,
            runtime_manager=_FakeRuntimeManager(terminal=False),
            transcript_entries=_FakeTranscriptEntries([]),
        )

        result = await coordinator.try_resolve_turn(
            "session-1",
            {
                "session_id": "session-1",
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            _pending_snapshot("turn-1"),
        )

        self.assertIsNone(result)
        self.assertEqual(snapshots.applied, [])
        self.assertEqual(message_view.messages, {})
        self.assertEqual([e["event_type"] for e in journal.claimed], [])

    async def test_a_terminated_box_with_an_incomplete_mirror_settles_failed(self):
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        journal.events.append(_dispatch_confirmed("session-1", "turn-1", "sandbox-1"))
        coordinator = _coordinator(
            journal=journal,
            snapshots=snapshots,
            runtime_manager=_FakeRuntimeManager(terminal=True),
            transcript_entries=_FakeTranscriptEntries([]),
        )

        result = await coordinator.try_resolve_turn(
            "session-1",
            {
                "session_id": "session-1",
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            _pending_snapshot("turn-1"),
        )

        self.assertIsNotNone(result)
        self.assertEqual(snapshots.applied[-1]["updates"]["last_turn_status"], "FAILED")
        self.assertEqual(
            [e["payload"]["reason"] for e in journal.claimed if e["event_type"] == "turn.failed"],
            ["sandbox_terminated_incomplete_mirror"],
        )

    async def test_a_turn_that_never_reached_a_box_settles_failed(self):
        # No dispatch on record: nothing carried the turn, so nothing can finish
        # it. Terminal — and named for the fact, so a lost dispatch row reads as
        # the durability defect it would be rather than as a dead anchor.
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        coordinator = _coordinator(
            journal=journal,
            snapshots=snapshots,
            runtime_manager=_FakeRuntimeManager(terminal=False),
            transcript_entries=_FakeTranscriptEntries([]),
        )

        result = await coordinator.try_resolve_turn(
            "session-1",
            {
                "session_id": "session-1",
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            _pending_snapshot("turn-1"),
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            [e["payload"]["reason"] for e in journal.claimed if e["event_type"] == "turn.failed"],
            ["no_turn_local_sandbox"],
        )

    async def test_existing_recovered_event_does_not_skip_missing_terminal_proof(self):
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        sessions = _FakeSessions()
        message_view = _FakeMessageView()
        frames = _FakeEngineFrames()
        session_id = "session-1"
        turn_id = "turn-1"
        journal.events.extend(
            [
                {
                    "session_id": session_id,
                    "channel": "conversation",
                    "turn_id": turn_id,
                    "event_type": "dispatch.confirmed",
                    "causation_id": "cmd-1",
                    "event_seq": 1,
                    "payload": {"recovery_context": {"sandbox_id": "sandbox-1"}},
                },
                {
                    "session_id": session_id,
                    "channel": "conversation",
                    "turn_id": turn_id,
                    "event_type": "turn.recovered",
                    "causation_id": f"recover:{session_id}:{turn_id}",
                    "event_seq": 3,
                    "payload": {
                        "assistant_text": "recovered from existing claim",
                        "blocks": [{"type": "text", "text": "recovered from existing claim"}],
                    },
                },
            ]
        )
        coordinator = TurnCoordinator(
            session_events_repo=_with_frames(journal, frames),
            session_snapshots_repo=snapshots,
            sessions_repo=sessions,
            message_view=message_view,
            runtime_manager=_FakeRuntimeManager(),
            transcript_entries_repo=_FakeTranscriptEntries(
                [
                    _prompt_entry(_PROMPT),
                    {
                        "type": "result",
                        "result": "fresh projection text",
                        "__astrabox_mirror_seq": 3,
                    },
                ]
            ),
        )
        _prompted(message_view, session_id, turn_id)

        result = await coordinator.try_resolve_turn(
            session_id,
            {
                "session_id": session_id,
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            {
                "conversation_state": "IDLE",
                "last_turn_status": "FAILED",
                "turn_recovery_phase": "TRANSCRIPT_PENDING",
                "last_turn_id": turn_id,
                "last_turn_command_id": "cmd-1",
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["last_turn_status"], "COMPLETED")
        finish_frames = [
            frame
            for frame in frames.frames
            if frame.get("payload") == {"type": "finish", "finishReason": "stop"}
        ]
        self.assertEqual(len(finish_frames), 1)
        terminal_proof = snapshots.applied[-1]["updates"]["last_turn_terminal_frame"]
        self.assertEqual(
            terminal_proof,
            {
                "turn_id": turn_id,
                "command_id": "cmd-1",
                "frame_seq": finish_frames[0]["frame_seq"],
                "type": "finish",
                "finish_reason": "stop",
            },
        )
        self.assertEqual(
            next(
                event
                for event in journal.events
                if event["event_type"] == "turn.recovered"
            )["payload"]["assistant_text"],
            "recovered from existing claim",
        )

    async def test_existing_recovery_finish_frame_is_reused(self):
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        frames = _FakeEngineFrames()
        session_id = "session-1"
        turn_id = "turn-1"
        frames.frames.append(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "command_id": "cmd-1",
                "source_kind": "turn_recovery",
                "frame_seq": 0,
                "payload": {"type": "finish", "finishReason": "stop"},
            }
        )
        coordinator = TurnCoordinator(
            session_events_repo=_with_frames(journal, frames),
            session_snapshots_repo=snapshots,
            sessions_repo=_FakeSessions(),
            message_view=_FakeMessageView(),
            runtime_manager=_FakeRuntimeManager(),
        )

        proof = await coordinator._append_recovery_finish_frame(
            session_id=session_id,
            turn_id=turn_id,
            command_id="cmd-1",
        )

        self.assertEqual(len(frames.frames), 1)
        self.assertEqual(
            proof,
            {
                "turn_id": turn_id,
                "command_id": "cmd-1",
                "frame_seq": 0,
                "type": "finish",
                "finish_reason": "stop",
            },
        )

    async def test_control_frames_do_not_block_recovered_assistant_materialization(self):
        frames = _FakeEngineFrames()
        session_id = "session-1"
        turn_id = "turn-1"
        for frame_seq, payload in enumerate(
            (
                {"type": "start", "messageId": turn_id},
                {
                    "type": "data-turn-accepted",
                    "data": {"turnId": turn_id, "clientMessageId": "client-1"},
                },
                {
                    "type": "data-session-store-reload",
                    "data": {"resumeSequence": 29},
                },
            )
        ):
            frames.frames.append(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "command_id": "cmd-1",
                    "frame_seq": frame_seq,
                    "payload": payload,
                }
            )
        coordinator = TurnCoordinator(
            session_events_repo=frames,
            session_snapshots_repo=_FakeSnapshots(),
            sessions_repo=_FakeSessions(),
            message_view=_FakeMessageView(),
            runtime_manager=_FakeRuntimeManager(),
        )

        for _ in range(2):
            await coordinator._materialize_recovered_assistant_frames(
                session_id=session_id,
                turn_id=turn_id,
                command_id="cmd-1",
                assistant_text="recovered answer",
                blocks=[{"type": "text", "text": "recovered answer"}],
                source_mirror_seq=31,
            )

        recovered = [
            frame
            for frame in frames.frames
            if frame.get("source_kind") == "transcript_mirror"
        ]
        self.assertEqual(
            [frame["payload"]["type"] for frame in recovered],
            ["start-step", "text-start", "text-delta", "text-end", "finish-step"],
        )
        self.assertEqual(recovered[2]["payload"]["delta"], "recovered answer")
        self.assertEqual(
            [frame["frame_seq"] for frame in recovered],
            [3, 4, 5, 6, 7],
        )

    async def test_existing_assistant_content_blocks_recovered_materialization(self):
        frames = _FakeEngineFrames()
        session_id = "session-1"
        turn_id = "turn-1"
        for frame_seq in range(500):
            frames.frames.append(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "command_id": "cmd-1",
                    "frame_seq": frame_seq,
                    "payload": {
                        "type": "data-resume-cursor",
                        "data": {"frameSeq": frame_seq},
                    },
                }
            )
        frames.frames.append(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "command_id": "cmd-1",
                "frame_seq": 500,
                "payload": {
                    "type": "text-delta",
                    "id": "live-text-0",
                    "delta": "already visible",
                },
            }
        )
        coordinator = TurnCoordinator(
            session_events_repo=frames,
            session_snapshots_repo=_FakeSnapshots(),
            sessions_repo=_FakeSessions(),
            message_view=_FakeMessageView(),
            runtime_manager=_FakeRuntimeManager(),
        )

        await coordinator._materialize_recovered_assistant_frames(
            session_id=session_id,
            turn_id=turn_id,
            command_id="cmd-1",
            assistant_text="recovered answer",
            blocks=[{"type": "text", "text": "recovered answer"}],
            source_mirror_seq=31,
        )

        self.assertFalse(
            any(
                frame.get("source_kind") == "transcript_mirror"
                for frame in frames.frames
            )
        )

    async def test_existing_completed_event_blocks_recovered_event(self):
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        sessions = _FakeSessions()
        message_view = _FakeMessageView()
        frames = _FakeEngineFrames()
        session_id = "session-1"
        turn_id = "turn-1"
        journal.events.append(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.completed",
                "causation_id": "cmd-1",
                "event_seq": 7,
                "payload": {
                    "command_id": "cmd-1",
                    "final_state": "READY",
                    "assistant_text": "completed truth",
                },
            }
        )
        frames.frames.append(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "command_id": "cmd-1",
                "frame_seq": 3,
                "payload": {"type": "finish", "finishReason": "stop"},
            }
        )
        coordinator = TurnCoordinator(
            session_events_repo=_with_frames(journal, frames),
            session_snapshots_repo=snapshots,
            sessions_repo=sessions,
            message_view=message_view,
            runtime_manager=_FakeRuntimeManager(),
        )

        result = await coordinator._complete_recovery(
            session_id=session_id,
            turn_id=turn_id,
            session={
                "session_id": session_id,
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            snapshot={
                "conversation_state": "IDLE",
                "last_turn_status": "FAILED",
                "turn_recovery_phase": "TRANSCRIPT_PENDING",
                "last_turn_id": turn_id,
                "last_turn_command_id": None,
            },
            projection={
                "done": True,
                "assistant_text": "recovery projection",
                "blocks": [{"type": "text", "text": "recovery projection"}],
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(journal.claimed, [])
        self.assertEqual(len(frames.frames), 1)
        self.assertEqual(snapshots.applied[-1]["event_seq"], 7)
        self.assertEqual(
            snapshots.applied[-1]["updates"]["last_turn_command_id"],
            "cmd-1",
        )
        self.assertEqual(
            snapshots.applied[-1]["updates"]["last_turn_terminal_frame"],
            {
                "turn_id": turn_id,
                "command_id": "cmd-1",
                "frame_seq": 3,
                "type": "finish",
                "finish_reason": "stop",
            },
        )
        self.assertEqual(
            project_session_messages(
                events=journal.events,
                frames=journal.frames,
                user_id="u1",
            )[-1]["content"],
            "completed truth",
        )

    async def test_existing_durable_finish_frame_is_reused(self):
        frames = _FakeEngineFrames()
        session_id = "session-1"
        turn_id = "turn-1"
        frames.frames.append(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "command_id": "cmd-live",
                "frame_seq": 11,
                "payload": {"type": "finish", "finishReason": "stop"},
            }
        )
        coordinator = TurnCoordinator(
            session_events_repo=frames,
            session_snapshots_repo=_FakeSnapshots(),
            sessions_repo=_FakeSessions(),
            message_view=_FakeMessageView(),
            runtime_manager=_FakeRuntimeManager(),
        )

        proof = await coordinator._append_recovery_finish_frame(
            session_id=session_id,
            turn_id=turn_id,
            command_id="cmd-recovery",
        )

        self.assertEqual(len(frames.frames), 1)
        self.assertNotIn("source_kind", frames.frames[0])
        self.assertEqual(
            proof,
            {
                "turn_id": turn_id,
                "command_id": "cmd-live",
                "frame_seq": 11,
                "type": "finish",
                "finish_reason": "stop",
            },
        )

    async def test_completed_recovery_deactivates_same_turn_interaction(self):
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        sessions = _FakeSessions()
        message_view = _FakeMessageView()
        frames = _FakeEngineFrames()
        interactions = _FakeInteractionSnapshots()
        journal.events.append(
            {
                "session_id": "session-1",
                "channel": "command",
                "turn_id": "turn-1",
                "event_type": "command.accepted",
                "causation_id": "cmd-1",
                "event_seq": 1,
                "payload": {"command_id": "cmd-1"},
            }
        )
        coordinator = TurnCoordinator(
            session_events_repo=_with_frames(journal, frames),
            session_snapshots_repo=snapshots,
            sessions_repo=sessions,
            message_view=message_view,
            runtime_manager=_FakeRuntimeManager(),
            interaction_snapshots_repo=interactions,
            worker_id="coordinator-1",
        )

        result = await coordinator._complete_recovery(
            session_id="session-1",
            turn_id="turn-1",
            session={
                "session_id": "session-1",
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            snapshot={
                "conversation_state": "IDLE",
                "last_turn_status": "FAILED",
                "turn_recovery_phase": "TRANSCRIPT_PENDING",
                "last_turn_id": "turn-1",
                "last_turn_command_id": "cmd-1",
            },
            projection={
                "done": True,
                "assistant_text": "done",
                "blocks": [{"type": "text", "text": "done"}],
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["last_turn_status"], "COMPLETED")
        self.assertEqual(interactions.deactivated, [("session-1", "turn-1")])
        finish_frames = [
            frame
            for frame in frames.frames
            if frame.get("payload") == {"type": "finish", "finishReason": "stop"}
        ]
        self.assertEqual(len(finish_frames), 1)


if __name__ == "__main__":
    unittest.main()


class OwnerDeadRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The reconcile lane's verdict lets the coordinator take a PROCESSING turn.

    The scenario is a platform restart under a running turn: the worker died
    with the process (no terminal written — writing one off a closing link was
    the false verdict this lane replaces), the box kept executing, and the
    restarted instance must settle the SAME turn COMPLETED once the mirror
    carries the evidence — with no interim FAILED ever surfacing, because the
    restart e2e (and any user poll) reads the state the moment it goes READY.
    """

    @staticmethod
    def _processing_snapshot(turn_id: str) -> dict:
        return {
            "conversation_state": "PROCESSING",
            "current_turn_id": turn_id,
            "last_turn_id": turn_id,
            "last_turn_command_id": "cmd-1",
        }

    async def test_owner_dead_with_a_complete_mirror_settles_completed(self):
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        message_view = _prompted(_FakeMessageView(), "session-1", "turn-1")
        journal.events.append(
            _dispatch_confirmed("session-1", "turn-1", "sandbox-1")
        )
        coordinator = _coordinator(
            journal=journal,
            snapshots=snapshots,
            message_view=message_view,
            runtime_manager=_FakeRuntimeManager(terminal=False),
            transcript_entries=_FakeTranscriptEntries(
                [
                    _prompt_entry(_PROMPT),
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "the answer"}],
                            "stop_reason": "end_turn",
                        },
                        "__astrabox_mirror_seq": 5,
                    },
                ]
            ),
        )

        result = await coordinator.try_resolve_turn(
            "session-1",
            {
                "session_id": "session-1",
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            self._processing_snapshot("turn-1"),
            owner_dead=True,
        )

        assert result is not None
        assert result["last_turn_status"] == "COMPLETED"
        assert [e["event_type"] for e in journal.claimed] == ["turn.recovered"], (
            "the one durable verdict must be the recovery — an interim FAILED "
            "here is exactly the false state a poller observes"
        )

    async def test_owner_dead_with_an_incomplete_mirror_stays_processing(self):
        journal = _FakeJournal()
        snapshots = _FakeSnapshots()
        message_view = _prompted(_FakeMessageView(), "session-1", "turn-1")
        journal.events.append(
            _dispatch_confirmed("session-1", "turn-1", "sandbox-1")
        )
        coordinator = _coordinator(
            journal=journal,
            snapshots=snapshots,
            message_view=message_view,
            runtime_manager=_FakeRuntimeManager(terminal=False),
            transcript_entries=_FakeTranscriptEntries([_prompt_entry(_PROMPT)]),
        )

        result = await coordinator.try_resolve_turn(
            "session-1",
            {
                "session_id": "session-1",
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            self._processing_snapshot("turn-1"),
            owner_dead=True,
        )

        assert result is None, "a live box still working must be waited for, not settled"
        assert snapshots.applied == []
        assert journal.claimed == []

    async def test_without_the_verdict_a_processing_turn_is_refused(self):
        """The single-owner rule stands: no owner-death evidence, no recovery
        of an active snapshot — a live writer may still be appending."""
        coordinator = _coordinator(
            transcript_entries=_FakeTranscriptEntries([_prompt_entry(_PROMPT)]),
        )

        result = await coordinator.try_resolve_turn(
            "session-1",
            {
                "session_id": "session-1",
                "user_id": "u1",
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
            },
            self._processing_snapshot("turn-1"),
        )

        assert result is None
