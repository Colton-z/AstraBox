"""Frame translators — wire-format events → the AI SDK UI message stream parts.

The part vocabulary (text-start/delta/end, reasoning-*, tool-input-*,
tool-output-*, data-*) matches the frontend's installed Vercel AI SDK. These
functions live inside the Claude adapter; the platform boundary is the typed
engine emission contract, not this module's frame shapes.

Each adapter maps its native protocol into public frames or closed platform
facts before emitting them. Additive public fields remain open after that
classification.

Assistant Responses SSE mapping:
    response.output_text.delta                        → text-delta
    response.output_item.added (function_call)        → tool-input-start
    response.output_item.done  (function_call)        → tool-input-available
    response.output_item.done  (function_call_output) → tool-output-available
    response.completed                                → result {finishReason: "stop"}
    response.failed                                   → result {finishReason: "error"}
    response.cancelled                                → result {finishReason: "cancelled"}
    response.created                                  → suppressed (start frame already emitted)
    response.in_progress                              → suppressed (informational filler)
    response.output_text.done                         → suppressed (text-delta already streamed)
    response.content_part.added/done                  → suppressed (informational filler)
    response.function_call_arguments.delta/done       → suppressed (output_item carries args)
    response.output_item.* (message)                  → suppressed (text-* covers content)
    response.output_item.added (function_call_output) → suppressed (matching done emits frame)

Claude Code SDK message mapping (translation-shell runner envelope; the
message dicts carry ``__sdk_type`` stamps from the runner's serializer):
    AssistantMessage / TextBlock       → text-start + text-delta + text-end
    AssistantMessage / ThinkingBlock   → reasoning-start + reasoning-delta + reasoning-end
    AssistantMessage / ToolUseBlock    → tool-input-start + tool-input-available
    UserMessage / ToolResultBlock      → tool-output-available / tool-output-error
    UserMessage (root string)          → data-input-consumed
    UserMessage (anything else)        → tool/subagent frames or data-raw-event
    SystemMessage                      → data-raw-event {subtype}
    StreamEvent (live cursor given)    → text/reasoning start+delta+end, tool-input-start/delta
    StreamEvent (no cursor)            → data-raw-event (caller opted out of live translation)
    ResultMessage                      → result {finishReason: "stop"|"error"}
      (``cancelled`` is the EngineClient's call — the SDK reports an interrupt
      as an error subtype and only the client knows it asked for it)

Assistant Runs SSE mapping (legacy diagnostic path):
    message.delta        → text-delta
    reasoning.available  → reasoning-start + reasoning-delta + reasoning-end
    tool.started         → tool-input-start
    tool.input.delta     → tool-input-delta
    tool.input.complete  → tool-input-available
    tool.completed       → tool-output-available / tool-output-error
    approval.request     → interaction.request
    approval.responded   → data-raw-event
    run.completed        → result {finishReason: "stop"}
    run.failed           → result {finishReason: "error"}
    run.cancelled        → result {finishReason: "cancelled"}

Unrecognized semantic events hard-fail when continuing would require guessing.
Diagnostic events are retained privately and never rendered as raw browser
JSON.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

from claude_agent_sdk.types import TERMINAL_TASK_STATUSES

from astrabox.core.service.orchestrator.engine.emissions import public_ui_frame
from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
    canonical_child_run_data,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
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
)
from astrabox.core.service.orchestrator.system_events import api_retry_payload


class UnknownWireEvent(ValueError):
    """Raised when a wire event has no registered translation.

    Per the fixed-enum frame contract, message-stream layer must reject
    unknown event types rather than silently passing them through.
    """


ASSISTANT_CURRENT_RUNS_SSE_EVENT_TYPES = frozenset(
    {
        "approval.request",
        "approval.responded",
        "message.delta",
        "reasoning.available",
        "run.cancelled",
        "run.completed",
        "run.failed",
        "tool.completed",
        "tool.started",
    }
)
ASSISTANT_RUNS_SSE_EVENT_TYPES = ASSISTANT_CURRENT_RUNS_SSE_EVENT_TYPES | frozenset(
    {
        "tool.input.delta",
        "tool.input.complete",
    }
)

ASSISTANT_RESPONSES_SSE_EVENT_TYPES = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.delta",
        "response.output_text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
    }
)

_ASSISTANT_FINISH_MAP: dict[str, str] = {
    "run.completed": "stop",
    "run.failed": "error",
    "run.cancelled": "cancelled",
}
ASSISTANT_TERMINAL_EVENT_TYPES = frozenset(_ASSISTANT_FINISH_MAP.keys())

_ASSISTANT_RESPONSE_FINISH_MAP: dict[str, str] = {
    "response.completed": "stop",
    "response.failed": "error",
    "response.cancelled": "cancelled",
    "response.incomplete": "error",
}
ASSISTANT_RESPONSE_TERMINAL_EVENT_TYPES = frozenset(_ASSISTANT_RESPONSE_FINISH_MAP.keys())


def _string_field(event: dict[str, Any], *names: str) -> str:
    for name in names:
        value = event.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _tool_name(event: dict[str, Any]) -> str:
    return _string_field(event, "tool_name", "toolName", "tool", "name") or "tool"


def _tool_call_id(event: dict[str, Any]) -> str:
    explicit = _string_field(event, "tool_call_id", "toolCallId", "call_id", "id")
    if explicit:
        return explicit
    run_id = _string_field(event, "run_id", "runId")
    tool_name = _tool_name(event)
    if run_id:
        return f"assistant:{run_id}:{tool_name}"
    return ""


def _raw_event_frame(
    event_type: str,
    event: dict[str, Any],
    *,
    family: str = "assistant.runs_sse",
) -> dict[str, Any]:
    return {
        "type": "data-raw-event",
        "data": {
            "event_type": family,
            "subtype": event_type,
            "raw": dict(event),
        },
    }


def _tool_output(event: dict[str, Any]) -> Any:
    for key in ("output", "result", "content", "preview"):
        if key in event:
            return event.get(key)
    metadata = {
        key: event.get(key)
        for key in ("tool", "tool_name", "name", "duration", "error")
        if key in event
    }
    return metadata or ""


def translate_assistant_event(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Translate a single Assistant Gateway SSE event into AI SDK frames.

    Yields zero or more frames. Most events map 1:1; a few wire events
    (e.g., tool batches) may produce multiple frames.

    Raises UnknownWireEvent for unmapped event types — caller must surface
    this rather than swallowing it.
    """
    event_type = str(event.get("type") or event.get("event") or "").strip()
    if not event_type:
        raise UnknownWireEvent(f"assistant event has no type: {event!r}")
    if event_type not in ASSISTANT_RUNS_SSE_EVENT_TYPES:
        raise UnknownWireEvent(
            f"assistant wire event type={event_type!r} has no registered translation; "
            f"add it to translate_assistant_event() — do not relax the gate"
        )

    if event_type == "message.delta":
        text = str(event.get("text") or event.get("delta") or "")
        if text:
            block_id = str(
                event.get("message_id")
                or event.get("id")
                or event.get("run_id")
                or event.get("timestamp")
                or "assistant-message"
            )
            yield {"type": "text-delta", "id": block_id, "delta": text}
        return

    if event_type == "reasoning.available":
        text = str(event.get("text") or event.get("delta") or "")
        if text:
            block_id = str(
                event.get("id")
                or event.get("reasoning_id")
                or event.get("run_id")
                or event.get("timestamp")
                or "assistant-reasoning"
            )
            yield {"type": "reasoning-start", "id": block_id}
            yield {"type": "reasoning-delta", "id": block_id, "delta": text}
            yield {"type": "reasoning-end", "id": block_id}
        return

    if event_type == "tool.started":
        yield public_ui_frame({
            "type": "tool-input-start",
            "toolCallId": _tool_call_id(event),
            "toolName": _tool_name(event),
            # See `tool-input-available` below: without the flag the AI SDK
            # builds a typed part no console reader accepts.
            "dynamic": True,
        })
        return

    if event_type == "tool.input.delta":
        yield {
            "type": "tool-input-delta",
            "toolCallId": _tool_call_id(event),
            "inputTextDelta": str(event.get("delta") or ""),
        }
        return

    if event_type == "tool.input.complete":
        yield public_ui_frame({
            "type": "tool-input-available",
            "toolCallId": _tool_call_id(event),
            "toolName": _tool_name(event),
            "input": event.get("input"),
            # `dynamic: True` is what makes the AI SDK build a `dynamic-tool`
            # part rather than a typed `tool-<name>` one, and every console
            # reader of a tool part accepts only the former — approval-to-card
            # binding and tool state included, which an Assistant has.
            "dynamic": True,
        })
        return

    if event_type == "tool.completed":
        tool_call_id = _tool_call_id(event)
        if event.get("error") is True:
            error_text = str(
                event.get("error_message")
                or event.get("message")
                or _tool_output(event)
                or "tool failed"
            )
            yield {
                "type": "tool-output-error",
                "toolCallId": tool_call_id,
                "errorText": error_text,
            }
            return
        yield {
            "type": "tool-output-available",
            "toolCallId": tool_call_id,
            "output": _tool_output(event),
        }
        return

    if event_type == "approval.request":
        yield {
            "type": "interaction.request",
            "interactionId": str(event.get("interaction_id") or event.get("id") or ""),
            "kind": "approval",
            "payload": event.get("payload")
            or {
                "tool_name": event.get("tool_name"),
                "input": event.get("input"),
                "choices": event.get("choices") or ["once", "always", "deny"],
            },
        }
        return

    if event_type == "approval.responded":
        yield _raw_event_frame(event_type, event)
        return

    if event_type in _ASSISTANT_FINISH_MAP:
        finish_reason = _ASSISTANT_FINISH_MAP[event_type]
        result_frame: dict[str, Any] = {
            "type": "result",
            "finishReason": finish_reason,
        }
        usage = event.get("usage")
        if isinstance(usage, dict):
            result_frame["usage"] = usage
        if finish_reason == "error":
            result_frame["error"] = {
                "code": str(event.get("error_code") or "ENGINE_ERROR"),
                "message": str(event.get("error_message") or event.get("error") or ""),
            }
        yield result_frame
        return


