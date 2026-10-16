"""Record a Claude conversation the way a deployment records it, for read-model tests.

The conversation is written with the Claude Agent SDK's own message types. The
in-box runner's serializer (``sandbox_runner._jsonable``) and envelope sender
(``sandbox_runner.EnvelopeSender``) turn them into the runner's journal frames;
the Claude runtime client (``engine/claude_runtime_client.py``) translates the
journal into runtime events; and a session reader records those events. A read
model under test therefore reads exactly what the reader would have stored for
the same SDK messages on a real box.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, cast

from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.engine.claude_runtime_client import ClaudeRuntimeClient
from astrabox.core.service.orchestrator.engine.runtime import (
    RuntimeCursor,
    RuntimeEvent,
    RuntimeInput,
    RuntimeState,
)
from astrabox.core.service.orchestrator.sandbox_runner import (
    EnvelopeSender,
    HistoryStoreSequence,
    _jsonable,
)
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.reader import ReaderHooks, SessionReader
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import FactBatch, SessionRuntimeRepository
from tests._agent_ledger import create_agent

FactConsumer = Callable[[str, FactBatch], Awaitable[None]]


class _Sink:
    """The runner's host link: keeps every frame the envelope sender writes."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True


class RunnerJournal:
    """The runner's journal of one scripted Claude conversation."""

    def __init__(self, session_id: str) -> None:
        self._sink = _Sink()
        self._sender = EnvelopeSender(self._sink, session_id)

    async def write(self, *messages: Any, engine_boundary: bool = False) -> None:
        """Journal SDK messages; ``engine_boundary`` marks the first as Claude answering on its own."""
        for index, message in enumerate(messages):
            message_type = type(message).__name__
            extra: dict[str, Any] = {}
            if engine_boundary and index == 0:
                extra = {"engine_boundary": True, "requires_persistence": True}
            await self._sender.send(
                "event",
                result_store_sequence=HistoryStoreSequence(0) if message_type == "ResultMessage" else None,
                message_type=message_type,
                message=_jsonable(message),
                **extra,
            )

    async def gate(self, interaction_id: str, *, tool_use_id: str, tool_name: str, tool_input: Any) -> None:
        """Journal a gate the way the runner's interaction broker asks the host (``InteractionBroker._ask``)."""
        await self._sender.send(
            "interaction",
            interaction_id=interaction_id,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    async def write_recorded(self, *payloads: dict[str, Any]) -> None:
        """Journal SDK messages the runner already serialized, as a recording kept them."""
        for payload in payloads:
            await self._sender.send("event", message_type=str(payload["__sdk_type"]), message=payload)

    @property
    def frames(self) -> list[dict[str, Any]]:
        return list(self._sink.frames)


class _JournalLink:
    """The part of ``RunnerLink`` the Claude client reads events through."""

    def __init__(self, frames: Sequence[dict[str, Any]]) -> None:
        self._frames = list(frames)

    @property
    def is_live(self) -> bool:
        return True

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        for frame in self._frames:
            yield frame

    async def close(self) -> None:
        return None


class _Replay:
    """A runtime that serves the events the Claude client translated, then stays connected until closed."""

    engine_kind = "claude_code"

    def __init__(self, events: list[RuntimeEvent]) -> None:
        self._events = events
        self._closed = asyncio.Event()

    @property
    def is_live(self) -> bool:
        return True

    @property
    def engine_session_key(self) -> str | None:
        return None

    async def submit(self, item: RuntimeInput) -> None:
        return None

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        start = 0
        if after is not None:
            start = next(i for i, event in enumerate(self._events) if event.cursor == after) + 1
        for event in self._events[start:]:
            yield event
        await self._closed.wait()

    async def state(self) -> RuntimeState:
        raise AssertionError("a replayed runtime is read from its cursor")

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        return None

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        return None

    async def committed(self, cursor: RuntimeCursor) -> None:
        return None

    async def capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(engine_kind="claude_code")

    async def close(self) -> None:
        self._closed.set()


async def record(session_id: str, journal: RunnerJournal, *, consumers: Sequence[FactConsumer] = ()) -> None:
    """Translate the journal with the Claude client and record it with a session reader."""
    client = ClaudeRuntimeClient(
        cast(Any, _JournalLink(journal.frames)),
        session_id=session_id,
        hello={"boot_id": "boot-1"},
        resume_session_key=None,
    )
    events = [event async for event in client.events(after=None)]
    repo = SessionRuntimeRepository()
    lease = await repo.claim_reader(session_id, owner="test-reader", ttl_s=30)
    assert lease is not None
    stored_cursor = (await repo.resume_point(session_id))[1]
    final = events[-1].cursor.as_json() if events and events[-1].cursor else None
    caught_up = asyncio.Event()
    if final is None or final == stored_cursor:
        caught_up.set()

    async def nothing(*_args: Any) -> None:
        return None

    async def committed(batch: FactBatch) -> None:
        for consumer in consumers:
            await consumer(session_id, batch)
        if batch.cursor == final:
            caught_up.set()

    replay = _Replay(events)
    reader = SessionReader(
        lease=lease,
        client=cast(Any, replay),
        repo=repo,
        feed=SessionFeed(),
        hooks=ReaderHooks(prepare=nothing, committed=committed, status=nothing, permission_mode=nothing),
    )
    running = asyncio.create_task(reader.run())
    # A live runtime stays connected after its last event; the stream ends
    # only once the consumers have seen everything, as on a deployment.
    await asyncio.wait_for(caught_up.wait(), 10)
    await replay.close()
    assert await asyncio.wait_for(running, 10) == "stream_ended"
    await repo.release_reader(lease)


async def create_session(session_id: str = "s1", *, user_id: str = "u1") -> str:
    """A READY Claude conversation of one Agent."""
    await create_agent(f"agent-{session_id}")
    await SessionRepository().create_session(
        {
            "session_id": session_id,
            "user_id": user_id,
            "session_kind": "agent_chat",
            "engine_kind": "claude_code",
            "agent_id": f"agent-{session_id}",
            "state": "READY",
        }
    )
    return session_id


async def accept(session_id: str, text: str, *, user_id: str = "u1") -> str:
    """Accept one input the way the send path does; its id is the uuid Claude journals on taking it."""
    input_id = str(uuid.uuid4())
    accepted = await SessionRuntimeRepository().accept_input(
        session_id=session_id,
        input_id=input_id,
        client_message_id=input_id,
        content=text,
        content_blocks=None,
        owner_user_id=user_id,
        author_user_id=user_id,
        source="console",
    )
    assert accepted is not None
    return input_id
