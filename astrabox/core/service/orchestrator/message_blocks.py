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


def upgrade_tool_identity_placeholders(
    value: Any,
    *,
    placeholder_id: str | None,
    resolved_id: str | None,
    tool_name: str | None = None,
) -> list[dict[str, Any]]:
    """Upgrade placeholder tool ids to a resolved tool_call_id.

    Some pending interactions are first projected with ``interaction_id`` as a
    synthetic tool block id and later upgraded when the raw Claude event
    provides the real ``tool_call_id``.  This helper rewrites the existing
    placeholder block/result ids so downstream readers continue to see a single
    logical tool invocation.
    """

    blocks = normalize_message_blocks(value)
    old_id = str(placeholder_id or "").strip()
    new_id = str(resolved_id or "").strip()
    normalized_tool_name = str(tool_name or "").strip()
    if not old_id or not new_id or old_id == new_id or not blocks:
        return blocks

    upgraded: list[dict[str, Any]] = []
    for block in blocks:
        current = dict(block)
        block_type = str(current.get("type") or "").strip()
        if block_type == "tool_use" and str(current.get("id") or "").strip() == old_id:
            if normalized_tool_name:
                existing_name = str(current.get("name") or "").strip()
                if existing_name and existing_name != normalized_tool_name:
                    upgraded.append(current)
                    continue
            current["id"] = new_id
        elif (
            block_type == "tool_result"
            and str(current.get("tool_use_id") or "").strip() == old_id
        ):
            current["tool_use_id"] = new_id
        upgraded.append(current)
    return deduplicate_message_blocks(upgraded)


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


def _settled_counterpart_index(
    settled_blocks: list[dict[str, Any]],
    live_block: dict[str, Any],
) -> int | None:
    """Where in the remaining settled rendering this live block is re-rendered."""

    stable_key = _stable_block_key(live_block)
    if stable_key is not None:
        return next(
            (
                index
                for index, block in enumerate(settled_blocks)
                if _stable_block_key(block) == stable_key
            ),
            None,
        )
    if str(live_block.get("type") or "").strip() not in _SETTLED_STREAM_TYPES:
        return None
    signature = _block_signature(live_block)
    return next(
        (
            index
            for index, block in enumerate(settled_blocks)
            if _block_signature(block) == signature
        ),
        None,
    )


_STREAM_TEXT_FIELD = {"text": "text", "thinking": "thinking"}


def _live_run_rendered_by(
    settled_block: dict[str, Any],
    live_blocks: list[dict[str, Any]],
    consumed: set[int],
) -> list[int]:
    """Live blocks whose concatenation is exactly this settled block's text.

    A settled payload re-renders a whole turn without the engine's message
    boundaries, so two thoughts that the stream kept apart arrive as one block
    holding both. Equality matching finds no counterpart for that combined
    block and would render the same text again.
    """

    block_type = str(settled_block.get("type") or "").strip()
    field = _STREAM_TEXT_FIELD.get(block_type)
    if field is None:
        return []
    target = str(settled_block.get(field) or "")
    if not target:
        return []
    run: list[int] = []
    joined = ""
    for index, live_block in enumerate(live_blocks):
        if index in consumed:
            continue
        if str(live_block.get("type") or "").strip() != block_type:
            # The blocks this settled one concatenates are not adjacent — the
            # engine put a tool call between two thoughts, and the settled
            # rendering dropped that boundary along with the message one.
            continue
        candidate = joined + str(live_block.get(field) or "")
        if not target.startswith(candidate):
            break
        run.append(index)
        joined = candidate
        if joined == target:
            return run
    return []


def _overlay_settled_block(
    live_block: dict[str, Any],
    settled_block: dict[str, Any],
) -> dict[str, Any]:
    stable_key = _stable_block_key(settled_block)
    if stable_key is None:
        return settled_block
    block_type = stable_key[0]
    if block_type == "tool_use":
        return _merge_tool_use_block(live_block, settled_block)
    if block_type == "tool_result":
        return _merge_tool_result_block(live_block, settled_block)
    return _merge_result_block(live_block, settled_block)