def _response_id(event: dict[str, Any]) -> str:
    direct = _string_field(event, "response_id", "responseId", "id")
    if direct:
        return direct
    response = event.get("response")
    if isinstance(response, dict):
        return _string_field(response, "id")
    return ""


def _response_usage(event: dict[str, Any]) -> dict[str, Any] | None:
    usage = event.get("usage")
    if isinstance(usage, dict):
        return dict(usage)
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        return dict(response["usage"])
    return None


def _response_error(event: dict[str, Any]) -> dict[str, str]:
    raw_error = event.get("error")
    if isinstance(raw_error, dict):
        code = str(raw_error.get("code") or "ENGINE_ERROR")
        message = str(raw_error.get("message") or raw_error.get("error") or "")
        return {"code": code, "message": message}
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("error"), dict):
        raw = response["error"]
        code = str(raw.get("code") or "ENGINE_ERROR")
        message = str(raw.get("message") or raw.get("error") or "")
        return {"code": code, "message": message}
    message = str(raw_error or event.get("message") or "")
    return {"code": "ENGINE_ERROR", "message": message}


def _parse_function_call_arguments(value: Any) -> Any:
    """Decode a Assistant Responses function_call.arguments payload.

    The Responses API encodes arguments as a JSON string (per OpenAI spec).
    Decode so AI SDK ``tool-input-available.input`` matches the dict shape
    consumers expect from the Runs ``tool.input.complete`` path. Empty/null
    arguments collapse to ``{}``;
    invalid JSON is a wire-data integrity bug and surfaces explicitly rather
    than silently degrading the tool card.
    """
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"assistant function_call.arguments is not valid JSON: {value!r}"
            ) from exc
    return value


