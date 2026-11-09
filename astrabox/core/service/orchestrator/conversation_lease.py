"""How long a conversation's sandbox is held: one lease after its last activity.

A sandbox's deadline is ``sandbox_lease_seconds`` after the latest activity
recorded by the conversations bound to it: an accepted input, a question
opened or answered, or stored output. The deadline is read from the database,
so it holds with no later event and across reader takeover and restart, and
every renewal sets the same absolute deadline whichever replica makes it.

Two triggers use it. The expiration watcher renews every due sandbox on each
tick, which covers a conversation that records nothing more. A runtime that
starts working or waits on the user asks for its own sandbox at once, so a
conversation resumed in the last minutes of its lease does not wait for a
tick that may come after the sandbox is gone. A sandbox is due once its
recorded lease ends within ``sandbox_lease_renew_threshold_seconds``, which
keeps provider calls to about one per lease while a conversation stays active.
"""

from __future__ import annotations

import contextlib
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.persistence.provider import get_persistence

logger = get_logger(__name__)


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


class ConversationLeases:
    """Renews conversation sandboxes to one lease after their last activity."""

    def __init__(self, *, sessions_repo: Any, runtime_manager: Any) -> None:
        self._sessions = sessions_repo
        self._runtime_manager = runtime_manager

    def renew_before(self) -> datetime:
        """Sandboxes whose lease ends before this are due."""
        threshold = int(load_astrabox_settings().sandbox_lease_renew_threshold_seconds)
        return datetime.now(timezone.utc) + timedelta(seconds=threshold)

    async def hold(self, box: dict[str, Any]) -> bool | None:
        """Renew one due sandbox to its deadline; ``None`` when it needs no renewal.

        ``box`` is a row of ``SessionRepository.list_lease_renewal_candidates``.
        Returns whether the renewal succeeded. A renewal that fails is logged
        and left to the next trigger.
        """
        activity = _moment(box.get("activity_at"))
        if activity is None:
            return None
        deadline = activity + timedelta(seconds=int(load_astrabox_settings().sandbox_lease_seconds))
        recorded = _moment(box.get("expires_at"))
        if deadline <= datetime.now(timezone.utc) or (recorded is not None and deadline <= recorded):
            return None
        sandbox_id = str(box["sandbox_id"])
        session_ids = [str(sid) for sid in box.get("session_ids") or ()]
        # Only a box with one conversation is parked, under that
        # conversation's runtime lock, and renewing a paused box fails it;
        # holding the same lock keeps a parking on another replica from
        # pausing the box between this check and the renewal.
        lock = (
            get_persistence().coordination.runtime_lock(session_ids[0])
            if len(session_ids) == 1 else contextlib.nullcontext()
        )
        async with lock:
            if len(session_ids) == 1:
                fresh = await self._sessions.get_session(session_ids[0])
                if not fresh or fresh.get("sandbox_id") != sandbox_id or fresh.get("sandbox_parked_at"):
                    return None
            # Measured after the lock, which can wait out a parking.
            remaining = math.ceil((deadline - datetime.now(timezone.utc)).total_seconds())
            if remaining <= 0:
                return None
            try:
                expires = await self._runtime_manager.renew_sandbox_by_id(sandbox_id, remaining)
            except Exception as exc:  # noqa: BLE001 — the next trigger retries
                logger.warning("conversation lease renewal failed sandbox=%s: %s", sandbox_id, exc)
                return False
        for session_id in session_ids:
            await self._sessions.compare_and_update_session(
                session_id,
                expected={"sandbox_id": sandbox_id},
                updates={"expires_at": expires.isoformat() if expires is not None else None},
                touch_updated_at=False,
            )
        return True

    async def hold_sandbox(self, sandbox_id: str) -> None:
        """Renew this sandbox now if it is due; for a runtime's own activity."""
        try:
            boxes = await self._sessions.list_lease_renewal_candidates(
                renew_before_iso=self.renew_before().isoformat(), sandbox_id=sandbox_id,
            )
            for box in boxes:
                await self.hold(box)
        except Exception:
            logger.exception("conversation lease renewal failed sandbox=%s", sandbox_id)
