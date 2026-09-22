import json
import re
from dataclasses import dataclass, field
from typing import Any

from astrabox.core.service.orchestrator.engine.child_runs import (
    canonical_child_run_data,
    canonicalize_child_run_blocks,
)
from astrabox.core.service.orchestrator.engine.claude_child_runs import (
    CLAUDE_CODE_ENGINE_KIND,
    ClaudeChildIdentities,
    claude_child_context_id,
    claude_content_blocks,
    claude_envelope_value,
    claude_lifecycle_child_run_id,
    claude_lifecycle_parent_child_run_id,
    claude_lifecycle_subtype,
    claude_message_role,
    infer_claude_child_run_parents,
)
from astrabox.core.service.orchestrator.system_events import (
    api_retry_payload,
)
from astrabox.core.service.orchestrator.tool_result_semantics import (
    normalize_tool_result_block,
)

from astrabox.core.service.orchestrator.message_blocks import (
    deduplicate_message_blocks,
    merge_message_blocks,
    normalize_message_blocks,
)


def get_tool_event_content_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    direct_content = data.get("content")
    if isinstance(direct_content, list):
        return [dict(block) for block in direct_content if isinstance(block, dict)]

    message = data.get("message")
    if isinstance(message, dict):
        nested_content = message.get("content")
        if isinstance(nested_content, list):
            return [dict(block) for block in nested_content if isinstance(block, dict)]

    return []


def _parse_api_retry_block(data: dict[str, Any]) -> dict[str, Any] | None:
    payload = api_retry_payload(data)
    if payload is None:
        return None
    return {
        "type": "api_retry",
        **payload,
    }


def _build_subagent_block_from_raw(
    data: dict[str, Any], *, identities: ClaudeChildIdentities, child_engine_ref: str | None = None
) -> dict[str, Any] | None:
    """Project one message from a Claude child context.

    Lifecycle messages have a separate ordered adapter path so task aliases
    can be resolved before a fact is emitted. This helper only prevents child
    transcript content from entering the root assistant message.
    """

    role = claude_message_role(data)
    content_blocks = claude_content_blocks(data)
    if not content_blocks:
        return None
    engine_ref = (child_engine_ref or identities.message_ref(data)) if role else ""
    if engine_ref and role:
        message_data: dict[str, Any] = {
            "kind": "message",
            "engineRef": engine_ref,
            "role": role,
            "content": content_blocks,
        }
        if parent_id := identities.parent_by_agent.get(engine_ref):
            message_data["parentEngineRef"] = parent_id
        message_data = canonical_child_run_data(
            message_data,
            engine_kind=CLAUDE_CODE_ENGINE_KIND,
        )
        message = data.get("message")
        message_id = str(data.get("message_id") or "").strip()
        if isinstance(message, dict):
            message_id = message_id or str(message.get("id") or "").strip()
        if message_id:
            message_data["messageId"] = message_id
        block = {"type": "subagent", "data": message_data}
        uuid_value = str(claude_envelope_value(data, "uuid") or "").strip()
        if uuid_value:
            block["id"] = f"subagent:msg:{uuid_value}"
        elif message_id:
            block["id"] = f"subagent:msg:{message_id}"
        else:
            block["id"] = f"subagent:msg:{engine_ref}:{role}"
        return block
    return None


