"""The one process that reads a session's runtime and records what it reports.

A reader holds the session's reader lease and a live :class:`RuntimeClient`.
It runs three loops until the runtime's event stream ends or the lease is
lost:

* **read**: each runtime event becomes recorded facts. A browser part is
  numbered and published to this process's subscribers at once, then
  committed with the next batch together with the runtime cursor it was read
  up to. A reader that takes over resumes from that cursor and numbers the
  same events the same way.
* **commit**: batches are committed in order under the lease generation;
  state changes (a response starting or ending, an interaction) are committed
  without waiting for the batch interval.
* **deliver**: what people asked of the runtime is handed to it over the
  reader's connection, the only one the runtime serves: inputs in acceptance
  order after the deployment's turn preparation, answers to open
  interactions, and interrupts. Any process may accept them; a notice wakes
  this loop.

Parts the platform adds itself (a refused input, a response the runtime lost)
are committed before they are published, because no runtime event would
reproduce them for a reader that takes over.
"""

from __future__ import annotations

import asyncio
import json
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.runtime import (
    InputAdmitted,
    InputRefused,
    InteractionClosed,
    InteractionNotOpen,
    InteractionOpened,
    ResponseEnded,
    ResponseOutput,
    ResponseStarted,
    RuntimeClient,
    RuntimeCursor,
    RuntimeCursorExpired,
    RuntimeDiagnostic,
    RuntimeEvent,
    RuntimeInput,
    RuntimeInputRefused,
    RuntimeState,
    StatusChanged,
)
from astrabox.common.fault_injection import pass_fault_barrier
from astrabox.core.service.orchestrator.tool_cards import card_parts
from astrabox.core.service.orchestrator.session_runtime.feed import (
    LivePart,
    Notice,
    Resync,
    SessionFeed,
)
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch,
    InputsAdmitted,
    InputsFailed,
    InteractionClose,
    InteractionOpen,
    OutputPart,
    ReaderLease,
    ResponseEnd,
    ResponseStart,
    SessionRuntimeRepository,
)

logger = get_logger(__name__)

#: How long browser parts wait to be committed together. State changes do not wait.
COMMIT_INTERVAL_S = 0.05
#: How long a reader lease lasts without a renewal; renewed every third of it.
LEASE_TTL_S = 30.0


class InputPreparationRefused(Exception):
    """The deployment's turn preparation refused an input; the person sees ``failure``."""

    def __init__(self, failure: dict[str, Any]) -> None:
        super().__init__(str(failure.get("message") or "input preparation refused"))
        self.failure = failure


@dataclass(frozen=True, slots=True)
class ReaderHooks:
    """What the reader asks of the rest of the platform.

    ``prepare`` runs the deployment's turn preparation before one input is
    written to the runtime and raises :class:`InputPreparationRefused` when the
    input must not be. ``committed`` receives each batch after it is durable,
    for consumers that act on recorded facts. ``status`` receives the
    runtime's own status as it changes. ``permission_mode`` applies the
    session's stored permission mode to the runtime.
    """

    prepare: Callable[[RuntimeInput], Awaitable[None]]
    committed: Callable[[FactBatch], Awaitable[None]]
    status: Callable[[str], Awaitable[None]]
    permission_mode: Callable[[], Awaitable[None]]


ReaderExit = Literal["stream_ended", "lease_lost"]


class _LeaseLost(Exception):
    pass


#: The most of one diagnostic's raw evidence written to the log.
_DIAGNOSTIC_LOG_LIMIT = 16_384


def _bounded_json(raw: Any) -> str:
    """One diagnostic's raw evidence as one log line, cut at the limit."""
    text = json.dumps(raw, ensure_ascii=False, default=str, separators=(",", ":"))
    return text if len(text) <= _DIAGNOSTIC_LOG_LIMIT else text[:_DIAGNOSTIC_LOG_LIMIT] + "…"


def lost_response_error(reason: str) -> dict[str, Any]:
    """Why the platform ended a response its runtime lost, as recorded and shown."""
    return {
        "code": reason.upper(),
        "message": f"The agent runtime stopped before this response finished ({reason}).",
    }


def lost_response_parts(response_id: str, reason: str) -> list[dict[str, Any]]:
    """The parts that close a response the runtime has lost.

    They open with the response's ``start``: a response paused on an
    interaction has already finished its browser message, and in a message
    still open a repeated ``start`` only names it again.
    """
    return [
        {"type": "start", "messageId": response_id},
        {"type": "error", "errorText": lost_response_error(reason)["message"]},
        {"type": "finish", "messageMetadata": {"response_id": response_id, "outcome": "failed"}},
    ]


