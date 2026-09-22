"""Per-turn latency trace extracted from :mod:`turn_worker`.

Self-contained value object: a stage timing/context accumulator emitted via
``mark(...)`` / ``log(...)`` and threaded by reference through the bridge command.
"""
from __future__ import annotations

import json
import time
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_kernel.workers.turn._helpers import (
    _elapsed_since_iso_ms,
)

logger = get_logger(__name__)


class _TurnLatencyTrace:
    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        command_id: str | None,
        command_type: str | None,
        accepted_at: Any,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.command_id = command_id
        self.command_type = command_type
        self.accepted_at = str(accepted_at or "").strip() or None
        self.user_id: str | None = None
        self.session_kind: str | None = None
        self._start_mono = time.monotonic()
        self._last_mono = self._start_mono
        self._samples: list[dict[str, Any]] = []

    def set_context(self, *, user: UserContext | None, session: dict[str, Any] | None) -> None:
        if user is not None:
            self.user_id = str(getattr(user, "user_id", "") or "").strip() or None
        if isinstance(session, dict):
            self.session_kind = (
                str(session.get("session_kind") or session.get("kind") or "").strip() or None
            )

    def mark(self, stage: str, **fields: Any) -> None:
        now = time.monotonic()
        sample = {
            "stage": stage,
            "elapsed_ms": round((now - self._start_mono) * 1000, 3),
            "delta_ms": round((now - self._last_mono) * 1000, 3),
        }
        for key, value in fields.items():
            if value is not None:
                sample[str(key)] = value
        self._samples.append(sample)
        self._last_mono = now

    def log(self, label: str) -> None:
        logger.info(
            "turn_latency_observation label=%s session=%s turn=%s command=%s "
            "command_type=%s session_kind=%s user_id=%s accepted_at=%s "
            "accepted_to_now_ms=%s total_elapsed_ms=%s samples=%s",
            label,
            self.session_id,
            self.turn_id,
            self.command_id,
            self.command_type,
            self.session_kind,
            self.user_id,
            self.accepted_at,
            _elapsed_since_iso_ms(self.accepted_at),
            round((time.monotonic() - self._start_mono) * 1000, 3),
            json.dumps(self._samples, ensure_ascii=False, separators=(",", ":")),
        )
