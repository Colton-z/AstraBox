"""Adapt historical DSH recordings without changing their recorded content."""

from typing import Any


def current_settlement_frames(
    frames: list[dict[str, Any]],
    *,
    session_id: str,
    until_turn_end: int | None = 1,
) -> list[dict[str, Any]]:
    """Retain recorded chunks inside their explicitly named settlement.

    dsh-llm 0.1.5-rc.2, lib/types/assistant-stream.d.ts, declares the raw
    {type: 'chunk', time, chunk} compact record. No chunk content, timestamp
    or order changes here. The old recording's sourceEventSeqs identifies
    every member; this is test input adaptation, not a current wire capture
    or a production migration implementation.
    """
    pending: dict[int, dict[str, Any]] = {}
    current = []
    turns_ended = 0
    for frame in frames:
        payload = frame["payload"]
        if payload.get("sessionId") != session_id:
            continue
        event = payload.get("event")
        if not isinstance(event, dict):
            current.append(frame)
            continue
        if event["type"] == "assistant/chunk":
            assert event["seq"] not in pending
            pending[event["seq"]] = event
            continue
        if event["type"] == "assistant/message":
            sources = event["sourceEventSeqs"]
            assert sources and sources == list(pending)
            chunks = [pending.pop(seq) for seq in sources]
            assert all(
                (chunk["data"]["turn"], chunk["data"]["step"])
                == (event["data"]["turn"], event["data"]["step"])
                for chunk in chunks
            )
            event = {
                key: value for key, value in event.items()
                if key != "sourceEventSeqs"
            }
            event["data"] = {
                **event["data"],
                "stream": [
                    {"type": "chunk", "time": chunk["time"],
                     "chunk": chunk["data"]["chunk"]}
                    for chunk in chunks
                ],
            }
            frame = {**frame, "payload": {**payload, "event": event}}
        current.append(frame)
        if event["type"] == "turn/end":
            assert not pending, "recorded chunks have no settlement"
            turns_ended += 1
            if until_turn_end is not None and turns_ended >= until_turn_end:
                return current
    assert not pending, "recorded chunks have no settlement"
    assert turns_ended, "recording has no root turn/end"
    assert until_turn_end is None, "recording has fewer turns than requested"
    return current
