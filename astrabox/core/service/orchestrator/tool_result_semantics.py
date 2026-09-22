"""Tool-result terminal semantics shared by stream and history adapters."""

from __future__ import annotations

from typing import Any

TOOL_RESULT_STATE_AVAILABLE = "output-available"
TOOL_RESULT_STATE_ERROR = "output-error"
TOOL_RESULT_STATE_DENIED = "output-denied"

TOOL_RESULT_STATES = frozenset(
    {
        TOOL_RESULT_STATE_AVAILABLE,
        TOOL_RESULT_STATE_ERROR,
        TOOL_RESULT_STATE_DENIED,
    }
)

AI_SDK_TOOL_OUTPUT_FRAME_TYPES = frozenset(
    {
        "tool-output-available",
        "tool-output-error",
        "tool-output-denied",
    }
)

DENIED_TOOL_RESULT_CONTENT = "The user doesn't want to proceed with this tool use."

_DENIED_TOOL_RESULT_MARKERS = (
    "the user doesn't want to proceed with this tool use",
    "user rejected claude's plan",
    "user declined to answer questions",
    "do not exit plan mode yet",
    # The interrupt deny never marks the interaction ANSWERED, so the structured
    # DENIED stamp does not fire on that path — and the deny comment the CLI
    # records is exactly this string, which without the marker classifies
    # output-error and clobbers the projection's structured DENIED in
    # _merge_tool_result_block.
    "user interrupted the session",
)


def tool_result_state_from_content(*, content: Any, is_error: bool) -> str:
    if not is_error:
        return TOOL_RESULT_STATE_AVAILABLE
    normalized = str(content or "").strip().lower()
    if any(marker in normalized for marker in _DENIED_TOOL_RESULT_MARKERS):
        return TOOL_RESULT_STATE_DENIED
    return TOOL_RESULT_STATE_ERROR


def normalize_tool_result_block(block: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(block)
    if str(normalized.get("type") or "").strip() != "tool_result":
        return normalized
    raw_state = str(normalized.get("tool_result_state") or "").strip()
    state = (
        raw_state
        if raw_state in TOOL_RESULT_STATES
        else tool_result_state_from_content(
            content=normalized.get("content"),
            is_error=normalized.get("is_error") is True,
        )
    )
    normalized["tool_result_state"] = state
    normalized["is_error"] = state != TOOL_RESULT_STATE_AVAILABLE
    return normalized


def tool_result_block(
    *,
    tool_call_id: str,
    content: Any,
    tool_result_state: str | None = None,
    is_error: bool | None = None,
) -> dict[str, Any]:
    state = (
        str(tool_result_state or "").strip()
        if str(tool_result_state or "").strip() in TOOL_RESULT_STATES
        else tool_result_state_from_content(
            content=content,
            is_error=bool(is_error),
        )
    )
    return {
        "type": "tool_result",
        "tool_use_id": tool_call_id,
        "content": str(content or ""),
        "is_error": state != TOOL_RESULT_STATE_AVAILABLE,
        "tool_result_state": state,
    }


def tool_result_block_from_ai_sdk_frame(payload: dict[str, Any]) -> dict[str, Any] | None:
    frame_type = str(payload.get("type") or "").strip()
    tool_call_id = str(payload.get("toolCallId") or "").strip()
    if not tool_call_id:
        return None
    if frame_type == "tool-output-available":
        return tool_result_block(
            tool_call_id=tool_call_id,
            content=payload.get("output"),
            tool_result_state=TOOL_RESULT_STATE_AVAILABLE,
        )
    if frame_type == "tool-output-error":
        return tool_result_block(
            tool_call_id=tool_call_id,
            content=payload.get("errorText"),
            tool_result_state=TOOL_RESULT_STATE_ERROR,
        )
    if frame_type == "tool-output-denied":
        return tool_result_block(
            tool_call_id=tool_call_id,
            content=DENIED_TOOL_RESULT_CONTENT,
            tool_result_state=TOOL_RESULT_STATE_DENIED,
        )
    return None


def canonical_event_from_tool_result_block(block: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_tool_result_block(block)
    tool_call_id = str(normalized.get("tool_use_id") or "").strip()
    state = str(normalized.get("tool_result_state") or "").strip()
    if state == TOOL_RESULT_STATE_DENIED:
        return {
            "type": "tool_output_denied",
            "tool_call_id": tool_call_id,
        }
    if state == TOOL_RESULT_STATE_ERROR:
        return {
            "type": "tool_output_error",
            "tool_call_id": tool_call_id,
            "error_text": str(normalized.get("content") or ""),
            "provider_executed": True,
        }
    return {
        "type": "tool_output_available",
        "tool_call_id": tool_call_id,
        "output": str(normalized.get("content") or ""),
        "provider_executed": True,
    }
