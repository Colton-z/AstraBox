"""Startup reconciliation: abandoned Session cleanup after a process restart.

Owned by :class:`AgentPlatformService`, run once (under ``_bootstrap_lock``) the
first time ``ensure_bootstrap`` is called. Structurally identical to
``expiration_watcher.py``'s scan-and-reconcile shape: the repository selects only interrupted
startup candidates, and the reconciler fences each write against the row it
judged stale.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import parse_iso, utcnow
from astrabox.config.settings import get_settings
from astrabox.core.model import SessionState
from astrabox.persistence.repository.keyset import SWEEP_PAGE_SIZE, iter_keyset_pages

logger = get_logger(__name__)

# Grace period before bootstrap treats a CREATING session as stale; shorter
# startups are still legitimately in flight.
STARTUP_ALLOCATION_GRACE_SECONDS = 5 * 60


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
        """Converge Session startups abandoned by a process restart.

        Principle: bootstrap never changes session state blindly.
        - Stale CREATING (no recent activity) → TERMINATED.
        - Recent CREATING remains in flight.
        - Every other state is absent from the repository query.

        Restarting the interrupted startup is unsafe because the sandbox
        provider has no create idempotency key. A normal startup failure also
        terminates the Session, so process loss converges to that same public
        outcome instead of leaving a CREATING row that no startup drives.
        """
        sessions_repo = self._platform._sessions_repo
        await self._platform._runtime_manager.reconcile_startup_allocations(
            stale_before=utcnow()
            - timedelta(seconds=STARTUP_ALLOCATION_GRACE_SECONDS),
            limit=10_000,
        )
        # Every interrupted startup, read in pages: this runs once per start,
        # and a row it does not reach waits for the next process restart.
        sessions = [
            row
            async for row in iter_keyset_pages(
                lambda after: sessions_repo.list_bootstrap_reconcile_candidates(
                    after_session_id=after, limit=SWEEP_PAGE_SIZE
                ),
                key="session_id",
            )
        ]

        for session in sessions:
            state = str(session.get("state") or "")
            session_id = str(session.get("session_id") or "")

            if state != SessionState.CREATING.value:
                continue

            if not self._is_recent_creating_session(session):
                updates = {
                    "state": SessionState.TERMINATED.value,
                    "runtime_unavailable": True,
                    "last_error": "startup abandoned during process restart",
                    "startup_progress": None,
                }
                expected = {"state": SessionState.CREATING.value}
                observed_updated_at = str(session.get("updated_at") or "").strip()
                if observed_updated_at:
                    expected["updated_at"] = observed_updated_at
                updated = await sessions_repo.compare_and_update_session(
                    session_id,
                    expected=expected,
                    updates=updates,
                    touch_updated_at=False,
                )
                if not updated:
                    continue
                logger.info("bootstrap: session %s stale CREATING → TERMINATED", session_id)

    @staticmethod
    def _is_recent_creating_session(session: dict[str, Any]) -> bool:
        if str(session.get("state") or "") != SessionState.CREATING.value:
            return False

        latest_touch = None
        for key in ("updated_at", "created_at"):
            raw_value = str(session.get(key) or "").strip()
            if not raw_value:
                continue
            try:
                parsed = parse_iso(raw_value)
            except (TypeError, ValueError):
                continue
            if latest_touch is None or parsed > latest_touch:
                latest_touch = parsed

        if latest_touch is None:
            return False

        age_seconds = (utcnow() - latest_touch).total_seconds()
        return age_seconds <= STARTUP_ALLOCATION_GRACE_SECONDS
