"""Two engine messages in one turn stay two blocks.

The projector joined a `text-delta` onto the previous block whenever that
block was text, which is right while one message streams and wrong the moment
an engine sends a second one. Codex answers a turn with more than one
`agentMessage` — the app-server spec calls each one an item with its own id —
and a turn that answered `PING` twice reached the transcript as `PINGPING`,
with no seam to show it had been two.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.session_kernel.active_turn_projection import (
    _project_blocks_from_frames,
)


def _frames(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"payload": payload, "created_at": "2026-08-18T00:00:00Z"} for payload in payloads]


def test_a_second_message_in_one_turn_is_a_second_block() -> None:
    blocks, _ = _project_blocks_from_frames(
        _frames(
            {"type": "text-start", "id": "a"},
            {"type": "text-delta", "id": "a", "delta": "PING"},
            {"type": "text-end", "id": "a"},
            {"type": "text-start", "id": "b"},
            {"type": "text-delta", "id": "b", "delta": "PING"},
            {"type": "text-end", "id": "b"},
        )
    )
    assert [b["text"] for b in blocks if b["type"] == "text"] == ["PING", "PING"]


def test_one_message_streamed_in_pieces_stays_one_block() -> None:
    """The joining this keeps: deltas of the SAME block still concatenate."""

    blocks, _ = _project_blocks_from_frames(
        _frames(
            {"type": "text-start", "id": "a"},
            {"type": "text-delta", "id": "a", "delta": "PI"},
            {"type": "text-delta", "id": "a", "delta": "NG"},
            {"type": "text-end", "id": "a"},
        )
    )
    assert [b["text"] for b in blocks if b["type"] == "text"] == ["PING"]


def test_an_engine_that_sends_no_start_still_joins_its_deltas() -> None:
    """Compatibility: a stream with no block boundaries behaves as before."""

    blocks, _ = _project_blocks_from_frames(
        _frames(
            {"type": "text-delta", "delta": "PI"},
            {"type": "text-delta", "delta": "NG"},
        )
    )
    assert [b["text"] for b in blocks if b["type"] == "text"] == ["PING"]


def test_reasoning_and_text_keep_their_own_boundaries() -> None:
    blocks, _ = _project_blocks_from_frames(
        _frames(
            {"type": "reasoning-start", "id": "r1"},
            {"type": "reasoning-delta", "id": "r1", "delta": "thinking"},
            {"type": "text-start", "id": "t1"},
            {"type": "text-delta", "id": "t1", "delta": "answer"},
            {"type": "reasoning-start", "id": "r2"},
            {"type": "reasoning-delta", "id": "r2", "delta": "more"},
        )
    )
    assert [(b["type"], b.get("text") or b.get("thinking")) for b in blocks] == [
        ("thinking", "thinking"),
        ("text", "answer"),
        ("thinking", "more"),
    ]
