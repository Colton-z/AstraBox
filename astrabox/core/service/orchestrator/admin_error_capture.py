"""Runtime error capture for the admin /errors page (observability).

The admin errors view (admin_service.admin_list_errors) combines persisted
session and failed-turn evidence with ERROR+ records that never touch those
resources, such as caught upstream failures and background task errors.

Common denominator: every such error is emitted via ``logger.error(...)`` under
the application's logging namespace. So capture happens at the logging layer:

- ``AdminErrorLogHandler`` is attached at import time and appends ERROR+ records
  to a bounded in-memory ring buffer. ``emit`` is synchronous, thread-safe, and
  never raises.
- A drain asyncio task, started lazily from the first error seen inside a
  running loop, records each buffered record
  (:class:`~astrabox.persistence.repository.runtime_error_repository.RuntimeErrorRepository`).
  A stable signature deduplicates repeated records into one row that counts
  them and keeps when the error was first and last seen. The expiration
  watcher deletes errors that have not recurred for the retention period.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import time
import traceback as _tb
from datetime import datetime, timezone
from typing import Any

from astrabox.common.logger.logger_factory import MODULE_APP_NAME
from astrabox.persistence.repository.runtime_error_repository import RuntimeErrorRepository

_BUFFER_MAXLEN = 2000
_FLUSH_INTERVAL_SECONDS = 0.5

# Diagnostics logger for this module — a plain stdlib logger that stays outside
# the captured shared logger, so the handler never captures its own failures
# (no recursion).
_diag = logging.getLogger("astrabox.admin_error_capture")

_buffer: "collections.deque[dict[str, Any]]" = collections.deque(maxlen=_BUFFER_MAXLEN)
_drain_task: "asyncio.Task[None] | None" = None
_handler: "AdminErrorLogHandler | None" = None


def _exc_type_name(record: logging.LogRecord) -> str | None:
    if record.exc_info and record.exc_info[0] is not None:
        return record.exc_info[0].__name__
    return None


def _signature(item: dict[str, Any]) -> str:
    raw = "|".join((
        str(item.get("logger") or ""),
        str(item.get("error_type") or ""),
        str(item.get("location") or ""),
        str(item.get("message") or "")[:200],
    ))
    return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()


class AdminErrorLogHandler(logging.Handler):
    """Append ERROR+ records to the ring buffer. Never blocks or raises."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR:
                return
            try:
                message = record.getMessage()
            except Exception:
                message = str(getattr(record, "msg", ""))
            _buffer.append({
                "logger": record.name,
                "severity": record.levelname,
                "error_type": _exc_type_name(record),
                "message": message[:2000],
                "location": f"{record.pathname}:{record.lineno}",
                "traceback": (
                    "".join(_tb.format_exception(*record.exc_info))[:6000]
                    if record.exc_info else None
                ),
                "ts": time.time(),
            })
            _bootstrap_drain()  # lazily start the async drain when a loop exists
        except Exception:
            # A logging handler must never raise into the caller.
            try:
                self.handleError(record)
            except Exception:
                pass


async def _persist(item: dict[str, Any]) -> None:
    await RuntimeErrorRepository().record(
        signature=_signature(item),
        logger=str(item.get("logger") or ""),
        severity=str(item.get("severity") or "ERROR"),
        error_type=item.get("error_type"),
        message=str(item.get("message") or ""),
        location=str(item.get("location") or ""),
        traceback=item.get("traceback"),
        seen=datetime.fromtimestamp(float(item.get("ts") or time.time()), tz=timezone.utc),
    )


async def _drain() -> None:
    while True:
        try:
            while _buffer:
                item = _buffer.popleft()
                try:
                    await _persist(item)
                except Exception as exc:
                    _diag.warning("runtime_errors persist failed: %s", exc)
        except Exception as exc:  # the drain loop itself must never die
            _diag.warning("runtime_errors drain loop error: %s", exc)
        await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)


def _bootstrap_drain() -> None:
    """Start the drain task once, if a running event loop is available.

    Called from emit (the error path). Safe to call repeatedly and from the
    loop thread; a no-op without a running loop (a later loop-thread error
    will bootstrap it)."""
    global _drain_task
    if _drain_task is not None and not _drain_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        return
    if not loop.is_running():
        return
    _drain_task = loop.create_task(_drain())


def _attach_handler() -> None:
    """Attach the capture handler to the module's loggers (idempotent).

    The ``MODULE_APP_NAME`` logger is the parent configured by ``get_logger``;
    every application module logger propagates to it while keeping its own name.
    Library loggers (pymongo/httpx/...) are deliberately left uncaptured to keep
    third-party noise out of the view.
    """
    global _handler
    try:
        handler = AdminErrorLogHandler(level=logging.ERROR)
        target = logging.getLogger(MODULE_APP_NAME)
        for existing in list(getattr(target, "handlers", [])):
            if isinstance(existing, AdminErrorLogHandler):
                target.removeHandler(existing)
        target.addHandler(handler)
        _handler = handler
    except Exception as exc:
        _diag.warning("[ERR-CAPTURE] attach failed: %s", exc)


async def list_runtime_errors(limit: int = 200) -> list[dict[str, Any]]:
    """The ``limit`` most recently seen captured errors, newest first."""
    return await RuntimeErrorRepository().list_recent(int(limit))


# Attach the handler before any application logger can emit an ERROR record.
_attach_handler()
