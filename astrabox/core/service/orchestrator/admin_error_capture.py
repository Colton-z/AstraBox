"""Runtime error capture for the admin /errors page (observability).

The admin errors view (admin_service.admin_list_errors) combines persisted
session and failed-turn evidence with ERROR+ records that never touch those
resources, such as caught upstream failures and background task errors.

Common denominator: every such error is emitted via ``logger.error(...)`` under
the application's logging namespace. So capture happens at the logging layer:

- ``AdminErrorLogHandler`` is attached at import time and appends ERROR+ records
  to a bounded in-memory ring buffer. ``emit`` is synchronous, thread-safe, and
  never raises.
- A drain asyncio task upserts buffered records into a deduplicated, TTL-bounded
  repository collection. It is bootstrapped lazily from the first error seen
  inside a running loop; :func:`start_error_capture` also provides an explicit
  bootstrap entry point. A stable signature deduplicates repeated records by
  incrementing their counter.
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

from astrabox.persistence.repository.backend import get_async_collection
from astrabox.common.logger.logger_factory import MODULE_APP_NAME

COLLECTION_NAME = "runtime_errors"
_TTL_SECONDS = 7 * 24 * 3600
_BUFFER_MAXLEN = 2000
_FLUSH_INTERVAL_SECONDS = 0.5

# Diagnostics logger for this module — a plain stdlib logger that stays outside
# the captured shared logger, so the handler never captures its own failures
# (no recursion).
_diag = logging.getLogger("astrabox.admin_error_capture")

_buffer: "collections.deque[dict[str, Any]]" = collections.deque(maxlen=_BUFFER_MAXLEN)
_drain_task: "asyncio.Task[None] | None" = None
_handler: "AdminErrorLogHandler | None" = None
_indexes_ready = False
_dropped = 0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


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
        global _dropped
        try:
            if record.levelno < logging.ERROR:
                return
            try:
                message = record.getMessage()
            except Exception:
                message = str(getattr(record, "msg", ""))
            if len(_buffer) >= _BUFFER_MAXLEN:
                _dropped += 1
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


async def _ensure_indexes(collection: Any) -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        await collection.create_index("last_seen_ts", expireAfterSeconds=_TTL_SECONDS)
        await collection.create_index("signature", unique=True)
    except Exception as exc:
        _diag.warning("runtime_errors index create failed: %s", exc)
    _indexes_ready = True


async def _persist(item: dict[str, Any]) -> None:
    collection = await get_async_collection(COLLECTION_NAME)
    if collection is None:
        return
    await _ensure_indexes(collection)
    sig = _signature(item)
    ts = float(item.get("ts") or time.time())
    # Use only $set + $inc here (no $setOnInsert), so first_seen is not tracked;
    # last_seen + count carry the useful signal.
    await collection.update_one(
        {"signature": sig},
        {
            "$set": {
                "signature": sig,
                "logger": item.get("logger"),
                "severity": item.get("severity"),
                "error_type": item.get("error_type"),
                "message": item.get("message"),
                "location": item.get("location"),
                "traceback": item.get("traceback"),
                "last_seen_ts": ts,
                "last_seen": _iso(ts),
            },
            "$inc": {"count": 1},
        },
        upsert=True,
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

    Called from emit (the error path) and from start_error_capture (fresh start).
    Safe to call repeatedly and from the loop thread; a no-op without a running
    loop (a later loop-thread error will bootstrap it)."""
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


def start_error_capture() -> None:
    """Best-effort explicit bootstrap for the handler and drain task."""
    _attach_handler()
    _bootstrap_drain()
    _diag.warning("[ERR-CAPTURE] runtime error capture ready")


async def list_runtime_errors(limit: int = 200) -> list[dict[str, Any]]:
    """Return recent captured runtime errors, newest first."""
    try:
        collection = await get_async_collection(COLLECTION_NAME)
        if collection is None:
            return []
        cursor = collection.find({}, {"_id": 0}).sort("last_seen_ts", -1).limit(int(limit))
        return [doc async for doc in cursor]
    except Exception as exc:
        _diag.warning("runtime_errors list failed: %s", exc)
        return []


# Attach the handler before any application logger can emit an ERROR record.
_attach_handler()
