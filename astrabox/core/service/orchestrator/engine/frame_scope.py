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


def mark_engine_public_ui_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """Preserve an adapter's public-frame declaration in the durable journal."""

    marked = dict(frame)
    marked[_PUBLIC_UI_FRAME_FIELD] = True
    return marked


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


def pop_engine_frame_scope(frame: dict[str, Any]) -> EngineFrameScope:
    """Consume scope metadata before a frame crosses the public wire."""

    scope = engine_frame_scope(frame)
    frame.pop(_FRAME_SCOPE_FIELD, None)
    return scope