def input_failed_part(input_id: str, failure: dict[str, Any]) -> dict[str, Any]:
    return {"type": "data-input-failed", "data": {"input_id": input_id, **failure}}


class SessionReader:
    def __init__(
        self,
        *,
        lease: ReaderLease,
        client: RuntimeClient,
        repo: SessionRuntimeRepository,
        feed: SessionFeed,
        hooks: ReaderHooks,
    ) -> None:
        self._lease = lease
        self._client = client
        self._repo = repo
        self._feed = feed
        self._hooks = hooks
        self._sid = lease.session_id
        self._seq = 0
        self._batch = FactBatch()
        self._dirty = asyncio.Event()
        self._urgent = asyncio.Event()
        self._commit_lock = asyncio.Lock()
        self._closing = False
        #: Responses whose first text this reader has read (for the delay stamp).
        self._texted: set[str] = set()

    @property
    def session_id(self) -> str:
        return self._sid

    async def run(self) -> ReaderExit:
        """Read until the runtime's stream ends or the lease is lost."""
        subscription = self._feed.subscribe(self._sid)
        lost = False
        try:
            async with asyncio.TaskGroup() as group:
                reading = group.create_task(self._read(), name=f"reader-read-{self._sid}")
                committing = group.create_task(self._commit_loop(), name=f"reader-commit-{self._sid}")
                delivering = group.create_task(
                    self._deliver_loop(subscription), name=f"reader-deliver-{self._sid}"
                )
                await reading
                delivering.cancel()
                # The commit loop flushes what is left and stops on its own.
                self._closing = True
                self._dirty.set()
                await committing
        except* _LeaseLost:
            logger.warning("session %s reader lost its lease", self._sid)
            lost = True
        finally:
            subscription.close()
        return "lease_lost" if lost else "stream_ended"

    # -- reading -----------------------------------------------------------

    async def _read(self) -> None:
        last_seq, cursor_json = await self._repo.resume_point(self._sid)
        self._seq = last_seq
        events = await self._open_events(RuntimeCursor.from_json(cursor_json))
        async for event in events:
            if isinstance(event, InputAdmitted):
                await pass_fault_barrier(
                    "reader_boundary", session_id=self._sid, phase="admission", item_id=event.input_id
                )
            cards: list[dict[str, Any]] = []
            if isinstance(event, ResponseOutput) and event.frame.get("type") == "tool-output-available":
                await pass_fault_barrier(
                    "tool_card_lookup",
                    session_id=self._sid,
                    response_id=event.response_id,
                    cursor=event.cursor.as_json() if event.cursor is not None else None,
                    frame=event.frame,
                )
                # Resolve cards before the source cursor can enter a commit.
                cards = await card_parts(self._sid, event.frame)
            self._take(event)
            if not isinstance(event, ResponseOutput):
                continue
            # No await separates the result and its cards in the shared batch.
            for part in cards:
                self._take(ResponseOutput(event.cursor, event.response_id, part))
            # Returns at once unless an E2E deployment armed a hold here.
            await pass_fault_barrier(
                "response_part_read",
                session_id=self._sid,
                frame_type=str(event.frame.get("type") or ""),
                prepare_hold=self._commit,
            )

    async def _open_events(self, cursor: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        """The event stream after ``cursor`` (from the start without one), or
        from the runtime's own state when ``cursor`` cannot be resumed (a
        replaced runtime, an expired position)."""
        stream = self._client.events(after=cursor)
        try:
            first = await anext(stream)
        except RuntimeCursorExpired:
            logger.info("session %s cursor %s expired; reconciling", self._sid, cursor and cursor.generation)
            # A replaced runtime may not hold what its predecessor received;
            # handing those inputs over again is safe because submit is
            # idempotent by input id.
            if await self._repo.reclaim_unadmitted(self._sid):
                self._feed.wake(self._sid)
        except StopAsyncIteration:
            return _empty()
        else:
            return _prepend(first, stream)
        state = await self._client.state()
        await self._reconcile(state)
        return self._client.events(after=state.cursor)

    def _take(self, event: RuntimeEvent) -> None:
        batch = self._batch
        urgent = True
        if isinstance(event, ResponseOutput):
            if event.response_id and event.frame.get("type") == "text-delta" and event.response_id not in self._texted:
                # The outbound half of the platform's delay starts here.
                self._texted.add(event.response_id)
                logger.info(
                    "first text read session=%s response=%s at=%.3f", self._sid, event.response_id, time.time()
                )
            self._seq += 1
            self._feed.publish_live(self._sid, LivePart(self._seq, event.response_id, event.frame))
            batch.output.append(OutputPart(self._seq, event.response_id, event.frame))
            urgent = False
        elif isinstance(event, ResponseStarted):
            batch.facts.append(ResponseStart(event.response_id, event.origin, tuple(event.input_ids)))
        elif isinstance(event, ResponseEnded):
            batch.facts.append(
                ResponseEnd(event.response_id, event.outcome, event.native_reason, event.usage, event.error)
            )
        elif isinstance(event, InteractionOpened):
            batch.facts.append(InteractionOpen(event.response_id, event.interaction_id, event.contract))
        elif isinstance(event, InteractionClosed):
            batch.facts.append(InteractionClose(event.interaction_id, event.reason))
        elif isinstance(event, InputAdmitted):
            batch.facts.append(InputsAdmitted((event.input_id,)))
            urgent = False
        elif isinstance(event, InputRefused):
            batch.facts.append(InputsFailed((event.input_id,), event.failure))
            self._take(ResponseOutput(event.cursor, None, input_failed_part(event.input_id, event.failure)))
        elif isinstance(event, StatusChanged):
            batch.status = event.status
        elif isinstance(event, RuntimeDiagnostic):
            logger.info(
                "runtime diagnostic session=%s type=%s raw=%s",
                self._sid,
                event.event_type,
                _bounded_json(event.raw),
            )
            urgent = False
        if event.cursor is not None:
            batch.cursor = event.cursor.as_json()
        self._dirty.set()
        if urgent:
            self._urgent.set()

    async def _reconcile(self, state: RuntimeState) -> None:
        """Align the recorded facts with the runtime's own account of itself."""
        recorded_open = set(await self._repo.open_response_ids(self._sid))
        runtime_open = {response.response_id: response for response in state.open_responses}
        recorded_interactions = set(await self._repo.open_interaction_ids(self._sid))
        pending = {item.interaction_id: item for item in state.pending_interactions}
        closed = {item.interaction_id: item for item in state.closed_interactions}
        batch = self._batch
        lost_parts: list[OutputPart] = []
        for interaction_id in sorted(recorded_interactions & closed.keys()):
            batch.facts.append(InteractionClose(interaction_id, closed[interaction_id].reason))
        for response_id in sorted(recorded_open - runtime_open.keys()):
            batch.facts.append(
                ResponseEnd(response_id, "failed", "runtime_lost", error=lost_response_error("runtime_lost"))
            )
            for part in lost_response_parts(response_id, "runtime_lost"):
                self._seq += 1
                lost_parts.append(OutputPart(self._seq, response_id, part))
        for response_id, response in runtime_open.items():
            if response_id not in recorded_open:
                batch.facts.append(ResponseStart(response_id, response.origin, tuple(response.input_ids)))
        for interaction_id in sorted(recorded_interactions - pending.keys() - closed.keys()):
            batch.facts.append(InteractionClose(interaction_id, "expired"))
        for interaction_id, item in pending.items():
            if interaction_id not in recorded_interactions:
                batch.facts.append(InteractionOpen(item.response_id, interaction_id, item.contract))
        if state.admitted_input_ids:
            batch.facts.append(InputsAdmitted(tuple(state.admitted_input_ids)))
        batch.output.extend(lost_parts)
        batch.status = state.status
        batch.cursor = state.cursor.as_json() if state.cursor is not None else None
        await self._commit()
        for lost in lost_parts:
            self._feed.publish_live(self._sid, LivePart(lost.seq, lost.response_id, lost.part))

    # -- committing --------------------------------------------------------

    async def _commit_loop(self) -> None:
        renew_every = LEASE_TTL_S / 3
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=renew_every)
            except TimeoutError:
                if not await self._repo.renew_reader(self._lease, ttl_s=LEASE_TTL_S):
                    raise _LeaseLost() from None
                continue
            if not self._closing:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._urgent.wait(), timeout=COMMIT_INTERVAL_S)
            await self._commit()
            if self._closing:
                return

    async def _commit(self) -> None:
        """Commit the pending batch and run its hooks.

        Shielded: a caller cancelled while a commit is under way does not stop
        it, so a stored batch always reaches the consumers of recorded facts.
        """
        await asyncio.shield(self._commit_now())

    async def _commit_now(self) -> None:
        async with self._commit_lock:
            batch, self._batch = self._batch, FactBatch()
            self._dirty.clear()
            self._urgent.clear()
            if not batch:
                return
            if not await self._repo.record(self._lease, batch, ttl_s=LEASE_TTL_S):
                raise _LeaseLost()
            cursor = RuntimeCursor.from_json(batch.cursor)
            if cursor is not None:
                await self._client.committed(cursor)
            if batch.status is not None:
                await self._hooks.status(batch.status)
        await self._hooks.committed(batch)

    async def _publish_platform(self, response_id: str | None, parts: list[dict[str, Any]]) -> None:
        """Commit parts the platform adds, then publish them."""
        numbered = []
        for part in parts:
            self._seq += 1
            numbered.append(OutputPart(self._seq, response_id, part))
        self._batch.output.extend(numbered)
        await self._commit()
        for item in numbered:
            self._feed.publish_live(self._sid, LivePart(item.seq, item.response_id, item.part))

    # -- delivering --------------------------------------------------------

    async def _deliver_loop(self, subscription: Any) -> None:
        # Requests for an earlier reader were about what ran then.
        await self._repo.expire_controls(self._sid, before_generation=self._lease.generation)
        # The mode may have changed while no reader was connected.
        await self._hooks.permission_mode()
        # Controls accepted between connections may have no notice left to read.
        await self._apply_controls()
        await self._deliver_pending()
        await self._deliver_answers()
        while True:
            item = await subscription.get()
            if isinstance(item, Resync):
                await self._deliver_pending()
                await self._deliver_answers()
                await self._apply_controls()
            elif isinstance(item, Notice) and item.kind == "input":
                await self._deliver_pending()
            elif isinstance(item, Notice) and item.kind == "answer":
                await self._deliver_answers()
            elif isinstance(item, Notice) and item.kind == "control":
                await self._apply_controls()
            elif isinstance(item, Notice) and item.kind == "permission":
                await self._hooks.permission_mode()

    async def _apply_controls(self) -> None:
        for row in await self._repo.pending_controls(self._lease):
            control_id = str(row["control_id"])
            try:
                if row["kind"] == "interrupt":
                    await self._client.interrupt()
                elif row["kind"] == "stop_child":
                    stop = getattr(self._client, "stop_child_run", None)
                    if not callable(stop):
                        raise RuntimeError(f"the {self._client.engine_kind} runtime cannot stop a sub-agent")
                    await stop(str(row["target"]))
                else:
                    raise RuntimeError(f"unknown control {row['kind']!r}")
            except Exception as exc:  # noqa: BLE001 - the request's outcome is recorded, not raised
                logger.warning("session %s control %s failed: %s", self._sid, control_id, exc)
                await self._repo.settle_control(control_id, state="failed", error=str(exc))
            else:
                await self._repo.settle_control(control_id, state="done")

    async def _deliver_pending(self) -> None:
        for row in await self._repo.undelivered_inputs(self._sid):
            item = RuntimeInput(
                input_id=str(row["input_id"]),
                content=str(row["content"] or ""),
                content_blocks=tuple(row.get("content_blocks") or ()),
                permission_mode=row.get("permission_mode"),
            )
            try:
                await self._hooks.prepare(item)
                await self._client.submit(item)
            except InputPreparationRefused as exc:
                await self._refuse(item.input_id, exc.failure)
                continue
            except RuntimeInputRefused as exc:
                await self._refuse(
                    item.input_id,
                    {"code": exc.code, "message": str(exc), "status_code": exc.status_code},
                )
                continue
            # The inbound half of the platform's delay ends here (see the route's "input received").
            logger.info("input delivered session=%s input=%s at=%.3f", self._sid, item.input_id, time.time())
            await self._repo.mark_delivered([item.input_id])

    async def _deliver_answers(self) -> None:
        for row in await self._repo.pending_answers(self._sid):
            interaction_id = str(row["interaction_id"])
            try:
                await self._client.answer(interaction_id, dict(row["answer"]))
            except InteractionNotOpen:
                self._batch.facts.append(InteractionClose(interaction_id, "expired"))
            else:
                self._batch.facts.append(InteractionClose(interaction_id, "answered"))
            # Runtime acceptance receipts distinguish redelivery after a lost commit.
            await self._commit()

    async def _refuse(self, input_id: str, failure: dict[str, Any]) -> None:
        logger.info("session %s input %s refused: %s", self._sid, input_id, failure.get("code"))
        await self._repo.mark_failed(input_id, failure)
        await self._publish_platform(None, [input_failed_part(input_id, failure)])


async def _empty() -> AsyncIterator[RuntimeEvent]:
    return
    yield  # pragma: no cover - makes this an async generator


async def _prepend(first: RuntimeEvent, rest: AsyncIterator[RuntimeEvent]) -> AsyncIterator[RuntimeEvent]:
    yield first
    async for event in rest:
        yield event
