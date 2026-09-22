"""Characterization tests for ``SessionKernelService._reconcile_stuck_turn``.

Each test drives one named tier of the stuck-turn reconciler and asserts the
*decision result* — the
returned snapshot state, the emitted journal events, and the snapshot CAS
writes — never log text.

Tiers pinned (line refs are approximate, current tree):

* Entry guards + Tier-2 gate (``None`` snapshot, non-active leave-alone).
* Transcript-pending handoff — IDLE + FAILED is resolved by the coordinator
  before the active-turn ladder, including a bridge that died before it could
  report a remote mirror coordinate.
* Tier 2 — active-state journal-terminal replay.
* Tier 2-checkpoint — COMMITTED-checkpoint split-brain convergence.
* Tier 2a — turn.awaiting_interaction journal replay (and the resolved no-op).
* WAITING_FOR_INTERACTION interaction-state preservation.
* No-anchor delivery adjudication: fresh no-op, checkpoint-evidence defer,
  RECEIVED+FAILED, pre_dispatch FAILED, NOT_RECEIVED.
* Has-anchor stale convergence: fresh-worker no-ops and the
  stale->FAILED->recurse settle.

The checkpoint tier is covered lightly here: this file's subject is the
reconcile ladder, and a deeper checkpoint case belongs beside the checkpoint
code rather than in the ladder's characterization.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from astrabox.core.service.orchestrator.session_kernel.service import (
    SessionKernelService,
)

_OLD_ISO = "2020-01-01T00:00:00+00:00"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _FakeJournal:
    """Journal keyed purely by ``event_type`` (the only discriminator the
    reconciler uses across its many ``list_events`` calls). ``append_event``
    hands back a strictly-increasing ``event_seq`` so the monotonic CAS guard
    downstream always sees a fresh sequence."""

    def __init__(
        self,
        events_by_type: dict[str, list[dict[str, Any]]] | None = None,
        *,
        start_seq: int = 1000,
    ) -> None:
        self._events_by_type = {
            k: [dict(e) for e in v] for k, v in (events_by_type or {}).items()
        }
        self.appended: list[dict[str, Any]] = []
        self.list_events_calls: list[str] = []
        self._next_seq = start_seq

    async def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        channel: str | None = None,
        turn_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.list_events_calls.append(str(event_type or ""))
        return [dict(e) for e in self._events_by_type.get(str(event_type or ""), [])]

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        seq = self._next_seq
        self._next_seq += 1
        stored = {**event, "event_seq": seq}
        self.appended.append(stored)
        return stored

    async def try_claim_event(
        self,
        event: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        existing = next(
            (
                row
                for row in self.appended
                if row.get("event_type") == event.get("event_type")
                and row.get("causation_id") == event.get("causation_id")
            ),
            None,
        )
        if existing is not None:
            return dict(existing), False
        return await self.append_event(event), True

    async def list_frames(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class _FakeSnapshots:
    """Records every ``apply_channel_update`` and returns the base snapshot with
    the update dict merged over it — a realistic converged snapshot, so tests
    can assert on the resulting ``conversation_state`` / ``delivery_state``."""

    def __init__(self, base: dict[str, Any] | None = None) -> None:
        self._base = dict(base or {})
        self.apply_calls: list[dict[str, Any]] = []

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return dict(self._base)

    async def apply_channel_update(
        self,
        session_id: str,
        *,
        channel: str,
        event_seq: int,
        updates: dict[str, Any],
        expected_conversation_state: str | None = None,
        extra_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.apply_calls.append(
            {
                "event_seq": event_seq,
                "updates": dict(updates),
                "expected_conversation_state": expected_conversation_state,
                "extra_filter": extra_filter,
            }
        )
        return {**self._base, **updates}


def _make_service(snapshot_base: dict[str, Any] | None = None) -> Any:
    """A bare ``SessionKernelService`` with every seam ``_reconcile_stuck_turn``
    touches replaced by an inert default (repos return nothing; delegated helper
    methods return the value that makes the reconciler treat them as no-ops).
    Individual tests override the seams relevant to their tier."""

    service = SessionKernelService.__new__(SessionKernelService)
    service._session_events_repo = _FakeJournal()
    service._session_snapshots_repo = _FakeSnapshots(snapshot_base)
    service._turn_checkpoints_repo = SimpleNamespace(
        get_checkpoint=AsyncMock(return_value=None),
        mark_terminal_committed=AsyncMock(return_value=None),
        claim_stale_active_owner=AsyncMock(return_value=None),
    )
    service._interaction_snapshots_repo = SimpleNamespace(
        get_interaction=AsyncMock(return_value=None),
        get_active_interaction=AsyncMock(return_value=None),
        deactivate_all_active=AsyncMock(return_value=True),
        project_open_interaction=AsyncMock(return_value=None),
    )
    service._message_view = SimpleNamespace(
        get_assistant_message_for_turn=AsyncMock(return_value=None)
    )
    service._session_events_repo.list_frames = AsyncMock(return_value=[])
    service._wakeup_turn_coordinator = MagicMock()
    service._replay_projection_from_journal_terminal = AsyncMock(return_value=None)
    service._resume_orphaned_answer_command = AsyncMock(return_value=None)
    service._resolve_sandbox_endpoint = AsyncMock(return_value="")
    service._try_recover_pending_interaction = AsyncMock(return_value=None)
    service._recover_stale_active_checkpoint_from_terminal_mirror = AsyncMock(
        return_value=None
    )
    service._collect_completed_turn_from_sandbox = AsyncMock(
        return_value={"completed": False}
    )
    service._try_recover_from_transcript = AsyncMock(return_value=None)
    return service


# ── Entry guards + Tier-2 gate ───────────────────────────────────────────────


class EntryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_snapshot_is_returned_untouched(self) -> None:
        service = _make_service()
        result = await service._reconcile_stuck_turn(
            session_id="s", session={}, snapshot=None
        )
        self.assertIsNone(result)
        self.assertEqual(service._session_events_repo.appended, [])
        self.assertEqual(service._session_snapshots_repo.apply_calls, [])

    async def test_idle_healthy_non_active_snapshot_is_left_alone(self) -> None:
        # A completed turn has no pending recovery phase and is not active, so
        # the Tier-2 gate returns it verbatim with no journal reads or CAS
        # writes.
        snapshot = {
            "session_id": "s",
            "conversation_state": "IDLE",
            "last_turn_status": "COMPLETED",
            "last_turn_id": "turn-done",
        }
        service = _make_service(snapshot)
        result = await service._reconcile_stuck_turn(
            session_id="s", session={"session_id": "s"}, snapshot=snapshot
        )
        self.assertIs(result, snapshot)
        self.assertEqual(service._session_events_repo.list_events_calls, [])
        self.assertEqual(service._session_events_repo.appended, [])
        self.assertEqual(service._session_snapshots_repo.apply_calls, [])


# ── Tier 1: IDLE + FAILED + anchor ───────────────────────────────────────────


def _tier1_snapshot() -> dict[str, Any]:
    return {
        "session_id": "s1",
        "conversation_state": "IDLE",
        "last_turn_status": "FAILED",
        "last_turn_id": "turn-1",
        "turn_recovery_phase": "TRANSCRIPT_PENDING",
        "current_turn_remote_anchor": {"sandbox_turn_id": 2},
    }


class TranscriptPendingHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_anchorless_pending_turn_is_resolved_before_active_ladder(self) -> None:
        snapshot = {
            **_tier1_snapshot(),
            "current_turn_remote_anchor": None,
        }
        resolved = {
            **snapshot,
            "turn_recovery_phase": None,
        }
        service = _make_service(snapshot)
        service._turn_coordinator = SimpleNamespace(
            try_resolve_turn=AsyncMock(return_value=resolved)
        )

        result = await service._reconcile_stuck_turn(
            session_id="s1",
            session={"session_id": "s1"},
            snapshot=snapshot,
        )

        self.assertIs(result, resolved)
        service._turn_coordinator.try_resolve_turn.assert_awaited_once_with(
            "s1",
            {"session_id": "s1"},
            snapshot,
        )
        self.assertEqual(service._session_events_repo.list_events_calls, [])


class Tier2JournalReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_state_with_journal_terminal_replays(self) -> None:
        # PROCESSING with a journal turn.failed terminal -> replay projection;
        # since replay converges to a non-active state, its result is returned.
        snapshot = {
            "session_id": "s2",
            "conversation_state": "PROCESSING",
            "current_turn_id": "turn-2",
            "current_turn_remote_anchor": {"sandbox_turn_id": 4},
            "updated_at": _now_iso(),
        }
        service = _make_service(snapshot)
        service._session_events_repo = _FakeJournal(
            {"turn.failed": [{"event_type": "turn.failed", "event_seq": 50}]}
        )
        replayed = {"conversation_state": "IDLE", "last_turn_status": "FAILED"}
        service._replay_projection_from_journal_terminal = AsyncMock(
            return_value=replayed
        )

        result = await service._reconcile_stuck_turn(
            session_id="s2", session={"session_id": "s2"}, snapshot=snapshot
        )
        self.assertIs(result, replayed)
        service._replay_projection_from_journal_terminal.assert_awaited_once()
        # Replay converged -> checkpoint split-brain path never runs.
        self.assertEqual(service._session_events_repo.appended, [])


class Tier2aAwaitingInteractionReplayTests(unittest.IsolatedAsyncioTestCase):
    def _snapshot(self) -> dict[str, Any]:
        return {
            "session_id": "s4",
            "conversation_state": "PROCESSING",
            "current_turn_id": "turn-4",
            "current_turn_remote_anchor": {"sandbox_turn_id": 3},
            "updated_at": _now_iso(),
        }

    async def test_unresolved_awaiting_event_projects_waiting(self) -> None:
        # PROCESSING + a journal turn.awaiting_interaction whose interaction is
        # still unresolved -> project the OPEN interaction, CAS to
        # WAITING_FOR_INTERACTION. The message overlay is derived at read time
        # from this interaction snapshot and the frame log.
        snapshot = self._snapshot()
        service = _make_service(snapshot)
        awaiting = {
            "event_type": "turn.awaiting_interaction",
            "event_seq": 500,
            "payload": {
                "interaction_id": "iid-await",
                "tool_name": "Bash",
                "presentation": "tool_approval",
                "raw_input": {"command": "ls"},
            },
        }
        service._session_events_repo = _FakeJournal(
            {"turn.awaiting_interaction": [awaiting]}
        )
        service._interaction_snapshots_repo.get_interaction = AsyncMock(
            return_value=None  # unresolved
        )
        service._interaction_snapshots_repo.project_open_interaction = AsyncMock(
            return_value={"interaction_state": "OPEN", "active": True}
        )

        result = await service._reconcile_stuck_turn(
            session_id="s4", session={"session_id": "s4"}, snapshot=snapshot
        )
        self.assertEqual(result["conversation_state"], "WAITING_FOR_INTERACTION")
        service._interaction_snapshots_repo.project_open_interaction.assert_awaited_once()
        self.assertEqual(len(service._session_snapshots_repo.apply_calls), 1)

    async def test_resolved_awaiting_event_does_not_replay(self) -> None:
        # The single awaiting interaction is already ANSWERED/inactive -> Tier 2a
        # replays nothing; a fresh worker (frames, recent updated_at) is left
        # alone by the has-anchor fresh path.
        snapshot = self._snapshot()
        service = _make_service(snapshot)
        awaiting = {
            "event_type": "turn.awaiting_interaction",
            "event_seq": 500,
            "payload": {"interaction_id": "iid-await", "tool_name": "Bash"},
        }
        service._session_events_repo = _FakeJournal(
            {"turn.awaiting_interaction": [awaiting]}
        )
        service._interaction_snapshots_repo.get_interaction = AsyncMock(
            return_value={"interaction_state": "ANSWERED", "active": False}
        )
        service._session_events_repo.list_frames = AsyncMock(
            return_value=[{"payload": {"type": "start"}, "frame_seq": 1}]
        )

        result = await service._reconcile_stuck_turn(
            session_id="s4", session={"session_id": "s4"}, snapshot=snapshot
        )
        self.assertIs(result, snapshot)
        service._interaction_snapshots_repo.project_open_interaction.assert_not_awaited()
        self.assertEqual(service._session_snapshots_repo.apply_calls, [])


# ── WAITING_FOR_INTERACTION state ────────────────────────────────────────────


class WaitingInteractionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_interaction_needs_no_second_message_projection(self) -> None:
        # WAITING with an active interaction owned by this turn is already a
        # complete durable state. The read model overlays it on the frame log.
        snapshot = {
            "session_id": "s5",
            "conversation_state": "WAITING_FOR_INTERACTION",
            "current_turn_id": "turn-5",
            "active_interaction_id": "iid-w",
            "current_turn_remote_anchor": {"sandbox_turn_id": 1},
            "updated_at": _now_iso(),
        }
        service = _make_service(snapshot)
        service._interaction_snapshots_repo.get_interaction = AsyncMock(
            return_value={"active": True, "turn_id": "turn-5"}
        )

        result = await service._reconcile_stuck_turn(
            session_id="s5", session={"session_id": "s5"}, snapshot=snapshot
        )
        self.assertIs(result, snapshot)
        # Resolved via active_interaction_id -> the get_active_interaction
        # fallback is never consulted.
        service._interaction_snapshots_repo.get_active_interaction.assert_not_awaited()
        self.assertEqual(service._session_snapshots_repo.apply_calls, [])


# ── No-anchor delivery adjudication ──────────────────────────────────────────


def _no_anchor_snapshot(*, updated_at: str) -> dict[str, Any]:
    return {
        "session_id": "s6",
        "conversation_state": "PROCESSING",
        "current_turn_id": "turn-6",
        # no current_turn_remote_anchor -> worker never talked to sandbox
        "updated_at": updated_at,
    }


class NoAnchorAdjudicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_snapshot_is_not_a_no_anchor_death_verdict(self) -> None:
        snapshot = _no_anchor_snapshot(updated_at=_now_iso())
        service = _make_service(snapshot)

        result = await service._adjudicate_no_anchor_delivery(
            session_id="s6",
            session={"session_id": "s6"},
            snapshot=snapshot,
            conversation_state="PROCESSING",
            turn_id="turn-6",
        )

        self.assertIs(result, snapshot)
        service._session_events_repo.list_frames.assert_not_awaited()

    async def test_stale_snapshot_is_the_no_anchor_death_verdict(self) -> None:
        snapshot = _no_anchor_snapshot(updated_at=_OLD_ISO)
        service = _make_service(snapshot)

        result = await service._adjudicate_no_anchor_delivery(
            session_id="s6",
            session={"session_id": "s6"},
            snapshot=snapshot,
            conversation_state="PROCESSING",
            turn_id="turn-6",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["delivery_state"], "NOT_RECEIVED")

    async def test_confirmed_dead_compute_is_a_death_verdict_without_the_wait(
        self,
    ) -> None:
        # A box the control plane says is gone cannot still be running the
        # worker that would write this turn. Waiting out the staleness
        # threshold anyway leaves the conversation PROCESSING, which is the one
        # state recover_session refuses — so the next message is rejected as a
        # non-retryable client error until the periodic reconciler catches up.
        snapshot = _no_anchor_snapshot(updated_at=_now_iso())
        service = _make_service(snapshot)

        result = await service._adjudicate_no_anchor_delivery(
            session_id="s6",
            session={"session_id": "s6", "runtime_unavailable": True},
            snapshot=snapshot,
            conversation_state="PROCESSING",
            turn_id="turn-6",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["delivery_state"], "NOT_RECEIVED")

    async def test_confirmed_dead_compute_spares_a_turn_that_reached_the_engine(
        self,
    ) -> None:
        # The dead box only settles a turn the engine never saw. One with an
        # engine anchor has a durable counterpart to recover from, and losing
        # the box is not evidence that the turn produced nothing.
        snapshot = _no_anchor_snapshot(updated_at=_now_iso())
        snapshot["current_turn_engine_anchor"] = {
            "engine_kind": "claude_code",
            "engine_turn_id": "engine-turn-6",
        }
        service = _make_service(snapshot)

        result = await service._adjudicate_no_anchor_delivery(
            session_id="s6",
            session={"session_id": "s6", "runtime_unavailable": True},
            snapshot=snapshot,
            conversation_state="PROCESSING",
            turn_id="turn-6",
        )

        self.assertIs(result, snapshot)
        service._session_events_repo.list_frames.assert_not_awaited()

    async def test_fresh_no_anchor_is_left_alone(self) -> None:
        # No anchor but the snapshot is fresh (< 30s), so there is no death
        # verdict: leave it alone without reading engine frames.
        snapshot = _no_anchor_snapshot(updated_at=_now_iso())
        service = _make_service(snapshot)

        result = await service._reconcile_stuck_turn(
            session_id="s6", session={"session_id": "s6"}, snapshot=snapshot
        )
        self.assertIs(result, snapshot)
        self.assertEqual(service._session_events_repo.appended, [])
        self.assertEqual(service._session_snapshots_repo.apply_calls, [])
        service._session_events_repo.list_frames.assert_not_awaited()

    async def test_stale_no_anchor_with_dispatch_evidence_is_received_failed(
        self,
    ) -> None:
        # Stale + no anchor + a journal dispatch.confirmed -> the sandbox did
        # receive the turn: converge to RECEIVED + FAILED (post_dispatch) and
        # write the partial turn_failure message.
        snapshot = _no_anchor_snapshot(updated_at=_OLD_ISO)
        service = _make_service(snapshot)
        service._session_events_repo = _FakeJournal(
            {"dispatch.confirmed": [{"event_type": "dispatch.confirmed", "event_seq": 10}]}
        )

        result = await service._reconcile_stuck_turn(
            session_id="s6", session={"session_id": "s6"}, snapshot=snapshot
        )
        self.assertEqual(result["conversation_state"], "IDLE")
        self.assertEqual(result["last_turn_status"], "FAILED")
        self.assertEqual(result["delivery_state"], "RECEIVED")
        # One audit fact plus one terminal fact citing the found evidence.
        self.assertEqual(len(service._session_events_repo.appended), 2)
        stale = service._session_events_repo.appended[0]
        self.assertEqual(stale["event_type"], "turn.stale_detected")
        self.assertEqual(stale["payload"]["reason"], "no_anchor_but_evidence_found")
        self.assertIs(stale["payload"]["has_dispatch_confirmed"], True)
        failed = service._session_events_repo.appended[1]
        self.assertEqual(failed["event_type"], "turn.failed")
        self.assertEqual(failed["payload"]["failure_phase"], "post_dispatch")
        apply = service._session_snapshots_repo.apply_calls[0]["updates"]
        self.assertEqual(apply["last_turn_failure_phase"], "post_dispatch")

    async def test_stale_no_anchor_no_evidence_with_accepted_command_pre_dispatch(
        self,
    ) -> None:
        # Stale + no anchor + no sandbox evidence, but a command.accepted exists
        # -> the turn failed before dispatch: FAILED(pre_dispatch) + NOT_RECEIVED,
        # attributed to the accepted command id.
        snapshot = _no_anchor_snapshot(updated_at=_OLD_ISO)
        service = _make_service(snapshot)
        service._session_events_repo = _FakeJournal(
            {
                "command.accepted": [
                    {
                        "event_type": "command.accepted",
                        "event_seq": 5,
                        "causation_id": "cmd-acc",
                    }
                ]
            }
        )

        result = await service._reconcile_stuck_turn(
            session_id="s6", session={"session_id": "s6"}, snapshot=snapshot
        )
        self.assertEqual(result["conversation_state"], "IDLE")
        self.assertEqual(result["last_turn_status"], "FAILED")
        self.assertEqual(result["delivery_state"], "NOT_RECEIVED")
        # Two events: the stale marker and the pre_dispatch turn.failed.
        types = [e["event_type"] for e in service._session_events_repo.appended]
        self.assertEqual(types, ["turn.stale_detected", "turn.failed"])
        failed = service._session_events_repo.appended[1]
        self.assertEqual(failed["causation_id"], "cmd-acc")
        self.assertEqual(failed["payload"]["failure_phase"], "pre_dispatch")

    async def test_stale_no_anchor_no_evidence_is_not_received(self) -> None:
        # Stale + no anchor + no evidence at all + no accepted command ->
        # NOT_RECEIVED with no failure status (the pre-write user message stays).
        snapshot = _no_anchor_snapshot(updated_at=_OLD_ISO)
        service = _make_service(snapshot)

        result = await service._reconcile_stuck_turn(
            session_id="s6", session={"session_id": "s6"}, snapshot=snapshot
        )
        self.assertEqual(result["delivery_state"], "NOT_RECEIVED")
        self.assertIsNone(result["last_turn_status"])
        self.assertIsNone(result["current_turn_id"])
        # Only the stale marker; no turn.failed fabricated.
        types = [e["event_type"] for e in service._session_events_repo.appended]
        self.assertEqual(types, ["turn.stale_detected"])
        self.assertEqual(
            service._session_events_repo.appended[0]["payload"]["reason"], "no_anchor"
        )


# ── Has-anchor stale convergence ─────────────────────────────────────────────


class StaleTurnAdjudicationTests(unittest.IsolatedAsyncioTestCase):
    def _snapshot(self, *, updated_at: str) -> dict[str, Any]:
        return {
            "session_id": "s7",
            "conversation_state": "STREAMING",
            "current_turn_id": "turn-7",
            "last_turn_command_id": "cmd-7",
            "updated_at": updated_at,
        }

    async def test_dead_worker_no_frames_converges_stale(self) -> None:
        # Owner heartbeat is stale and there are no frames or terminal
        # evidence -> the stale adjudication settles the turn instead of
        # leaving the session stuck.
        snapshot = self._snapshot(updated_at=_OLD_ISO)
        service = _make_service(snapshot)
        service._session_events_repo.list_frames = AsyncMock(return_value=[])

        result = await service._reconcile_stuck_turn(
            session_id="s7",
            session={"session_id": "s7"},
            snapshot=snapshot,
            worker_heartbeat_stale=True,
        )
        self.assertIsNotNone(result)
        appended_types = [
            e["event_type"] for e in service._session_events_repo.appended
        ]
        self.assertIn("turn.stale_detected", appended_types)


if __name__ == "__main__":
    unittest.main()
