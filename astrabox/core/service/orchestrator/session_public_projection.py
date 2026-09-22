"""Explicit public projections for Session and transcript read APIs.

Persistence rows, runtime bindings, and engine events contain server-only
locators.  Public responses are constructed from allowlists here instead of
copying an internal document and trying to remember every field that must be
removed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from astrabox.core.service.orchestrator.engine.interaction_contract import (
    InteractionContractError,
    public_interaction_view,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    PublicEngineFrameError,
    public_engine_frame_payload,
)


class PublicProjectionError(RuntimeError):
    """An internal record has no declared public representation."""


_OWNER_SESSION_FIELDS = frozenset(
    {
        "session_id",
        "user_id",
        "template_name",
        "state",
        "permission_mode",
        "model_name",
        "sandbox_id",
        "terminal_cwd",
        "title",
        "source_type",
        "agent_id",
        "deployment_name",
        "session_kind",
        "engine_kind",
        "engine_available",
        "expires_at",
        "created_at",
        "updated_at",
        "deleted",
        "runtime_unavailable",
        "runtime_warning",
        "last_error",
        "startup_progress",
        "current_turn_id",
        "last_turn_id",
        "last_turn_status",
        "last_turn_error",
        "last_turn_command_id",
        "delivery_state",
        "last_turn_failure_phase",
        "last_turn_terminal_reason",
        "recovery_policy",
        "recovery_reason",
        "slash_commands",
        "slash_command_details",
        "workspace_panels",
        "engine_capabilities",
    }
)

_AGENT_RUNTIME_FIELDS = frozenset(
    {
        "agent_id",
        "state",
        "sandbox_id",
        "expires_at",
        "startup_progress",
        "last_error",
        "runtime_unavailable",
    }
)

_RUNTIME_BINDING_FIELDS = frozenset(
    {
        "state",
        "can_dispatch",
        "reason_code",
        "reason_message",
    }
)

_BACKGROUND_TASK_FIELDS = frozenset(
    {
        "state",
        "pending_manifest_count",
        "pending_task_count",
        "opened_event_seq",
        "source_turn_id",
    }
)

_DELIVERY_FAILURE_FIELDS = frozenset(
    {"turn_id", "client_message_id", "text", "summary"}
)

_PENDING_INPUT_FIELDS = frozenset(
    {
        "command_id",
        "input_id",
        "client_message_id",
        "content",
        "sequence",
        "status",
    }
)

_MESSAGE_FIELDS = frozenset(
    {
        "message_id",
        "turn_id",
        "client_message_id",
        "role",
        "content",
        "created_at",
        # Present only on a history-blocks read; a plain message page never
        # carries it, so that response is unchanged by this field existing.
        "history_block_id",
    }
)

_BLOCK_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset({"type", "text"}),
    "image": frozenset({"type", "source"}),
    "thinking": frozenset({"type", "thinking"}),
    "tool_use": frozenset({"type", "id", "name", "input"}),
    "tool_result": frozenset(
        {"type", "tool_use_id", "content", "is_error", "tool_result_state"}
    ),
    "result": frozenset(
        {
            "type",
            "result",
            "duration_ms",
            "duration_api_ms",
            "total_cost_usd",
            "num_turns",
            "stop_reason",
            "usage",
        }
    ),
    "turn_failure": frozenset({"type", "error", "failure_phase"}),
    "api_retry": frozenset(
        {"type", "id", "attempt", "max_retries", "error_status", "error"}
    ),
    # A folded-work header. ``process_details`` is copied whole because every
    # member of it is platform vocabulary this package authored — the block id,
    # the checkpoint cursor to reopen it with, the tool count and the label.
    "process_block": frozenset({"type", "process_details"}),
}


def _copy_fields(source: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {key: deepcopy(source[key]) for key in fields if key in source}


def project_public_pending_interaction(
    value: Any, *, include_tool_input: bool = True
) -> dict[str, Any] | None:
    """Return only the browser-facing half of an interaction contract.

    Adapter-authored transcript replies, native answer keys, snapshot
    watermarks, and settlement records stay server-side.  The tool input is
    intentionally visible because the approval UI shows exactly what the user
    is being asked to allow.
    """

    try:
        return public_interaction_view(
            value,
            include_tool_input=include_tool_input,
        )
    except InteractionContractError as exc:
        raise PublicProjectionError(str(exc)) from exc


def project_public_message_block(value: Any) -> dict[str, Any] | None:
    """Project one transcript block; raw vendor envelopes are diagnostic-only."""

    if not isinstance(value, dict):
        raise PublicProjectionError("message block must be an object")
    block_type = str(value.get("type") or "").strip()
    if block_type == "raw_event":
        return None
    if block_type == "ui_data":
        part = value.get("part")
        if not isinstance(part, dict):
            raise PublicProjectionError("ui_data part must be an object")
        part_type = str(part.get("type") or "").strip()
        if not part_type.startswith("data-") or "data" not in part:
            raise PublicProjectionError("ui_data requires a data-* part with data")
        part_id = part.get("id")
        if part_id is not None and not isinstance(part_id, str):
            raise PublicProjectionError("ui_data part id must be a string")
        try:
            public_part = public_engine_frame_payload(
                part,
                frame_seq=None,
                scope="turn",
            )
        except PublicEngineFrameError as exc:
            raise PublicProjectionError(str(exc)) from exc
        if public_part is None:
            raise PublicProjectionError(f"ui_data part {part_type!r} is not public")
        return {"type": "ui_data", "part": public_part}
    fields = _BLOCK_FIELDS.get(block_type)
    if fields is None:
        raise PublicProjectionError(
            f"message block has no public projection: {block_type!r}"
        )
    public = _copy_fields(value, fields)
    if block_type == "image":
        source = public.get("source")
        if not isinstance(source, dict):
            raise PublicProjectionError("image source must be an object")
        public["source"] = _copy_fields(
            source,
            frozenset({"type", "media_type", "data"}),
        )
    if block_type == "result" and "usage" in public:
        usage = public.get("usage")
        if not isinstance(usage, dict):
            raise PublicProjectionError("result usage must be an object")
        public["usage"] = _copy_fields(
            usage,
            frozenset({"input_tokens", "output_tokens"}),
        )
    return public


def project_public_message(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PublicProjectionError("message must be an object")
    public = _copy_fields(value, _MESSAGE_FIELDS)
    blocks: list[dict[str, Any]] = []
    for block in value.get("blocks") or []:
        projected = project_public_message_block(block)
        if projected is not None:
            blocks.append(projected)
    public["blocks"] = blocks
    return public


def _project_active_turn_overlay(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PublicProjectionError("active turn overlay must be an object")
    public = _copy_fields(value, frozenset({"turn_id", "resume_cursor"}))
    message = value.get("message")
    if isinstance(message, dict):
        public["message"] = project_public_message(message)
    messages = value.get("messages")
    if isinstance(messages, list):
        public["messages"] = [project_public_message(item) for item in messages]
    return public


def project_owner_session(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PublicProjectionError("session must be an object")
    public = _copy_fields(value, _OWNER_SESSION_FIELDS)
    if not str(public.get("session_id") or "").strip():
        raise PublicProjectionError("session_id is required for a public session")

    agent_runtime = value.get("agent_runtime")
    if isinstance(agent_runtime, dict):
        public["agent_runtime"] = _copy_fields(agent_runtime, _AGENT_RUNTIME_FIELDS)
    runtime_binding = value.get("runtime_binding")
    if isinstance(runtime_binding, dict):
        public["runtime_binding"] = _copy_fields(
            runtime_binding, _RUNTIME_BINDING_FIELDS
        )
    background = value.get("background_task_state")
    if isinstance(background, dict):
        public["background_task_state"] = _copy_fields(
            background, _BACKGROUND_TASK_FIELDS
        )
    failure = value.get("delivery_failure")
    if isinstance(failure, dict):
        public["delivery_failure"] = _copy_fields(failure, _DELIVERY_FAILURE_FIELDS)
    elif "delivery_failure" in value:
        public["delivery_failure"] = None
    if "pending_interaction" in value:
        public["pending_interaction"] = project_public_pending_interaction(
            value.get("pending_interaction")
        )
    pending_inputs = value.get("pending_inputs")
    if isinstance(pending_inputs, list):
        public["pending_inputs"] = [
            _copy_fields(row, _PENDING_INPUT_FIELDS)
            for row in pending_inputs
            if isinstance(row, dict)
        ]
    return public


def project_shared_session(
    value: Any, *, allow_download: bool
) -> dict[str, Any]:
    owner = project_owner_session(value)
    shared = _copy_fields(
        owner,
        frozenset(
            {
                "title",
                "delivery_state",
                "delivery_failure",
            }
        ),
    )
    shared["pending_interaction"] = project_public_pending_interaction(
        owner.get("pending_interaction"),
        include_tool_input=False,
    )
    shared["share_allow_download"] = bool(allow_download)
    return shared


def project_public_message_page(value: Any, *, shared: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PublicProjectionError("message page must be an object")
    public: dict[str, Any] = {
        "messages": [
            project_public_message(message) for message in value.get("messages") or []
        ],
        "has_more": bool(value.get("has_more")),
        "active_turn_overlay": _project_active_turn_overlay(
            value.get("active_turn_overlay")
        ),
        "pending_interaction": project_public_pending_interaction(
            value.get("pending_interaction"),
            include_tool_input=not shared,
        ),
    }
    if not shared:
        public["session_frame_seq"] = value.get("session_frame_seq")
    # Block paging carries three more members. They are added only when the
    # page was produced by a history-blocks read, so a message page keeps the
    # exact shape it has always had.
    for key in ("paging_mode", "next_cursor", "block_count"):
        if key in value:
            public[key] = value[key]
    return public