def _flatten_function_call_output(value: Any) -> str:
    """Flatten a Assistant Responses function_call_output.item.output to a string.

    Assistant emits output as a list of ``{"type": "input_text", "text": ...}``
    blocks (matching the OpenAI Responses content-part shape). The AI SDK
    ``tool-output-available.output`` field is rendered as a string by the
    platform consumer; concatenate the text parts deterministically and reject
    non-text shapes.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if (
                isinstance(entry, dict)
                and entry.get("type") == "input_text"
                and isinstance(entry.get("text"), str)
            ):
                parts.append(entry["text"])
                continue
            raise ValueError(
                f"assistant function_call_output entry has unexpected shape: {entry!r}"
            )
        return "".join(parts)
    raise ValueError(
        f"assistant function_call_output.output has unexpected type: {type(value).__name__}"
    )


def _translate_response_output_item_event(
    event_type: str, event: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    item = event.get("item")
    item_type = ""
    if isinstance(item, dict):
        item_type = str(item.get("type") or "").strip()

    if item_type == "function_call" and isinstance(item, dict):
        tool_call_id = _string_field(item, "call_id", "id")
        tool_name = _string_field(item, "name") or "tool"
        if event_type == "response.output_item.added":
            # ``dynamic: True`` matches the ``dynamic-tool`` UIMessagePart kind
            # the AI SDK client uses when no statically-typed tool schema is
            # registered. The frontend only renders ``case 'dynamic-tool'``;
            # without this flag the live tool card never appears (the user has
            # to refresh and read it from the durable event-derived projection
            # instead). Mirrors the Claude Code engine translator.
            yield public_ui_frame({
                "type": "tool-input-start",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "dynamic": True,
            })
            return
        # response.output_item.done — Assistant packs the full arguments here.
        yield public_ui_frame({
            "type": "tool-input-available",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "input": _parse_function_call_arguments(item.get("arguments")),
            "dynamic": True,
        })
        return

    if item_type == "function_call_output" and isinstance(item, dict):
        if event_type == "response.output_item.done":
            tool_call_id = _string_field(item, "call_id", "id")
            yield {
                "type": "tool-output-available",
                "toolCallId": tool_call_id,
                "output": _flatten_function_call_output(item.get("output")),
                "providerExecuted": True,
            }
            return
        # added: the matching done emits the AI SDK frame; suppress this event
        # to keep the durable AI SDK frame stream free of informational filler
        # that would otherwise render as a noisy card per tool call.
        return

    # message item (or items without a payload) carry no user-visible content
    # beyond what text-* frames already convey. Suppress to avoid noisy cards.
    return


def translate_assistant_response_event(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Translate a Assistant Responses SSE event into AI SDK frames.

    Text, tool lifecycle, and terminal semantics are translated. Other
    registered events (response.created, response.in_progress,
    response.output_text.done, response.content_part.*,
    response.function_call_arguments.*) are informational filler already
    covered by other AI SDK frames (start/text-*/result/finish) and are
    suppressed to keep the durable frame stream free of render-noise.

    The fixed enum still rejects unregistered wire events via
    ``UnknownWireEvent``; suppression here applies only to known
    informational events.
    """
    event_type = str(event.get("type") or event.get("event") or "").strip()
    if not event_type:
        raise UnknownWireEvent(f"assistant response event has no type: {event!r}")
    if event_type not in ASSISTANT_RESPONSES_SSE_EVENT_TYPES:
        raise UnknownWireEvent(
            f"assistant responses event type={event_type!r} has no registered "
            "translation; add it to translate_assistant_response_event()"
        )

    if event_type == "response.output_text.delta":
        delta = str(event.get("delta") or event.get("text") or "")
        if delta:
            block_id = str(
                event.get("item_id")
                or event.get("output_index")
                or _response_id(event)
                or "assistant-response"
            )
            yield {"type": "text-delta", "id": block_id, "delta": delta}
        return

    if event_type in ("response.output_item.added", "response.output_item.done"):
        yield from _translate_response_output_item_event(event_type, event)
        return

    if event_type in _ASSISTANT_RESPONSE_FINISH_MAP:
        finish_reason = _ASSISTANT_RESPONSE_FINISH_MAP[event_type]
        result_frame: dict[str, Any] = {
            "type": "result",
            "finishReason": finish_reason,
        }
        response_id = _response_id(event)
        if response_id:
            result_frame["responseId"] = response_id
        usage = _response_usage(event)
        if usage:
            result_frame["usage"] = usage
        if finish_reason == "error":
            result_frame["error"] = _response_error(event)
        yield result_frame
        return

    # Remaining registered event types are informational filler, suppressed here.
    return


# ── Claude Code SDK messages (translation-shell runner envelope) ────────────

#: Background-task lifecycle notices (subagent started / progress / updated /
#: finished). Translated as raw events, verbatim. The resident runner link also
#: commits these messages to the engine event log before routing them, so a
#: post-Result terminal does not depend on a turn consumer or transcript row.
#: This translator does not invent another lifecycle vocabulary for them.
CLAUDE_SDK_TASK_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "TaskStartedMessage",
        "TaskProgressMessage",
        "TaskUpdatedMessage",
        "TaskNotificationMessage",
    }
)

