"""Channel spine reconciler — the durable recovery owner (channel-spine.md).

Boot-time sweep plus a periodic scan over durable channel work:

* expired inbound work items (a worker died between ack and settle) are
  CAS-reclaimed and re-driven; the Session accepts a message's input once, so
  a message survives the loss of a worker process and is lost only with the
  database;
* due outbox rows (PENDING past their retry backoff, or SENDING with a lapsed
  lease) are leased and delivered against the exact bound reply, each
  Session's replies in order;
* authorizations whose observation never arrived die with evidence, and the
  participant who invoked the agent is told;
* Session output subscriptions whose Session recorded output past their
  position are projected into delivery records, covering any batch the
  reader's fact consumer did not reach; a subscription whose binding is
  disabled, or whose Session is gone, is removed.

Same host shape as :class:`~astrabox.core.service.orchestrator.expiration_watcher.ExpirationWatcher`:
owned by the platform service, started after bootstrap, cancelled on quiesce.
Every mutation goes through the fenced compare-and-set methods on
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
        return max(1, int(os.getenv("ASTRABOX_CHANNEL_RECONCILE_INTERVAL_SECONDS", "5")))
    except ValueError:
        return 5


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
        projected = await self._ingress.project_session_outputs()
        recovered = await self._ingress.recover_inbound()
        swept = await self._ingress.sweep_pending_outbox()
        expired = await self._ingress.expire_authorizations()
        return {
            "output_projected": projected, "inbound_recovered": recovered,
            "outbox_swept": swept, "authorizations_expired": expired,
        }