def parse_tool_event_blocks(
    data: Any,
    *,
    identities: ClaudeChildIdentities | None = None,
    child_engine_ref: str | None = None,
) -> list[dict[str, Any]]:
    # The subagent intercept must run before the generic block parsing below.
    # Otherwise `collect_message_blocks_from_raw_events` and the canonical
    # projector path both wrap task_* SystemMessages as opaque `raw_event`
    # blocks and fold inner-subagent AssistantMessages into the parent
    # message's content, which puts child output in the parent's transcript
    # and loses the child-run tree the console rebuilds on reload.
    if not isinstance(data, dict):
        return []

    if identities is None:
        identities = ClaudeChildIdentities.from_messages([data])
    subagent_block = _build_subagent_block_from_raw(
        data, identities=identities, child_engine_ref=child_engine_ref
    )
    if subagent_block is not None:
        return [subagent_block]
    if claude_lifecycle_subtype(data):
        return []

    api_retry_block = _parse_api_retry_block(data)
    if api_retry_block is not None:
        return [api_retry_block]

    data_type = str(data.get("type") or "").strip().lower()
    data_subtype = str(data.get("subtype") or "").strip().lower()
    if (
        data_type == "result"
        or data_subtype in {"result", "success"}
        or data.get("result") is not None
    ):
        usage = data.get("usage")
        block: dict[str, Any] = {
            "type": "result",
            "result": str(data.get("result") or ""),
        }
        if isinstance(data.get("duration_ms"), (int, float)):
            block["duration_ms"] = data["duration_ms"]
        if isinstance(data.get("duration_api_ms"), (int, float)):
            block["duration_api_ms"] = data["duration_api_ms"]
        if isinstance(data.get("total_cost_usd"), (int, float)):
            block["total_cost_usd"] = data["total_cost_usd"]
        if isinstance(data.get("num_turns"), int):
            block["num_turns"] = data["num_turns"]
        if isinstance(usage, dict):
            usage_block: dict[str, Any] = {}
            if isinstance(usage.get("input_tokens"), int):
                usage_block["input_tokens"] = usage["input_tokens"]
            if isinstance(usage.get("output_tokens"), int):
                usage_block["output_tokens"] = usage["output_tokens"]
            if usage_block:
                block["usage"] = usage_block
        return [block]

    blocks: list[dict[str, Any]] = []
    for block in get_tool_event_content_blocks(data):
        thinking = block.get("thinking")
        if isinstance(thinking, str):
            blocks.append({"type": "thinking", "thinking": thinking})
            continue

        name = block.get("name")
        block_id = block.get("id")
        if isinstance(name, str) and isinstance(block_id, str):
            raw_input = block.get("input")
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block_id,
                    "name": name,
                    "input": dict(raw_input) if isinstance(raw_input, dict) else {},
                }
            )
            continue

        tool_use_id = block.get("tool_use_id")
        if isinstance(tool_use_id, str):
            raw_content = block.get("content")
            result_content = ""
            if isinstance(raw_content, str):
                result_content = raw_content
            elif isinstance(raw_content, list):
                parts: list[str] = []
                for item in raw_content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                        continue
                    try:
                        parts.append(json.dumps(item, ensure_ascii=False))
                    except TypeError:
                        parts.append(str(item))
                result_content = "\n".join(parts)
            blocks.append(
                normalize_tool_result_block(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_content,
                        "is_error": block.get("is_error") is True,
                    }
                )
            )
            continue

        text = block.get("text")
        if isinstance(text, str):
            blocks.append({"type": "text", "text": text})

    return blocks


_TOOL_MARKUP_TAG_RE = re.compile(
    r"<\s*/?\s*("
    r"write|bash|edit|multiedit|read|todowrite|notebookedit|"
    r"grep|glob|ls|webfetch|task|agent|askuserquestion|exitplanmode"
    r")(?:\s|>|/)",
    re.IGNORECASE,
)


def _block_contains_tool_markup(block: dict[str, Any]) -> bool:
    block_type = str(block.get("type") or "").strip()
    if block_type == "text":
        _, has_markup = split_tool_markup_prefix(str(block.get("text") or ""))
        return has_markup
    if block_type == "thinking":
        _, has_markup = split_tool_markup_prefix(str(block.get("thinking") or ""))
        return has_markup
    return False


def split_tool_markup_prefix(text: str) -> tuple[str, bool]:
    value = str(text or "")
    match = _TOOL_MARKUP_TAG_RE.search(value)
    if match is None:
        return value, False
    return value[: match.start()], True


def suppress_tool_markup_snapshot_blocks(blocks: Any) -> list[dict[str, Any]]:
    normalized = normalize_message_blocks(blocks)
    if not any(str(block.get("type") or "").strip() == "tool_use" for block in normalized):
        return normalized
    if not any(_block_contains_tool_markup(block) for block in normalized):
        return normalized
    suppressed: list[dict[str, Any]] = []
    for block in normalized:
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            prefix, has_markup = split_tool_markup_prefix(str(block.get("text") or ""))
            if has_markup:
                if prefix.strip():
                    next_block = dict(block)
                    next_block["text"] = prefix
                    suppressed.append(next_block)
                continue
        elif block_type == "thinking":
            prefix, has_markup = split_tool_markup_prefix(str(block.get("thinking") or ""))
            if has_markup:
                if prefix.strip():
                    next_block = dict(block)
                    next_block["thinking"] = prefix
                    suppressed.append(next_block)
                continue
        suppressed.append(block)
    return suppressed