CLAUDE_SDK_MESSAGE_TYPES: frozenset[str] = (
    frozenset(
        {
            "AssistantMessage",
            "UserMessage",
            "SystemMessage",
            "HookEventMessage",
            "StreamEvent",
            "ResultMessage",
        }
    )
    | CLAUDE_SDK_TASK_MESSAGE_TYPES
)

_CLAUDE_RESULT_SUCCESS_SUBTYPE = "success"


def claude_result_data(message: dict[str, Any]) -> dict[str, Any]:
    """Project the SDK's reader-facing Result metrics.

    Native conversation, control, error and deferred-tool fields remain in the
    adapter's private diagnostic/terminal facts. The result card is an open
    public payload whose contents are metrics the SDK already presents to the
    user, so additive fields inside ``usage`` do not require a core change.
    """

    public: dict[str, Any] = {}
    for key in ("duration_ms", "total_cost_usd", "num_turns", "stop_reason"):
        value = message.get(key)
        if value is not None:
            public[key] = deepcopy(value)
    usage = message.get("usage")
    if isinstance(usage, dict):
        public["usage"] = deepcopy(usage)
    return public


def _claude_raw_frame(subtype: str, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "data-raw-event",
        "data": {
            "event_type": "claude_code.sdk",
            "subtype": subtype,
            "raw": dict(message),
        },
    }


def _claude_message_id(message: dict[str, Any]) -> str:
    nested = message.get("message")
    if isinstance(nested, dict):
        nested_id = str(nested.get("id") or "").strip()
        if nested_id:
            return nested_id
    return str(message.get("message_id") or message.get("id") or "").strip()


def _claude_task_status(message: dict[str, Any], *, subtype: str) -> str:
    """The SDK task status this lifecycle event reports, empty when it reports none.

    ``task_notification`` always carries one: the SDK reads ``data["status"]``
    unconditionally. ``task_updated`` need not. The vendor documents a patch of
    only ``end_time``/``result``/``error`` as a non-terminal update whose status
    is ``None``, and writes its own parser never to raise on a lifecycle event.
    A status is therefore not something the engine promises here, and the
    projection below already reads its absence as a non-terminal update.
    """

    status = str(claude_envelope_value(message, "status") or "").strip()
    if subtype == "task_updated":
        patch = claude_envelope_value(message, "patch")
        if isinstance(patch, dict):
            status = str(patch.get("status") or status).strip()
        return status
    if subtype == "task_notification" and not status:
        raise UnknownWireEvent(f"Claude {subtype} lacks the SDK task status")
    return status


def _translate_claude_subagent_lifecycle(
    message: dict[str, Any],
    *,
    cursor: "ClaudeStreamCursor | None",
) -> dict[str, Any] | None:
    subtype = claude_lifecycle_subtype(message)
    if not subtype:
        return None
    task_id = str(claude_envelope_value(message, "task_id") or "").strip()
    engine_ref = cursor.child_identities.lifecycle_ref(message) if cursor is not None else ""
    if not engine_ref:
        return None

    engine_status = _claude_task_status(message, subtype=subtype)
    event = "opened" if subtype == "task_started" else "updated"
    if engine_status in TERMINAL_TASK_STATUSES:
        event = "closed"
    frame_data: dict[str, Any] = {
        "kind": "lifecycle",
        "engineRef": engine_ref,
        "event": event,
        "engineEvent": subtype,
        "operations": ["stop"] if task_id and event != "closed" else [],
    }
    if engine_status:
        frame_data["engineStatus"] = engine_status
    parent_engine_ref = ""
    if cursor is not None:
        parent_tool_id = claude_lifecycle_parent_child_run_id(message)
        if parent_tool_id:
            parent_engine_ref = cursor.child_identities.message_ref(
                {"parent_tool_use_id": parent_tool_id}
            )
            cursor.child_identities.bind_parent(engine_ref, parent_engine_ref)
        else:
            parent_engine_ref = cursor.child_identities.parent_by_agent.get(engine_ref, "")
    if parent_engine_ref:
        frame_data["parentEngineRef"] = parent_engine_ref
    field_map = {
        "tool_use_id": "toolCallId",
        "description": "description",
        "task_type": "taskType",
        "usage": "usage",
        "last_tool_name": "lastToolName",
        "summary": "summary",
    }
    for source_key, target_key in field_map.items():
        value = claude_envelope_value(message, source_key)
        if isinstance(value, dict):
            frame_data[target_key] = dict(value)
        elif isinstance(value, str) and value:
            frame_data[target_key] = value
    if task_id:
        frame_data["controlRef"] = task_id
    frame_data = canonical_child_run_data(
        frame_data,
        engine_kind=CLAUDE_CODE_ENGINE_KIND,
    )
    uuid_value = str(claude_envelope_value(message, "uuid") or "").strip()
    frame_id = (
        f"subagent:lifecycle:{uuid_value}"
        if uuid_value
        else f"subagent:lifecycle:{engine_ref}:{subtype}"
    )
    return session_scoped_engine_frame(
        {"type": "data-subagent", "id": frame_id, "data": frame_data}
    )


