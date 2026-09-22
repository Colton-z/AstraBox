"""Characterization tests for the turn worker's dispatch and stream seams.

These tests cover four dispatch boundaries. They assert decision
results and state transitions (snapshot fields, emitted journal events,
projected terminal shape), never log text.  Where the target logic lives inside
a deeply-nested ``_run_bridge_command`` closure that reads dozens of loop
nonlocals (``_record_translated_frame``, ``_persist_frames``,
``_project_turn_terminal``) it cannot be invoked without standing up the whole
bridge (background writer/heartbeat tasks, translator, checkpoint lease); those
are pinned at the nearest faithful seam: the module-level pure classifier the
closure delegates to, plus a source-structure assertion over the exact wiring.

Four behaviors pinned:

* **Single-dispatch guard.**
  ``_project_command_accepted`` admits a StartTurn's watermark CAS gated by an
  ``extra_filter={"current_turn_id": None}``: a second concurrently-accepted
  StartTurn cannot re-project over a turn already in flight, while the
  legitimate sequential case (new turn after terminal, current_turn_id back to
  None) and the reconcile path are unaffected.
* **Frame journaling seq / idempotency.** The writer atomically reserves each
  durable batch's session-scoped range and assigns a strictly-monotonic
  ``frame_seq``; replay re-appends of a terminal
  ``data-result`` are guarded (idempotent) by a durable-type scan.
* **Completed-vs-errored terminal classification.** The worker reads only its
  durable finish/error proof. Public ``data-result`` payloads are display data
  and cannot reclassify a turn.
* **Dead / missing runtime binding.** ``run_once`` fails fast on a
  missing command / session; ``_run_interrupt_command`` gates on the turn id,
  no-ops a stale-turn interrupt, and refuses to settle a pending interaction.
"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    build_turn_terminal_snapshot_updates,
    turn_terminal_frame_matches,
)
from astrabox.core.service.orchestrator.session_kernel.workers.models import WorkerWakeup
from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_frames,
    bridge_journal,
    bridge_loop,
    bridge_terminal,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
    _BridgeRunState,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.projections import (
    _TurnProjectionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.worker import (
    TurnWorker,
)


# ── shared fakes ─────────────────────────────────────────────────────────────


class _WatermarkSnapshotsRepo:
    """Session-snapshots fake that mirrors the real ``apply_channel_update``
    contract: a monotonic event-seq watermark CAS plus the OPTIONAL
    ``expected_conversation_state`` / ``extra_filter`` conditional guards. The
    worker never passes ``expected_conversation_state`` from the command
    projection, so this fake makes concurrent double-dispatch observable."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.doc: dict[str, Any] | None = dict(initial) if initial else None
        self.watermark = -1
        self.apply_calls: list[dict[str, Any]] = []
        self.force_calls: list[dict[str, Any]] = []

    async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        _ = session_id
        return dict(self.doc) if self.doc is not None else None

    async def apply_channel_update(
        self,
        session_id: str,
        *,
        channel: str,
        event_seq: int,
        updates: dict[str, Any],
        expected_conversation_state: str | None = None,
        extra_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        self.apply_calls.append(
            {
                "channel": channel,
                "event_seq": int(event_seq),
                "updates": dict(updates),
                "expected_conversation_state": expected_conversation_state,
                "extra_filter": dict(extra_filter) if isinstance(extra_filter, dict) else None,
            }
        )
        if int(event_seq) <= self.watermark:
            return None
        current = self.doc or {}
        if (
            expected_conversation_state is not None
            and current.get("conversation_state") != expected_conversation_state
        ):
            return None
        if isinstance(extra_filter, dict):
            for key, value in extra_filter.items():
                if current.get(key) != value:
                    return None
        base = dict(self.doc) if self.doc is not None else {"session_id": session_id}
        base.update(updates)
        self.doc = base
        self.watermark = int(event_seq)
        return dict(self.doc)

    async def force_update_fields(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        extra_filter: dict[str, Any] | None = None,
    ) -> bool:
        _ = session_id
        self.force_calls.append(
            {
                "updates": dict(updates),
                "extra_filter": dict(extra_filter) if isinstance(extra_filter, dict) else None,
            }
        )
        if self.doc is None:
            return False
        if isinstance(extra_filter, dict):
            for key, value in extra_filter.items():
                if self.doc.get(key) != value:
                    return False
        self.doc.update(updates)
        return True


def _command_event(
    *,
    command_id: str,
    event_seq: int,
    command_type: str,
    turn_id: str,
    content: str = "",
) -> dict[str, Any]:
    return {
        "causation_id": command_id,
        "correlation_id": command_id,
        "turn_id": turn_id,
        "event_seq": event_seq,
        "occurred_at": "2026-07-07T00:00:00+00:00",
        "payload": {"command_type": command_type, "content": content},
    }


# ── Single-dispatch guard ────────────────────────────────────────────────────


class SingleDispatchGuardTests(unittest.IsolatedAsyncioTestCase):
    def _worker(
        self, snapshots: _WatermarkSnapshotsRepo
    ) -> TurnWorker:
        worker = TurnWorker.__new__(TurnWorker)
        worker._session_snapshots_repo = snapshots
        return worker

    async def test_start_turn_projects_processing_via_watermark_cas_only(self) -> None:
        # A StartTurn projection mints PROCESSING for its turn via the
        # event-seq watermark CAS, guarded by an extra_filter that only
        # admits the write when no turn currently owns the active slot
        # (current_turn_id is None). With no active turn the guard admits the
        # write. StartTurn also clears the remote anchor and heartbeats.
        snapshots = _WatermarkSnapshotsRepo(initial=None)
        worker = self._worker(snapshots)

        await worker._project_command_accepted(
            session_id="s-1",
            turn_id="turn-A",
            command_event=_command_event(
                command_id="cmd-A", event_seq=10, command_type="StartTurn", turn_id="turn-A",
                content="hello",
            ),
            command_type="StartTurn",
        )

        self.assertEqual(snapshots.doc["conversation_state"], "PROCESSING")
        self.assertEqual(snapshots.doc["current_turn_id"], "turn-A")
        self.assertEqual(snapshots.doc["current_turn_worker_command_id"], "cmd-A")
        self.assertIsNone(snapshots.doc["current_turn_remote_anchor"])
        self.assertTrue(str(snapshots.doc.get("worker_heartbeat_at") or "").startswith("20"))
        self.assertEqual(len(snapshots.apply_calls), 1)
        call = snapshots.apply_calls[0]
        self.assertEqual(call["event_seq"], 10)
        # No conversation-state guard — the single-dispatch guard is expressed
        # as a current_turn_id-is-empty extra_filter instead.
        self.assertIsNone(call["expected_conversation_state"])
        self.assertEqual(call["extra_filter"], {"current_turn_id": None})

    async def test_concurrent_start_turns_double_project_over_active_turn(self) -> None:
        # A second StartTurn (higher seq, different turn) accepted while the
        # first turn is still in flight must NOT re-project over it. The CAS
        # is guarded by extra_filter={"current_turn_id": None}, so turn-B's
        # write only lands in the empty slot — with turn-A still PROCESSING,
        # the guard mismatches and the write (and both heartbeat force-update
        # fallbacks, which are fenced on turn-B's own id) no-op.
        snapshots = _WatermarkSnapshotsRepo(initial=None)
        worker = self._worker(snapshots)

        await worker._project_command_accepted(
            session_id="s-1",
            turn_id="turn-A",
            command_event=_command_event(
                command_id="cmd-A", event_seq=10, command_type="StartTurn", turn_id="turn-A",
                content="first",
            ),
            command_type="StartTurn",
        )
        self.assertEqual(snapshots.doc["conversation_state"], "PROCESSING")
        self.assertEqual(snapshots.doc["current_turn_id"], "turn-A")

        # A concurrently-accepted StartTurn arrives while turn-A is PROCESSING.
        await worker._project_command_accepted(
            session_id="s-1",
            turn_id="turn-B",
            command_event=_command_event(
                command_id="cmd-B", event_seq=11, command_type="StartTurn", turn_id="turn-B",
                content="second",
            ),
            command_type="StartTurn",
        )

        # turn-A is untouched: the guard refused to re-project turn-B over
        # an active turn, and neither heartbeat fallback matched turn-B's id.
        self.assertEqual(snapshots.doc["conversation_state"], "PROCESSING")
        self.assertEqual(snapshots.doc["current_turn_id"], "turn-A")
        self.assertEqual(snapshots.doc["current_turn_worker_command_id"], "cmd-A")
        self.assertEqual(len(snapshots.apply_calls), 2)
        self.assertEqual(snapshots.apply_calls[1]["extra_filter"], {"current_turn_id": None})
        # Both heartbeat force-update fallbacks ran (fenced on turn-B's own
        # id, which never became current) and both failed to match.
        self.assertEqual(len(snapshots.force_calls), 2)
        self.assertEqual(snapshots.doc.get("current_turn_worker_command_id"), "cmd-A")

    async def test_replayed_start_turn_watermark_rejected_refreshes_heartbeat(self) -> None:
        # A re-delivery of the SAME command (seq not advanced) loses the
        # watermark CAS (apply returns None) and falls back to a force-update
        # heartbeat fenced on (current_turn_id, current_turn_worker_command_id)
        # — idempotent, does not re-open the turn.
        snapshots = _WatermarkSnapshotsRepo(initial=None)
        worker = self._worker(snapshots)
        first = _command_event(
            command_id="cmd-A", event_seq=10, command_type="StartTurn", turn_id="turn-A",
            content="hi",
        )
        await worker._project_command_accepted(
            session_id="s-1", turn_id="turn-A", command_event=first, command_type="StartTurn",
        )

        await worker._project_command_accepted(
            session_id="s-1", turn_id="turn-A", command_event=first, command_type="StartTurn",
        )

        # Second apply lost the CAS (same seq) → fell through to force-update.
        self.assertEqual(len(snapshots.apply_calls), 2)
        self.assertEqual(len(snapshots.force_calls), 1)
        self.assertEqual(
            snapshots.force_calls[0]["extra_filter"],
            {"current_turn_id": "turn-A", "current_turn_worker_command_id": "cmd-A"},
        )
        self.assertEqual(snapshots.doc["current_turn_id"], "turn-A")


# ── Frame journaling: seq assignment + replay idempotency ─────────────────────


class FrameJournalSeqTests(unittest.IsolatedAsyncioTestCase):
    def _worker(self, turn_max: int | None) -> TurnWorker:
        worker = TurnWorker.__new__(TurnWorker)
        worker._session_events_repo = SimpleNamespace(
            get_max_turn_frame_seq=AsyncMock(return_value=turn_max),
        )
        return worker

    async def test_get_max_frame_seq_reads_the_turns_own_watermark(self) -> None:
        # Allocation is session-scoped, so the session maximum can belong to a
        # concurrent background-lane append. A turn's watermark must be asked
        # for by turn, or the assistant message claims frames that are not its.
        worker = self._worker(turn_max=4)
        self.assertEqual(await worker._get_max_frame_seq("s-1", "turn-1"), 4)
        worker._session_events_repo.get_max_turn_frame_seq.assert_awaited_once_with(
            "s-1", turn_id="turn-1"
        )

    async def test_get_max_frame_seq_none_when_turn_has_no_frames(self) -> None:
        worker = self._worker(turn_max=None)
        self.assertIsNone(await worker._get_max_frame_seq("s-1", "turn-1"))

    async def test_get_max_frame_seq_none_without_turn_id_skips_repo(self) -> None:
        worker = self._worker(turn_max=4)
        self.assertIsNone(await worker._get_max_frame_seq("s-1", ""))
        worker._session_events_repo.get_max_turn_frame_seq.assert_not_awaited()

    def test_frame_writer_reserves_batch_and_assigns_monotonic_seq(self) -> None:
        # _persist_frames reserves one session-scoped range for the whole batch,
        # then stamps each doc with the current seq and increments by one.
        persist = inspect.getsource(bridge_journal._persist_frames)
        self.assertIn("allocate_session_frame_seq", persist)
        self.assertIn("count=len(frames)", persist)
        self.assertIn('"frame_seq": current_seq', persist)
        self.assertIn("frame_seq = current_seq + 1", persist)

    def test_replay_terminal_result_reappend_is_idempotent(self) -> None:
        # On answer-continuation replay the worker must not double-journal the
        # terminal data-result: the re-append is guarded by a durable-type scan.
        # This guard is a (worker, state, ctx)-parameterised free function in
        # the bridge_terminal module; the scan-then-skip shape is pinned
        # there.
        guard = inspect.getsource(
            bridge_terminal._append_replay_terminal_result_if_missing
        )
        self.assertIn("_turn_has_durable_frame_type(", guard)
        self.assertIn('"data-result"', guard)
        self.assertIn("return", guard)

    async def test_session_child_frame_is_durable_before_publish_and_has_no_turn(self) -> None:
        queued = AsyncMock()
        state = _BridgeRunState()
        state.effective_turn_id = "turn-1"
        frame = session_scoped_engine_frame(
            {
                "type": "data-subagent",
                "id": "child:start",
                "data": {
                    "kind": "lifecycle",
                    "childRunId": "child-1",
                    "engineKind": "claude_code",
                    "phase": "started",
                },
            }
        )
        ctx = SimpleNamespace(
            ordered_assistant_segments=[],
            durable_semantic_coalescer=SimpleNamespace(
                ingest=lambda item: [dict(item)]
            ),
        )

        with patch.object(
            bridge_journal,
            "_enqueue_durable_frames",
            queued,
        ), patch.object(
            bridge_frames,
            "_publish_live_frame",
            AsyncMock(),
        ) as publish_live:
            await bridge_frames._process_translated_frames(
                SimpleNamespace(),
                state,
                ctx,
                [frame],
            )

        publish_live.assert_not_awaited()
        queued.assert_awaited_once()
        queued_frames = queued.await_args.args[3]
        self.assertEqual(queued_frames, [(frame, None)])

    async def test_session_child_frame_persists_scope_and_publishes_after_append(self) -> None:
        stored: list[dict[str, Any]] = []
        published: list[dict[str, Any]] = []

        class _Repo:
            async def allocate_session_frame_seq(self, _session_id: str, *, count: int) -> int:
                if count != 1:
                    raise AssertionError(f"expected one frame, got {count}")
                return 12

            async def append_frame(self, doc: dict[str, Any]) -> None:
                stored.append(dict(doc))

        async def run_mongo(_label: str, operation: Any, *, turn_id: str | None) -> Any:
            self.assertIsNone(turn_id)
            return await operation()

        async def publish(event: dict[str, Any]) -> None:
            self.assertEqual(len(stored), 1, "publish must follow durable append")
            published.append(dict(event))

        worker = SimpleNamespace(_session_events_repo=_Repo())
        state = _BridgeRunState()
        ctx = SimpleNamespace(
            session_id="session-1",
            command_id="command-1",
            ensure_frame_seq_initialized=AsyncMock(),
            run_live_frame_mongo_op=run_mongo,
            publish_broker_event=publish,
        )
        frame = session_scoped_engine_frame(
            {
                "type": "data-subagent",
                "id": "child:start",
                "data": {"kind": "lifecycle", "childRunId": "child-1"},
            }
        )

        docs = await bridge_journal._persist_frames(
            worker,
            state,
            ctx,
            [(frame, None)],
        )

        self.assertEqual(docs[0]["scope"], "session")
        self.assertIsNone(docs[0]["turn_id"])
        self.assertNotIn("__engine_frame_scope", docs[0]["payload"])
        self.assertTrue(docs[0]["payload"]["transient"])
        self.assertEqual(published[0]["turn_id"], None)
        self.assertEqual(published[0]["scope"], "session")


# ── Completed-vs-errored terminal classification ────────────────────────────


class TerminalClassificationTests(unittest.TestCase):
    def test_completed_terminal_shape_is_successful_and_clears_turn(self) -> None:
        # turn_failed=False + READY → COMPLETED: IDLE, turn cleared, no error,
        # no recovery anchor retained. This is the shape a false-green settles.
        updates = build_turn_terminal_snapshot_updates(
            turn_id="turn-1",
            status="COMPLETED",
            error_text=None,
            command_id="cmd-1",
            terminal_frame=None,
        )
        self.assertEqual(updates["conversation_state"], "IDLE")
        self.assertIsNone(updates["current_turn_id"])
        self.assertEqual(updates["last_turn_id"], "turn-1")
        self.assertEqual(updates["last_turn_status"], "COMPLETED")
        self.assertIsNone(updates["last_turn_error"])
        self.assertIsNone(updates["current_turn_remote_anchor"])
        self.assertIsNone(updates["turn_recovery_phase"])
        self.assertIsNone(updates["last_turn_failure_phase"])

    def test_failed_terminal_shape_carries_error_and_recovery_anchor(self) -> None:
        updates = build_turn_terminal_snapshot_updates(
            turn_id="turn-1",
            status="FAILED",
            error_text="API Error: 402 Insufficient Balance",
            command_id="cmd-1",
            recovery_anchor={"sandbox_turn_id": 3, "last_sandbox_seq": 9},
            failure_phase="post_dispatch",
            terminal_frame=None,
        )
        self.assertEqual(updates["last_turn_status"], "FAILED")
        self.assertEqual(updates["last_turn_error"], "API Error: 402 Insufficient Balance")
        self.assertEqual(updates["current_turn_remote_anchor"], {"sandbox_turn_id": 3, "last_sandbox_seq": 9})
        self.assertEqual(updates["turn_recovery_phase"], "TRANSCRIPT_PENDING")
        self.assertEqual(updates["last_turn_failure_phase"], "post_dispatch")

    def test_completed_requires_matching_finish_frame_proof(self) -> None:
        # The COMPLETED branch is gated on a durable finish-frame proof; a
        # matching finish greens, a mismatched command / an error type does not.
        proof = {
            "turn_id": "turn-1",
            "command_id": "cmd-1",
            "frame_seq": 7,
            "type": "finish",
            "finish_reason": AI_SDK_FINISH_REASON_STOP,
        }
        self.assertTrue(
            turn_terminal_frame_matches(
                proof, turn_id="turn-1", command_id="cmd-1",
                frame_type="finish", finish_reason=AI_SDK_FINISH_REASON_STOP,
            )
        )
        self.assertFalse(
            turn_terminal_frame_matches(
                proof, turn_id="turn-1", command_id="cmd-OTHER",
                frame_type="finish", finish_reason=AI_SDK_FINISH_REASON_STOP,
            )
        )
        self.assertFalse(
            turn_terminal_frame_matches(
                {**proof, "type": "error"}, turn_id="turn-1", command_id="cmd-1",
                frame_type="finish", finish_reason=AI_SDK_FINISH_REASON_STOP,
            )
        )

    def test_public_result_data_never_classifies_the_turn(self) -> None:
        # Result cards are an open browser extension point. An engine terminal
        # crosses a different typed seam, so even a payload containing fields
        # that resemble a verdict cannot settle or fail a turn.
        ctx = SimpleNamespace(ordered_assistant_segments=[])
        state = _BridgeRunState()
        recorded = bridge_frames._record_translated_frame(
            None,
            state,
            ctx,
            {
                "type": "data-result",
                "data": {
                    "isError": True,
                    "terminalReason": "api_error",
                    "newVendorMetric": 7,
                },
            },
        )

        self.assertTrue(recorded)
        self.assertFalse(state.saw_result_frame)
        self.assertFalse(state.saw_error_event)

    def test_streamed_turn_result_message_does_not_reemit_answer_text(self) -> None:
        # The engine translator is the only writer. Its Result fallback must
        # see text already emitted from the live stream, while the generic
        # worker projects the standard frames once into the terminal message.
        state = _BridgeRunState()
        ctx = SimpleNamespace(ordered_assistant_segments=[])
        cursor = ClaudeStreamCursor()
        live_frames: list[dict[str, Any]] = []
        for message in (
            {
                "__sdk_type": "StreamEvent",
                "uuid": "evt-start",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text"},
                },
            },
            {
                "__sdk_type": "StreamEvent",
                "uuid": "evt-delta",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "茶，源于中国"},
                },
            },
            {
                "__sdk_type": "StreamEvent",
                "uuid": "evt-stop",
                "event": {"type": "content_block_stop", "index": 0},
            },
        ):
            live_frames.extend(
                translate_claude_sdk_message(message, envelope_seq=1, cursor=cursor)
            )
        result_frames = list(
            translate_claude_sdk_message(
                {
                    "__sdk_type": "ResultMessage",
                    "subtype": "success",
                    "result": "茶，源于中国",
                },
                envelope_seq=2,
                cursor=cursor,
            )
        )
        frame_types = [str(frame.get("type") or "") for frame in result_frames]
        self.assertNotIn("text-delta", frame_types)
        self.assertNotIn("text-start", frame_types)
        for frame in live_frames:
            bridge_frames._record_translated_frame(None, state, ctx, frame)
        bridge_frames._record_translated_frame(
            None,
            state,
            ctx,
            {"type": "data-result", "data": {"result": "茶，源于中国"}},
        )
        blocks = _TurnProjectionMixin()._build_terminal_assistant_blocks(state, ctx)
        self.assertEqual(
            [block for block in blocks if block.get("type") == "text"],
            [{"type": "text", "text": "茶，源于中国"}],
        )

    def test_result_only_turn_keeps_the_result_text_fallback(self) -> None:
        # The fallback belongs to the Claude translator, not a second generic
        # projector. The generic worker consumes the resulting text frames.
        state = _BridgeRunState()
        ctx = SimpleNamespace(ordered_assistant_segments=[])
        frames = list(
            translate_claude_sdk_message(
                {
                    "__sdk_type": "ResultMessage",
                    "subtype": "success",
                    "result": "只有结果文本",
                },
                envelope_seq=1,
                cursor=ClaudeStreamCursor(),
            )
        )
        deltas = [f for f in frames if str(f.get("type") or "") == "text-delta"]
        self.assertEqual([str(f.get("delta")) for f in deltas], ["只有结果文本"])
        for frame in frames:
            if frame.get("type") != "result":
                bridge_frames._record_translated_frame(None, state, ctx, frame)
        bridge_frames._record_translated_frame(
            None,
            state,
            ctx,
            {"type": "data-result", "data": {"result": "只有结果文本"}},
        )
        blocks = _TurnProjectionMixin()._build_terminal_assistant_blocks(state, ctx)
        self.assertEqual(
            [block for block in blocks if block.get("type") == "text"],
            [{"type": "text", "text": "只有结果文本"}],
        )

    def test_terminal_settlement_does_not_read_public_result_verdicts(self) -> None:
        bridge_src = inspect.getsource(bridge_loop._run_bridge_command)
        self.assertNotIn("saw_error_result", bridge_src)


