from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

async def drain_cancelled_stream_start(
    session_id: str,
    agen: AsyncIterator[dict[str, Any]],
    first_event_task: asyncio.Task,
) -> None:
    """Continue a turn in background when the request disconnects early."""
    try:
        with contextlib.suppress(StopAsyncIteration):
            await first_event_task
        async for _ in agen:
            pass
    except Exception:
        logger.warning(
            "background drain after cancelled stream start failed session=%s",
            session_id,
            exc_info=True,
        )
    finally:
        with contextlib.suppress(Exception):
            await agen.aclose()


def spawn_drain_cancelled_stream_start(
    session_id: str,
    agen: AsyncIterator[dict[str, Any]],
    first_event_task: asyncio.Task,
    *,
    spawn_background_task: Callable[..., asyncio.Task],
) -> asyncio.Task:
    """Hand a cancelled request's drain to the platform task owner.

    The platform spawner rejects work after quiesce, detaches the task from the
    request context, and retains it until completion. The logging callback here
    only observes a terminal failure; it does not own the task's lifetime.

    Used by ``turns.py``'s ``send_message_ai_stream`` when the client
    disconnects before the turn's first event arrives: the underlying
    generator may already have durably dispatched the command and spawned a
    worker task (both already decoupled from this request's lifetime), so
    cancelling here would abandon that worker with nobody draining its
    output, wedging the session until reconciliation's staleness timeout
    reclaims it. Continuing to drain the SAME generator in the background
    lets the turn finish normally instead.
    """
    task = spawn_background_task(
        drain_cancelled_stream_start(session_id, agen, first_event_task),
        name=f"ai-stream-drain-cancelled-{session_id}",
    )
    task.add_done_callback(_log_drain_task_done)
    return task


def _log_drain_task_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "background drain after cancelled stream start crashed: %s",
            exc,
            exc_info=exc,
        )
