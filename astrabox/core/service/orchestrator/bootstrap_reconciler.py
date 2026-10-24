"""Startup reconciliation: abandoned Session cleanup after a process restart.

Owned by :class:`AgentPlatformService`, run once under ``_bootstrap_lock``.
The same startup reconciliation runs on every expiration-watcher tick, so a
recent interrupted start remains eligible after its grace period.
"""

from __future__ import annotations

import contextlib
import os
from datetime import timedelta
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow
from astrabox.config.settings import get_settings

logger = get_logger(__name__)

# Unowned starts stay eligible until this grace period has elapsed. A live
# startup lock, not age, protects a startup still driven by another replica.
STARTUP_ALLOCATION_GRACE_SECONDS = int(
    os.getenv("ASTRABOX_SESSION_CREATION_TIMEOUT_SECONDS", "") or 300
)


class BootstrapReconciler:
    """Repo-wide startup sweep. Owned by :class:`AgentPlatformService`.

    Reaches into the platform facade's collaborators by reference (the same
    ``platform_service=self`` wiring :class:`ExpirationWatcher` uses): this is
    platform-level bookkeeping (``_bootstrapped`` / ``_bootstrap_lock``), not
    an independently-testable domain service.
    """

    def __init__(self, *, platform_service: Any) -> None:
        self._platform = platform_service

    # ── Bootstrap ────────────────────────────────────────────────────────

    async def ensure_bootstrap(self) -> None:
        platform = self._platform
        platform._raise_if_quiesced()
        if platform._bootstrapped:
            return
        async with platform._bootstrap_lock:
            platform._raise_if_quiesced()
            if platform._bootstrapped:
                return
            try:
                await self._reconcile_bootstrap_session_states()
                platform._bootstrapped = True
            except Exception as exc:
                logger.warning("bootstrap deferred (the database may be temporarily unavailable): %s", exc)
            with contextlib.suppress(Exception):
                platform._expiration_watcher.ensure_started()
            if not get_settings().background_execution_enabled:
                return
            # The channel spine's recovery owner: its first tick re-drives
            # expired inbound work items and re-delivers abandoned outbox
            # rows (docs/channel-spine.md), covering boot-time recovery.
            with contextlib.suppress(Exception):
                platform._channel_spine_reconciler.ensure_started()
            # Broker consumers for sourcing channel providers: each envelope
            # is acked only after the spine's durable claim (invariant A).
            with contextlib.suppress(Exception):
                platform._channel_source_host.ensure_started()

    async def _reconcile_bootstrap_session_states(self) -> None:
        """Run the periodic startup cleanup once before starting the watchers."""
        await self._platform._runtime_manager.reconcile_startup_allocations(
            stale_before=utcnow()
            - timedelta(seconds=STARTUP_ALLOCATION_GRACE_SECONDS),
            on_abandoned=self._platform._session_lifecycle.converge_abandoned_startup,
            limit=10_000,
        )