# ── Dead / missing runtime binding ───────────────────────────────────────────


class DeadMissingBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_missing_command_event_raises(self) -> None:
        worker = TurnWorker.__new__(TurnWorker)
        worker._session_events_repo = SimpleNamespace(
            get_command_event=AsyncMock(return_value=None),
        )
        wakeup = WorkerWakeup(
            session_id="s-1", channel="conversation", command_id="cmd-missing",
        )
        with self.assertRaises(RuntimeError) as ctx:
            await worker.run_once(wakeup)
        self.assertIn("missing command.accepted event", str(ctx.exception))

    async def test_run_once_missing_session_raises(self) -> None:
        worker = TurnWorker.__new__(TurnWorker)
        worker._session_events_repo = SimpleNamespace(
            get_command_event=AsyncMock(
                return_value=_command_event(
                    command_id="cmd-1", event_seq=5, command_type="StartTurn", turn_id="turn-1",
                )
            ),
        )
        worker._sessions_repo = SimpleNamespace(get_session=AsyncMock(return_value=None))
        wakeup = WorkerWakeup(session_id="s-gone", channel="conversation", command_id="cmd-1")
        with self.assertRaises(RuntimeError) as ctx:
            await worker.run_once(wakeup)
        self.assertIn("missing session", str(ctx.exception))

    def _interrupt_worker(
        self,
        *,
        interrupt_mark_lands: bool = True,
        snapshot: dict[str, Any] | None = None,
        session: dict[str, Any] | None = None,
        active_interaction: dict[str, Any] | None = None,
        frames: list[dict[str, Any]] | None = None,
        apply_result: dict[str, Any] | None = None,
    ) -> tuple[TurnWorker, SimpleNamespace, AsyncMock, list[dict[str, Any]]]:
        journal_events: list[dict[str, Any]] = []

        async def _append_event(event: dict[str, Any]) -> dict[str, Any]:
            stored = {**dict(event), "event_seq": 99}
            journal_events.append(stored)
            return stored

        turn_service = SimpleNamespace(interrupt_engine_turn=AsyncMock())
        worker = TurnWorker.__new__(TurnWorker)
        worker._sessions_repo = SimpleNamespace(
            get_session=AsyncMock(return_value=session),
        )
        worker._session_snapshots_repo = SimpleNamespace(
            get_snapshot=AsyncMock(return_value=snapshot),
            apply_channel_update=AsyncMock(return_value=apply_result),
            force_update_fields=AsyncMock(return_value=interrupt_mark_lands),
        )
        worker._interaction_snapshots_repo = SimpleNamespace(
            get_interaction=AsyncMock(return_value=active_interaction),
            get_active_interaction=AsyncMock(return_value=active_interaction),
        )
        worker._session_events_repo = SimpleNamespace(
            append_event=_append_event,
            list_frames=AsyncMock(return_value=list(frames or []))
        )
        worker._turn_service = turn_service
        return worker, turn_service, worker._session_snapshots_repo.force_update_fields, journal_events

    async def test_interrupt_without_turn_id_is_idle_noop(self) -> None:
        # No turn id → the interrupt cannot target a binding; the worker returns
        # idle without requesting or dispatching anything.
        worker, turn_service, interrupt_mark, journal = self._interrupt_worker()
        seq, result = await worker._run_interrupt_command(
            user=SimpleNamespace(user_id="u"),
            session_id="s-1",
            command_event={"causation_id": "cmd-1", "event_seq": 42, "turn_id": ""},
        )
        self.assertEqual(seq, 42)
        self.assertEqual(result["status"], "idle")
        interrupt_mark.assert_not_awaited()
        turn_service.interrupt_engine_turn.assert_not_awaited()
        self.assertEqual(journal, [])

    async def test_interrupt_stale_turn_mismatch_is_idle_noop(self) -> None:
        # The snapshot mark CAS lost AND the snapshot's current turn differs →
        # a stale interrupt for a dead binding: idle, no runtime dispatch, no
        # journal event.
        worker, turn_service, _req, journal = self._interrupt_worker(
            interrupt_mark_lands=False,
            snapshot={"current_turn_id": "turn-OTHER"},
        )
        seq, result = await worker._run_interrupt_command(
            user=SimpleNamespace(user_id="u"),
            session_id="s-1",
            command_event={"causation_id": "cmd-1", "event_seq": 7, "turn_id": "turn-1"},
        )
        self.assertEqual(seq, 7)
        self.assertEqual(result["status"], "idle")
        turn_service.interrupt_engine_turn.assert_not_awaited()
        self.assertEqual(journal, [])

    async def test_interrupt_with_pending_interaction_dispatches_runtime_interrupt(self) -> None:
        # A pending interaction is an active turn awaiting the runner's
        # PreToolUse resolution — InterruptTurn cancels the engine turn like
        # any other active turn (the runner resolves its pending wait as part
        # of the interrupt); there is no separate answer-shaped interrupt.
        pending = {
            "interaction_id": "iid-1",
            "turn_id": "turn-1",
            "interaction_state": "OPEN",
            "active": True,
        }
        worker, turn_service, _req, journal = self._interrupt_worker(
            interrupt_mark_lands=True,
            snapshot={"current_turn_id": "turn-1", "active_interaction_id": "iid-1"},
            session={"sandbox_id": "sbx-1", "sandbox_endpoint": "http://sidecar.local"},
            active_interaction=pending,
        )
        seq, result = await worker._run_interrupt_command(
            user=SimpleNamespace(user_id="u"),
            session_id="s-1",
            command_event={"causation_id": "cmd-1", "event_seq": 8, "turn_id": "turn-1"},
        )
        turn_service.interrupt_engine_turn.assert_awaited_once_with(
            {"sandbox_id": "sbx-1", "sandbox_endpoint": "http://sidecar.local"}
        )
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(
            any(e.get("event_type") == "turn.interrupt_requested" for e in journal)
        )

    async def test_interrupt_dispatches_to_runtime_and_appends_event(self) -> None:
        # Happy path: an active matching turn with a live runtime → the engine
        # turn is interrupted through the runtime manager (engine client), and
        # a turn.interrupt_requested event is journaled.
        worker, turn_service, interrupt_mark, journal = self._interrupt_worker(
            interrupt_mark_lands=True,
            snapshot={"current_turn_id": "turn-1"},
            session={"sandbox_id": "sbx-1", "sandbox_endpoint": "http://sidecar.local"},
            active_interaction=None,
        )
        seq, result = await worker._run_interrupt_command(
            user=SimpleNamespace(user_id="u"),
            session_id="s-1",
            command_event={
                "causation_id": "cmd-int", "correlation_id": "corr-1",
                "event_seq": 8, "turn_id": "turn-1",
            },
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(seq, 99)
        # The mark must land on the SNAPSHOT (the store that owns
        # current_turn_id) CAS-scoped to the interrupted turn — the park side
        # reads the same store, which is what closes the park-vs-interrupt
        # race. A mark scoped against the sessions row's terminal-time mirror
        # never lands during a live turn.
        mark_call = interrupt_mark.await_args_list[0]
        self.assertEqual(mark_call.args[0], "s-1")
        self.assertTrue(bool(mark_call.args[1].get("interrupt_requested")))
        self.assertEqual(mark_call.kwargs.get("extra_filter"), {"current_turn_id": "turn-1"})
        turn_service.interrupt_engine_turn.assert_awaited_once_with(
            {"sandbox_id": "sbx-1", "sandbox_endpoint": "http://sidecar.local"}
        )
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["event_type"], "turn.interrupt_requested")
        self.assertEqual(journal[0]["payload"]["command_id"], "cmd-int")

    async def test_pre_first_token_interrupt_force_settles_the_turn(self) -> None:
        # The interrupt-settle race: an active PROCESSING turn with ZERO frames
        # (interrupt landed before the first token; the sidecar suppresses the
        # terminal) → the worker force-settles it with a terminal event so
        # the UI does not hang "Stopping…".
        worker, _rm, _req, journal = self._interrupt_worker(
            interrupt_mark_lands=True,
            snapshot={"current_turn_id": "turn-1", "conversation_state": "PROCESSING"},
            session={"sandbox_id": "sbx-1", "sandbox_endpoint": "http://sidecar.local"},
            active_interaction=None,
            frames=[],  # no frames -> the hang case
            apply_result={"conversation_state": "IDLE"},
        )
        seq, result = await worker._run_interrupt_command(
            user=SimpleNamespace(user_id="u"),
            session_id="s-1",
            command_event={"causation_id": "cmd-int", "event_seq": 8, "turn_id": "turn-1"},
        )
        self.assertEqual(result["status"], "accepted")
        # A terminal was appended (after turn.interrupt_requested).
        # The terminal follows the OUTCOME: the user ended the turn, so it ends
        # completed. This platform-side race closure did not observe an engine
        # terminal, so it must not invent a vendor terminal reason.
        self.assertEqual(
            [e["event_type"] for e in journal],
            ["turn.interrupt_requested", "turn.completed"],
        )
        self.assertIsNone(journal[-1]["payload"]["terminal_reason"])
        self.assertIsNone(journal[-1]["payload"]["failure_phase"])
        self.assertIsNone(journal[-1]["payload"]["error_text"])
        worker._session_snapshots_repo.apply_channel_update.assert_awaited_once()
        # The returned seq is the settle event's seq (the settling write).
        self.assertEqual(seq, 99)

    async def test_interrupt_on_parked_interaction_settles_and_deactivates(self) -> None:
        # The turn parked awaiting an interaction (WAITING_FOR_INTERACTION):
        # its worker exited at the interaction boundary, so nothing is left to
        # consume the cancelled terminal the runtime interrupt produces. The
        # interrupt command must settle the turn itself AND deactivate the
        # pending interaction — without this the session sits WAITING_INPUT
        # forever after a Stop.
        worker, _rm, _req, journal = self._interrupt_worker(
            interrupt_mark_lands=True,
            snapshot={
                "current_turn_id": "turn-1",
                "conversation_state": "WAITING_FOR_INTERACTION",
            },
            session={"sandbox_id": "sbx-1", "sandbox_endpoint": "http://sidecar.local"},
            active_interaction={"interaction_id": "i-1", "turn_id": "turn-1"},
            apply_result={"conversation_state": "IDLE"},
        )
        deactivate = AsyncMock(return_value=1)
        worker._interaction_snapshots_repo.deactivate_active_for_turn = deactivate
        seq, result = await worker._run_interrupt_command(
            user=SimpleNamespace(user_id="u"),
            session_id="s-1",
            command_event={"causation_id": "cmd-int", "event_seq": 8, "turn_id": "turn-1"},
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(
            [e["event_type"] for e in journal],
            ["turn.interrupt_requested", "turn.completed"],
        )
        # The worker closes the platform interaction state; only an adapter may
        # supply a vendor terminal reason.
        self.assertIsNone(journal[-1]["payload"]["terminal_reason"])
        self.assertIsNone(journal[-1]["payload"]["failure_phase"])
        self.assertIsNone(journal[-1]["payload"]["error_text"])
        deactivate.assert_awaited_once_with("s-1", "turn-1")
        # The settle CAS pins the state it is settling FROM.
        kwargs = worker._session_snapshots_repo.apply_channel_update.await_args.kwargs
        self.assertEqual(
            kwargs.get("expected_conversation_state"), "WAITING_FOR_INTERACTION"
        )
        self.assertEqual(seq, 99)

    async def test_interrupt_with_frames_leaves_settle_to_the_bridge(self) -> None:
        # Frames already exist (post-first-token) → the bridge owns the terminal
        # settle (it may carry partial content); the worker must NOT force-settle.
        worker, _rm, _req, journal = self._interrupt_worker(
            interrupt_mark_lands=True,
            snapshot={"current_turn_id": "turn-1", "conversation_state": "STREAMING"},
            session={"sandbox_id": "sbx-1", "sandbox_endpoint": "http://sidecar.local"},
            active_interaction=None,
            frames=[{"frame_seq": 0}],  # content produced -> not the hang case
        )
        await worker._run_interrupt_command(
            user=SimpleNamespace(user_id="u"),
            session_id="s-1",
            command_event={"causation_id": "cmd-int", "event_seq": 8, "turn_id": "turn-1"},
        )
        self.assertEqual([e["event_type"] for e in journal], ["turn.interrupt_requested"])
        worker._session_snapshots_repo.apply_channel_update.assert_not_awaited()


class EngineStreamDetachTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unsettled_detach_marks_the_binding_for_a_control_plane_probe(
        self,
    ) -> None:
        update_session = AsyncMock(return_value=True)
        worker = SimpleNamespace(
            _sessions_repo=SimpleNamespace(update_session=update_session),
        )
        state = _BridgeRunState(effective_turn_id="turn-1", last_event_seq=17)
        ctx = SimpleNamespace(session_id="session-1")

        with patch.object(
            bridge_loop,
            "utcnow_iso",
            return_value="2026-08-17T20:00:00+00:00",
        ):
            result = await bridge_loop._handle_engine_stream_detached(
                worker,
                state,
                ctx,
                EngineStreamDetached("transport closed"),
            )

        self.assertEqual(result, 17)
        update_session.assert_awaited_once_with(
            "session-1",
            {"sandbox_liveness_suspect_at": "2026-08-17T20:00:00+00:00"},
            touch_updated_at=False,
        )

    async def test_a_detach_after_terminal_does_not_schedule_a_sandbox_probe(
        self,
    ) -> None:
        update_session = AsyncMock(return_value=True)
        worker = SimpleNamespace(
            _sessions_repo=SimpleNamespace(update_session=update_session),
        )
        state = _BridgeRunState(
            effective_turn_id="turn-1",
            last_event_seq=23,
            turn_settled=True,
        )

        result = await bridge_loop._handle_engine_stream_detached(
            worker,
            state,
            SimpleNamespace(session_id="session-1"),
            EngineStreamDetached("normal terminal teardown"),
        )

        self.assertEqual(result, 23)
        update_session.assert_not_awaited()

    async def test_a_failed_probe_mark_stays_non_terminal_and_is_reported(
        self,
    ) -> None:
        update_session = AsyncMock(side_effect=RuntimeError("database unavailable"))
        worker = SimpleNamespace(
            _sessions_repo=SimpleNamespace(update_session=update_session),
        )
        state = _BridgeRunState(effective_turn_id="turn-1", last_event_seq=31)

        with patch.object(bridge_loop.logger, "warning") as warning:
            result = await bridge_loop._handle_engine_stream_detached(
                worker,
                state,
                SimpleNamespace(session_id="session-1"),
                EngineStreamDetached("transport closed"),
            )

        self.assertEqual(result, 31)
        self.assertFalse(state.turn_settled)
        self.assertTrue(
            any(
                "failed to mark" in str(call.args[0])
                for call in warning.call_args_list
            )
        )


# ── helpers ──────────────────────────────────────────────────────────────────


def _slice_between(source: str, start_header: str, end_header: str) -> str:
    """Return the source slice of one nested closure inside a larger method,
    from its ``def`` header up to the next named closure's header."""
    start = source.index(start_header)
    end = source.index(end_header, start + len(start_header))
    return source[start:end]


if __name__ == "__main__":
    unittest.main()


# ── the failure phase reaching the live stream, not just history ────────────


class TurnFailureStreamProjectionTests(unittest.TestCase):
    """A failed turn's stream carries a versioned ``data-turn-failure`` data
    part with the phase (pre_dispatch = the engine never received the write,
    retry-safe; post_dispatch = it may have). Clients drive retry/settlement
    affordances off this without racing the stream close to poll the detail."""

    def test_error_terminal_emits_the_versioned_failure_data_part(self) -> None:
        source = inspect.getsource(bridge_terminal._append_terminal_signal)
        self.assertIn('"data-turn-failure"', source)
        self.assertIn('"failure_phase": failure_phase', source)
        self.assertIn('"version": 1', source)
        self.assertIn(
            'state.dispatch_confirmed else "pre_dispatch"', source,
            "the wire phase derives from the same dispatch_confirmed signal "
            "as the durable turn_failure block",
        )

    def test_failure_data_part_is_replay_idempotent(self) -> None:
        source = inspect.getsource(bridge_terminal._append_terminal_signal)
        self.assertIn("_turn_has_durable_frame_type(", source)
        # And the deterministic identity mirrors data-result's.
        normalize = inspect.getsource(bridge_journal._normalize_frame)
        self.assertIn('"data-turn-failure"', normalize)
        self.assertIn('f"turn-failure:{failure_id}"', normalize)

    def test_finish_terminal_emits_no_failure_data_part(self) -> None:
        # The data part is scoped to the error branch: a clean finish appends
        # only the standard finish frame.
        source = inspect.getsource(bridge_terminal._append_terminal_signal)
        error_branch = source.split('payload_doc = {"type": "error"')[1]
        self.assertIn('"data-turn-failure"', error_branch)
        finish_branch = source.split('payload_doc = {"type": "error"')[0]
        self.assertNotIn('"data-turn-failure"', finish_branch)
