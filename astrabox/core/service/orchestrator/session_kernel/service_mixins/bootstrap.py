"""BootstrapWiringMixin — leaf mixin for :class:`SessionKernelService`."""
from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import os
import time

from astrabox.common.utils.errors import APIError
from astrabox.persistence.repository.backend import active_backend_name
from astrabox.persistence.repository.sqlite.engine import (
    checkpoint_loop as sqlite_checkpoint_loop,
)
from astrabox.core.service.orchestrator.session_kernel.workers import (
    ReconcileWorker,
    SessionLifecycleWorker,
    TerminalWorker,
    TurnWorker,
)
from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import SCAN_INTERVAL_S as _RECONCILE_SCAN_INTERVAL_S
from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)


_LOOP_STALL_DUMP_S = float(os.getenv("ASTRABOX_LOOP_STALL_DUMP_S", "5.0") or 5.0)
_LOOP_STALL_WARN_S = 1.0
_LOOP_STALL_TICK_S = 0.5


async def _loop_stall_watchdog() -> None:
    """Name whatever freezes the event loop, from outside the loop.

    Two instruments run on each tick. Drift logs how late the fixed sleep wakes,
    identifying loop delay and its duration. For stacks,
    ``faulthandler.dump_traceback_later`` is re-armed every tick as a C-level
    timer on a watchdog thread, so when the loop stalls past the dump
    threshold, every thread's stack — including the frame hogging the loop —
    lands on stderr while the stall is still happening. A stalled loop cannot
    log its own stall; this is the one diagnostic that needs no cooperation.
    """
    try:
        while True:
            faulthandler.dump_traceback_later(_LOOP_STALL_DUMP_S, exit=False)
            before = time.monotonic()
            await asyncio.sleep(_LOOP_STALL_TICK_S)
            drift = time.monotonic() - before - _LOOP_STALL_TICK_S
            if drift >= _LOOP_STALL_WARN_S:
                logger.warning(
                    "event loop stalled %.2fs past its tick "
                    "(thread stacks dumped to stderr if the stall exceeded %.1fs)",
                    drift,
                    _LOOP_STALL_DUMP_S,
                )
    finally:
        faulthandler.cancel_dump_traceback_later()


