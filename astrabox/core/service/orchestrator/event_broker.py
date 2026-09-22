from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import Any

#: Delivered as the last item a disconnected (overflowing) subscriber's queue
#: will ever receive. Its ``type`` deliberately does not match any recognized
#: broker-frame shape (``ai_sdk_frame`` / ``ai_sdk_live_frame``), so existing
#: consumers treat it as just another unrecognized event and fall back to
#: their own durable resync path; it exists purely to wake a consumer that is
#: blocked on ``queue.get()`` immediately, instead of making it wait out its
#: own poll timeout to notice the disconnect.
OVERFLOW_SENTINEL: dict[str, Any] = {"type": "broker_overflow_disconnect"}


class SessionEventBroker:
    def __init__(self) -> None:
        self._listeners: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._listeners[session_id].add(queue)
        return queue

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            listeners = self._listeners.get(session_id)
            if not listeners:
                return
            listeners.discard(queue)
            if not listeners:
                self._listeners.pop(session_id, None)

    async def publish(self, session_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            listeners = list(self._listeners.get(session_id, set()))

        overflowed: list[asyncio.Queue] = []
        for queue in listeners:
            if queue.full():
                # This is an ordered protocol (e.g. a text-start frame must
                # precede the text-delta frames for the same block). Evicting
                # the oldest queued frame to make room for the newest can
                # silently discard an opening frame while a later frame that
                # depends on it survives: corruption, not truncation. The
                # lagging subscriber is disconnected instead: it stops
                # receiving live events (handled below).
                overflowed.append(queue)
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Lost a race against another publish; treat the same as a
                # detected overflow above.
                overflowed.append(queue)

        if not overflowed:
            return

        async with self._lock:
            listener_set = self._listeners.get(session_id)
            for queue in overflowed:
                if listener_set is not None:
                    listener_set.discard(queue)
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(dict(OVERFLOW_SENTINEL))
            if listener_set is not None and not listener_set:
                self._listeners.pop(session_id, None)
