"""This process's session readers: where they start, how they end.

A session needs a reader while its runtime lives or while the platform holds
inputs for it. :meth:`ReaderHost.ensure` starts one here unless a live reader
lease exists somewhere; the send path calls it for every accepted input, and
a ``reader`` notice (a runtime became ready, a box was declared dead) or the
periodic sweep calls it for the rest.

A reader whose event stream ends reconnects through the connector, which
decides whether the runtime is still there. When it is confirmed gone, the
host records what was lost under the reader lease, takes back the inputs the
runtime never durably held, and, if any are waiting, asks for a replacement
runtime; the replacement's readiness notice starts the next reader.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.fault_injection import pass_fault_barrier
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.runtime import RuntimeClient
from astrabox.core.service.orchestrator.session_runtime.feed import LivePart, SessionFeed
from astrabox.core.service.orchestrator.session_runtime.reader import (
    LEASE_TTL_S,
    ReaderHooks,
    SessionReader,
    input_failed_part,
    lost_response_error,
    lost_response_parts,
)
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch,
    InputsFailed,
    OutputPart,
    ReaderLease,
    ResponseEnd,
    SessionRuntimeRepository,
)
from astrabox.seams.runtime_preparation import (
    TURN_PREPARATION_USER_ACTION_REQUIRED,
    validated_user_action,
)

logger = get_logger(__name__)

#: Delay before reconnecting to a runtime whose stream ended, doubling to the cap.
_RECONNECT_DELAY_S = 0.5
_RECONNECT_DELAY_CAP_S = 10.0
_SWEEP_INTERVAL_S = 5.0


class RuntimeGone(Exception):
    """The session's runtime is confirmed gone (its box died or was reclaimed)."""


class RuntimeEnded(Exception):
    """The conversation ended (terminated or deleted): its runtime is gone and will not be replaced."""


class RuntimeNotReady(Exception):
    """The session has no runtime to connect to yet (startup in progress or waiting on the user)."""


class RuntimeStartFailed(Exception):
    """A replacement for the waiting inputs already failed to start; they get no second attempt."""


@dataclass(frozen=True, slots=True)
class ConnectedRuntime:
    """A live client and the box it runs in."""

    client: RuntimeClient
    sandbox_id: str
    sandbox_handle: Any
    runtime_identity: dict[str, Any]
    engine_kind: str
    #: The adapter's host-side refresh before each input (rotated MCP credentials).
    refresh: Callable[[], Awaitable[None]] | None = None


class RuntimeConnector(Protocol):
    async def connect(self, session_id: str) -> ConnectedRuntime:
        """A live client for the session's runtime.

        Raises :class:`RuntimeGone` when the runtime is confirmed gone and
        :class:`RuntimeNotReady` when there is none yet.
        """
        ...

    async def replace(self, session_id: str) -> None:
        """Start a replacement runtime for a session whose runtime is gone."""
        ...

    async def release(self, session_id: str) -> None:
        """Drop this process's hold on the session's runtime once nothing here reads it."""
        ...

    async def sessions_needing_readers(self) -> list[str]:
        """Sessions with a live runtime or held inputs and no live reader lease."""
        ...


HooksFactory = Callable[[str, ConnectedRuntime], ReaderHooks]
#: Runs the consumers of recorded facts for one committed batch of a session.
CommittedFacts = Callable[[str, FactBatch], Awaitable[None]]


