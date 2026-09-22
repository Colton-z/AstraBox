from __future__ import annotations

from typing import Any


def is_full_assistant_snapshot(raw: Any, chunk_type: Any = None) -> bool:
    if not isinstance(raw, dict):
        return str(chunk_type or "").strip().lower() in {"assistant", "assistantmessage"}

    chunk_type_lower = str(chunk_type or "").strip().lower()
    raw_type = str(raw.get("type") or "").strip().lower()
    if raw_type == "assistant" or chunk_type_lower in {"assistant", "assistantmessage"}:
        return True

    # Live AssistantMessage payloads are serialized as {"model": ..., "content": [...]}
    # without an explicit type. They are full-text snapshots, not incremental deltas.
    content = raw.get("content")
    if (
        isinstance(content, list)
        and "event" not in raw
        and "chunk" not in raw
        and "text" not in raw
        and "result" not in raw
    ):
        return True

    return False


def collect_incremental_chunk_text(chunks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        if not text:
            continue
        if is_full_assistant_snapshot(chunk.get("raw"), chunk.get("type")):
            continue
        parts.append(text)
    return "".join(parts)


def coalesce_assistant_text(streamed_text: str | None, result_text: str | None) -> str:
    """Choose the most complete assistant text between streamed chunks and result payload."""
    streamed = streamed_text or ""
    result = (result_text or "").strip()

    if streamed and result:
        if streamed == result:
            return result
        if result in streamed:
            return streamed
        if streamed in result:
            return result
        return streamed if len(streamed) >= len(result) else result

    return streamed or result