def _translate_claude_subagent_message(
    message: dict[str, Any],
    *,
    cursor: "ClaudeStreamCursor | None",
) -> dict[str, Any] | None:
    if not claude_child_context_id(message):
        return None
    role = claude_message_role(message)
    if role not in {"assistant", "user"}:
        return None
    content = claude_content_blocks(message)
    if not content:
        return None
    if cursor is None:
        raise ChildRunProjectionError("Claude child messages require native identity context")
    engine_ref = cursor.child_identities.message_ref(message)
    frame_data: dict[str, Any] = {
        "kind": "message",
        "engineRef": engine_ref,
        "role": role,
        "content": content,
    }
    if cursor is not None:
        parent_engine_ref = cursor.child_identities.parent_by_agent.get(engine_ref, "")
        if parent_engine_ref:
            frame_data["parentEngineRef"] = parent_engine_ref
    message_id = _claude_message_id(message)
    if message_id:
        frame_data["messageId"] = message_id
    frame_data = canonical_child_run_data(
        frame_data,
        engine_kind=CLAUDE_CODE_ENGINE_KIND,
    )
    uuid_value = str(message.get("uuid") or "").strip()
    frame_id = (
        f"subagent:msg:{uuid_value}" if uuid_value else f"subagent:msg:{message_id or engine_ref}"
    )
    return session_scoped_engine_frame(
        {"type": "data-subagent", "id": frame_id, "data": frame_data}
    )


def _translate_claude_assistant_blocks(
    message: dict[str, Any],
    envelope_seq: int,
    cursor: "ClaudeStreamCursor | None" = None,
) -> Iterator[dict[str, Any]]:
    blocks = message.get("content")
    if not isinstance(blocks, list):
        raise UnknownWireEvent(f"claude AssistantMessage content is not a block list: {message!r}")
    live = cursor is not None and cursor.content_was_streamed(message)
    for index, block in enumerate(blocks):
        block_type = str((block or {}).get("__sdk_type") or "")
        if live and block_type in ("TextBlock", "ThinkingBlock"):
            # Already streamed token-by-token via StreamEvents; re-emitting the
            # complete block would double-render it.
            continue
        if block_type == "TextBlock":
            text = str(block.get("text") or "")
            block_id = f"claude-text:{envelope_seq}:{index}"
            if cursor is not None and text:
                cursor.emitted_text = True
            yield {"type": "text-start", "id": block_id}
            if text:
                yield {"type": "text-delta", "id": block_id, "delta": text}
            yield {"type": "text-end", "id": block_id}
        elif block_type == "ThinkingBlock":
            text = str(block.get("thinking") or "")
            block_id = f"claude-reasoning:{envelope_seq}:{index}"
            yield {"type": "reasoning-start", "id": block_id}
            if text:
                yield {"type": "reasoning-delta", "id": block_id, "delta": text}
            yield {"type": "reasoning-end", "id": block_id}
        elif block_type == "ToolUseBlock":
            tool_call_id = str(block.get("id") or f"claude-tool:{envelope_seq}:{index}")
            tool_name = str(block.get("name") or "tool")
            if not (cursor is not None and tool_call_id in cursor.started_tool_ids):
                yield public_ui_frame({
                    "type": "tool-input-start",
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "dynamic": True,
                })
            yield public_ui_frame({
                "type": "tool-input-available",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "dynamic": True,
                "input": block.get("input"),
            })
        else:
            raise UnknownWireEvent(
                f"claude assistant content block type={block_type!r} has no "
                f"registered translation; add it to "
                f"translate_claude_sdk_message() — do not relax the gate"
            )


def _translate_claude_user_message(message: dict[str, Any]) -> Iterator[dict[str, Any]]:
    from astrabox.core.service.orchestrator.engine.claude_file_changes import claude_file_changes

    content = message.get("content")
    if not isinstance(content, list):
        if not isinstance(content, str):
            raise UnknownWireEvent("root UserMessage content must be a string")
        input_id = str(message.get("uuid") or "").strip()
        response_id = input_response_message_id(input_id)
        yield {
            "type": "data-input-consumed",
            "id": f"input-consumed:{input_id}",
            "transient": True,
            "data": {
                "inputId": input_id,
                "responseMessageId": response_id,
                "content": content,
            },
        }
        return
    emitted = False
    for block in content:
        block_type = str((block or {}).get("__sdk_type") or (block or {}).get("type") or "")
        if block_type in ("ToolResultBlock", "tool_result"):
            tool_call_id = str(block.get("tool_use_id") or "")
            if block.get("is_error"):
                yield {
                    "type": "tool-output-error",
                    "toolCallId": tool_call_id,
                    "errorText": str(block.get("content") or "tool failed"),
                }
            else:
                yield {
                    "type": "tool-output-available",
                    "toolCallId": tool_call_id,
                    "output": block.get("content"),
                }
                result = message.get("tool_use_result")
                if isinstance(result, dict):
                    change_frame = claude_file_changes(tool_call_id, result)
                    if change_frame is not None:
                        yield change_frame
            emitted = True
    if not emitted:
        yield _claude_raw_frame("user_echo", message)