def _owner_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class ReaderHost:
    def __init__(
        self,
        *,
        repo: SessionRuntimeRepository,
        feed: SessionFeed,
        connector: RuntimeConnector,
        hooks: HooksFactory,
        committed: CommittedFacts,
    ) -> None:
        self._repo = repo
        self._feed = feed
        self._connector = connector
        self._hooks = hooks
        self._committed = committed
        self.owner = _owner_name()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: Sessions whose reader lease this host is claiming now.
        self._claiming: set[str] = set()
        self._sweeper: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._sweeper is None:
            self._feed.on_reader_needed(self._reader_needed)
            self._sweeper = asyncio.create_task(self._sweep_forever(), name="reader-host-sweep")

    def _reader_needed(self, session_id: str) -> None:
        task = asyncio.create_task(self.ensure(session_id), name=f"reader-ensure-{session_id}")
        task.add_done_callback(_log_failure)

    async def stop(self) -> None:
        tasks = [task for task in (self._sweeper, *self._tasks.values()) if task is not None]
        self._sweeper = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def reads(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    async def ensure(self, session_id: str) -> None:
        """Start a reader for the session here unless one is running anywhere.

        One claim at a time per session: a claim by this host always succeeds
        and starts a new generation, so a second concurrent claim would revoke
        the lease of the reader the first one starts.
        """
        if self.reads(session_id) or session_id in self._claiming:
            return
        self._claiming.add(session_id)
        try:
            lease = await self._repo.claim_reader(session_id, owner=self.owner, ttl_s=LEASE_TTL_S)
            if lease is None:
                return
            self._tasks[session_id] = asyncio.create_task(
                self._serve(lease), name=f"session-reader-{session_id}"
            )
        finally:
            self._claiming.discard(session_id)

    # -- one session -------------------------------------------------------

    async def _serve(self, lease: ReaderLease) -> None:
        session_id = lease.session_id
        delay = _RECONNECT_DELAY_S
        connected: ConnectedRuntime | None = None
        try:
            while True:
                try:
                    connected = await self._connector.connect(session_id)
                except RuntimeNotReady:
                    return
                except RuntimeGone:
                    await self._runtime_gone(lease, reason="sandbox_lost")
                    return
                except RuntimeEnded:
                    await self._runtime_gone(lease, reason="conversation_ended")
                    return
                await pass_fault_barrier(
                    "reader_boundary", session_id=session_id, phase="connected", item_id=""
                )
                reader = SessionReader(
                    lease=lease,
                    client=connected.client,
                    repo=self._repo,
                    feed=self._feed,
                    hooks=self._hooks(session_id, connected),
                )
                try:
                    ended = await reader.run()
                finally:
                    with contextlib.suppress(Exception):
                        await connected.client.close()
                if ended == "lease_lost":
                    return
                logger.info("session %s runtime stream ended; reconnecting in %.1fs", session_id, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_DELAY_CAP_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("session %s reader failed", session_id)
        finally:
            self._tasks.pop(session_id, None)
            if connected is not None:
                with contextlib.suppress(Exception):
                    await self._connector.release(session_id)
            with contextlib.suppress(Exception):
                await self._repo.release_reader(lease)

    async def _runtime_gone(self, lease: ReaderLease, *, reason: str) -> None:
        """Record the responses the runtime took with it, and hand its inputs on.

        A lost box's inputs go to a replacement runtime; an ended
        conversation's inputs fail, since nothing will answer them.
        """
        session_id = lease.session_id
        last_seq, _cursor = await self._repo.resume_point(session_id)
        batch = FactBatch(status="idle")
        for response_id in await self._repo.open_response_ids(session_id):
            batch.facts.append(ResponseEnd(response_id, "failed", reason, error=lost_response_error(reason)))
            for part in lost_response_parts(response_id, reason):
                last_seq += 1
                batch.output.append(OutputPart(last_seq, response_id, part))
        if not await self._repo.record(lease, batch, ttl_s=LEASE_TTL_S):
            return
        for out in batch.output:
            self._feed.publish_live(session_id, LivePart(out.seq, out.response_id, out.part))
        await self._committed(session_id, batch)
        if reason == "conversation_ended":
            await self._fail_open_inputs(
                lease,
                {"code": "CONVERSATION_ENDED", "message": "the conversation ended before this message was answered"},
            )
            return
        await self._repo.reclaim_unadmitted(session_id)
        if await self._repo.undelivered_inputs(session_id):
            logger.info("session %s runtime gone with inputs waiting; starting a replacement", session_id)
            try:
                await self._connector.replace(session_id)
            except RuntimeStartFailed:
                # A later message gets a new attempt; these were waiting for
                # the one that failed.
                await self._fail_open_inputs(
                    lease,
                    {
                        "code": "RUNTIME_START_FAILED",
                        "message": "the conversation's runtime could not be started; send the message again to retry",
                    },
                )
            except APIError as exc:
                if exc.code != TURN_PREPARATION_USER_ACTION_REQUIRED:
                    raise
                # The last startup waits on the person; their messages are
                # answered with what they must do instead of waiting unseen.
                action = validated_user_action((exc.evidence or {}).get("user_action"))
                failure: dict[str, Any] = {"code": exc.code, "message": exc.message}
                if action is not None:
                    failure["user_action"] = action
                await self._fail_open_inputs(lease, failure)

    async def _fail_open_inputs(self, lease: ReaderLease, failure: dict[str, Any]) -> None:
        """Fail the inputs nothing will answer, and tell the page which and why."""
        session_id = lease.session_id
        input_ids = await self._repo.open_input_ids(session_id)
        if not input_ids:
            return
        last_seq, _cursor = await self._repo.resume_point(session_id)
        batch = FactBatch(facts=[InputsFailed(tuple(input_ids), failure)])
        for input_id in input_ids:
            last_seq += 1
            batch.output.append(OutputPart(last_seq, None, input_failed_part(input_id, failure)))
        if not await self._repo.record(lease, batch, ttl_s=LEASE_TTL_S):
            return
        for out in batch.output:
            self._feed.publish_live(session_id, LivePart(out.seq, out.response_id, out.part))

    # -- sweep -------------------------------------------------------------

    async def _sweep_forever(self) -> None:
        while True:
            try:
                for session_id in await self._connector.sessions_needing_readers():
                    await self.ensure(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reader host sweep failed")
            await asyncio.sleep(_SWEEP_INTERVAL_S)


def _log_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("starting a session reader failed", exc_info=task.exception())
