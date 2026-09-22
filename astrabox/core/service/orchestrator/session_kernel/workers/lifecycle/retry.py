"""Generic Mongo retry / settle-write infrastructure for the lifecycle worker.

Zero-domain-knowledge plumbing wrapping
``astrabox.common.utils.retry_utils.retry_async_call`` with this worker's
mongo-fail-fast-context handling and a fixed retry window/delay.
``_update_session_with_settle_retry`` additionally polls until a write is
confirmed to have "settled" to expected field values. Gathered here as
:class:`_LifecycleRetryMixin`, mixed into ``SessionLifecycleWorker``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recovery_ownership import RecoveryOwnership

from astrabox.persistence.repository.backend import (
    is_mongo_transient_error,
    mongo_fail_fast_context,
    mongo_fail_fast_reset,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.retry_utils import (
    build_retry_warning_before_sleep,
    retry_async_call,
)

logger = get_logger(__name__)


def _startup_settle_retry_window_seconds() -> float:
    raw = str(os.getenv("ASTRABOX_STARTUP_SETTLE_RETRY_WINDOW_SECONDS", "120")).strip()
    try:
        value = float(raw)
    except Exception:
        return 120.0
    if value < 5:
        return 5.0
    if value > 600:
        return 600.0
    return value


class _LifecycleRetryMixin:
    """Mongo retry / settle-write infrastructure, mixed into
    :class:`SessionLifecycleWorker`."""

    async def _run_startup_mongo_op_with_retry(
        self,
        *,
        session_id: str,
        operation: str,
        op,
        max_delay_seconds: float | None = None,
    ):
        async def _run_fail_fast():
            token = mongo_fail_fast_context()
            try:
                return await op()
            finally:
                mongo_fail_fast_reset(token)

        return await retry_async_call(
            _run_fail_fast,
            should_retry_exception=is_mongo_transient_error,
            max_delay_seconds=(
                self._STARTUP_SETTLE_RETRY_WINDOW_S
                if max_delay_seconds is None
                else max_delay_seconds
            ),
            wait_seconds=self._STARTUP_SETTLE_RETRY_DELAY_S,
            before_sleep=build_retry_warning_before_sleep(
                logger,
                lambda retry_state, exc: (
                    "startup mongo retry session=%s op=%s attempt=%s err=%s"
                    % (
                        session_id,
                        operation,
                        retry_state.attempt_number,
                        exc,
                    )
                ),
            ),
        )

    async def _run_session_repo_op_with_retry(
        self,
        op,
        *,
        operation: str = "startup:session_repo_op",
        session_id: str | None = None,
        max_delay_seconds: float | None = None,
    ):
        return await self._run_startup_mongo_op_with_retry(
            session_id=session_id or "<unknown>",
            operation=operation,
            op=op,
            max_delay_seconds=max_delay_seconds,
        )

    @staticmethod
    def _session_matches_expected_fields(
        session: dict[str, Any] | None,
        expected_fields: dict[str, Any],
    ) -> bool:
        if not isinstance(session, dict):
            return False
        for key, expected_value in expected_fields.items():
            if session.get(key) != expected_value:
                return False
        return True

    async def _update_session_with_settle_retry(
        self,
        *,
        session_id: str,
        updates: dict[str, Any],
        expected_fields: dict[str, Any] | None = None,
        ownership: RecoveryOwnership | None = None,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + self._STARTUP_SETTLE_RETRY_WINDOW_S

        def _remaining_budget() -> float:
            return deadline - time.monotonic()

        while True:
            remaining = _remaining_budget()
            if remaining <= 0:
                raise RuntimeError(
                    f"session {session_id} did not settle to expected fields {expected_fields}"
                )
            updated = await self._run_session_repo_op_with_retry(
                lambda: (ownership.update(updates) if ownership is not None
                         else self._sessions_repo.update_session(session_id, updates)),
                operation="startup:settle_update_session",
                session_id=session_id,
                max_delay_seconds=remaining,
            )
            if updated is False:
                raise RuntimeError(f"missing session {session_id}")
            remaining = _remaining_budget()
            if remaining <= 0:
                raise RuntimeError(
                    f"session {session_id} did not settle to expected fields {expected_fields}"
                )
            latest = await self._run_session_repo_op_with_retry(
                lambda: self._sessions_repo.get_session(session_id),
                operation="startup:settle_get_session",
                session_id=session_id,
                max_delay_seconds=remaining,
            )
            if not expected_fields or self._session_matches_expected_fields(latest, expected_fields):
                return latest
            remaining = _remaining_budget()
            if remaining <= 0:
                raise RuntimeError(
                    f"session {session_id} did not settle to expected fields {expected_fields}"
                )
            await asyncio.sleep(min(self._STARTUP_SETTLE_RETRY_DELAY_S, remaining))