def _extract_message_id(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None

    message = raw.get("message")
    if isinstance(message, dict):
        message_id = str(message.get("id") or "").strip()
        if message_id:
            return message_id

    event = raw.get("event")
    if isinstance(event, dict):
        message_id = str(event.get("id") or "").strip()
        if message_id:
            return message_id
        inner_msg = event.get("message")
        if isinstance(inner_msg, dict):
            msg_id = str(inner_msg.get("id") or "").strip()
            if msg_id:
                return msg_id

    return None


def _extract_stop_reason(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None

    direct = str(raw.get("stop_reason") or "").strip().lower()
    if direct:
        return direct

    message = raw.get("message")
    if isinstance(message, dict):
        message_reason = str(message.get("stop_reason") or "").strip().lower()
        if message_reason:
            return message_reason

    event = raw.get("event")
    if isinstance(event, dict):
        delta = event.get("delta")
        if isinstance(delta, dict):
            delta_reason = str(delta.get("stop_reason") or "").strip().lower()
            if delta_reason:
                return delta_reason

    return None


def _is_message_stop_event(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    event = raw.get("event")
    if not isinstance(event, dict):
        return False
    return str(event.get("type") or "").strip().lower() == "message_stop"


def _is_stream_event(raw: Any) -> bool:
    return isinstance(raw, dict) and str(raw.get("type") or "").strip().lower() == "stream_event"


def _is_assistant_event(raw: Any) -> bool:
    return isinstance(raw, dict) and str(raw.get("type") or "").strip().lower() == "assistant"


def _is_interaction_request(raw: Any) -> bool:
    return isinstance(raw, dict) and str(raw.get("type") or "").strip() == "interaction_request"


@dataclass
class _AssistantBlockSegment:
    message_id: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    pending_stop_reason: str | None = None
    awaiting_closure: bool = False
    closed: bool = False
    streaming: dict[int, dict[str, Any]] = field(default_factory=dict)


def collect_terminal_message_blocks(raw_items: list[Any]) -> list[dict[str, Any]]:
    """Collect final assistant blocks while respecting assistant-message boundaries.

    Claude can emit a complete assistant tool_use message that is later superseded by
    another assistant message in the same user turn before any permission request or
    tool_result is produced. In that case the earlier tool_use should not leak into
    the final persisted transcript.
    """

    identities = ClaudeChildIdentities.from_messages(
        [raw for raw in raw_items if isinstance(raw, dict)]
    )
    collected: list[dict[str, Any]] = []
    current_segment: _AssistantBlockSegment | None = None

    def _commit_current_segment() -> None:
        nonlocal collected, current_segment
        if current_segment is not None:
            if current_segment.streaming:
                ordered = [current_segment.streaming[i] for i in sorted(current_segment.streaming)]
                finalized: list[dict[str, Any]] = []
                for b in ordered:
                    fb = dict(b)
                    if (
                        fb.get("type") == "tool_use"
                        and fb.get("_input_json")
                        and not fb.get("input")
                    ):
                        try:
                            fb["input"] = json.loads(str(fb.pop("_input_json")))
                        except (json.JSONDecodeError, TypeError):
                            fb.pop("_input_json", None)
                    else:
                        fb.pop("_input_json", None)
                    finalized.append(fb)
                finalized = [
                    b
                    for b in finalized
                    if b.get("thinking")
                    or b.get("text")
                    or b.get("name")
                    or b.get("type") == "tool_use"
                ]
                if finalized:
                    current_segment.blocks = suppress_tool_markup_snapshot_blocks(
                        merge_message_blocks(current_segment.blocks, finalized)
                    )
                current_segment.streaming.clear()
            if current_segment.blocks:
                collected = merge_message_blocks(
                    collected,
                    suppress_tool_markup_snapshot_blocks(current_segment.blocks),
                )
        current_segment = None

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        if _is_assistant_event(raw):
            message_id = _extract_message_id(raw)
            if message_id:
                if current_segment is not None and current_segment.message_id != message_id:
                    if current_segment.awaiting_closure and not current_segment.closed:
                        current_segment = None
                    else:
                        _commit_current_segment()

                if current_segment is None or current_segment.message_id != message_id:
                    current_segment = _AssistantBlockSegment(message_id=message_id)

            # Assistant snapshot supersedes in-flight streaming for this message
            if current_segment is not None and current_segment.message_id == message_id:
                current_segment.streaming.clear()

            new_blocks = suppress_tool_markup_snapshot_blocks(
                parse_tool_event_blocks(raw, identities=identities)
            )
            if new_blocks:
                if current_segment is not None:
                    current_segment.blocks = merge_message_blocks(
                        current_segment.blocks,
                        new_blocks,
                    )
                else:
                    collected = merge_message_blocks(collected, new_blocks)

            stop_reason = _extract_stop_reason(raw)
            if current_segment is not None and stop_reason:
                current_segment.pending_stop_reason = stop_reason
                if stop_reason == "tool_use":
                    current_segment.awaiting_closure = True
                else:
                    current_segment.closed = True
            continue

        if _is_stream_event(raw):
            event = raw.get("event")
            if not isinstance(event, dict):
                continue
            evt_type = str(event.get("type") or "").strip()

            # message_start → create/switch segment
            if evt_type == "message_start":
                message_id = _extract_message_id(raw)
                if message_id:
                    if current_segment is not None and current_segment.message_id != message_id:
                        if current_segment.awaiting_closure and not current_segment.closed:
                            current_segment = None
                        else:
                            _commit_current_segment()
                    if current_segment is None or current_segment.message_id != message_id:
                        current_segment = _AssistantBlockSegment(message_id=message_id)
                continue

            if current_segment is None:
                continue

            # content_block_start/delta/stop → accumulate in segment.streaming
            idx = event.get("index")
            if isinstance(idx, int):
                if evt_type == "content_block_start":
                    cb = event.get("content_block") or {}
                    cb_type = str(cb.get("type") or "").strip()
                    if cb_type == "thinking":
                        current_segment.streaming[idx] = {"type": "thinking", "thinking": ""}
                    elif cb_type == "text":
                        current_segment.streaming[idx] = {"type": "text", "text": ""}
                    elif cb_type == "tool_use":
                        current_segment.streaming[idx] = {
                            "type": "tool_use",
                            "id": str(cb.get("id") or ""),
                            "name": str(cb.get("name") or ""),
                            "input": {},
                        }
                elif evt_type == "content_block_delta":
                    block = current_segment.streaming.get(idx)
                    if block is not None:
                        delta = event.get("delta") or {}
                        delta_type = str(delta.get("type") or "").strip()
                        if delta_type == "thinking_delta":
                            block["thinking"] = block.get("thinking", "") + str(
                                delta.get("thinking") or ""
                            )
                        elif delta_type == "text_delta":
                            block["text"] = block.get("text", "") + str(delta.get("text") or "")
                        elif delta_type == "input_json_delta":
                            block.setdefault("_input_json", "")
                            block["_input_json"] += str(delta.get("partial_json") or "")
                elif evt_type == "content_block_stop":
                    block = current_segment.streaming.get(idx)
                    if block is not None and block.get("_input_json"):
                        try:
                            block["input"] = json.loads(block.pop("_input_json"))
                        except (json.JSONDecodeError, TypeError):
                            block.pop("_input_json", None)

            # stop_reason / message_stop tracking
            message_id = _extract_message_id(raw)
            if message_id and message_id != current_segment.message_id:
                continue
            stop_reason = _extract_stop_reason(raw)
            if stop_reason:
                current_segment.pending_stop_reason = stop_reason
            if _is_message_stop_event(raw):
                final_reason = current_segment.pending_stop_reason
                current_segment.pending_stop_reason = None
                if final_reason == "tool_use":
                    current_segment.awaiting_closure = True
                else:
                    current_segment.closed = True
            continue

        new_blocks = parse_tool_event_blocks(raw, identities=identities)
        if current_segment is not None and current_segment.awaiting_closure:
            if new_blocks:
                current_segment.blocks = merge_message_blocks(
                    current_segment.blocks,
                    new_blocks,
                )
            if _is_interaction_request(raw):
                current_segment.closed = True
            elif any(str(block.get("type") or "").strip() == "tool_result" for block in new_blocks):
                current_segment.closed = True
            continue

        if new_blocks:
            collected = merge_message_blocks(collected, new_blocks)

    if current_segment is not None and (
        current_segment.closed or not current_segment.awaiting_closure
    ):
        _commit_current_segment()

    return collected


def collect_message_blocks_from_raw_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild blocks from serialized raw SDK events.

    Handles both ``assistant`` snapshots (which carry ``message.content``)
    and ``stream_event`` entries (``content_block_start/delta/stop``) so
    that blocks produced *after* the last snapshot are not lost.

    A single turn may contain multiple assistant messages (e.g. before and
    after a pending-interaction).  Within the same message, later snapshots
    *replace* earlier ones.  Across messages, blocks are accumulated.
    """
    identities = ClaudeChildIdentities.from_messages(
        [event["raw"] for event in events if isinstance(event.get("raw"), dict)]
    )
    # Committed blocks from fully-processed messages
    committed: list[dict[str, Any]] = []
    # Blocks from the current assistant message (latest snapshot wins)
    current_msg_snapshot: list[dict[str, Any]] = []
    current_msg_id: str | None = None
    # In-flight streaming blocks keyed by content_block index
    streaming: dict[int, dict[str, Any]] = {}
    seen_seqs: set[int] = set()

    def _finalize_streaming_block(block: dict[str, Any]) -> dict[str, Any]:
        finalized = dict(block)
        if (
            finalized.get("type") == "tool_use"
            and finalized.get("_input_json")
            and not finalized.get("input")
        ):
            try:
                finalized["input"] = json.loads(str(finalized.pop("_input_json")))
            except (json.JSONDecodeError, TypeError):
                finalized.pop("_input_json", None)
        else:
            finalized.pop("_input_json", None)
        return finalized

    def _commit_current_message() -> None:
        """Flush streaming blocks and commit the current message."""
        nonlocal committed, current_msg_snapshot, streaming, current_msg_id
        # Streaming blocks that came after the last snapshot for this message
        if streaming:
            ordered = [_finalize_streaming_block(streaming[i]) for i in sorted(streaming)]
            ordered = [b for b in ordered if b.get("thinking") or b.get("text") or b.get("name")]
            if ordered:
                current_msg_snapshot = suppress_tool_markup_snapshot_blocks(
                    merge_message_blocks(current_msg_snapshot, ordered)
                )
            streaming = {}
        if current_msg_snapshot:
            committed = merge_message_blocks(
                committed,
                suppress_tool_markup_snapshot_blocks(current_msg_snapshot),
            )
            current_msg_snapshot = []
        current_msg_id = None

    for event in events:
        seq = event.get("seq")
        if isinstance(seq, int):
            if seq in seen_seqs:
                continue
            seen_seqs.add(seq)

        raw = event.get("raw")
        if not isinstance(raw, dict):
            continue

        raw_type = str(raw.get("type") or "").strip().lower()

        # --- assistant snapshot: accumulate within same message ---
        if raw_type == "assistant":
            parsed = suppress_tool_markup_snapshot_blocks(
                parse_tool_event_blocks(raw, identities=identities)
            )
            if parsed:
                msg_id = _extract_message_id(raw) or ""
                if msg_id and msg_id != current_msg_id:
                    if current_msg_id is not None:
                        _commit_current_message()
                    current_msg_id = msg_id
                # An assistant snapshot carries only the *current* content
                # block, not all accumulated blocks.  Merge to collect them all.
                current_msg_snapshot = merge_message_blocks(current_msg_snapshot, parsed)
                streaming.clear()
            continue

        # --- stream_event: track content_block lifecycle ---
        if raw_type == "stream_event":
            event = raw.get("event")
            if not isinstance(event, dict):
                continue
            evt_type = str(event.get("type") or "").strip()
            idx = event.get("index")
            if not isinstance(idx, int):
                if evt_type == "message_start" and current_msg_id is not None:
                    _commit_current_message()
                continue

            if evt_type == "content_block_start":
                cb = event.get("content_block") or {}
                cb_type = str(cb.get("type") or "").strip()
                if cb_type == "thinking":
                    streaming[idx] = {"type": "thinking", "thinking": ""}
                elif cb_type == "text":
                    streaming[idx] = {"type": "text", "text": ""}
                elif cb_type == "tool_use":
                    streaming[idx] = {
                        "type": "tool_use",
                        "id": str(cb.get("id") or ""),
                        "name": str(cb.get("name") or ""),
                        "input": {},
                    }
            elif evt_type == "content_block_delta":
                block = streaming.get(idx)
                if block is None:
                    continue
                delta = event.get("delta") or {}
                delta_type = str(delta.get("type") or "").strip()
                if delta_type == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + str(delta.get("thinking") or "")
                elif delta_type == "text_delta":
                    block["text"] = block.get("text", "") + str(delta.get("text") or "")
                elif delta_type == "input_json_delta":
                    partial = str(delta.get("partial_json") or "")
                    block.setdefault("_input_json", "")
                    block["_input_json"] += partial
            elif evt_type == "content_block_stop":
                block = streaming.get(idx)
                if (
                    block is not None
                    and block.get("type") == "tool_use"
                    and block.get("_input_json")
                ):
                    try:
                        block["input"] = json.loads(block.pop("_input_json"))
                    except (json.JSONDecodeError, TypeError):
                        block.pop("_input_json", None)
            continue

        # --- other event types (user tool_result, result, etc.) ---
        parsed = parse_tool_event_blocks(raw, identities=identities)
        if parsed:
            _commit_current_message()
            committed = merge_message_blocks(committed, parsed)

    _commit_current_message()
    linked = infer_claude_child_run_parents(committed, identities=identities)
    return canonicalize_child_run_blocks(
        linked,
        engine_kind=CLAUDE_CODE_ENGINE_KIND,
    )
