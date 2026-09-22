"""Channel spine reconciler — the durable recovery owner (channel-spine.md).

Boot-time sweep plus a periodic scan over the two recoverable surfaces:

* expired inbound work items (a worker died between ack and settle) are
  CAS-reclaimed and re-driven through the attach-not-append path, so a turn
  survives the loss of a worker process and is lost only with the database;
* abandoned outbox rows (PENDING, or SENDING with an expired lease) are
  re-leased and re-delivered against the exact bound turn.

Same host shape as :class:`~astrabox.core.service.orchestrator.expiration_watcher.ExpirationWatcher`:
owned by the platform service, started after bootstrap, cancelled on quiesce.
Every mutation goes through the fenced CAS methods on
:class:`~astrabox.persistence.repository.channel_repository.ChannelRepository`,
so any number of replicas can run the loop concurrently.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)


def _interval_seconds() -> int:
    try:
        return max(10, int(os.getenv("ASTRABOX_CHANNEL_RECONCILE_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60


class ChannelSpineReconciler:
    """Periodic re-drive/re-deliver loop over the channel spine."""

    def __init__(self, *, ingress_service: Any, spawn_background_task: Any) -> None:
        self._ingress = ingress_service
        self._spawn_background_task = spawn_background_task
        self._task: asyncio.Task | None = None
        self._closed = False

    # ── Lifecycle ───────────────────────────────────────────────────────

    def ensure_started(self) -> None:
        if self._closed:
            logger.warning("channel_spine_reconciler: not starting (closed)")
            return
        task = self._task
        if isinstance(task, asyncio.Task) and not task.done():
            return
        self._task = self._spawn_background_task(
            self._loop(), name="channel-spine-reconciler",
        )
        logger.info(
            "channel_spine_reconciler: started interval=%ds", _interval_seconds()
        )

    def quiesce(self) -> None:
        self._closed = True
        task = self._task
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel("shutdown")

    # ── Loop ────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        try:
            while not self._closed:
                try:
                    summary = await self.scan_once()
                    if any(summary.values()):
                        logger.info("channel_spine_reconciler: tick %s", summary)
                except Exception:
                    if self._closed:
                        return
                    logger.exception("channel_spine_reconciler: scan failed")
                if self._closed:
                    return
                try:
                    await asyncio.sleep(_interval_seconds())
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            logger.warning("channel_spine_reconciler: loop cancelled")
            raise

    # ── Single tick ─────────────────────────────────────────────────────

    async def scan_once(self) -> dict[str, int]:
        recovered = await self._ingress.recover_inbound()
        swept = await self._ingress.sweep_pending_outbox()
        return {"inbound_recovered": recovered, "outbox_swept": swept}
