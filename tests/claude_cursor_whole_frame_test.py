"""A frame the platform has read whole is never read again.

The host's cursor counts the events it translated from one runner frame. A
later replay can translate the same frame differently: once a compaction has
dropped the frame that opened its response, a Result reads as a standalone
message with more events than it had. A cursor that named the last event by
its count would let that replay report the surplus as new output. It names
the whole frame instead, and the runner is told the frame is durable, so it
keeps nothing back for a replay. The frames are the runner's own, serialized
and journaled by ``sandbox_runner`` from the Claude SDK's types.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, UserMessage

from astrabox.core.service.orchestrator.engine.claude_runtime_client import ClaudeRuntimeClient
from astrabox.core.service.orchestrator.engine.runtime import RuntimeCursor, RuntimeEvent
from astrabox.core.service.orchestrator.sandbox_runner import (
    EnvelopeSender,
    HistoryStoreSequence,
    _jsonable,
)

_NATIVE = "native-session-1"
_MODEL = "claude-sonnet-4-5"


class _Sink:
    """A connected host link that keeps every frame it is sent."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True


class _Link:
    """The part of ``RunnerLink`` the Claude client reads and acknowledges one attach through."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = frames
        self.acknowledged: list[int] = []

    @property
    def is_live(self) -> bool:
        return True

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        for frame in self._frames:
            yield frame

    async def acknowledge_persisted(self, up_to_seq: int) -> None:
        self.acknowledged.append(up_to_seq)


def _client(link: _Link) -> ClaudeRuntimeClient:
    return ClaudeRuntimeClient(
        cast(Any, link), session_id="s1", hello={"boot_id": "boot-1"}, resume_session_key=None
    )


async def _read(link: _Link, after: RuntimeCursor | None) -> list[RuntimeEvent]:
    return [event async for event in _client(link).events(after=after)]


async def test_a_result_read_whole_is_passed_over_when_a_replay_reads_it_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_child_agents(client: ClaudeRuntimeClient) -> None:
        # Child identities come from the stored transcript, and no child runs here.
        client._children_restored = True

    monkeypatch.setattr(ClaudeRuntimeClient, "_restore_children", no_child_agents)
    sink = _Sink()
    sender = EnvelopeSender(sink, "s1")
    answer = "The answer"
    await sender.send(
        "event",
        message_type="UserMessage",
        message=_jsonable(UserMessage(content="A question", uuid=str(uuid.uuid4()))),
        requires_persistence=True,
    )
    await sender.send(
        "event",
        message_type="AssistantMessage",
        message=_jsonable(AssistantMessage(
            content=[TextBlock(text=answer)], model=_MODEL, session_id=_NATIVE, message_id="msg-1",
        )),
        requires_persistence=True,
    )
    result = await sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(1),
        message_type="ResultMessage",
        message=_jsonable(ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=1,
            session_id=_NATIVE, result=answer, usage={"input_tokens": 3, "output_tokens": 5},
            uuid=str(uuid.uuid4()),
        )),
        requires_persistence=True,
    )
    reading = _Link(sink.frames)
    events = await _read(reading, None)
    read_whole = events[-1].cursor
    assert read_whole is not None and read_whole.position["seq"] == result

    await _client(reading).committed(read_whole)
    assert reading.acknowledged == [result], "the runner learns the Result itself is durable"

    # The journal a compaction left when it kept the Result without the frame
    # that opened its response.
    alone = [
        {"op": "gap", "after_sequence": 0, "first_retained_sequence": result, "last_sequence": result},
        sink.frames[-1],
    ]
    before_result = RuntimeCursor("boot-1", {"seq": result - 1, "index": read_whole.position["index"]})
    from_result = [event for event in events if event.cursor and event.cursor.position["seq"] == result]
    assert len(await _read(_Link(alone), before_result)) > len(from_result), "read alone, it reports more"
    assert await _read(_Link(alone), read_whole) == []
