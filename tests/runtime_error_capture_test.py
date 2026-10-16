"""Captured runtime errors: replicas recording one new error at once count every sighting."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from astrabox.core.service.orchestrator import admin_error_capture

pytestmark = pytest.mark.usefixtures("database")


def _record(message: str, at: float) -> dict[str, Any]:
    """A buffered record as ``AdminErrorLogHandler.emit`` builds it."""
    return {
        "logger": "astrabox.channels",
        "severity": "ERROR",
        "error_type": "RuntimeError",
        "message": message,
        "location": "/app/astrabox/channels.py:42",
        "traceback": f"Traceback (most recent call last):\nRuntimeError: {message}",
        "ts": at,
    }


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat()


async def test_replicas_recording_a_new_error_at_once_count_every_sighting() -> None:
    now = time.time()

    await asyncio.gather(*(
        admin_error_capture._persist(_record("upstream refused the call", now + index))
        for index in range(20)
    ))

    [row] = await admin_error_capture.list_runtime_errors()
    assert row["count"] == 20
    assert (row["first_seen"], row["last_seen"]) == (_iso(now), _iso(now + 19))
