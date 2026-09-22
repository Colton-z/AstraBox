from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.claude_code_client import (
    require_result_history_checkpoint,
)
from astrabox.core.service.orchestrator.engine.runner_link import RunnerLinkError
from astrabox.core.service.orchestrator.sandbox_runner import (
    EnvelopeSender,
    HistoryLiveSequence,
    HistoryStoreSequence,
    ResultHistoryCheckpoint,
    RunnerProtocolError,
)


class _Link:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(dict(frame))
        return True


async def test_result_publishes_one_atomic_dual_coordinate_checkpoint() -> None:
    link = _Link()
    sender = EnvelopeSender(link, "session-checkpoint")
    await sender.replay_after(0)
    await sender.send("status", state="busy")
    result_sequence = await sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(917),
        message_type="ResultMessage",
        message={"__sdk_type": "ResultMessage"},
    )

    status, result = link.frames
    assert "store_sequence" not in status
    assert "history_live_sequence" not in status
    assert result["store_sequence"] == 917
    assert result["history_live_sequence"] == result_sequence == result["seq"]

    checkpoint = require_result_history_checkpoint(result)
    assert checkpoint.store.value == 917
    assert checkpoint.live.value == result_sequence
    with pytest.raises(TypeError):
        _ = checkpoint.store < checkpoint.live  # type: ignore[operator]


def test_coordinate_types_make_store_live_swaps_invalid() -> None:
    with pytest.raises(TypeError, match="checkpoint.store"):
        ResultHistoryCheckpoint(  # type: ignore[arg-type]
            store=HistoryLiveSequence(4),
            live=HistoryStoreSequence(4),
        )

    with pytest.raises(RunnerLinkError, match="atomic"):
        require_result_history_checkpoint(
            {
                "op": "event",
                "seq": 12,
                "message_type": "ResultMessage",
                "store_sequence": 12,
                "history_live_sequence": 900,
            }
        )


async def test_non_result_frames_cannot_publish_a_checkpoint() -> None:
    sender = EnvelopeSender(_Link(), "session-checkpoint")
    assert sender.history_checkpoint is None
    before = sender.last_seq
    with pytest.raises(RunnerProtocolError, match="exactly ResultMessage"):
        await sender.send(
            "status",
            result_store_sequence=HistoryStoreSequence(1),
            state="idle",
        )
    assert sender.last_seq == before
    assert sender.history_checkpoint is None
