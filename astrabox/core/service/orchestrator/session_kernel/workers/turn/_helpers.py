"""Pure, stateless helpers extracted from :mod:`turn_worker`.

Block/text canonicalization, assistant-text synthesis, watermark math,
remote/engine anchor build+extract, transcript-config
dir, and sandbox-pending-matches-turn. No I/O, no ``self``.
"""
from __future__ import annotations

import inspect
from typing import Any

from astrabox.common.utils.time_utils import parse_iso, utcnow
from astrabox.core.service.orchestrator.assistant_text import coalesce_assistant_text
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    coerce_int as _coerce_int,
    normalize_current_turn_engine_anchor as _normalize_current_turn_engine_anchor,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
)
from astrabox.core.service.orchestrator.tool_result_semantics import (
    TOOL_RESULT_STATE_AVAILABLE,
)


def _session_transcript_config_dir(session: dict[str, Any] | None) -> str | None:
    if not isinstance(session, dict):
        return None
    candidates: list[Any] = [session.get("runtime_identity")]
    workspace_ref = session.get("workspace_ref")
    if isinstance(workspace_ref, dict):
        candidates.append(workspace_ref.get("runtime_identity"))
    for identity in candidates:
        if not isinstance(identity, dict):
            continue
        config_dir = str(identity.get("config_dir") or "").strip()
        if config_dir:
            return config_dir
    return None


def _elapsed_since_iso_ms(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return round((utcnow() - parse_iso(text)).total_seconds() * 1000, 3)
    except (TypeError, ValueError):
        return None


def _callable_accepts_keyword(fn: Any, keyword: str) -> bool:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    if keyword in signature.parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _append_interaction_resolution_blocks(
    raw_blocks: Any,
    *,
    tool_call_id: str,
    result_content: str,
    tool_result_state: str = TOOL_RESULT_STATE_AVAILABLE,
) -> list[dict[str, Any]]:
    blocks = [
        dict(item)
        for item in raw_blocks
        if isinstance(item, dict)
    ] if isinstance(raw_blocks, list) else []
    if not tool_call_id:
        return blocks
    for block in blocks:
        if str(block.get("type") or "").strip() != "tool_result":
            continue
        if str(block.get("tool_use_id") or "").strip() == tool_call_id:
            block["content"] = result_content
            block["tool_result_state"] = tool_result_state
            block["is_error"] = tool_result_state != TOOL_RESULT_STATE_AVAILABLE
            return blocks
    blocks.append(
        {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": result_content,
            "is_error": tool_result_state != TOOL_RESULT_STATE_AVAILABLE,
            "tool_result_state": tool_result_state,
        }
    )
    return blocks


def _collect_visible_unresolved_tool_use_ids(
    raw_blocks: Any,
    *,
    anchor_tool_call_id: str,
) -> set[str]:
    blocks = [
        dict(item)
        for item in raw_blocks
        if isinstance(item, dict)
    ] if isinstance(raw_blocks, list) else []
    if not blocks:
        return set()
    resolved_tool_use_ids = {
        str(block.get("tool_use_id") or "").strip()
        for block in blocks
        if str(block.get("type") or "").strip() == "tool_result"
        and str(block.get("tool_use_id") or "").strip()
    }
    unresolved_ids_in_order = [
        str(block.get("id") or "").strip()
        for block in blocks
        if str(block.get("type") or "").strip() == "tool_use"
        and str(block.get("id") or "").strip()
        and str(block.get("id") or "").strip() not in resolved_tool_use_ids
    ]
    if not unresolved_ids_in_order:
        return set()
    normalized_anchor = str(anchor_tool_call_id or "").strip()
    if not normalized_anchor or normalized_anchor not in unresolved_ids_in_order:
        return set(unresolved_ids_in_order)
    anchor_index = unresolved_ids_in_order.index(normalized_anchor)
    return set(unresolved_ids_in_order[anchor_index:])


def _synthesize_assistant_text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    text_parts: list[str] = []
    result_text: str | None = None
    for block in blocks:
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "text":
            text = str(block.get("text") or "")
            if text:
                text_parts.append(text)
            continue
        if block_type == "result":
            candidate = str(block.get("result") or "").strip()
            if candidate:
                result_text = candidate
    return coalesce_assistant_text("".join(text_parts), result_text)


def _wm_max(a: int | None, b: int | None) -> int | None:
    """Return the max of two optional watermarks.  None means 'no watermark'."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _parse_wm(raw: Any) -> int | None:
    """Parse a raw watermark value.  None / absent → None, otherwise int."""
    return int(raw) if raw is not None else None


def _replace_plain_text_blocks(
    blocks: list[dict[str, Any]],
    *,
    authoritative_text: str | None,
) -> list[dict[str, Any]]:
    normalized_text = str(authoritative_text or "").strip()
    if not normalized_text:
        return [dict(block) for block in blocks if isinstance(block, dict)]
    if any(
        str(block.get("type") or "").strip() in {"tool_use", "tool_result"}
        for block in blocks
        if isinstance(block, dict)
    ):
        return [dict(block) for block in blocks if isinstance(block, dict)]

    replaced: list[dict[str, Any]] = []
    text_written = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            if text_written:
                continue
            replaced.append({**dict(block), "text": normalized_text})
            text_written = True
            continue
        replaced.append(dict(block))

    if text_written:
        return replaced

    insert_index = 0
    if replaced and str(replaced[0].get("type") or "").strip() == "thinking":
        insert_index = 1
    replaced.insert(insert_index, {"type": "text", "text": normalized_text})
    return replaced


def _build_current_turn_remote_anchor(
    *,
    sandbox_turn_id: int | None,
    last_sandbox_seq: int | None,
) -> dict[str, int] | None:
    return _normalize_current_turn_remote_anchor(
        {
            "sandbox_turn_id": sandbox_turn_id,
            "last_sandbox_seq": last_sandbox_seq,
        }
    )


def _extract_current_turn_remote_anchor(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    return _build_current_turn_remote_anchor(
        sandbox_turn_id=_coerce_int(raw.get("__sandbox_turn_id")),
        last_sandbox_seq=_coerce_int(raw.get("__sandbox_seq")),
    )


def _build_current_turn_engine_anchor(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    explicit = _normalize_current_turn_engine_anchor(raw.get("engine_anchor"))
    if isinstance(explicit, dict):
        return explicit
    return _normalize_current_turn_engine_anchor(
        {
            "engine_kind": raw.get("engine_kind"),
            "engine_turn_id": raw.get("engine_turn_id"),
            "engine_session_key": raw.get("engine_session_key"),
            "engine_sequence_number": raw.get("engine_sequence_number"),
        }
    )


def _sandbox_pending_interaction_matches_turn(
    broker_pending: dict[str, Any] | None,
    sandbox_turn_id: int | None,
) -> bool:
    if not isinstance(broker_pending, dict):
        return False
    if not bool(broker_pending.get("pending")):
        return False
    pending_turn_id = broker_pending.get("turn_id")
    if sandbox_turn_id is None:
        return isinstance(pending_turn_id, int)
    return pending_turn_id == sandbox_turn_id
