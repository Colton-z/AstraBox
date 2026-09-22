from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class _StartupProgressState:
    """Holder for ``_run_session_startup_direct``'s progress-reporting locals.

    Gives ``_do_progress``/``_emit_progress``/``_drain_progress`` a shared,
    explicit object to mutate instead of a ``nonlocal`` closure cell.
    """

    last_progress: str = ""
    progress_tasks: list[asyncio.Task] = field(default_factory=list)


@dataclass
class _StartupEventCursor:
    """Holder for ``_run_startup_command``'s ``last_event_seq`` local.

    Gives ``_record_progress``/``_record_ready``/``_record_failed`` a shared,
    explicit object to mutate instead of a ``nonlocal`` closure cell.
    """

    last_event_seq: int = 0
