"""Ownership carried by every translated engine frame.

A turn-scoped frame contributes to one root assistant message. A session-scoped
frame updates an engine-owned child run whose lifetime may cross root turns.
The adapter declares the distinction; orchestration carries it without
reinterpreting provider vocabulary.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, cast

from astrabox.core.service.orchestrator.engine.interaction_contract import (
    public_interaction_view,
)

EngineFrameScope = Literal["turn", "session"]

_FRAME_SCOPE_FIELD = "__engine_frame_scope"
_PUBLIC_UI_FRAME_FIELD = "__engine_public_ui"
_VALID_FRAME_SCOPES = frozenset({"turn", "session"})


class PublicEngineFrameError(RuntimeError):
    """A turn frame has no complete browser-facing projection."""


def mark_engine_public_ui_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """Preserve an adapter's public-frame declaration in the durable journal."""

    marked = dict(frame)
    marked[_PUBLIC_UI_FRAME_FIELD] = True
    return marked


def is_engine_public_ui_frame(frame: dict[str, Any]) -> bool:
    """Whether a durable frame came from the adapter's public UI lane."""

    return frame.get(_PUBLIC_UI_FRAME_FIELD) is True


def _project_turn_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    frame_type = str(frame.get("type") or "").strip()
    if not frame_type:
        raise PublicEngineFrameError("engine frame type is required")
    if frame_type == "data-raw-event":
        return None
    public = {
        key: deepcopy(value)
        for key, value in frame.items()
        if not str(key).startswith("__")
    }
    if frame_type != "data-interaction":
        return public
    data = public.get("data")
    if not isinstance(data, dict):
        raise PublicEngineFrameError("data-interaction has no data object")
    projected = public_interaction_view(data, include_tool_input=True)
    if projected is None:
        raise PublicEngineFrameError("data-interaction has no interaction")
    public["data"] = projected
    return public


def session_scoped_engine_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """Mark one adapter-produced data frame as Session-owned and transient.

    The AI SDK delivers transient data parts through ``onData`` without adding
    them to its current assistant message. Durable child-run state is read from
    the platform projection after this change notification arrives.
    """

    scoped = dict(frame)
    scoped[_FRAME_SCOPE_FIELD] = "session"
    scoped["transient"] = True
    return scoped


def engine_frame_scope(frame: dict[str, Any]) -> EngineFrameScope:
    """Return the adapter-declared scope, defaulting ordinary frames to turn."""

    raw_scope = str(frame.get(_FRAME_SCOPE_FIELD) or "turn").strip()
    if raw_scope not in _VALID_FRAME_SCOPES:
        raise RuntimeError(f"engine frame has unsupported scope={raw_scope!r}")
    return cast(EngineFrameScope, raw_scope)


def stored_engine_frame_scope(raw_scope: Any) -> EngineFrameScope:
    """Validate the ownership field read from the durable frame journal."""

    normalized = str(raw_scope or "").strip()
    if normalized not in _VALID_FRAME_SCOPES:
        raise RuntimeError(
            f"stored engine frame has unsupported scope={normalized!r}"
        )
    return cast(EngineFrameScope, normalized)


def pop_engine_frame_scope(frame: dict[str, Any]) -> EngineFrameScope:
    """Consume scope metadata before a frame crosses the public wire."""

    scope = engine_frame_scope(frame)
    frame.pop(_FRAME_SCOPE_FIELD, None)
    return scope


def engine_frame_turn_id(
    frame: dict[str, Any],
    active_turn_id: str | None,
) -> str | None:
    """Bind only turn-owned engine output to the active platform turn."""

    if engine_frame_scope(frame) == "session":
        return None
    return str(active_turn_id or "").strip() or None


def public_engine_frame_payload(
    frame: dict[str, Any],
    *,
    frame_seq: int | None,
    scope: EngineFrameScope,
) -> dict[str, Any] | None:
    """Publish projected Session messages and invalidate private child facts.

    A durable Session frame is a read model input and may hold adapter-authored
    control facts. The browser only needs a monotonic invalidation after that
    frame is durable. Session message payloads are already public read-model
    records; native child-run facts still cross only as invalidations.
    """

    if scope == "turn":
        return _project_turn_frame(frame)
    if (
        not isinstance(frame_seq, int)
        or isinstance(frame_seq, bool)
        or frame_seq < 0
    ):
        raise RuntimeError("child-run invalidation requires a durable frame sequence")
    if frame.get("type") == "data-session-message":
        return {
            "type": "data-session-message",
            "id": frame["id"],
            "transient": True,
            "data": deepcopy(frame["data"]),
        }
    return {
        "type": "data-child-runs-changed",
        "transient": True,
        "data": {"frameSeq": frame_seq},
    }
