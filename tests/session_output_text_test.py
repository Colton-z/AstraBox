"""Plain-text destinations retain the boundaries between streamed text parts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_output import (
    SessionOutputSubscriptionMixin,
)


class Output(SessionOutputSubscriptionMixin):
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = frames

    async def follow_session_output(
        self, session_id: str, *, after_seq: int = -1, available_only: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        for frame in self.frames:
            yield frame


@pytest.mark.parametrize("chunks", [("Next paragraph.",), ("Next ", "paragraph."), ("", "Next", " paragraph.")])
@pytest.mark.parametrize("empty_part", [False, True])
async def test_text_parts_keep_paragraph_boundaries_without_separating_chunks(chunks, empty_part):
    frames = [
        {"type": "start", "messageId": "reply"},
        {"type": "text-start", "id": "first"},
        {"type": "text-delta", "id": "first", "delta": "```\nreceipt"},
        {"type": "text-delta", "id": "first", "delta": "\n```"},
        {"type": "text-end", "id": "first"},
        {"type": "reasoning-start", "id": "private"},
        {"type": "reasoning-delta", "id": "private", "delta": "not public prose"},
        {"type": "reasoning-end", "id": "private"},
    ]
    if empty_part:
        frames += [
            {"type": "text-start", "id": "empty"},
            {"type": "text-end", "id": "empty"},
        ]
    frames += [
        {"type": "text-start", "id": "second"},
        *({"type": "text-delta", "id": "second", "delta": chunk} for chunk in chunks),
        {"type": "text-end", "id": "second"},
    ]
    partial = await Output(frames)._read_available_response("session", after_seq=0)
    assert partial.after_seq == 0
    assert [(reply.text, reply.complete) for reply in partial.responses] == [
        ("```\nreceipt\n```\n\nNext paragraph.", False),
    ]
    frames += [
        {"type": "finish", "finishReason": "stop"},
        {"type": "start", "messageId": "next-reply"},
        {"type": "text-start", "id": "third"},
        {"type": "text-delta", "id": "third", "delta": "Independent reply."},
        {"type": "text-end", "id": "third"},
        {"type": "finish", "finishReason": "stop"},
    ]
    resumed = await Output(frames)._read_available_response("session", after_seq=partial.after_seq)
    assert [(reply.response_id, reply.text, reply.complete) for reply in resumed.responses] == [
        ("reply", "```\nreceipt\n```\n\nNext paragraph.", True),
        ("next-reply", "Independent reply.", True),
    ]
