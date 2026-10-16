"""Process-wide wakeups for sessions: live output and notices from other processes.

A session's subscribers in this process hear from two sources:

* :meth:`SessionFeed.publish_live` delivers parts from a reader running in
  this process as it reads them, with no database work in between;
* The selected persistence provider carries committed-state notices from any process:
  an input was accepted (``input``), a reader committed output through a
  sequence (``output``), a reader released its lease (``reader``). The
  transaction that changes the database enqueues its notice, so it arrives
  only after the change is visible.

A subscriber that cannot tell what it missed (its live queue overflowed, or the
listening connection was replaced) receives :class:`Resync` and reads the
output log again.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from astrabox.persistence.provider import get_persistence
from astrabox.seams.persistence_coordination import Notice, Resync, SessionNotice

#: Live parts a subscriber may have queued before further ones are dropped;
#: the subscriber reads dropped parts back from the output log.
_LIVE_QUEUE_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class LivePart:
    seq: int
    response_id: str | None
    part: dict[str, Any]


FeedItem = LivePart | Notice | Resync


class Subscription:
    """One subscriber's queue for one session."""

    def __init__(self, feed: SessionFeed, session_id: str) -> None:
        self._feed = feed
        self.session_id = session_id
        self._queue: asyncio.Queue[FeedItem] = asyncio.Queue()
        self._live = 0
        self._dropped = False

    def _put_live(self, item: LivePart) -> None:
        if self._live >= _LIVE_QUEUE_LIMIT:
            if not self._dropped:
                self._dropped = True
                self._queue.put_nowait(Resync())
            return
        self._live += 1
        self._queue.put_nowait(item)

    def _put(self, item: Notice | Resync) -> None:
        self._queue.put_nowait(item)

    async def get(self) -> FeedItem:
        item = await self._queue.get()
        if isinstance(item, LivePart):
            self._live -= 1
        elif isinstance(item, Resync):
            self._dropped = False
        return item

    def close(self) -> None:
        self._feed._unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class SessionFeed:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[Subscription]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._reader_needed: Callable[[str], None] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start listening; returns once the first connection is listening."""
        if self._task is None:
            self._task = asyncio.create_task(self._listen_forever(), name="session-feed-listener")
        await self._ready.wait()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _listen_forever(self) -> None:
        await get_persistence().coordination.listen(
            on_notice=self._on_notice,
            on_resync=lambda: self._broadcast(Resync()),
            on_ready=self._ready.set,
        )

    # -- subscribers -------------------------------------------------------

    def on_reader_needed(self, callback: Callable[[str], None]) -> None:
        """Call ``callback(session_id)`` for every ``reader`` notice: a session's
        runtime became ready, or its box was declared dead."""
        self._reader_needed = callback

    def subscribe(self, session_id: str) -> Subscription:
        subscription = Subscription(self, session_id)
        self._subscribers[session_id].add(subscription)
        return subscription

    def _unsubscribe(self, subscription: Subscription) -> None:
        subscribers = self._subscribers.get(subscription.session_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            self._subscribers.pop(subscription.session_id, None)

    def wake(self, session_id: str) -> None:
        """Tell this process's subscribers that inputs wait for the session's runtime."""
        for subscription in tuple(self._subscribers.get(session_id, ())):
            subscription._put(Notice(kind="input", seq=0))

    def publish_live(self, session_id: str, item: LivePart) -> None:
        for subscription in tuple(self._subscribers.get(session_id, ())):
            subscription._put_live(item)

    def _on_notice(self, notice: SessionNotice) -> None:
        kind, seq, session_id = notice.kind, notice.seq, notice.session_id
        if kind == "reader" and self._reader_needed is not None:
            self._reader_needed(session_id)
        item = Notice(kind=kind, seq=seq)
        for subscription in tuple(self._subscribers.get(session_id, ())):
            subscription._put(item)

    def _broadcast(self, item: Resync) -> None:
        for subscribers in tuple(self._subscribers.values()):
            for subscription in tuple(subscribers):
                subscription._put(item)
