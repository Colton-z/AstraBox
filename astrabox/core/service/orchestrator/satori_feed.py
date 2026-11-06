"""One capture loop per Satori feed in this process, shared by the feed's connections.

Every WebSocket connection of one identity reads the same feed. Capturing it
once per connection multiplied the database work by the connections open, and
each of them held a database connection while it waited for the others. So a
process runs one loop per feed while any connection uses it, and a connection
only reads the events it has not sent once the loop reports a newer number.
Replicas still capture independently; the repository's compare-and-set keeps
their numbering exact.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from astrabox.common.logger.logger_factory import get_logger
from astrabox.persistence.repository.satori_repository import SatoriRepository

logger = get_logger(__name__)

#: How often a feed with open connections is captured.
CAPTURE_INTERVAL_S = 0.25


class FeedWatch:
    """The latest event number of one feed, as this process's capture loop last read it."""

    def __init__(self, repo: SatoriRepository, user_id: str, agent_id: str) -> None:
        self._repo = repo
        self._user_id = user_id
        self._agent_id = agent_id
        self._changed = asyncio.Condition()
        self._sn: int | None = None
        self._failure: Exception | None = None
        self._users = 0
        self._task: asyncio.Task[None] | None = None

    @property
    def failed(self) -> bool:
        return self._failure is not None

    async def beyond(self, after: int) -> int:
        """Wait until the feed has a number past ``after``, and return it.

        Raises once the capture loop has stopped on an error, so a connection
        does not wait on a feed nothing captures.
        """
        async with self._changed:
            await self._changed.wait_for(
                lambda: self._failure is not None or (self._sn is not None and self._sn > after)
            )
            if self._failure is not None:
                raise RuntimeError("the Satori feed capture stopped") from self._failure
            assert self._sn is not None
            return self._sn

    async def _run(self) -> None:
        try:
            while True:
                sn = await self._repo.capture(self._user_id, self._agent_id)
                if sn != self._sn:
                    async with self._changed:
                        self._sn = sn
                        self._changed.notify_all()
                await asyncio.sleep(CAPTURE_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Satori feed capture failed agent=%s", self._agent_id)
            async with self._changed:
                self._failure = exc
                self._changed.notify_all()


_WATCHES: dict[str, FeedWatch] = {}


@contextlib.asynccontextmanager
async def watch(repo: SatoriRepository, user_id: str, agent_id: str) -> AsyncIterator[FeedWatch]:
    """Use the feed's capture loop for the duration of one connection.

    The loop starts with the feed's first connection in this process and stops
    with its last. A loop that failed is replaced for the next connection; the
    connections that saw it fail have ended.
    """
    key = json.dumps([user_id, agent_id], ensure_ascii=False)
    feed = _WATCHES.get(key)
    if feed is None or feed.failed:
        feed = FeedWatch(repo, user_id, agent_id)
        _WATCHES[key] = feed
    feed._users += 1
    if feed._task is None:
        feed._task = asyncio.create_task(feed._run())
    try:
        yield feed
    finally:
        feed._users -= 1
        if feed._users == 0:
            if _WATCHES.get(key) is feed:
                del _WATCHES[key]
            feed._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feed._task