def translate_claude_sdk_message(
    message: dict[str, Any],
    *,
    envelope_seq: int,
    cursor: "ClaudeStreamCursor | None" = None,
) -> Iterator[dict[str, Any]]:
    """Translate one runner-enveloped Claude SDK message into AI SDK frames.

    ``envelope_seq`` (the runner frame's monotonic seq) seeds deterministic
    block ids — the SDK message itself carries none, and replaying the same
    envelope must yield the same ids. Raises UnknownWireEvent for unmapped
    message or block types — callers surface it, never swallow it.
    """
    sdk_type = str(message.get("__sdk_type") or "")
    if sdk_type not in CLAUDE_SDK_MESSAGE_TYPES:
        raise UnknownWireEvent(
            f"claude sdk message type={sdk_type!r} has no registered translation; "
            f"add it to translate_claude_sdk_message() — do not relax the gate"
        )

    if cursor is None:
        cursor = ClaudeStreamCursor()
    cursor.child_identities.observe(message)

    lifecycle_frame = _translate_claude_subagent_lifecycle(
        message,
        cursor=cursor,
    )
    if lifecycle_frame is not None:
        yield lifecycle_frame
        return

    if claude_child_context_id(message):
        subagent_frame = _translate_claude_subagent_message(
            message,
            cursor=cursor,
        )
        if subagent_frame is not None:
            yield subagent_frame
        # Child stream deltas are followed by a complete child message. Never
        # bleed them into the parent text lane.
        return

    if sdk_type == "AssistantMessage":
        if message.get("error") is not None:
            yield _claude_raw_frame("assistant_message_error", message)
        # When the content streamed, its step boundary already arrived with the
        # SDK's own message_start/message_stop. Without partial messages there
        # are no stream events at all, and this complete message is the step —
        # so the boundary is emitted here instead. Either way a turn's frames
        # carry the engine's message boundaries; neither configuration leaves a
        # flat run for a later reader to re-segment.
        streamed = cursor is not None and cursor.message_was_started(message)
        if not streamed:
            if cursor is not None:
                cursor.stream_message_id = ""
                cursor.streamed_text = False
            yield {"type": "start-step"}
        yield from _translate_claude_assistant_blocks(message, envelope_seq, cursor)
        if not streamed:
            yield {"type": "finish-step"}
        return

    if sdk_type == "UserMessage":
        yield from _translate_claude_user_message(message)
        return

    if sdk_type in {"SystemMessage", "HookEventMessage"}:
        # thinking_tokens is a per-chunk count with no reader; the result usage
        # carries the same information. Suppress it before durable publication.
        # `init` remains because extract_agent_session_metadata reads it.
        subtype = str(message.get("subtype") or "").strip().lower()
        if subtype == "thinking_tokens":
            return
        retry = api_retry_payload(message)
        if retry is not None:
            yield {
                "type": "data-api-retry",
                "id": "api-retry",
                "data": retry,
            }
            return
        yield _claude_raw_frame(str(message.get("subtype") or "system"), message)
        return

    if sdk_type in CLAUDE_SDK_TASK_MESSAGE_TYPES:
        yield _claude_raw_frame(str(message.get("subtype") or sdk_type), message)
        return

    if sdk_type == "StreamEvent":
        if cursor is not None:
            yield from translate_claude_stream_event(message, cursor=cursor)
        else:
            yield _claude_raw_frame("stream_event", message)
        return

    # ResultMessage — the SDK's whole-cycle terminal. A successful CLI run can
    # contain its only human-readable answer in ``result``. The engine-owned
    # translator projects that text only when no assistant text crossed the
    # seam, keeping live and reconnect translation in one vocabulary.
    subtype = str(message.get("subtype") or "")
    result_text = str(message.get("result") or "")
    # ``is_error`` is the SDK's own verdict on the run, and it does not always
    # agree with ``subtype``. A model endpoint that refuses the request ends the
    # CLI's ten-attempt retry ladder with subtype="success", is_error=true,
    # terminal_reason="api_error" and api_error_status=401; reading the subtype
    # alone would file a turn killed by a bad credential as a normal stop and
    # drop the gateway's account of it ("Failed to authenticate. API Error:
    # 401 …") from the turn's failure. Where the engine states a verdict, the
    # verdict is the engine's; the subtype answers only when it states none.
    succeeded = subtype == _CLAUDE_RESULT_SUCCESS_SUBTYPE and message.get("is_error") is not True
    if succeeded and cursor is not None and not cursor.emitted_text and result_text:
        block_id = f"claude-result:{envelope_seq}"
        cursor.emitted_text = True
        yield {"type": "text-start", "id": block_id}
        yield {"type": "text-delta", "id": block_id, "delta": result_text}
        yield {"type": "text-end", "id": block_id}
    result_frame: dict[str, Any] = {
        "type": "result",
        "finishReason": "stop" if succeeded else "error",
    }
    terminal_reason = str(message.get("terminal_reason") or "").strip()
    if terminal_reason:
        result_frame["__engine_terminal_reason"] = terminal_reason
    if isinstance(message.get("deferred_tool_use"), dict):
        # The SDK stopped this run at a deferred tool boundary. The adapter
        # owns that meaning and declares the platform action directly; core
        # never inspects the vendor's deferred_tool_use object.
        result_frame["__interaction_closed"] = True
    usage = message.get("usage")
    if isinstance(usage, dict):
        result_frame["usage"] = usage
    # ``finishReason`` belongs to the platform frame contract. Remaining fields
    # keep the SDK's names as private terminal evidence; the typed emission seam
    # stores them without making core or the browser interpret their vocabulary.
    for verbatim_key in (
        "terminal_reason",
        "num_turns",
        "total_cost_usd",
        "api_error_status",
        "permission_denials",
        "deferred_tool_use",
        "model_usage",
    ):
        value = message.get(verbatim_key)
        if value is not None:
            result_frame[verbatim_key] = value
    if result_frame["finishReason"] == "error":
        # The code is whichever name the engine gave the failure. `subtype`
        # names it when the run ended badly on its own terms
        # (error_during_execution, error_max_turns); when the run ended on
        # something upstream it stays "success" and `terminal_reason` is the
        # one that says what happened (api_error). Both are the vendor's words,
        # carried as spelled — a deployment can point at any gateway, so a
        # platform-defined vocabulary here would rename the engine's verdict.
        code = subtype if subtype != _CLAUDE_RESULT_SUCCESS_SUBTYPE else ""
        result_frame["error"] = {
            "code": code or str(message.get("terminal_reason") or "") or "ENGINE_ERROR",
            "message": result_text,
        }
    yield result_frame


