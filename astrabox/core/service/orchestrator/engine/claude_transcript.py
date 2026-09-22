"""Claude Code durable transcript recovery.

The CLI JSONL mirror is vendor vocabulary. Only the Claude engine adapter
parses it; the platform receives a settled, engine-neutral projection.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.assistant_text import (
    coalesce_assistant_text,
)
from astrabox.core.service.orchestrator.engine.base import EngineSettledProjection
from astrabox.core.service.orchestrator.engine.claude_message_blocks import (
    _extract_stop_reason,
    _is_assistant_event,
    _is_message_stop_event,
    _is_stream_event,
    collect_terminal_message_blocks,
)
from astrabox.core.service.orchestrator.message_blocks import (
    canonicalize_terminal_message_blocks,
)


def has_terminal_evidence(raw_items: list[Any]) -> bool:
    """Whether Claude's mirror contains a non-tool-use turn terminal."""

    pending_stop_reason: str | None = None
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        if _is_assistant_event(raw):
            reason = _extract_stop_reason(raw)
            if reason:
                pending_stop_reason = reason
            continue
        if _is_stream_event(raw):
            reason = _extract_stop_reason(raw)
            if reason:
                pending_stop_reason = reason
            if _is_message_stop_event(raw):
                if pending_stop_reason and pending_stop_reason != "tool_use":
                    return True
                pending_stop_reason = None
    return bool(pending_stop_reason and pending_stop_reason != "tool_use")


def project_settled_transcript(
    raw_items: list[Any],
    *,
    done: bool = False,
) -> EngineSettledProjection:
    """Reduce Claude's raw transcript into the platform recovery result."""

    blocks = canonicalize_terminal_message_blocks(
        collect_terminal_message_blocks(raw_items)
    )
    has_result = any(
        str(block.get("type") or "").strip() == "result" for block in blocks
    )
    text = "".join(
        str(block.get("text") or "")
        for block in blocks
        if str(block.get("type") or "").strip() == "text"
    )
    result_text = next(
        (
            str(block.get("result") or "")
            for block in blocks
            if str(block.get("type") or "").strip() == "result"
        ),
        None,
    )
    return EngineSettledProjection(
        blocks=blocks,
        assistant_text=coalesce_assistant_text(text, result_text),
        completed=has_result or (done and has_terminal_evidence(raw_items)),
        has_result=has_result,
    )


def _entry_text(entry: dict[str, Any]) -> str:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""


def slice_turn_tail_entries(
    raw_items: list[dict[str, Any]],
    *,
    prompt_text: str,
) -> list[dict[str, Any]]:
    """Return Claude's newest turn, anchored by its prompt entry."""

    wanted = str(prompt_text or "").strip()
    if not wanted:
        return []
    start: int | None = None
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type") or "") != "user":
            continue
        if entry.get("isSidechain") or isinstance(entry.get("origin"), dict):
            continue
        text = _entry_text(entry).strip()
        if text and (text == wanted or wanted in text):
            start = index
    if start is None:
        return []
    return [entry for entry in raw_items[start:] if isinstance(entry, dict)]
