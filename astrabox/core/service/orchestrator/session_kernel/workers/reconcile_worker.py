"""Background ReconcileWorker: stuck-session and transcript-recovery detection.

This worker periodically scans ``session_snapshots`` for sessions whose
``conversation_state`` is active (PROCESSING / STREAMING /
WAITING_FOR_INTERACTION) but whose ``worker_heartbeat_at`` is stale, and
also for turns already settled to ``IDLE + TRANSCRIPT_PENDING`` that still
need authoritative transcript recovery.

For each detected stuck session, it invokes ``service._recover_stuck_turn``:
repair an existing terminal projection, otherwise resume the engine.
Reconciliation stays in this background worker so GET paths remain read-only.

Usage:
    The class is instantiated with the same repositories that the
    SessionKernelService uses.  It is started as a background loop
    from ``SessionKernelService.ensure_bootstrap()``.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default

from astrabox.persistence.repository.backend import (
    get_async_collection,
    is_mongo_transient_error,
    run_mongo_with_retry,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso_utc, utcnow_iso
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING,
    derive_turn_recovery_phase,
)

logger = get_logger(__name__)

# Tuning knobs ----------------------------------------------------------------

SCAN_INTERVAL_S: float = _env_float("ASTRABOX_RECONCILE_SCAN_INTERVAL_S", 10.0)
"""How often ``scan_once`` should be called by the host loop
(``ASTRABOX_RECONCILE_SCAN_INTERVAL_S``, default 10s). Lower it to shorten the
window before another replica takes over a dead worker's in-flight turn during
a rolling deploy."""

HEARTBEAT_STALE_S: float = _env_float("ASTRABOX_RECONCILE_HEARTBEAT_STALE_S", 20.0)
"""A worker heartbeat older than this is considered stale
(``ASTRABOX_RECONCILE_HEARTBEAT_STALE_S``, default 20s)."""

NO_ANCHOR_STALE_S: float = 30.0
"""A turn without a remote anchor older than this is suspicious."""

# States the stuck-turn scan considers active. WAITING_FOR_INTERACTION is
# deliberately excluded: a parked interaction is a legitimate quiet state
# (the user owns the next move; the runner's own interaction_wait_s defers
# it engine-side), and anchor recovery has nothing to do for it — scanning
# it produces a no-op "reconciled" every tick.
_ACTIVE_CONVERSATION_STATES = frozenset(
    {"PROCESSING", "STREAMING", "INTERRUPTING"}
)
_TERMINAL_SESSION_LIFECYCLE_STATES = frozenset({"TERMINATED", "DELETED"})

#: Where a row goes when it can never be recovered. Not in
#: ``_ACTIVE_CONVERSATION_STATES`` and not parked, so writing it is what makes a
#: row leave the candidate set rather than merely stop being reported.
_SETTLED_CONVERSATION_STATE = "IDLE"


@dataclass(frozen=True)
class DeadSandboxRecoverySettlement:
    """An anchor recovery that settled because its sandbox is confirmed gone."""

    snapshot: dict[str, Any]


def _why_recovery_can_never_run(snapshot: dict[str, Any] | None) -> str:
    """Why this selected row can never reach recovery, or ``""`` when it can.

    The scan selects on ``conversation_state`` and a stale heartbeat. Recovery
    needs a turn to recover — an id, and the worker command that owns it. Those
    two conditions are not the same question, and where they disagree the row is
    selected on every tick for ever.

    A reason belongs here only if it is permanent, and the bar is evidence, not
    plausibility. A missing current turn qualifies: settling a turn clears
    ``current_turn_id`` onto ``last_turn_id``, so a row in an active state
    naming no current turn is one nothing will ever give a turn back to.

    A missing ``current_turn_worker_command_id`` does not qualify, though
    recovery also refuses without it. There is no evidence here that the field
    cannot be written shortly after ``current_turn_id``, and a turn settled on
    that guess would be a live turn ended by a reconciler. It stays in the
    incomplete outcome, where it is visible and reversible.
    """
    row = snapshot or {}
    if not str(row.get("current_turn_id") or "").strip():
        return (
            "the row claims an active conversation but names no current turn; "
            "nothing later gives a turn back to a conversation that ended one"
        )
    return ""

#: Parked: never a candidate for recovery, always one for presence. The box
#: holds the human's question in memory and stops waiting once the host has
#: been absent past its budget, so after a platform restart something has to go
#: back or the approval dies. It gets its own arm rather than joining the
#: active set above, because the two consumers want opposite things from the
#: same row — recovery must never see it, attachment must.
_PARKED_CONVERSATION_STATES = frozenset({"WAITING_FOR_INTERACTION"})


def _stuck_sessions_query(threshold_iso: str) -> dict[str, Any]:
    """The stuck-session scan's database query.

    The terminal-lifecycle exclusion lives inside the active-stale arm, not at
    the top level: a TERMINATED/DELETED session's stale PROCESSING is not
    recoverable work, but a DELETED session's TRANSCRIPT_PENDING orphan is the
    one row whose resolver can only be reached from here. Duplicating the
    exclusion at the top level as well as in the Python gate would filter that
    row out before the resolver can route it.
    """
    return {
        "$or": [
            {
                "session_lifecycle_state": {
                    "$nin": list(_TERMINAL_SESSION_LIFECYCLE_STATES),
                },
                "conversation_state": {"$in": list(_ACTIVE_CONVERSATION_STATES)},
                "$or": [
                    {"worker_heartbeat_at": {"$lt": threshold_iso}},
                    {
                        "worker_heartbeat_at": {"$exists": False},
                        "updated_at": {"$lt": threshold_iso},
                    },
                ],
            },
            {
                # Presence-only arm: parked rows, for the attach branch in
                # scan_once. Nothing downstream recovers these — the parked
                # fence returns before any recovery path — but without the arm
                # the fence never sees them at all, and a restart drops the
                # pending approval while the fence itself stays correct.
                "session_lifecycle_state": {
                    "$nin": list(_TERMINAL_SESSION_LIFECYCLE_STATES),
                },
                "conversation_state": {"$in": list(_PARKED_CONVERSATION_STATES)},
            },
            {
                # Every TRANSCRIPT_PENDING row, deleted sessions included, so
                # the coordinator gets to look at it. What happens next is its
                # call: recover from the mirror, or settle unrecoverable. Its
                # own guards (_get_unresolved_turn_id) gate the actual settle,
                # so widening this scan cannot over-settle a live turn.
                "turn_recovery_phase": TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING,
            },
        ]
    }


class ReconcileWorker:
    """Background scanner for stuck sessions.

    Parameters mirror the service layer repositories so the worker can
    query snapshots and delegate to the existing reconcile logic.
    """

    def __init__(
        self,
        *,
        session_snapshots_repo: Any,
        sessions_repo: Any,
        session_events_repo: Any,
        interaction_snapshots_repo: Any,
        resolve_sandbox_endpoint_fn: Any,
        resume_orphaned_answer_command_fn: Any | None = None,
        recover_engine_session_fn: Any | None = None,
        wakeup_turn_coordinator_fn: Any | None = None,
        attach_parked_runtime_fn: Any | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._session_snapshots_repo = session_snapshots_repo
        self._sessions_repo = sessions_repo
        self._session_events_repo = session_events_repo
        self._interaction_snapshots_repo = interaction_snapshots_repo
        self._resolve_sandbox_endpoint = resolve_sandbox_endpoint_fn
        self._resume_orphaned_answer_command = resume_orphaned_answer_command_fn
        self._wakeup_turn_coordinator = wakeup_turn_coordinator_fn
        # The platform repairs already-journaled terminals before delegating
        # unfinished work to the engine's anchor-recovery protocol.
        self._recover_engine_session = recover_engine_session_fn
        #: Re-attach, rather than recover, a session parked on a question. The
        #: box holds the approval request in memory while the host must remain
        #: present; the parked-turn fence prevents recovery from failing a
        #: healthy turn.
        self._attach_parked_runtime = attach_parked_runtime_fn
        self._worker_id = worker_id or "reconcile-worker"

    async def scan_once(self) -> dict[str, int]:
        """Scan for stuck sessions and reconcile them.

        Returns a breakdown by outcome, not a count of attempts: a tick that
        calls recovery on ten rows and moves none of them is a different event
        from a tick that settles ten turns, and a single attempt count cannot
        tell them apart, so a livelock would read as steady progress.

        Every arm either converges its row or makes it leave the candidate set.
        The one arm that may legitimately leave a row selectable is
        ``recovery_incomplete`` — more engine events are still expected — so
        that number staying high across ticks, on the same rows, is the
        livelock itself rather than a hint of one.
        """
        stale_threshold = datetime.now(timezone.utc) - timedelta(
            seconds=HEARTBEAT_STALE_S
        )
        stuck_snapshots = await self._find_stuck_sessions(
            stale_threshold=stale_threshold,
        )
        if not stuck_snapshots:
            return {}

        summary: dict[str, int] = {
            "candidates": len(stuck_snapshots),
            "parked_present": 0,
            "transcript_pending_routed": 0,
            "answer_resumed": 0,
            "recovery_settled": 0,
            "recovery_incomplete": 0,
            "settled_dead_sandbox": 0,
            "settled_unrecoverable": 0,
            "settled_deleted_session_orphan": 0,
            "skipped": 0,
            "failed": 0,
        }
        for snapshot in stuck_snapshots:
            session_id = str(snapshot.get("session_id") or "").strip()
            if not session_id:
                continue
            try:
                latest_snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
                if not self._should_reconcile_snapshot(
                    latest_snapshot,
                    stale_threshold=stale_threshold,
                ):
                    summary["skipped"] += 1
                    continue
                parked_state = str(
                    (latest_snapshot or {}).get("conversation_state") or ""
                ).strip() in _PARKED_CONVERSATION_STATES
                if parked_state or await self._is_parked_awaiting_interaction(
                    latest_snapshot, session_id
                ):
                    # A turn parked at an interaction is waiting on a human,
                    # not stuck: its worker exits at the interaction boundary
                    # by design (the SSE segment must close), so a stale
                    # heartbeat is the park's normal signature. Recovery here
                    # would fail a healthy turn. Once the interaction is
                    # answered it leaves the active set, and a continuation
                    # that died is picked up by the orphaned-answer resume
                    # below.
                    #
                    # The worker must still be present: the box holds the
                    # question in memory and stops waiting when the host remains
                    # absent, so this branch re-attaches without recovering.
                    if self._attach_parked_runtime is not None:
                        try:
                            await self._attach_parked_runtime(session_id)
                        except Exception:
                            logger.warning(
                                "reconcile_worker: parked runtime attach failed "
                                "session_id=%s",
                                session_id,
                                exc_info=True,
                            )
                    summary["parked_present"] += 1
                    continue
                recovery_phase = derive_turn_recovery_phase(latest_snapshot)
                session = await self._sessions_repo.get_session(session_id)
                if not isinstance(session, dict):
                    if recovery_phase == TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING:
                        # get_session deliberately hides soft-deleted rows. A
                        # pending snapshot left behind by one has no owner that
                        # can run the turn coordinator, and session ids are never
                        # recreated, so retrying is not a transient outcome.
                        if await self._leave_the_candidate_set(
                            session_id,
                            latest_snapshot,
                            reason=(
                                "the transcript-pending turn has no owning session; "
                                "deleted session ids are never recreated"
                            ),
                            terminal_status="FAILED",
                        ):
                            summary["settled_deleted_session_orphan"] += 1
                        else:
                            summary["skipped"] += 1
                        continue
                    logger.warning(
                        "reconcile_worker: session not found session_id=%s, skipping",
                        session_id,
                    )
                    summary["skipped"] += 1
                    continue
                # A delivered answer must regain its projection before turn
                # settlement deactivates the still-OPEN interaction.
                if self._resume_orphaned_answer_command is not None:
                    resumed = await self._resume_orphaned_answer_command(
                        session_id=session_id,
                        session=session,
                        snapshot=latest_snapshot,
                    )
                    if resumed:
                        summary["answer_resumed"] += 1
                        logger.info(
                            "reconcile_worker: resumed orphaned answer command "
                            "session_id=%s conversation_state=%s",
                            session_id,
                            str((latest_snapshot or {}).get("conversation_state") or ""),
                        )
                        continue
                # TRANSCRIPT_PENDING belongs to the mirror-fed turn coordinator.
                # Ownerless rows have already left the candidate set above;
                # live rows reach this handoff after answer projection repair.
                if (
                    recovery_phase == TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING
                    and self._wakeup_turn_coordinator is not None
                ):
                    self._wakeup_turn_coordinator(session_id)
                    summary["transcript_pending_routed"] += 1
                    logger.info(
                        "reconcile_worker: transcript-pending routed to turn "
                        "coordinator session_id=%s",
                        session_id,
                    )
                    continue
                if self._recover_engine_session is None:
                    raise RuntimeError(
                        "reconcile_worker requires recover_engine_session_fn — "
                        "the stuck-turn recovery callback is required"
                    )
                # Completed journal entries need only projection repair;
                # unfinished work resumes through the engine's own anchor.
                unrecoverable = _why_recovery_can_never_run(latest_snapshot)
                if unrecoverable:
                    if await self._leave_the_candidate_set(
                        session_id, latest_snapshot, reason=unrecoverable
                    ):
                        summary["settled_unrecoverable"] += 1
                    else:
                        summary["skipped"] += 1
                    continue
                settled = await self._recover_engine_session(
                    session=session,
                    snapshot=latest_snapshot,
                )
                # The return value is the outcome: a snapshot on an ordinary
                # terminal commit, a tagged confirmed-death settlement, or None
                # when more events are still expected. Discarding it would make
                # every call look like a success.
                if isinstance(settled, DeadSandboxRecoverySettlement):
                    summary["settled_dead_sandbox"] += 1
                    logger.info(
                        "reconcile_worker: confirmed-dead sandbox settled the turn "
                        "session_id=%s conversation_state=%s settled_state=%s",
                        session_id,
                        str((latest_snapshot or {}).get("conversation_state") or ""),
                        str(settled.snapshot.get("conversation_state") or ""),
                    )
                elif settled is None:
                    summary["recovery_incomplete"] += 1
                    logger.info(
                        "reconcile_worker: anchor recovery made no progress "
                        "session_id=%s conversation_state=%s turn_recovery_phase=%s",
                        session_id,
                        str((latest_snapshot or {}).get("conversation_state") or ""),
                        str((latest_snapshot or {}).get("turn_recovery_phase") or ""),
                    )
                else:
                    summary["recovery_settled"] += 1
                    logger.info(
                        "reconcile_worker: anchor recovery settled the turn "
                        "session_id=%s conversation_state=%s",
                        session_id,
                        str((latest_snapshot or {}).get("conversation_state") or ""),
                    )
            except Exception:
                summary["failed"] += 1
                logger.exception(
                    "reconcile_worker: failed to reconcile session_id=%s",
                    session_id,
                )
        return summary

    async def _leave_the_candidate_set(
        self,
        session_id: str,
        snapshot: dict[str, Any] | None,
        *,
        reason: str,
        terminal_status: str | None = None,
    ) -> bool:
        """Settle a selected row so the scan stops selecting it.

        Every arm of the scan either converges its row or sends it here; the
        only arm allowed to leave a row selectable is the one meaning "more
        engine events are still expected". Without an explicit destination for
        a row it cannot handle, a branch's cheapest exit is to try again next
        tick — an exit that needs no reason and produces a livelock that
        reports success.

        The write is conditional on the row being unchanged since it was judged,
        so a worker that came back to life in between keeps its turn.
        """
        row = snapshot or {}
        observed_state = str(row.get("conversation_state") or "")
        updates: dict[str, Any] = {
            "conversation_state": _SETTLED_CONVERSATION_STATE,
            "turn_recovery_phase": None,
            "current_turn_remote_anchor": None,
            "current_turn_engine_anchor": None,
        }
        if terminal_status is not None:
            updates["last_turn_status"] = terminal_status
        landed = await self._session_snapshots_repo.force_update_fields(
            session_id,
            updates,
            extra_filter={
                "conversation_state": observed_state,
                "worker_heartbeat_at": row.get("worker_heartbeat_at"),
                "turn_recovery_phase": row.get("turn_recovery_phase"),
            },
        )
        if landed:
            logger.info(
                "reconcile_worker: settled a row nothing can recover session_id=%s "
                "conversation_state=%s reason=%s",
                session_id,
                observed_state,
                reason,
            )
        else:
            logger.info(
                "reconcile_worker: row changed while being judged, left alone "
                "session_id=%s conversation_state=%s",
                session_id,
                observed_state,
            )
        return bool(landed)

    async def _find_stuck_sessions(
        self,
        *,
        stale_threshold: datetime,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query session_snapshots for sessions with stale heartbeats.

        A session is considered stuck when:
        1. conversation_state is in ``_ACTIVE_CONVERSATION_STATES`` and
           worker_heartbeat_at is earlier than ``stale_threshold`` OR
           worker_heartbeat_at does not exist (worker never started heartbeat)
        2. turn_recovery_phase is TRANSCRIPT_PENDING with a recovery anchor
        """
        collection = await get_async_collection("session_snapshots")
        query = _stuck_sessions_query(stale_threshold.isoformat())

        async def _find_window(*, direction: int, window_limit: int) -> list[dict[str, Any]]:
            cursor = (
                collection.find(query)
                .sort("updated_at", direction)
                .limit(window_limit)
            )
            return [doc async for doc in cursor]

        async def _find() -> list[dict[str, Any]]:
            if limit <= 1:
                return await _find_window(direction=-1, window_limit=max(1, limit))

            newest_limit = max(1, limit // 2)
            oldest_limit = max(1, limit - newest_limit)
            oldest = await _find_window(direction=1, window_limit=oldest_limit)
            newest = await _find_window(direction=-1, window_limit=newest_limit)
            merged: dict[str, dict[str, Any]] = {}
            for doc in [*oldest, *newest]:
                session_id = str(doc.get("session_id") or "").strip()
                if not session_id:
                    session_id = str(doc.get("_id") or "").strip()
                if session_id and session_id not in merged:
                    merged[session_id] = doc
            return list(merged.values())[:limit]

        return await run_mongo_with_retry(
            "reconcile_worker.find_stuck_sessions",
            _find,
        )

    @staticmethod
    def _parse_snapshot_time(raw: Any) -> datetime | None:
        value = str(raw or "").strip()
        if not value:
            return None
        try:
            return parse_iso_utc(value)
        except ValueError:
            return None

    async def _is_parked_awaiting_interaction(
        self,
        snapshot: dict[str, Any] | None,
        session_id: str,
    ) -> bool:
        """True when the current turn is parked at a still-open interaction.

        A parked turn's worker exits at the interaction boundary by design
        (the SSE segment must close), so its heartbeat goes stale while it
        waits on a human. The healthy park projects WAITING_FOR_INTERACTION
        and never reaches this scan; the park that does reach it is one whose
        WAITING_FOR_INTERACTION snapshot projection lost the watermark race
        and stayed PROCESSING/STREAMING. Keying on the interaction — not the
        state — covers both. An answered or deactivated interaction means
        somebody should be consuming the stream, and a stale heartbeat there
        is evidence of death.
        """
        current_turn_id = str((snapshot or {}).get("current_turn_id") or "").strip()
        if not current_turn_id:
            return False
        interaction = await self._interaction_snapshots_repo.get_active_interaction(session_id)
        return (
            isinstance(interaction, dict)
            and str(interaction.get("turn_id") or "").strip() == current_turn_id
        )

    def _should_reconcile_snapshot(
        self,
        snapshot: dict[str, Any] | None,
        *,
        stale_threshold: datetime,
    ) -> bool:
        if not isinstance(snapshot, dict):
            return False
        recovery_phase = str(snapshot.get("turn_recovery_phase") or "").strip()
        if recovery_phase == TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING:
            # With or without a recovery anchor: anchored rows recover from the
            # durable mirror; anchor-less rows are unrecoverable and get settled
            # FAILED. Both route through try_resolve_turn, which guards the
            # actual settle (IDLE + last_turn_status=FAILED + last_turn_id).
            #
            # Checked before the terminal-lifecycle exclusion below: a DELETED
            # session's orphaned turn is the one case whose resolver can only
            # be reached from here, and excluding terminal lifecycles first
            # would silently reinstate the livelock this arm exists to end.
            return True
        lifecycle_state = str(snapshot.get("session_lifecycle_state") or "").strip().upper()
        if lifecycle_state in _TERMINAL_SESSION_LIFECYCLE_STATES:
            return False

        conversation_state = str(snapshot.get("conversation_state") or "").strip()
        if conversation_state in _PARKED_CONVERSATION_STATES:
            # Through to the parked fence, which attaches and returns. Staleness
            # is not a signal here: a parked turn's heartbeat is stopped by
            # design, and the box's question has been waiting since it was
            # asked, not since it went stale.
            return True
        if conversation_state not in _ACTIVE_CONVERSATION_STATES:
            return False

        heartbeat_at = self._parse_snapshot_time(snapshot.get("worker_heartbeat_at"))
        if heartbeat_at is not None:
            return heartbeat_at < stale_threshold

        updated_at = self._parse_snapshot_time(snapshot.get("updated_at"))
        return updated_at is not None and updated_at < stale_threshold

    def _active_heartbeat_stale(
        self,
        snapshot: dict[str, Any] | None,
        *,
        stale_threshold: datetime,
    ) -> bool:
        if not isinstance(snapshot, dict):
            return False
        conversation_state = str(snapshot.get("conversation_state") or "").strip()
        if conversation_state not in _ACTIVE_CONVERSATION_STATES:
            return False

        heartbeat_at = self._parse_snapshot_time(snapshot.get("worker_heartbeat_at"))
        if heartbeat_at is not None:
            return heartbeat_at < stale_threshold

        updated_at = self._parse_snapshot_time(snapshot.get("updated_at"))
        return updated_at is not None and updated_at < stale_threshold