# ── Claude Code live stream (StreamEvent → token-level AI SDK frames) ───────────
#
# Three projection constraints, each of which renders a visibly wrong
# transcript when violated: every contiguous text run gets its own id (the AI
# SDK collapses same-id text parts, so tool cards would all render after one
# merged blob); block boundaries come from the API's content_block indexes,
# which is also what closes text before tool frames; and a delta whose start
# was never seen (reattach after a gap) self-heals by synthesizing the start,
# because dropping it loses the run entirely.

_LIVE_BLOCK_KINDS = frozenset({"text", "thinking", "tool_use"})


class ClaudeStreamCursor:
    """Per-turn, in-memory live-stream state (the engine client owns one per
    turn; rebuilt from journaled frame metadata on reconnect). Tracks open blocks
    by ``index`` — the Anthropic stream's own block identity — and which tool calls already
    streamed.

    Blocks are keyed by the API content-block ``index``. The CLI ``uuid`` names
    an individual stream event, so it cannot correlate starts, deltas, and stops
    across events."""

    def __init__(self, *, child_identities: ClaudeChildIdentities | None = None) -> None:
        self.blocks: dict[int, dict[str, str]] = {}
        self.streamed_text = False
        self.stream_message_id = ""
        self.emitted_text = False
        self.started_tool_ids: set[str] = set()
        self.child_identities = (
            child_identities if child_identities is not None else ClaudeChildIdentities()
        )

    def message_was_started(self, message: dict[str, Any]) -> bool:
        """Whether this complete block belongs to an observed native step."""
        return bool(
            self.stream_message_id
            and _claude_message_id(message) == self.stream_message_id
            and message.get("error") is None
        )

    def content_was_streamed(self, message: dict[str, Any]) -> bool:
        """Deduplicate only the native message whose partial content was observed.

        Provider error messages remain visible even when the SDK attributes them
        to the interrupted message. Event UUIDs identify individual envelopes,
        so only the SDK's native message ID can establish content duplication.
        """
        return self.streamed_text and self.message_was_started(message)

    def register(self, index: int, kind: str, block_id: str, tool_call_id: str = "") -> None:
        self.blocks[index] = {
            "kind": kind,
            "id": block_id,
            "tool_call_id": tool_call_id,
        }


def _live_block_id(uuid: str, index: int) -> str:
    return f"claude-live:{uuid}:{index}"


#: Private field a lane-opening live frame carries so a durable row still
#: names the API content-block index it translated. ``pop`` it off the row
#: into ``engine_block_index`` when journaling; never send it to a browser.
_BLOCK_INDEX_FIELD = "__engine_block_index"
BLOCK_INDEX_FIELD = _BLOCK_INDEX_FIELD
_LIVE_BLOCK_ID_PREFIX = "claude-live:"
# Retained inside journal payloads; the public-frame projection strips private fields.
_NATIVE_MESSAGE_ID_FIELD = "__claude_stream_message_id"


def _committed_block_index(row: dict[str, Any], payload: dict[str, Any]) -> int | None:
    for candidate in (row.get("engine_block_index"), payload.get(_BLOCK_INDEX_FIELD)):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    return None


def rebuild_claude_stream_cursor(
    committed_frames: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    child_identities: ClaudeChildIdentities | None = None,
) -> ClaudeStreamCursor:
    """Resume the live translator from the platform's journal of one response.

    ``committed_frames`` are this adapter's own translated rows for the
    response, in journal order — the acknowledged output authority, not the
    runner's retained prefix, which may already be compacted. Three things a
    suffix delta depends on are read back: whether text streamed (a complete
    AssistantMessage must not re-render it), which tool calls already opened
    (``tool-input-start`` must not repeat), and which content blocks are still
    open by API index, so a ``content_block_delta`` after the rebuild lands on
    the block id the platform already holds instead of minting a new one.
    """

    cursor = ClaudeStreamCursor(child_identities=child_identities)
    open_lanes: dict[int, dict[str, str]] = {}

    def _close_by(field: str, value: str) -> None:
        for index, lane in list(open_lanes.items()):
            if lane[field] == value:
                open_lanes.pop(index, None)

    for row in committed_frames:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        frame_type = str(payload.get("type") or "").strip()
        index = _committed_block_index(row, payload)
        if frame_type == "start-step":
            cursor.stream_message_id = str(payload.get(_NATIVE_MESSAGE_ID_FIELD) or "")
            cursor.streamed_text = False
        elif frame_type in ("text-start", "reasoning-start"):
            block_id = str(payload.get("id") or "").strip()
            if block_id.startswith(_LIVE_BLOCK_ID_PREFIX):
                cursor.streamed_text = True
            if frame_type == "text-start":
                cursor.emitted_text = True
            if index is not None and block_id.startswith(_LIVE_BLOCK_ID_PREFIX):
                open_lanes[index] = {
                    "kind": "text" if frame_type == "text-start" else "thinking",
                    "id": block_id,
                    "tool_call_id": "",
                }
        elif frame_type in ("text-end", "reasoning-end"):
            _close_by("id", str(payload.get("id") or "").strip())
        elif frame_type == "tool-input-start":
            tool_call_id = str(payload.get("toolCallId") or "").strip()
            if tool_call_id:
                cursor.started_tool_ids.add(tool_call_id)
            if index is not None and tool_call_id:
                open_lanes[index] = {
                    "kind": "tool_use",
                    "id": tool_call_id,
                    "tool_call_id": tool_call_id,
                }
        elif frame_type == "tool-input-available":
            _close_by("tool_call_id", str(payload.get("toolCallId") or "").strip())
        elif frame_type in ("finish-step", "finish", "error"):
            open_lanes.clear()
    for index, lane in open_lanes.items():
        cursor.register(index, lane["kind"], lane["id"], lane["tool_call_id"])
    return cursor


