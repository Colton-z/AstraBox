"""Repeated blocks with one id stay one block across live and stored views.

``bridge_journal._normalize_frame`` assigns one id per turn and subtype. The
AI SDK and the durable message projector both replace the prior block carrying
that id, so a reload preserves the live stream's identity semantics.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.message_blocks import (
    merge_projected_message_blocks,
)
from astrabox.core.service.orchestrator.session_kernel.active_turn_projection import (
    build_active_turn_message,
)


def _retry_frame(frame_seq: int, attempt: int) -> dict[str, object]:
    return {
        "frame_seq": frame_seq,
        "created_at": f"2026-08-17T00:00:{frame_seq:02d}Z",
        "payload": {
            "type": "data-raw-event",
            "id": "raw-event:turn-1:api_retry",
            "data": {
                "event_type": "claude_code.sdk",
                "subtype": "api_retry",
                "raw": {
                    "__sdk_type": "SystemMessage",
                    "subtype": "api_retry",
                    "data": {
                        "type": "system",
                        "subtype": "api_retry",
                        "attempt": attempt,
                        "max_retries": 10,
                        "error_status": 401,
                        "error": "authentication_failed",
                    },
                },
            },
        },
    }


def _blocks(message: object) -> list[dict[str, object]]:
    assert isinstance(message, dict)
    blocks = message.get("blocks")
    assert isinstance(blocks, list)
    return blocks


def test_a_retry_ladder_is_one_block_carrying_the_latest_attempt() -> None:
    message = build_active_turn_message(
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
        existing_message=None,
        default_message_seq=10,
        frames=[_retry_frame(index, attempt=index) for index in range(1, 11)],
    )
    retries = [b for b in _blocks(message) if b.get("subtype") == "api_retry"]

    assert len(retries) == 1
    payload = retries[0]["raw"]["data"]
    assert payload["attempt"] == 10
    assert payload["error_status"] == 401
    assert payload["error"] == "authentication_failed"


def test_the_block_keeps_the_live_frames_id() -> None:
    # A reader who reloads mid-ladder gets this block back as a data part. The
    # id is what lets the next live frame replace it instead of appending
    # beside it, so dropping it here would restore the divergence one turn
    # later than before.
    message = build_active_turn_message(
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
        existing_message=None,
        default_message_seq=10,
        frames=[_retry_frame(1, attempt=1)],
    )
    (retry,) = [b for b in _blocks(message) if b.get("subtype") == "api_retry"]

    assert retry["id"] == "raw-event:turn-1:api_retry"


def test_two_subtypes_keep_two_blocks() -> None:
    # Collapsing is per subtype, not per turn: an unrelated system event must
    # not be overwritten by a retry.
    other = _retry_frame(11, attempt=1)
    other["payload"]["data"]["subtype"] = "compact_boundary"  # type: ignore[index]
    other["payload"]["id"] = "raw-event:turn-1:compact_boundary"  # type: ignore[index]

    message = build_active_turn_message(
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
        existing_message=None,
        default_message_seq=10,
        frames=[_retry_frame(1, attempt=1), other, _retry_frame(2, attempt=2)],
    )
    subtypes = [b.get("subtype") for b in _blocks(message) if b.get("type") == "raw_event"]

    assert sorted(subtypes) == ["api_retry", "compact_boundary"]


def test_the_stored_merge_uses_the_same_identity() -> None:
    # The projection above is the live-turn view; a settled turn's blocks go
    # through merge_projected_message_blocks. Both must answer the same, or the
    # count changes again when the turn settles.
    def block(attempt: int) -> dict[str, object]:
        return {
            "type": "raw_event",
            "event_type": "claude_code.sdk",
            "subtype": "api_retry",
            "raw": {"data": {"attempt": attempt}},
        }

    merged = merge_projected_message_blocks([block(1)], [block(2)])

    assert len(merged) == 1
    assert merged[0]["raw"] == {"data": {"attempt": 2}}


def test_an_event_with_no_subtype_keeps_no_identity() -> None:
    # No subtype, no key: such an event has nothing to be "the same" as, and
    # silently folding two of them together would lose one.
    def block(marker: str) -> dict[str, object]:
        return {"type": "raw_event", "event_type": "claude_code.sdk", "raw": {"m": marker}}

    merged = merge_projected_message_blocks([block("a")], [block("b")])

    assert len(merged) == 2
