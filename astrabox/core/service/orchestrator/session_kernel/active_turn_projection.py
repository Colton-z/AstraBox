from __future__ import annotations

import json
from typing import Any

from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.assistant_text import coalesce_assistant_text
from astrabox.core.service.orchestrator.engine.frame_scope import (
    is_engine_public_ui_frame,
    public_engine_frame_payload,
)
from astrabox.core.service.orchestrator.message_blocks import (
    merge_projected_message_blocks,
    normalize_message_blocks,
    ui_data_block_key,
    upgrade_tool_identity_placeholders,
)
from astrabox.core.service.orchestrator.tool_result_semantics import (
    AI_SDK_TOOL_OUTPUT_FRAME_TYPES,
    tool_result_block_from_ai_sdk_frame,
)


def _parse_tool_input_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if isinstance(parsed, dict):
        return dict(parsed)
    return {}


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


def _merge_single_stable_block(
    existing_block: dict[str, Any] | None,
    incoming_block: dict[str, Any],
) -> dict[str, Any]:
    merged = merge_projected_message_blocks(
        [dict(existing_block)] if isinstance(existing_block, dict) else [],
        [dict(incoming_block)],
    )
    if merged:
        return dict(merged[0])
    return dict(incoming_block)


def _project_blocks_from_frames(frames: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    blocks: list[dict[str, Any]] = []
    latest_created_at: str | None = None
    tool_names: dict[str, str] = {}
    tool_input_buffers: dict[str, str] = {}
    tool_use_indexes: dict[str, int] = {}
    #: The prose block the engine currently has open, and what kind it is.
    #: A delta joins the previous block only while that block is still the
    #: one the engine opened.
    open_prose_id: str | None = None
    open_prose_kind: str | None = None
    projected_result_index_by_tool: dict[str, int] = {}

    def _ensure_tool_use_block(tool_call_id: str) -> int:
        existing_index = tool_use_indexes.get(tool_call_id)
        if existing_index is not None:
            return existing_index
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_call_id,
                "name": str(tool_names.get(tool_call_id) or ""),
                "input": {},
            }
        )
        tool_use_indexes[tool_call_id] = len(blocks) - 1
        return tool_use_indexes[tool_call_id]

    for row in frames:
        if latest_created_at is None:
            latest_created_at = str(row.get("created_at") or "").strip() or None

        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = str(payload.get("type") or "").strip()

        # An engine opening a block says where one piece of prose ends and the
        # next begins, and the id it carries says they are different pieces.
        # Merging on "the last block is text" instead reads two separate
        # messages as one: Codex answers a turn with more than one
        # `agentMessage`, and a turn that said PING twice arrived as PINGPING.
        if payload_type in ("text-start", "reasoning-start"):
            block_id = str(payload.get("id") or "").strip()
            if block_id and block_id != open_prose_id:
                open_prose_id = block_id
                open_prose_kind = None
            continue

        if payload_type == "reasoning-delta":
            delta = str(payload.get("delta") or "")
            if not delta:
                continue
            if open_prose_kind == "thinking" and blocks and str(
                blocks[-1].get("type") or ""
            ).strip() == "thinking":
                blocks[-1]["thinking"] = f"{str(blocks[-1].get('thinking') or '')}{delta}"
            else:
                blocks.append({"type": "thinking", "thinking": delta})
            open_prose_kind = "thinking"
            continue

        if payload_type == "text-delta":
            delta = str(payload.get("delta") or "")
            if not delta:
                continue
            if open_prose_kind == "text" and blocks and str(
                blocks[-1].get("type") or ""
            ).strip() == "text":
                blocks[-1]["text"] = f"{str(blocks[-1].get('text') or '')}{delta}"
            else:
                blocks.append({"type": "text", "text": delta})
            open_prose_kind = "text"
            continue

        if payload_type == "tool-input-start":
            tool_call_id = str(payload.get("toolCallId") or "").strip()
            if not tool_call_id:
                continue
            tool_name = str(payload.get("toolName") or "").strip()
            if tool_name:
                tool_names[tool_call_id] = tool_name
            tool_input_buffers.setdefault(tool_call_id, "")
            index = _ensure_tool_use_block(tool_call_id)
            if tool_name and not str(blocks[index].get("name") or "").strip():
                blocks[index]["name"] = tool_name
            continue

        if payload_type == "tool-input-delta":
            tool_call_id = str(payload.get("toolCallId") or "").strip()
            if not tool_call_id:
                continue
            tool_input_buffers[tool_call_id] = (
                f"{tool_input_buffers.get(tool_call_id, '')}{str(payload.get('inputTextDelta') or '')}"
            )
            index = _ensure_tool_use_block(tool_call_id)
            parsed_input = _parse_tool_input_payload(tool_input_buffers[tool_call_id])
            if parsed_input:
                blocks[index]["input"] = parsed_input
            continue

        if payload_type == "tool-input-available":
            tool_call_id = str(payload.get("toolCallId") or "").strip()
            if not tool_call_id:
                continue
            tool_name = str(payload.get("toolName") or "").strip()
            if tool_name:
                tool_names[tool_call_id] = tool_name
            index = _ensure_tool_use_block(tool_call_id)
            if tool_name:
                blocks[index]["name"] = tool_name
            if isinstance(payload.get("input"), dict):
                blocks[index]["input"] = dict(payload.get("input") or {})
            continue

        if payload_type in AI_SDK_TOOL_OUTPUT_FRAME_TYPES:
            result_block = tool_result_block_from_ai_sdk_frame(payload)
            if result_block is None:
                continue
            tool_call_id = str(result_block.get("tool_use_id") or "").strip()
            existing_index = projected_result_index_by_tool.get(tool_call_id)
            if existing_index is not None:
                blocks[existing_index] = _merge_single_stable_block(blocks[existing_index], result_block)
            else:
                blocks.append(result_block)
                projected_result_index_by_tool[tool_call_id] = len(blocks) - 1
            continue

        if payload_type == "data-result":
            frame_data = payload.get("data")
            if not isinstance(frame_data, dict):
                continue
            result_block = {"type": "result", **dict(frame_data)}
            if blocks and str(blocks[-1].get("type") or "").strip() == "result":
                blocks[-1] = _merge_single_stable_block(blocks[-1], result_block)
            else:
                blocks.append(result_block)
            continue

        if payload_type == "data-api-retry":
            frame_data = payload.get("data")
            if not isinstance(frame_data, dict):
                continue
            retry_block: dict[str, Any] = {
                "type": "api_retry",
                **dict(frame_data),
            }
            frame_id = str(payload.get("id") or "").strip()
            if frame_id:
                retry_block["id"] = frame_id
            existing_index = next(
                (
                    index
                    for index, candidate in enumerate(blocks)
                    if candidate.get("type") == "api_retry"
                ),
                None,
            )
            if existing_index is not None:
                blocks[existing_index] = retry_block
            else:
                blocks.append(retry_block)
            continue

        if payload_type == "data-raw-event":
            frame_data = payload.get("data")
            if not isinstance(frame_data, dict):
                continue
            raw_event_block: dict[str, Any] = {
                "type": "raw_event",
                "event_type": str(frame_data.get("event_type") or ""),
                "subtype": frame_data.get("subtype"),
                "raw": dict(frame_data.get("raw"))
                if isinstance(frame_data.get("raw"), dict)
                else dict(frame_data),
            }
            # The frame's own id, carried rather than dropped. A reader who
            # reloads mid-ladder gets this block back as a data part; without
            # the id the SDK cannot tell it from a new one and the next live
            # retry frame appends a second line beside the restored one.
            frame_id = str(payload.get("id") or "").strip()
            if frame_id:
                raw_event_block["id"] = frame_id
            # Same identity the live stream uses (message_blocks'
            # `_stable_block_key`): a turn keeps one block per raw-event
            # subtype, carrying the latest one. A retry ladder is one
            # condition, not ten events, and the reader watching it live
            # already sees a single line. An event with no subtype keeps no
            # identity and still appends.
            subtype = str(raw_event_block.get("subtype") or "").strip()
            existing_index = next(
                (
                    index
                    for index, candidate in enumerate(blocks)
                    if candidate.get("type") == "raw_event"
                    and str(candidate.get("subtype") or "").strip() == subtype
                ),
                None,
            ) if subtype else None
            if existing_index is not None:
                blocks[existing_index] = _merge_single_stable_block(
                    blocks[existing_index], raw_event_block
                )
            else:
                blocks.append(raw_event_block)
            continue

        # Child-run frames are Session-owned and have their own read model.
        # They never become hidden blocks inside the active root-turn message.
        if payload_type == "data-subagent":
            continue

        if is_engine_public_ui_frame(payload) and payload_type.startswith("data-"):
            public_part = public_engine_frame_payload(
                payload,
                frame_seq=None,
                scope="turn",
            )
            if public_part is None:
                continue
            data_block = {"type": "ui_data", "part": public_part}
            stable_key = _incremental_stable_block_key(data_block)
            existing_index = (
                next(
                    (
                        index
                        for index, candidate in enumerate(blocks)
                        if _incremental_stable_block_key(candidate) == stable_key
                    ),
                    None,
                )
                if stable_key is not None
                else None
            )
            if existing_index is None:
                blocks.append(data_block)
            else:
                blocks[existing_index] = data_block
            continue

    return blocks, latest_created_at


