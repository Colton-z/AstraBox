from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.sandbox_runner import (
    EnvelopeSender,
    HistoryStoreSequence,
)


class _Link:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return self.connected

    async def send(self, frame: dict[str, Any]) -> bool:
        if not self.connected:
            return False
        self.frames.append(dict(frame))
        return True


async def _event(sender: EnvelopeSender, event_type: str, index: int) -> int:
    return await sender.send(
        "event",
        message_type="StreamEvent",
        message={
            "__sdk_type": "StreamEvent",
            "event": {"type": event_type, "index": index},
        },
    )


async def test_detached_journal_never_discards_a_partial_content_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(EnvelopeSender, "_JOURNAL_COMPACTION_THRESHOLD", 3)
    original = _Link()
    sender = EnvelopeSender(original, "session-journal")
    await sender.replay_after(0)
    original.connected = False

    await _event(sender, "content_block_start", 0)
    for _ in range(8):
        await _event(sender, "content_block_delta", 0)
    await _event(sender, "content_block_stop", 0)

    assert sender.journal_count == 10, (
        "the size trigger cannot evict an arbitrary suffix before a Result "
        "checkpoint covers a complete prefix"
    )
    replacement = _Link()
    sender.set_link(replacement)
    replay = await sender.replay_after(0)
    assert replay.gap is False
    assert [frame["seq"] for frame in replacement.frames] == list(range(1, 11))
    assert replacement.frames[0]["message"]["event"]["type"] == "content_block_start"
    assert replacement.frames[-1]["message"]["event"]["type"] == "content_block_stop"


async def test_compaction_requires_a_result_and_a_fully_flushed_store_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(EnvelopeSender, "_JOURNAL_COMPACTION_THRESHOLD", 3)
    link = _Link()
    sender = EnvelopeSender(link, "session-journal")
    await sender.replay_after(0)
    for index in range(4):
        await _event(sender, "content_block_delta", index)
    result_sequence = await sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(23),
        message_type="ResultMessage",
        message={"__sdk_type": "ResultMessage"},
    )

    assert await sender.compact_after_result(
        result_sequence, store_fully_flushed=False
    ) == 0
    assert sender.first_retained_seq == 1
    assert await sender.compact_after_result(
        result_sequence, store_fully_flushed=True
    ) == 4
    assert sender.first_retained_seq == result_sequence
    assert sender.journal_count == 1, "the Result boundary itself remains replayable"
    expired = sender.cursor_window(0)
    assert expired.gap is True
    assert expired.first_retained_sequence == result_sequence

    replacement = _Link()
    sender.set_link(replacement)
    replay = await sender.replay_after(0, emit_gap=True)
    assert replay.gap is True
    assert replay.replayed == 1
    assert replacement.frames[0] == {
        "op": "gap",
        "after_sequence": 0,
        "first_retained_sequence": result_sequence,
        "last_sequence": result_sequence,
    }
    assert replacement.frames[1]["seq"] == result_sequence