def translate_claude_stream_event(
    message: dict[str, Any], *, cursor: ClaudeStreamCursor
) -> Iterator[dict[str, Any]]:
    """One runner-enveloped ``StreamEvent`` → zero or more live AI SDK frames.

    Child-run stream events (``parent_tool_use_id`` set) are retained as
    Session-scoped diagnostics. Complete child messages arrive through typed
    envelopes; allowing partial child deltas into the root turn would give one
    text lane two owners and corrupt both transcripts.
    """
    if message.get("parent_tool_use_id"):
        yield session_scoped_engine_frame(_claude_raw_frame("subagent_stream_event", message))
        return
    uuid = str(message.get("uuid") or "")
    event = message.get("event") or {}
    event_type = str(event.get("type") or "")

    if event_type == "content_block_start":
        index = int(event.get("index") or 0)
        block = event.get("content_block") or {}
        kind = str(block.get("type") or "")
        superseded = cursor.blocks.get(index)
        if superseded is not None:
            # A start over an open lane means its stop was lost (indexes are
            # reused only across messages, and a stop precedes reuse). Close
            # the translator's lane before opening the new one — an unclosed
            # lane renders as reasoning/text that never ends.
            if superseded["kind"] == "text":
                yield {"type": "text-end", "id": superseded["id"]}
            elif superseded["kind"] == "thinking":
                yield {"type": "reasoning-end", "id": superseded["id"]}
        # The API's content-block index rides on the lane-opening frame as a
        # private field. The public wire and the message projection strip
        # ``__``-prefixed keys; the durable row keeps it, and it is what lets
        # a translator rebuilt from the platform's journal address a delta
        # for a block that opened before the rebuild
        # (``rebuild_claude_stream_cursor``).
        if kind == "text":
            block_id = _live_block_id(uuid, index)
            cursor.register(index, "text", block_id)
            cursor.streamed_text = True
            cursor.emitted_text = True
            yield {"type": "text-start", "id": block_id, _BLOCK_INDEX_FIELD: index}
        elif kind == "thinking":
            block_id = _live_block_id(uuid, index)
            cursor.register(index, "thinking", block_id)
            cursor.streamed_text = True
            yield {"type": "reasoning-start", "id": block_id, _BLOCK_INDEX_FIELD: index}
        elif kind == "tool_use":
            tool_call_id = str(block.get("id") or _live_block_id(uuid, index))
            cursor.register(index, "tool_use", _live_block_id(uuid, index), tool_call_id)
            cursor.started_tool_ids.add(tool_call_id)
            yield public_ui_frame({
                "type": "tool-input-start",
                "toolCallId": tool_call_id,
                "toolName": str(block.get("name") or "tool"),
                # Engine-side tools are absent from the client's typed tool set,
                # so the AI SDK dynamic flag makes the reducer create a renderable
                # dynamic tool part rather than a static `tool-<name>` part.
                "dynamic": True,
                _BLOCK_INDEX_FIELD: index,
            })
        else:
            # Unknown live block kinds surface, they don't crash a live turn.
            yield _claude_raw_frame(f"live_block:{kind}", message)
        return

    if event_type == "content_block_delta":
        index = int(event.get("index") or 0)
        delta = event.get("delta") or {}
        delta_type = str(delta.get("type") or "")
        state = cursor.blocks.get(index)
        if state is None and delta_type in ("text_delta", "thinking_delta", "input_json_delta"):
            # Start lost (gap/reattach): synthesize it, never drop the delta.
            # Keyed by index this fires once per genuinely-lost block — the
            # synthesized lane registers and the block's later deltas find it.
            synth_kind = {
                "text_delta": "text",
                "thinking_delta": "thinking",
                "input_json_delta": "tool_use",
            }[delta_type]
            synth = {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": synth_kind},
            }
            yield from translate_claude_stream_event({"uuid": uuid, "event": synth}, cursor=cursor)
            state = cursor.blocks[index]
        if delta_type == "text_delta" and state is not None:
            yield {"type": "text-delta", "id": state["id"], "delta": str(delta.get("text") or "")}
        elif delta_type == "thinking_delta" and state is not None:
            yield {
                "type": "reasoning-delta",
                "id": state["id"],
                "delta": str(delta.get("thinking") or ""),
            }
        elif delta_type == "input_json_delta" and state is not None:
            yield {
                "type": "tool-input-delta",
                "toolCallId": state["tool_call_id"] or state["id"],
                "inputTextDelta": str(delta.get("partial_json") or ""),
            }
        # signature_delta and friends are internal bookkeeping: suppressed.
        return

    if event_type == "content_block_stop":
        index = int(event.get("index") or 0)
        state = cursor.blocks.pop(index, None)
        if state is None:
            return
        if state["kind"] == "text":
            yield {"type": "text-end", "id": state["id"]}
        elif state["kind"] == "thinking":
            yield {"type": "reasoning-end", "id": state["id"]}
        # tool_use: tool-input-available comes from the complete
        # AssistantMessage block, which carries the parsed full input.
        return

    if event_type == "message_start":
        # The engine's own message boundary, carried as the AI SDK's step
        # boundary. A platform turn is one UI message and the engine answers it
        # in several of its own — think, call a tool, read the result, think
        # again. Both levels exist in the stream vocabulary, and dropping this
        # one leaves the turn a flat run of blocks that any later reader has to
        # re-segment from content.
        cursor.stream_message_id = _claude_message_id(event)
        cursor.streamed_text = False
        yield {
            "type": "start-step",
            _NATIVE_MESSAGE_ID_FIELD: cursor.stream_message_id,
        }
        return

    if event_type == "message_stop":
        yield {"type": "finish-step"}
        return

    # message_delta / ping — informational filler at this layer; usage lands
    # with the ResultMessage.
    return