def merge_settled_message_blocks(live: Any, settled: Any) -> list[dict[str, Any]]:
    """Overlay a settled rendering onto the live projection, in the live order.

    The frames are the turn's order. A settled payload is the same turn
    re-rendered, and it may combine blocks that the engine streamed separately.
    Reconciling the two as independent orderings — subtract the duplicates,
    then concatenate the leftovers — hoists every omitted block to the head of
    the message, so an assistant that thought between six tool calls reads as
    one that thought six times before doing anything.

    So there is one spine, and it is the live projection: each live block is
    replaced in place by its settled counterpart, and settled blocks with no
    live counterpart enter at the point the settled rendering puts them. Keyed
    blocks still merge by identity; live-only blocks (raw engine events,
    subagent windows, an interaction's tool call answered outside the engine
    stream) keep their position rather than being dropped.

    The durable payload stays authoritative for content because frames can age
    out while the settled rendering remains.
    """

    settled_blocks = normalize_message_blocks(settled)
    live_blocks = normalize_message_blocks(live)
    if not settled_blocks:
        return live_blocks
    if not live_blocks:
        return settled_blocks

    # Pair first, emit second. A settled block whose live counterpart comes
    # later must not be flushed ahead of an earlier live block that happens to
    # reach its own counterpart first: the settled rendering carries fewer
    # blocks than the stream, so "the next settled block" is not "the block
    # that belongs here". Flushing a later settled thought early would render
    # it twice and move one copy before the tool call it followed.
    live_to_settled: dict[int, int] = {}
    settled_to_live: dict[int, int] = {}
    consumed_live: set[int] = set()
    for settled_index, settled_block in enumerate(settled_blocks):
        run = _live_run_rendered_by(settled_block, live_blocks, consumed_live)
        if run:
            # The settled rendering states as one block what the engine streamed
            # as several: it drops the message boundaries between them, and its
            # text is their concatenation. Keep the live run — same characters,
            # and each thought still sits where the engine put it.
            consumed_live.update(run)
            settled_to_live[settled_index] = run[0]
            continue
        for live_index, live_block in enumerate(live_blocks):
            if live_index in live_to_settled or live_index in consumed_live:
                continue
            if _settled_counterpart_index([settled_block], live_block) is None:
                continue
            live_to_settled[live_index] = settled_index
            settled_to_live[settled_index] = live_index
            consumed_live.add(live_index)
            break

    ordered: list[dict[str, Any]] = []
    emitted_settled = 0
    for live_index, live_block in enumerate(live_blocks):
        settled_index = live_to_settled.get(live_index)
        if settled_index is None:
            ordered.append(live_block)
            continue
        # Unpaired settled blocks before this anchor belong to the engine too,
        # and the settled rendering is the only thing that orders them.
        while emitted_settled < settled_index:
            if emitted_settled not in settled_to_live:
                ordered.append(settled_blocks[emitted_settled])
            emitted_settled += 1
        ordered.append(_overlay_settled_block(live_block, settled_blocks[settled_index]))
        emitted_settled = settled_index + 1
    ordered.extend(
        block
        for index, block in enumerate(settled_blocks)
        if index >= emitted_settled and index not in settled_to_live
    )
    return ordered


def canonicalize_terminal_message_blocks(value: Any) -> list[dict[str, Any]]:
    """Keep terminal status blocks at the tail of a settled assistant message."""

    blocks = normalize_message_blocks(value)
    if not blocks:
        return []

    head: list[dict[str, Any]] = []
    result_block: dict[str, Any] | None = None
    failure_blocks: list[dict[str, Any]] = []
    for block in blocks:
        block_type = str(block.get("type") or "").strip()
        if block_type == "result":
            result_block = (
                _merge_result_block(result_block, block)
                if isinstance(result_block, dict)
                else dict(block)
            )
            continue
        if block_type == "turn_failure":
            failure_blocks.append(dict(block))
            continue
        head.append(dict(block))

    if isinstance(result_block, dict):
        head.append(result_block)
    head.extend(failure_blocks)
    return head


def drop_unresolved_tool_use_blocks(
    value: Any,
    *,
    keep_tool_use_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Drop unresolved tool_use blocks that are not explicitly kept.

    For terminal assistant messages, any tool_use without a matching
    tool_result is stale and should be removed. For active pending-interaction
    projections, the current pending tool_use remains authoritative, so callers
    can preserve it by passing ``keep_tool_use_ids``.
    """

    blocks = normalize_message_blocks(value)
    if not blocks:
        return []
    keep_ids = {
        str(tool_use_id or "").strip()
        for tool_use_id in (keep_tool_use_ids or set())
        if str(tool_use_id or "").strip()
    }

    resolved_tool_use_ids = {
        str(block.get("tool_use_id") or "").strip()
        for block in blocks
        if str(block.get("type") or "").strip() == "tool_result"
        and str(block.get("tool_use_id") or "").strip()
    }
    return [
        dict(block)
        for block in blocks
        if (
            str(block.get("type") or "").strip() != "tool_use"
            or str(block.get("id") or "").strip() in resolved_tool_use_ids
            or str(block.get("id") or "").strip() in keep_ids
        )
    ]


def message_blocks_need_repair(value: Any) -> bool:
    blocks = normalize_message_blocks(value)
    if not blocks:
        return False

    seen_tool_uses: set[str] = set()
    seen_tool_results: set[str] = set()
    result_count = 0

    for block in blocks:
        block_type = str(block.get("type") or "").strip()
        if block_type == "result":
            result_count += 1
            if result_count > 1:
                return True
            continue

        if block_type == "tool_use":
            block_id = str(block.get("id") or "").strip()
            if block_id:
                if block_id in seen_tool_uses:
                    return True
                seen_tool_uses.add(block_id)
            continue

        if block_type == "tool_result":
            tool_use_id = str(block.get("tool_use_id") or "").strip()
            if tool_use_id:
                if tool_use_id in seen_tool_results:
                    return True
                seen_tool_results.add(tool_use_id)

    return False


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


def message_blocks_have_unresolved_tool_use(value: Any) -> bool:
    blocks = normalize_message_blocks(value)
    if not blocks:
        return False

    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()

    for block in blocks:
        block_type = str(block.get("type") or "").strip()
        if block_type == "tool_use":
            block_id = str(block.get("id") or "").strip()
            if block_id:
                tool_use_ids.add(block_id)
            continue
        if block_type == "tool_result":
            tool_use_id = str(block.get("tool_use_id") or "").strip()
            if tool_use_id:
                tool_result_ids.add(tool_use_id)

    return bool(tool_use_ids - tool_result_ids)
