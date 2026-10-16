"""Engine-neutral assistant message block normalization and merging."""

from __future__ import annotations

import json
from typing import Any

from astrabox.core.service.orchestrator.tool_result_semantics import (
    normalize_tool_result_block,
)


def normalize_message_blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() == "tool_result":
            blocks.append(normalize_tool_result_block(item))
        else:
            blocks.append(dict(item))
    return blocks


def _block_signature(block: dict[str, Any]) -> str:
    try:
        return json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return json.dumps(
            block,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )


def merge_message_blocks(
    existing: Any,
    incoming: Any,
) -> list[dict[str, Any]]:
    merged = normalize_message_blocks(existing)
    new_blocks = normalize_message_blocks(incoming)
    if not new_blocks:
        return merged
    if not merged:
        return new_blocks

    merged_signatures = [_block_signature(block) for block in merged]
    new_signatures = [_block_signature(block) for block in new_blocks]

    if len(new_signatures) <= len(merged_signatures):
        max_start = len(merged_signatures) - len(new_signatures)
        for start in range(max_start + 1):
            if merged_signatures[start : start + len(new_signatures)] == new_signatures:
                return merged

    overlap = 0
    max_overlap = min(len(merged_signatures), len(new_signatures))
    for size in range(max_overlap, 0, -1):
        if merged_signatures[-size:] == new_signatures[:size]:
            overlap = size
            break

    return [*merged, *new_blocks[overlap:]]


def ui_data_block_key(block: dict[str, Any]) -> tuple[str, str] | None:
    """Return the AI SDK replacement identity for a persisted data part."""

    if str(block.get("type") or "").strip() != "ui_data":
        return None
    part = block.get("part")
    if not isinstance(part, dict):
        return None
    part_type = str(part.get("type") or "").strip()
    part_id = str(part.get("id") or "").strip()
    if part_type.startswith("data-") and part_id:
        identity = json.dumps([part_type, part_id], ensure_ascii=False, separators=(",", ":"))
        return ("ui_data", identity)
    return None


def _stable_block_key(block: dict[str, Any]) -> tuple[str, str] | None:
    block_type = str(block.get("type") or "").strip()
    if block_type == "tool_use":
        block_id = str(block.get("id") or "").strip()
        return (block_type, block_id) if block_id else None
    if block_type == "tool_result":
        tool_use_id = str(block.get("tool_use_id") or "").strip()
        return (block_type, tool_use_id) if tool_use_id else None
    if block_type == "result":
        return (block_type, "__single__")
    if block_type == "api_retry":
        return (block_type, "__single__")
    if block_type == "ui_data":
        return ui_data_block_key(block)
    if block_type == "raw_event":
        # One block per subtype per turn, because that is what the live stream
        # already is: `bridge_journal._normalize_frame` gives every raw event
        # of one subtype in one turn the same frame id, and the AI SDK replaces
        # a data part it has already seen by that id rather than appending.
        # Appending instead would store a retry ladder as ten lines while the
        # live stream shows one, so the transcript would grow the moment the
        # reader refreshed. A subtype-less event keeps no identity and appends.
        subtype = str(block.get("subtype") or "").strip()
        return (block_type, subtype) if subtype else None
    return None


def _merge_tool_use_block(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    merged.update(incoming)

    existing_name = str(existing.get("name") or "").strip()
    incoming_name = str(incoming.get("name") or "").strip()
    if not incoming_name and existing_name:
        merged["name"] = existing_name

    existing_input = (
        dict(existing.get("input"))
        if isinstance(existing.get("input"), dict)
        else {}
    )
    incoming_input = (
        dict(incoming.get("input"))
        if isinstance(incoming.get("input"), dict)
        else {}
    )
    merged["input"] = incoming_input or existing_input
    return merged


def _merge_tool_result_block(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    merged.update(incoming)

    incoming_content = str(incoming.get("content") or "")
    existing_content = str(existing.get("content") or "")
    merged["content"] = incoming_content or existing_content

    if "is_error" not in incoming and "is_error" in existing:
        merged["is_error"] = existing.get("is_error")
    return normalize_tool_result_block(merged)


def _merge_result_block(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "type":
            merged[key] = value
            continue
        if value in (None, "", {}, []):
            if key not in merged:
                merged[key] = value
            continue
        merged[key] = value
    return merged


def merge_projected_message_blocks(
    existing: Any,
    incoming: Any,
) -> list[dict[str, Any]]:
    """Merge projected assistant blocks while replacing stale tool placeholders.

    Projection code can first persist a synthetic placeholder block for a tool
    interaction and later merge the richer/native block for the same tool call.
    For those stable block identities, merging replaces/upgrades the matching
    block in place rather than appending a duplicate.
    """

    merged = normalize_message_blocks(existing)
    new_blocks = normalize_message_blocks(incoming)
    if not new_blocks:
        return merged
    if not merged:
        return new_blocks

    if len(new_blocks) == 1:
        block = new_blocks[0]
        stable_key = _stable_block_key(block)
        if stable_key is not None:
            match_index = next(
                (
                    index
                    for index, existing_block in enumerate(merged)
                    if _stable_block_key(existing_block) == stable_key
                ),
                None,
            )
            if match_index is not None:
                existing_block = merged[match_index]
                block_type = stable_key[0]
                if block_type == "tool_use":
                    merged[match_index] = _merge_tool_use_block(existing_block, block)
                elif block_type == "tool_result":
                    merged[match_index] = _merge_tool_result_block(existing_block, block)
                else:
                    merged[match_index] = _merge_result_block(existing_block, block)
                return merged

    existing_by_key = {
        stable_key: block
        for block in merged
        if (stable_key := _stable_block_key(block)) is not None
    }
    resolved_incoming: list[dict[str, Any]] = []
    incoming_keys: set[tuple[str, str]] = set()
    for block in new_blocks:
        stable_key = _stable_block_key(block)
        if stable_key is None:
            resolved_incoming.append(block)
            continue

        incoming_keys.add(stable_key)
        existing_block = existing_by_key.get(stable_key)
        if existing_block is None:
            resolved_incoming.append(block)
            continue

        block_type = stable_key[0]
        if block_type == "tool_use":
            resolved_incoming.append(_merge_tool_use_block(existing_block, block))
        elif block_type == "tool_result":
            resolved_incoming.append(_merge_tool_result_block(existing_block, block))
        else:
            resolved_incoming.append(_merge_result_block(existing_block, block))

    filtered_existing = [
        block
        for block in merged
        if (stable_key := _stable_block_key(block)) is None or stable_key not in incoming_keys
    ]
    return merge_message_blocks(filtered_existing, resolved_incoming)


#: Streamed content the settle payload re-renders in its own order.
_SETTLED_STREAM_TYPES = frozenset({"text", "thinking"})


def deduplicate_message_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate tool_use, tool_result, and result blocks."""
    seen_tool_uses: set[str] = set()
    seen_tool_results: set[str] = set()
    result_seen = False
    deduped: list[dict[str, Any]] = []
    for block in blocks:
        block_type = str(block.get("type") or "").strip()
        if block_type == "result":
            if result_seen:
                continue
            result_seen = True
        elif block_type == "tool_use":
            block_id = str(block.get("id") or "").strip()
            if block_id and block_id in seen_tool_uses:
                continue
            if block_id:
                seen_tool_uses.add(block_id)
        elif block_type == "tool_result":
            tool_use_id = str(block.get("tool_use_id") or "").strip()
            if tool_use_id and tool_use_id in seen_tool_results:
                continue
            if tool_use_id:
                seen_tool_results.add(tool_use_id)
        deduped.append(block)
    return deduped
