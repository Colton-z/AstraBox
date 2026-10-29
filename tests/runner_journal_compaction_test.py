"""A compacted runner journal reads, from the host's position, as it did when written.

The host translates each frame within the response it belongs to and resumes
from a position counted in translated events. Replaying what a compaction
retains, from the position the host committed, must report exactly what the
first reading had left to report: nothing for a frame read whole, the same
remainder for a frame read in part. The frames are the runner's own,
serialized and journaled by ``sandbox_runner`` from the Claude SDK's types.
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


class _Replay:
    """The part of ``RunnerLink`` the Claude client reads one attach's frames through."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._frames = frames

    @property
    def is_live(self) -> bool:
        return True

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        for frame in self._frames:
            yield frame


async def _read(frames: list[dict[str, Any]], after: RuntimeCursor | None) -> list[RuntimeEvent]:
    client = ClaudeRuntimeClient(
        cast(Any, _Replay(frames)), session_id="s1", hello={"boot_id": "boot-1"}, resume_session_key=None
    )
    return [event async for event in client.events(after=after)]


async def _turn(sender: EnvelopeSender, text: str, store: int) -> tuple[list[int], int]:
    """Journal one answered input as the runner does; returns its durable sequences and its Result's."""
    started = await sender.send(
        "event",
        message_type="UserMessage",
        message=_jsonable(UserMessage(content=text, uuid=str(uuid.uuid4()))),
        requires_persistence=True,
    )
    sender.note_response_start(started)
    answer = f"An answer to {text}"
    replied = await sender.send(
        "event",
        message_type="AssistantMessage",
        message=_jsonable(AssistantMessage(
            content=[TextBlock(text=answer)], model=_MODEL, session_id=_NATIVE, message_id=f"msg-{store}",
        )),
        requires_persistence=True,
    )
    result = await sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(store),
        message_type="ResultMessage",
        message=_jsonable(ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=1,
            session_id=_NATIVE, result=answer, usage={"input_tokens": 3, "output_tokens": 5},
            uuid=str(uuid.uuid4()),
        )),
        requires_persistence=True,
    )
    return [started, replied, result], result


@pytest.mark.parametrize("read", ["whole", "in_part"])
async def test_a_compacted_journal_resumes_the_last_result_where_the_host_left_it(
    monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    # Any journal is long enough to compact here; the threshold only spaces compactions out.
    monkeypatch.setattr(EnvelopeSender, "_JOURNAL_COMPACTION_THRESHOLD", 0)

    async def no_child_agents(client: ClaudeRuntimeClient) -> None:
        # Child identities come from the stored transcript, and no child runs here.
        client._children_restored = True

    monkeypatch.setattr(ClaudeRuntimeClient, "_restore_children", no_child_agents)
    sink = _Sink()
    sender = EnvelopeSender(sink, "s1")
    durable, _ = await _turn(sender, "the first question", 1)
    second, result = await _turn(sender, "the second question", 2)
    durable += second

    first_reading = await _read(sink.frames, None)
    from_result = [event for event in first_reading if event.cursor and event.cursor.position["seq"] == result]
    assert len(from_result) > 1
    reached = from_result[-1 if read == "whole" else 0]
    position = reached.cursor
    assert position is not None
    left = first_reading[first_reading.index(reached) + 1:]

    # What the host acknowledges for that position: every frame before the one it points into.
    for sequence in durable:
        if sequence < position.position["seq"]:
            await sender.acknowledge_event_persistence(sequence)
    assert await sender.compact_after_store_flush(HistoryStoreSequence(2)) > 0

    attach = _Sink()
    sender.set_link(attach)
    await sender.replay_after(0, emit_gap=True)
    assert attach.frames[0]["op"] == "gap"
    assert attach.frames[1]["seq"] == second[0], "the retained journal starts where the last response opened"
    assert await _read(attach.frames, position) == left
