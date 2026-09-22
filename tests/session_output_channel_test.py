"""Input and output use separate channels, and output responses span one turn.

A POST starts a turn without owning its response stream. The session output
subscription remains available while idle and reopens after each terminal turn.
One response may span tool-permission pauses inside that turn, but it cannot
carry the next turn into the same AI SDK parser.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
    _TailFrameState,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _ResumeCursorTracker,
    _emit_resumable_payloads,
)
from astrabox.core.service.orchestrator.session_message_view import SessionMessageView


def _state(**over) -> _TailFrameState:
    kwargs = {
        "session_id": "s-1",
        "command_id": None,
        "turn_id": None,
        "after_seq": -1,
        "include_terminal_resume_cursor": False,
        "stop_on_segment_finish": True,
    }
    kwargs.update(over)
    return _TailFrameState(**kwargs)


class SessionFollowEndsAtOneTurnBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mixin = TurnDispatchStreamingMixin()

    def test_a_session_follower_spans_segments_but_stops_at_a_turn_terminal(self) -> None:
        following = _state(follow_session=True, stop_on_segment_finish=False)
        self.assertFalse(
            self.mixin._tail_payload_should_stop(
                following,
                {"type": "finish", "finishReason": "tool-calls"},
            ),
            "a tool-permission park is a segment boundary inside the same turn",
        )
        for payload in (
            {"type": "finish", "finishReason": "stop"},
            {"type": "error", "errorText": "boom"},
        ):
            with self.subTest(payload["type"] + str(payload.get("finishReason"))):
                self.assertTrue(
                    self.mixin._tail_payload_should_stop(following, payload),
                    "one HTTP response must not carry frames from two turns",
                )

    def test_the_turn_scoped_modes_are_unchanged(self) -> None:
        # The coupled path still stops where it always did — this separation is
        # additive until the console moves over.
        segment = _state(stop_on_segment_finish=True)
        self.assertTrue(
            self.mixin._tail_payload_should_stop(segment, {"type": "finish", "finishReason": "tool-calls"})
        )
        turn = _state(stop_on_segment_finish=False)
        self.assertFalse(
            self.mixin._tail_payload_should_stop(turn, {"type": "finish", "finishReason": "tool-calls"}),
            "a park is not a turn terminal",
        )
        self.assertTrue(
            self.mixin._tail_payload_should_stop(turn, {"type": "finish", "finishReason": "stop"})
        )


class _LiveFrameBroker:
    def __init__(self, event: dict[str, Any]) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.queue.put_nowait(event)

    async def subscribe(self, session_id: str) -> asyncio.Queue[Any]:
        _ = session_id
        return self.queue

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue[Any]) -> None:
        _ = (session_id, queue)


class _SessionFollowerHarness(TurnDispatchStreamingMixin):
    def __init__(self, event: dict[str, Any]) -> None:
        self._broker = _LiveFrameBroker(event)
        self._poll_interval_s = 5.0

    async def _tail_read_available_frames(self, state: _TailFrameState):
        _ = state
        return [], False


class SessionFollowerDeliversLiveBrokerFramesTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_text_reaches_the_follower_before_durable_storage(self) -> None:
        payload = {"type": "text-delta", "id": "text-1", "delta": "first token"}
        harness = _SessionFollowerHarness(
            {
                "type": "ai_sdk_live_frame",
                "live_seq": 1,
                "command_id": "command-1",
                "turn_id": "turn-1",
                "payload": payload,
            }
        )
        stream = harness._tail_frames(
            "session-1",
            follow_session=True,
            include_terminal_resume_cursor=False,
        )
        try:
            self.assertEqual(
                await asyncio.wait_for(anext(stream), timeout=0.1),
                {"type": "text-start", "id": "text-1"},
            )
            self.assertEqual(await asyncio.wait_for(anext(stream), timeout=0.1), payload)
        finally:
            await stream.aclose()


class _SilentBroker:
    async def subscribe(self, session_id: str) -> asyncio.Queue[Any]:
        _ = session_id
        return asyncio.Queue()

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue[Any]) -> None:
        _ = (session_id, queue)


class _FollowerFrameHistory:
    def __init__(self) -> None:
        self.frames = [
            {
                "session_id": "session-1",
                "frame_seq": 1,
                "turn_id": "turn-a",
                "scope": "turn",
                "command_id": "command-a",
                "live_seq": 0,
                "payload": {"type": "start", "messageId": "assistant-a"},
            },
            {
                "session_id": "session-1",
                "frame_seq": 2,
                "turn_id": "turn-a",
                "scope": "turn",
                "command_id": "command-a",
                "live_seq": 1,
                "payload": {"type": "finish", "finishReason": "stop"},
            },
            {
                "session_id": "session-1",
                "frame_seq": 3,
                "turn_id": "turn-b",
                "scope": "turn",
                "command_id": "command-b",
                # The engine's live counter restarts for each turn.
                "live_seq": 0,
                "payload": {"type": "start", "messageId": "assistant-b"},
            },
            {
                "session_id": "session-1",
                "frame_seq": 4,
                "turn_id": "turn-b",
                "scope": "turn",
                "command_id": "command-b",
                "live_seq": 1,
                "payload": {"type": "finish", "finishReason": "stop"},
            },
        ]

    async def list_frames(
        self,
        session_id: str,
        *,
        after_seq: int = -1,
        **_filters: Any,
    ) -> list[dict[str, Any]]:
        if session_id != "session-1":
            raise AssertionError(f"unexpected session: {session_id}")
        return [
            dict(frame)
            for frame in self.frames
            if int(frame["frame_seq"]) > after_seq
        ]

    async def list_events(
        self,
        session_id: str,
        **_filters: Any,
    ) -> list[dict[str, Any]]:
        # These scenarios have turn frames only: no resident engine message
        # was journalled for the follower to merge.
        if session_id != "session-1":
            raise AssertionError(f"unexpected session: {session_id}")
        return []


class _DurableFollowerHarness(TurnDispatchStreamingMixin):
    def __init__(self) -> None:
        self._broker = _SilentBroker()
        self._session_events_repo = _FollowerFrameHistory()
        self._message_view = SessionMessageView(self._session_events_repo)
        self._poll_interval_s = 5.0
        self._turn_terminal_settle_retry_window_s = 0.1
        self._turn_terminal_settle_retry_delay_s = 0.001

    async def _must_get_projection_backed_session(self, *_args: Any, **_kwargs: Any):
        return {"session_id": "session-1", "state": "READY"}

    async def _rewind_unsafe_resume_cursor(
        self,
        session_id: str,
        *,
        requested_after_seq: int,
    ) -> int:
        if session_id != "session-1":
            raise AssertionError(f"unexpected session: {session_id}")
        return requested_after_seq


class SessionFollowerResponseBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_response_ends_with_its_turn_cursor_before_the_next_turn(self) -> None:
        harness = _DurableFollowerHarness()

        async def collect(after_seq: int) -> list[dict[str, Any]]:
            return [
                frame
                async for frame in harness.follow_session_stream(
                    object(),  # type: ignore[arg-type]
                    "session-1",
                    after_seq=after_seq,
                )
            ]

        first = await asyncio.wait_for(collect(-1), timeout=0.2)
        self.assertEqual(
            [frame.get("messageId") for frame in first if frame.get("type") == "start"],
            ["assistant-a"],
        )
        terminal_index = first.index({"type": "finish", "finishReason": "stop"})
        terminal_cursors = [
            frame for frame in first if frame.get("type") == "data-resume-cursor"
        ]
        self.assertGreater(first.index(terminal_cursors[-1]), terminal_index)
        self.assertEqual(
            terminal_cursors[-1],
            {
                "type": "data-resume-cursor",
                "transient": True,
                "data": {
                    "frameSeq": 2,
                    "turnId": "turn-a",
                },
            },
        )

        second = await asyncio.wait_for(collect(2), timeout=0.2)
        self.assertEqual(
            [frame.get("messageId") for frame in second if frame.get("type") == "start"],
            ["assistant-b"],
        )
        self.assertEqual(
            [
                frame for frame in second if frame.get("type") == "data-resume-cursor"
            ][-1]["data"]["frameSeq"],
            4,
        )

    async def test_a_live_terminal_waits_for_its_durable_cursor_before_closing(self) -> None:
        harness = _DurableFollowerHarness()
        harness._session_events_repo.frames = []
        harness._broker = _LiveFrameBroker(
            {
                "type": "ai_sdk_live_frame",
                "live_seq": 7,
                "command_id": "command-a",
                "turn_id": "turn-a",
                "payload": {"type": "finish", "finishReason": "stop"},
            }
        )
        stream = harness.follow_session_stream(
            object(),  # type: ignore[arg-type]
            "session-1",
        )

        terminal_task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        self.assertFalse(
            terminal_task.done(),
            "the response must stay open until the terminal has a durable coordinate",
        )

        durable = {
            "session_id": "session-1",
            "frame_seq": 9,
            "turn_id": "turn-a",
            "scope": "turn",
            "command_id": "command-a",
            "live_seq": 7,
            "payload": {"type": "finish", "finishReason": "stop"},
        }
        harness._session_events_repo.frames.append(durable)
        harness._broker.queue.put_nowait(
            {
                "type": "ai_sdk_frame",
                "frame_seq": 9,
                "turn_id": "turn-a",
                "scope": "turn",
                "command_id": "command-a",
                "live_seq": 7,
                "payload": {"type": "finish", "finishReason": "stop"},
            }
        )

        self.assertEqual(
            await asyncio.wait_for(terminal_task, timeout=0.2),
            {"type": "finish", "finishReason": "stop"},
        )
        self.assertEqual(
            await asyncio.wait_for(anext(stream), timeout=0.2),
            {
                "type": "data-resume-cursor",
                "transient": True,
                "data": {
                    "frameSeq": 9,
                    "turnId": "turn-a",
                },
            },
        )
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=0.2)


class ToolInvocationResumeBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mixin = TurnDispatchStreamingMixin()

    def test_child_run_stream_event_is_only_a_public_invalidation(self) -> None:
        emitted, terminal = _emit_resumable_payloads(
            cursor_tracker=_ResumeCursorTracker(),
            payload={
                "type": "data-subagent",
                "id": "native-frame-1",
                "transient": True,
                "data": {
                    "childRunId": "public-child-1",
                    "controlId": "vendor-control-secret",
                    "engineKind": "hermes",
                    "phase": "progress",
                },
            },
            frame_seq=91,
            payload_scope="session",
            payload_turn_id=None,
            include_resume_cursor=False,
        )

        self.assertFalse(terminal)
        self.assertEqual(
            emitted,
            [
                {
                    "type": "data-child-runs-changed",
                    "transient": True,
                    "data": {"frameSeq": 91},
                }
            ],
        )
        self.assertNotIn("vendor-control-secret", json.dumps(emitted))

    def test_every_session_scoped_frame_uses_the_public_invalidation(self) -> None:
        emitted, terminal = _emit_resumable_payloads(
            cursor_tracker=_ResumeCursorTracker(),
            payload={
                "type": "data-engine-private-fact",
                "data": {"opaqueControl": "must-not-cross-the-wire"},
            },
            frame_seq=92,
            payload_scope="session",
            payload_turn_id=None,
            include_resume_cursor=False,
        )

        self.assertFalse(terminal)
        self.assertEqual(
            emitted,
            [
                {
                    "type": "data-child-runs-changed",
                    "transient": True,
                    "data": {"frameSeq": 92},
                }
            ],
        )
        self.assertNotIn("must-not-cross-the-wire", json.dumps(emitted))

    def test_cursor_stays_before_tool_call_until_the_invocation_resolves(self) -> None:
        tracker = _ResumeCursorTracker()
        frames = (
            {"type": "tool-input-start", "toolCallId": "call-1", "toolName": "Write"},
            {"type": "tool-input-delta", "toolCallId": "call-1", "inputTextDelta": "{}"},
            {"type": "tool-input-available", "toolCallId": "call-1", "toolName": "Write", "input": {}},
            {"type": "tool-approval-request", "toolCallId": "call-1", "approvalId": "approval-1"},
            {
                "type": "data-interaction",
                "data": {
                    "interaction_id": "approval-1",
                    "turn_id": "turn-1",
                    "tool_call_id": "call-1",
                    "tool_name": "Write",
                    "presentation": "tool_approval",
                    "prompt": "Allow Write to continue?",
                    "raw_input": {},
                },
            },
            {"type": "finish", "finishReason": "tool-calls"},
        )

        emitted = []
        for frame_seq, frame in enumerate(frames, start=57):
            payloads, _ = _emit_resumable_payloads(
                cursor_tracker=tracker,
                payload=frame,
                frame_seq=frame_seq,
                payload_scope="turn",
                payload_turn_id="turn-1",
                include_resume_cursor=True,
                include_terminal_resume_cursor=False,
            )
            emitted.extend(payloads)

        self.assertFalse(
            any(payload.get("type") == "data-resume-cursor" for payload in emitted),
            "replay after the invocation would orphan its approval frame",
        )

        resolved, _ = _emit_resumable_payloads(
            cursor_tracker=tracker,
            payload={
                "type": "tool-output-available",
                "toolCallId": "call-1",
                "output": "written",
            },
            frame_seq=63,
            payload_scope="turn",
            payload_turn_id="turn-1",
            include_resume_cursor=True,
            include_terminal_resume_cursor=False,
        )
        self.assertEqual(resolved[-1]["type"], "data-resume-cursor")
        self.assertEqual(resolved[-1]["data"]["frameSeq"], 63)

        tracker_without_prefix = _state(follow_session=True)
        self.assertEqual(
            self.mixin._tail_emit_durable_cursor_for_live_frame(
                tracker_without_prefix,
                payload={
                    "type": "tool-input-available",
                    "toolCallId": "call-2",
                    "toolName": "Write",
                    "input": {},
                },
                frame_seq=70,
                payload_turn_id="turn-2",
            ),
            [],
            "even an available frame without its live start remains an unresolved invocation",
        )

    def test_an_error_advances_the_cursor_before_the_sdk_raises_it(self) -> None:
        emitted, terminal = _emit_resumable_payloads(
            cursor_tracker=_ResumeCursorTracker(),
            payload={"type": "error", "errorText": "turn failed"},
            frame_seq=71,
            payload_scope="turn",
            payload_turn_id="turn-1",
            include_resume_cursor=True,
            include_terminal_resume_cursor=True,
        )

        self.assertTrue(terminal)
        self.assertEqual(
            [frame["type"] for frame in emitted],
            ["data-resume-cursor", "error"],
            "the AI SDK stops parsing at error, so its durable cursor must arrive first",
        )
        self.assertEqual(emitted[0]["data"]["frameSeq"], 71)

    def test_an_unrelated_turn_finish_cannot_resolve_another_turns_tool(self) -> None:
        tracker = _ResumeCursorTracker()
        tracker.observe(
            {"type": "tool-input-start", "toolCallId": "call-a", "toolName": "Write"},
            scope="turn-a",
        )
        tracker.observe(
            {"type": "finish", "finishReason": "stop"},
            scope="background-turn-b",
        )
        self.assertFalse(
            tracker.can_resume_after_current_frame(),
            "a concurrent turn terminal must not erase another turn's parser dependency",
        )

    def test_finish_closes_nonempty_block_sets_without_mutating_during_iteration(self) -> None:
        tracker = _ResumeCursorTracker()
        tracker.observe(
            {"type": "text-start", "id": "text-a"},
            scope="turn-a",
        )
        tracker.observe(
            {"type": "reasoning-start", "id": "reasoning-a"},
            scope="turn-a",
        )
        tracker.observe(
            {"type": "text-start", "id": "text-b"},
            scope="turn-b",
        )

        # A terminal frame closes unterminated text/reasoning blocks without
        # invalidating the session-follow stream or its durable cursor.
        tracker.observe(
            {"type": "finish", "finishReason": "stop"},
            scope="turn-a",
        )

        self.assertTrue(
            tracker.can_resume_after_current_frame(),
            "closing one turn's blocks must leave a resumable parser boundary",
        )
        self.assertEqual(
            tracker.synthesize_missing_starts(
                {"type": "text-end", "id": "text-b"},
                scope="turn-b",
            ),
            [],
            "closing turn-a must not erase turn-b's parser state",
        )
        tracker.observe(
            {"type": "text-end", "id": "text-b"},
            scope="turn-b",
        )
        self.assertTrue(tracker.can_resume_after_current_frame())

    def test_durable_live_counterpart_repairs_a_missing_live_tracker_observation(self) -> None:
        """A durable counterpart must be safe even when its live observation was lost.

        The broker and durable journal are independent deliveries.  A durable
        frame can carry a ``live_seq`` that says the payload was already sent
        even when a reconnect/race prevented the local cursor tracker from
        observing that live payload.  In that case, blindly emitting the
        durable cursor strands the next AI SDK stream after the tool prefix.
        """
        state = _state(follow_session=True)
        unresolved = (
            {"type": "tool-input-start", "toolCallId": "call-1", "toolName": "Write"},
            {"type": "tool-input-delta", "toolCallId": "call-1", "inputTextDelta": "{}"},
            {"type": "tool-input-available", "toolCallId": "call-1", "toolName": "Write", "input": {}},
            {"type": "tool-approval-request", "toolCallId": "call-1", "approvalId": "approval-1"},
            {
                "type": "data-interaction",
                "data": {
                    "interaction_id": "approval-1",
                    "turn_id": "turn-1",
                    "tool_call_id": "call-1",
                    "tool_name": "Write",
                    "presentation": "tool_approval",
                    "prompt": "Allow Write to continue?",
                    "raw_input": {},
                },
            },
            {"type": "finish", "finishReason": "tool-calls"},
        )

        emitted = []
        for frame_seq, payload in enumerate(unresolved, start=57):
            emitted.extend(self.mixin._tail_emit_durable_cursor_for_live_frame(
                state,
                payload=payload,
                frame_seq=frame_seq,
                payload_turn_id="turn-1",
            ))

        self.assertEqual(
            emitted,
            [],
            "a lost live observation must not make the middle of a tool invocation resumable",
        )

        resolved = self.mixin._tail_emit_durable_cursor_for_live_frame(
            state,
            payload={
                "type": "tool-output-available",
                "toolCallId": "call-1",
                "output": "written",
            },
            frame_seq=63,
            payload_turn_id="turn-1",
        )
        self.assertEqual(resolved[-1]["type"], "data-resume-cursor")
        self.assertEqual(resolved[-1]["data"]["frameSeq"], 63)


class _FrameHistory:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.frames: list[dict[str, Any]] = [
            {"session_id": "s-1", "frame_seq": frame_seq, "payload": payload}
            for frame_seq, payload in enumerate(payloads, start=43)
        ]

    async def list_frames(
        self,
        session_id: str,
        *,
        after_seq: int = -1,
        limit: int = 500,
        **_filters: Any,
    ) -> list[dict[str, Any]]:
        self.assert_session(session_id)
        return [
            dict(frame)
            for frame in self.frames
            if int(frame["frame_seq"]) > after_seq
        ][:limit]

    @staticmethod
    def assert_session(session_id: str) -> None:
        if session_id != "s-1":
            raise AssertionError(f"unexpected session: {session_id}")


class RequestedResumeCursorValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_old_cursor_inside_a_tool_invocation_rewinds_to_its_prefix(self) -> None:
        mixin = TurnDispatchStreamingMixin()
        mixin._session_events_repo = _FrameHistory([  # type: ignore[attr-defined]
            {"type": "start", "messageId": "assistant-1"},
            {"type": "reasoning-start", "id": "reasoning-1"},
            {"type": "reasoning-delta", "id": "reasoning-1", "delta": "plan"},
            {"type": "reasoning-end", "id": "reasoning-1"},
            {"type": "tool-input-start", "toolCallId": "call-1", "toolName": "Write"},
            {"type": "tool-input-delta", "toolCallId": "call-1", "inputTextDelta": "{}"},
            {"type": "tool-input-available", "toolCallId": "call-1", "toolName": "Write", "input": {}},
            {"type": "tool-approval-request", "toolCallId": "call-1", "approvalId": "approval-1"},
            {"type": "data-interaction", "data": {"interaction_id": "approval-1"}},
            {"type": "finish", "finishReason": "tool-calls"},
            {"type": "tool-output-available", "toolCallId": "call-1", "output": "written"},
            {"type": "finish", "finishReason": "stop"},
        ])

        # The AI SDK builds fresh parser state on resume, so a cursor inside
        # the unfinished reply replays that whole reply from before its start
        # at frame 43: its identity, and with it the invocation prefix at 47.
        self.assertEqual(
            await mixin._rewind_unsafe_resume_cursor("s-1", requested_after_seq=49),
            42,
            "frame 49 is tool-input-available; replay must begin before frame 47 "
            "tool-input-start and the reply's frame 43 start",
        )
        self.assertEqual(
            await mixin._rewind_unsafe_resume_cursor("s-1", requested_after_seq=52),
            42,
            "approval, interaction and tool-calls finish all depend on the invocation prefix",
        )
        self.assertEqual(
            await mixin._rewind_unsafe_resume_cursor("s-1", requested_after_seq=53),
            42,
            "a resolved invocation still belongs to the unfinished reply that replays it",
        )
        self.assertEqual(
            await mixin._rewind_unsafe_resume_cursor("s-1", requested_after_seq=54),
            54,
            "the reply's terminal completes it and becomes a valid cursor",
        )


class DispatchReturnsAReceiptNotContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_streaming_path_is_dispatch_plus_follow(self) -> None:
        # `stream_ai_stream` must not re-implement admission, journalling or
        # projection: it dispatches through the same entry the ack-only route
        # uses, so the two channels cannot drift into disagreeing about what
        # accepting a turn means.
        import inspect

        source = inspect.getsource(TurnDispatchStreamingMixin.stream_ai_stream)
        self.assertIn("dispatch_turn_input", source)
        for owned_by_dispatch in (
            "_append_command_accepted",
            "_project_command_to_snapshot_and_message",
            "enforce_admission",
        ):
            self.assertNotIn(
                owned_by_dispatch,
                source,
                f"{owned_by_dispatch} belongs to the input channel, not the output one",
            )

    async def test_dispatch_answers_with_the_identifiers_and_no_frames(self) -> None:
        source_annotations = TurnDispatchStreamingMixin.dispatch_turn_input.__annotations__
        self.assertEqual(
            source_annotations.get("return"),
            "dict[str, Any]",
            "the input channel returns a receipt; it is not an iterator",
        )


if __name__ == "__main__":
    unittest.main()
