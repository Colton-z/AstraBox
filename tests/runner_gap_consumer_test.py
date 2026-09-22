from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.engine.base import (
    EngineStreamDetached,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.engine.claude_code_client import (
    ClaudeCodeEngineClient,
    session_store_reload_frame,
)
from astrabox.core.service.orchestrator.engine.runner_link import RunnerLinkError


class _GapLink:
    is_live = True

    def __init__(self) -> None:
        self._frames = self.frames()

    async def next_frame(self) -> dict[str, Any] | None:
        return await anext(self._frames, None)

    async def frames(self):  # noqa: ANN201 - runner async iterator
        yield {
            "op": "gap",
            "after_sequence": 2,
            "first_retained_sequence": 9,
            "last_sequence": 14,
        }
        yield {"op": "event", "seq": 9, "message_type": "StreamEvent"}


async def test_expired_cursor_emits_one_rebuild_instruction_then_ends() -> None:
    link = _GapLink()
    client = ClaudeCodeEngineClient(
        link,  # type: ignore[arg-type]
        session_id="session-gap",
        transcript_store=AsyncMock(),
        workspace_dir="/workspace",
    )
    receipt = EngineTurnReceipt(
        engine_turn_id="turn-gap",
        engine_session_key="",
        started_at_monotonic_ns=time.monotonic_ns(),
    )
    stream = client.iter_turn_events(receipt)

    assert await anext(stream) == {
        "type": "data-session-store-reload",
        "transient": True,
        "data": {"resumeSequence": 8},
    }
    with pytest.raises(EngineStreamDetached, match="SessionStore rebuild"):
        await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


def test_gap_resume_point_is_the_cursor_before_the_first_retained_event() -> None:
    frame = session_store_reload_frame(
        {
            "after_sequence": 0,
            "first_retained_sequence": 2,
            "last_sequence": 5,
        }
    )
    assert frame["data"]["resumeSequence"] == 1

    with pytest.raises(RunnerLinkError, match="malformed"):
        session_store_reload_frame(
            {
                "after_sequence": 0,
                "first_retained_sequence": 7,
                "last_sequence": 6,
            }
        )

    with pytest.raises(RunnerLinkError, match="malformed"):
        session_store_reload_frame(
            {
                "after_sequence": 4,
                "first_retained_sequence": 5,
                "last_sequence": 8,
            }
        )