def _merge_projected_blocks_with_existing(
    projected_blocks: list[dict[str, Any]],
    existing_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not existing_blocks:
        return projected_blocks
    if not projected_blocks:
        return existing_blocks

    existing_tool_uses = {
        str(block.get("id") or "").strip(): block
        for block in existing_blocks
        if str(block.get("type") or "").strip() == "tool_use"
        and str(block.get("id") or "").strip()
    }
    existing_tool_results = {
        str(block.get("tool_use_id") or "").strip(): block
        for block in existing_blocks
        if str(block.get("type") or "").strip() == "tool_result"
        and str(block.get("tool_use_id") or "").strip()
    }
    existing_result_block = next(
        (block for block in existing_blocks if str(block.get("type") or "").strip() == "result"),
        None,
    )
    projected_tool_result_ids = {
        str(block.get("tool_use_id") or "").strip()
        for block in projected_blocks
        if str(block.get("type") or "").strip() == "tool_result"
        and str(block.get("tool_use_id") or "").strip()
    }
    consumed_existing_result_ids: set[str] = set()

    merged_blocks: list[dict[str, Any]] = []
    for block in projected_blocks:
        block_type = str(block.get("type") or "").strip()
        if block_type == "tool_use":
            tool_call_id = str(block.get("id") or "").strip()
            merged_blocks.append(
                _merge_single_stable_block(existing_tool_uses.get(tool_call_id), block)
            )
            if tool_call_id and tool_call_id not in projected_tool_result_ids:
                existing_result = existing_tool_results.get(tool_call_id)
                if isinstance(existing_result, dict):
                    merged_blocks.append(dict(existing_result))
                    consumed_existing_result_ids.add(tool_call_id)
            continue
        if block_type == "tool_result":
            tool_call_id = str(block.get("tool_use_id") or "").strip()
            merged_blocks.append(
                _merge_single_stable_block(existing_tool_results.get(tool_call_id), block)
            )
            if tool_call_id:
                consumed_existing_result_ids.add(tool_call_id)
            continue
        if block_type == "result":
            merged_blocks.append(
                _merge_single_stable_block(existing_result_block, block)
            )
            continue
        merged_blocks.append(dict(block))

    for block in existing_blocks:
        if str(block.get("type") or "").strip() != "tool_result":
            continue
        tool_call_id = str(block.get("tool_use_id") or "").strip()
        if tool_call_id and tool_call_id not in consumed_existing_result_ids:
            merged_blocks.append(dict(block))
            consumed_existing_result_ids.add(tool_call_id)

    return merged_blocks


def _blocks_equivalent(existing_block: dict[str, Any], projected_block: dict[str, Any]) -> bool:
    existing_type = str(existing_block.get("type") or "").strip()
    projected_type = str(projected_block.get("type") or "").strip()
    if existing_type != projected_type:
        return False
    if existing_type == "text":
        return str(existing_block.get("text") or "") == str(projected_block.get("text") or "")
    if existing_type == "thinking":
        return str(existing_block.get("thinking") or "") == str(projected_block.get("thinking") or "")
    if existing_type == "tool_use":
        return str(existing_block.get("id") or "").strip() == str(projected_block.get("id") or "").strip()
    if existing_type == "tool_result":
        return str(existing_block.get("tool_use_id") or "").strip() == str(projected_block.get("tool_use_id") or "").strip()
    if existing_type == "result":
        return True
    return existing_block == projected_block


def _leading_existing_context_prefix(
    existing_blocks: list[dict[str, Any]],
    projected_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not existing_blocks or not projected_blocks:
        return []
    first_projected = projected_blocks[0]
    match_index = next(
        (
            index
            for index, block in enumerate(existing_blocks)
            if _blocks_equivalent(block, first_projected)
        ),
        None,
    )
    prefix = existing_blocks if match_index is None else existing_blocks[:match_index]
    return [
        dict(block)
        for block in prefix
        if str(block.get("type") or "").strip() in {"thinking", "text"}
    ]


def _incremental_stable_block_key(block: dict[str, Any]) -> tuple[str, str] | None:
    block_type = str(block.get("type") or "").strip()
    if block_type == "tool_use":
        block_id = str(block.get("id") or "").strip()
        return (block_type, block_id) if block_id else None
    if block_type == "tool_result":
        tool_use_id = str(block.get("tool_use_id") or "").strip()
        return (block_type, tool_use_id) if tool_use_id else None
    if block_type == "result":
        return (block_type, "__single__")
    if block_type == "ui_data":
        return ui_data_block_key(block)
    return None


def _merge_incremental_blocks(
    existing_blocks: list[dict[str, Any]],
    projected_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = normalize_message_blocks(existing_blocks)
    for block in projected_blocks:
        stable_key = _incremental_stable_block_key(block)
        if stable_key is None:
            merged.append(dict(block))
            continue
        match_index = next(
            (
                index
                for index, existing_block in enumerate(merged)
                if _incremental_stable_block_key(existing_block) == stable_key
            ),
            None,
        )
        if match_index is None:
            merged.append(dict(block))
            continue

        upgraded = _merge_single_stable_block(merged[match_index], block)
        block_type = stable_key[0]
        if block_type == "tool_use":
            merged[match_index] = upgraded
            continue

        # Result blocks are rendered in transcript order. When an existing
        # stable result is updated after the watermark, move the upgraded
        # block to the current incremental position instead of keeping it in
        # the pre-watermark slot.
        merged.pop(match_index)
        merged.append(upgraded)
    return merged


def build_active_turn_message(
    *,
    session_id: str,
    turn_id: str,
    message_id: str,
    frames: list[dict[str, Any]],
    existing_message: dict[str, Any] | None,
    default_message_seq: int,
    user_id: str | None = None,
    active_interaction: dict[str, Any] | None = None,
    incremental: bool = False,
) -> dict[str, Any] | None:
    platform_turn_id = str(turn_id or "").strip()
    engine_message_id = str(message_id or "").strip()
    if not platform_turn_id:
        raise ValueError("active message projection requires a platform turn_id")
    if not engine_message_id:
        raise ValueError("active message projection requires a message_id")

    current_message = dict(existing_message or {})
    current_message_id = str(current_message.get("message_id") or "").strip()
    if current_message_id and current_message_id != engine_message_id:
        raise ValueError(
            "active message projection cannot replace an existing message identity"
        )
    normalized_existing_blocks = normalize_message_blocks(current_message.get("blocks"))
    projected_blocks, latest_created_at = _project_blocks_from_frames(frames)
    existing_frame_watermark_raw = current_message.get("source_frame_seq_applied")
    existing_frame_watermark = (
        int(existing_frame_watermark_raw)
        if existing_frame_watermark_raw is not None
        else None
    )
    projected_frame_watermark = max(
        [
            int(frame.get("frame_seq") or -1)
            for frame in frames
            if int(frame.get("frame_seq") or -1) >= 0
        ],
        default=-1,
    )

    if incremental:
        # In incremental mode (watermark present), existing blocks are the
        # already-absorbed base and projected blocks are only the new
        # increment after the watermark. Stable tool/result ids must still
        # upgrade in place, but ordinary text/thinking deltas must append
        # even if their content repeats later in the turn.
        merged_blocks = _merge_incremental_blocks(
            normalized_existing_blocks,
            projected_blocks,
        )
    else:
        merged_projected_blocks = _merge_projected_blocks_with_existing(
            projected_blocks,
            normalized_existing_blocks,
        )
        leading_prefix = _leading_existing_context_prefix(
            normalized_existing_blocks,
            merged_projected_blocks,
        )
        merged_blocks = normalize_message_blocks([*leading_prefix, *merged_projected_blocks])
    merged_blocks = upgrade_tool_identity_placeholders(
        merged_blocks,
        placeholder_id=str((active_interaction or {}).get("interaction_id") or "").strip() or None,
        resolved_id=str((active_interaction or {}).get("tool_call_id") or "").strip() or None,
        tool_name=str((active_interaction or {}).get("tool_name") or "").strip() or None,
    )
    projected_content = (
        _synthesize_assistant_text_from_blocks(merged_blocks)
        or str(current_message.get("content") or "")
    )

    if not merged_blocks and not projected_content.strip():
        return dict(current_message) if current_message else None

    return {
        **current_message,
        "session_id": session_id,
        "message_id": engine_message_id,
        "message_seq": int(current_message.get("message_seq") or default_message_seq),
        "turn_id": platform_turn_id,
        "role": "assistant",
        "user_id": str(current_message.get("user_id") or user_id or ""),
        "content": projected_content,
        "blocks": merged_blocks,
        "created_at": str(current_message.get("created_at") or latest_created_at or utcnow_iso()),
        "source_event_seq_applied": int(current_message.get("source_event_seq_applied") or 0),
        "source_frame_seq_applied": max(
            existing_frame_watermark if existing_frame_watermark is not None else -1,
            projected_frame_watermark,
        ) if (
            existing_frame_watermark is not None
            or projected_frame_watermark >= 0
        ) else None,
    }


def build_active_engine_fifo_messages(
    *,
    session_id: str,
    turn_id: str,
    frames: list[dict[str, Any]],
    default_message_seq: int,
    user_id: str | None = None,
    active_interaction: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Project every root input consumed before the current engine result."""

    platform_turn_id = str(turn_id or "").strip()
    if not platform_turn_id:
        raise ValueError("engine FIFO projection requires a platform turn_id")

    boundaries: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(frames):
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "data-input-consumed":
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("engine input consumption frame data is malformed")
        input_id = str(data.get("inputId") or "").strip()
        response_id = str(data.get("responseMessageId") or "").strip()
        content = data.get("content")
        if not input_id or not response_id or not isinstance(content, str):
            raise ValueError("engine input consumption frame is incomplete")
        boundaries.append((index, data))

    messages: list[dict[str, Any]] = []
    for boundary_index, (frame_index, data) in enumerate(boundaries):
        input_id = str(data["inputId"])
        response_id = str(data["responseMessageId"])
        client_message_id = str(data.get("clientMessageId") or input_id).strip()
        content = str(data["content"])
        next_index = (
            boundaries[boundary_index + 1][0]
            if boundary_index + 1 < len(boundaries)
            else len(frames)
        )
        marker = frames[frame_index]
        marker_frame_seq = marker.get("frame_seq")
        message_seq = (
            int(marker_frame_seq)
            if isinstance(marker_frame_seq, int)
            and not isinstance(marker_frame_seq, bool)
            and marker_frame_seq >= 0
            else default_message_seq + len(messages)
        )
        created_at = str(marker.get("created_at") or utcnow_iso())
        messages.append(
            {
                "session_id": session_id,
                "message_id": f"{input_id}:user",
                "message_seq": message_seq,
                "turn_id": platform_turn_id,
                "role": "user",
                "user_id": str(user_id or ""),
                "client_message_id": client_message_id,
                "content": content,
                "blocks": [
                    dict(block)
                    for block in (data.get("contentBlocks") or [])
                    if isinstance(block, dict)
                    and str(block.get("type") or "") != "text"
                ],
                "source_event_seq_applied": 0,
                "created_at": created_at,
            }
        )
        response_frames = frames[frame_index + 1 : next_index]
        response_frame_seqs = [
            int(frame["frame_seq"])
            for frame in response_frames
            if isinstance(frame.get("frame_seq"), int)
            and not isinstance(frame.get("frame_seq"), bool)
            and int(frame["frame_seq"]) >= 0
        ]
        assistant_message_seq = (
            min(response_frame_seqs)
            if response_frame_seqs
            else max(message_seq + 1, default_message_seq)
        )
        assistant = build_active_turn_message(
            session_id=session_id,
            turn_id=platform_turn_id,
            message_id=response_id,
            frames=response_frames,
            existing_message=None,
            default_message_seq=assistant_message_seq,
            user_id=user_id,
            active_interaction=(
                active_interaction
                if boundary_index == len(boundaries) - 1
                else None
            ),
        )
        if assistant is None:
            assistant = {
                "session_id": session_id,
                "message_id": response_id,
                "message_seq": assistant_message_seq,
                "turn_id": platform_turn_id,
                "role": "assistant",
                "user_id": str(user_id or ""),
                "content": "",
                "blocks": [],
                "source_event_seq_applied": 0,
                "created_at": created_at,
            }
        messages.append(assistant)
    return messages