class BootstrapWiringMixin:
    """Background-task tracking/quiesce, the periodic reconcile loop, and the
    three worker factory builders. ``__init__`` remains in the facade."""

    def _spawn_tracked_background_task(self, coro, *, name: str | None = None) -> asyncio.Task:
        if self._quiesced_reason:
            close = getattr(coro, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
            raise APIError(
                code="ASTRABOX_RELEASING",
                message=f"session kernel is closing for shutdown: {self._quiesced_reason}",
                status_code=503,
            )
        task = self._raw_spawn_background_task(coro, name=name)
        self._active_background_tasks.add(task)
        task.add_done_callback(self._active_background_tasks.discard)
        return task

    async def ensure_bootstrap(self) -> None:
        await self._session_events_repo.ensure_indexes()
        await self._session_snapshots_repo.ensure_indexes()
        await self._interaction_snapshots_repo.ensure_indexes()
        await self._artifacts_repo.ensure_indexes()
        self.ensure_background_tasks_started()

    def ensure_background_tasks_started(self) -> None:
        """Keep process-local background loops alive.

        This is intentionally cheap and side-effect free for durable state:
        callers may invoke it on every request to guarantee that the
        reconcile loop still exists, even after shutdown or task loss.
        """
        if self._quiesced_reason:
            logger.warning(
                "session kernel background start skipped during shutdown close: worker_id=%s reason=%s",
                self._worker_id,
                self._quiesced_reason,
            )
            return
        self._ensure_reconcile_worker_started()
        self._ensure_wal_checkpointer_started()
        self._ensure_loop_stall_watchdog_started()

    def _ensure_wal_checkpointer_started(self) -> None:
        """The sqlite WAL checkpointer — same liveness contract as reconcile.

        With ``wal_autocheckpoint`` disabled, this loop is the only component
        draining SQLite's WAL. Failing to spawn it causes unbounded WAL growth.
        Other persistence backends do not need this task.
        """
        if active_backend_name() != "sqlite":
            return
        task = getattr(self, "_wal_checkpoint_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            return
        self._wal_checkpoint_task = self._spawn_background_task(
            sqlite_checkpoint_loop(),
            name="sqlite-wal-checkpointer",
        )

    def _ensure_loop_stall_watchdog_started(self) -> None:
        task = getattr(self, "_loop_stall_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            return
        self._loop_stall_task = self._spawn_background_task(
            _loop_stall_watchdog(),
            name="event-loop-stall-watchdog",
        )

    def quiesce(self, *, reason: str) -> None:
        if self._quiesced_reason:
            logger.info(
                "session kernel already closing for shutdown: worker_id=%s previous=%s current=%s",
                self._worker_id,
                self._quiesced_reason,
                reason,
            )
            return
        self._quiesced_reason = str(reason or "shutdown").strip() or "shutdown"
        tasks: list[asyncio.Task] = []
        reconcile_task = getattr(self, "_reconcile_task", None)
        if isinstance(reconcile_task, asyncio.Task):
            tasks.append(reconcile_task)
        for attr in ("_wal_checkpoint_task", "_loop_stall_task"):
            extra_task = getattr(self, attr, None)
            if isinstance(extra_task, asyncio.Task):
                tasks.append(extra_task)
        active_tasks = getattr(self, "_active_background_tasks", set())
        tasks.extend(
            task for task in list(active_tasks)
            if isinstance(task, asyncio.Task)
        )
        tasks.extend(
            task for task in list(self._recovery_tasks.values())
            if isinstance(task, asyncio.Task)
        )
        if hasattr(active_tasks, "clear"):
            active_tasks.clear()
        self._recovery_tasks.clear()
        unique_tasks = list(dict.fromkeys(tasks))
        logger.warning(
            "closing session kernel for shutdown: worker_id=%s reason=%s tasks=%d",
            self._worker_id,
            self._quiesced_reason,
            len(unique_tasks),
        )
        for task in unique_tasks:
            if not task.done():
                task.cancel("shutdown")

    def _ensure_reconcile_worker_started(self) -> None:
        task = getattr(self, "_reconcile_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            return
        worker = ReconcileWorker(
            session_snapshots_repo=self._session_snapshots_repo,
            sessions_repo=self._sessions_repo,
            session_events_repo=self._session_events_repo,
            interaction_snapshots_repo=self._interaction_snapshots_repo,
            resolve_sandbox_endpoint_fn=self._resolve_sandbox_endpoint,
            resume_orphaned_answer_command_fn=self._resume_orphaned_answer_command,
            recover_engine_session_fn=self._recover_stuck_turn,
            wakeup_turn_coordinator_fn=self._wakeup_turn_coordinator,
            attach_parked_runtime_fn=self._attach_parked_runtime,
            worker_id=self._worker_id,
        )
        self._reconcile_task = self._spawn_background_task(
            self._reconcile_loop(worker), name="reconcile-worker",
        )
        logger.info(
            "reconcile_worker: task created worker_id=%s task_id=%s done=%s",
            self._worker_id,
            id(self._reconcile_task),
            self._reconcile_task.done(),
        )
        self._reconcile_task.add_done_callback(self._log_reconcile_task_done)

    async def _reconcile_loop(self, worker: ReconcileWorker) -> None:
        """Periodic background loop that calls ``worker.scan_once()``."""
        logger.info("reconcile_worker: loop started worker_id=%s", self._worker_id)
        try:
            while True:
                try:
                    summary = await worker.scan_once()
                    if any(summary.values()):
                        logger.info("reconcile_worker: tick %s", summary)
                    background_count = await self._materialize_background_continuations_once()
                    if background_count:
                        logger.info(
                            "background continuation: materialized %d opened manifests",
                            background_count,
                        )
                except Exception:
                    logger.exception("reconcile_worker: scan_once failed")
                await asyncio.sleep(_RECONCILE_SCAN_INTERVAL_S)
        except asyncio.CancelledError:
            logger.warning("reconcile_worker: loop cancelled worker_id=%s", self._worker_id)
            raise

    @staticmethod
    def _log_reconcile_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            logger.warning("reconcile_worker: task done by cancellation task_id=%s", id(task))
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "reconcile_worker: task crashed task_id=%s err=%s",
                id(task),
                exc,
                exc_info=exc,
            )
            return
        logger.info("reconcile_worker: task exited cleanly task_id=%s", id(task))

    @staticmethod
    def _loop_time() -> float:
        return asyncio.get_running_loop().time()

    def _build_turn_worker(self) -> TurnWorker:
        return TurnWorker(
            worker_id=self._worker_id,
            sessions_repo=self._sessions_repo,
            turn_service=self._turn_service,
            runtime_manager=self._runtime_manager,
            broker=self._broker,
            session_events_repo=self._session_events_repo,
            session_snapshots_repo=self._session_snapshots_repo,
            interaction_snapshots_repo=self._interaction_snapshots_repo,
            transcript_entries_repo=self._transcript_entries_repo,
            title_service=self._session_title_service,
            bridge_event_stall_timeout_s=self._bridge_event_stall_timeout_s,
            worker_heartbeat_interval_s=self._turn_worker_heartbeat_interval_s,
            live_frame_retry_window_s=self._turn_live_frame_retry_window_s,
            live_frame_retry_delay_s=self._turn_live_frame_retry_delay_s,
            requested_projection_retry_window_s=self._turn_requested_projection_retry_window_s,
            requested_projection_retry_delay_s=self._turn_requested_projection_retry_delay_s,
            terminal_settle_retry_window_s=self._turn_terminal_settle_retry_window_s,
            terminal_settle_retry_delay_s=self._turn_terminal_settle_retry_delay_s,
            redrive_stranded_inputs=self._redrive_stranded_inputs_after_settle,
            spawn_background_task=self._spawn_tracked_background_task,
        )

    def _build_lifecycle_worker(self) -> SessionLifecycleWorker:
        return SessionLifecycleWorker(
            worker_id=self._worker_id,
            sessions_repo=self._sessions_repo,
            session_service=self._session_service,
            turn_service=self._turn_service,
            runtime_manager=self._runtime_manager,
            session_events_repo=self._session_events_repo,
            session_snapshots_repo=self._session_snapshots_repo,
            interaction_snapshots_repo=self._interaction_snapshots_repo,
            agent_repo=self._agent_repo,
            assistant_workspace_service=self._assistant_workspace_service,
            runtime_subjects=self._runtime_subjects,
        )

    def _build_terminal_worker(self) -> TerminalWorker:
        return TerminalWorker(
            worker_id=self._worker_id,
            sessions_repo=self._sessions_repo,
            terminal_service=self._terminal_service,
            runtime_manager=self._runtime_manager,
            broker=self._broker,
            artifacts_repo=self._artifacts_repo,
            session_events_repo=self._session_events_repo,
            session_snapshots_repo=self._session_snapshots_repo,
        )
