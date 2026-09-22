"""Durable frame coalescing for the turn worker.

:class:`_DurableSemanticFrameCoalescer` merges transport-sized deltas into
semantic durable records before they land in ``session_events``.
"""
from __future__ import annotations

import json
from typing import Any

from astrabox.core.service.orchestrator.tool_result_semantics import (
    AI_SDK_TOOL_OUTPUT_FRAME_TYPES,
)


_REPLAY_COMPARABLE_FRAME_TYPES = frozenset(
    {
        "start-step",
        "finish-step",
        "reasoning-start",
        "reasoning-delta",
        "reasoning-end",
        "text-start",
        "text-delta",
        "text-end",
        "tool-input-start",
        "tool-input-delta",
        "tool-input-available",
        *AI_SDK_TOOL_OUTPUT_FRAME_TYPES,
    }
)
_REPLAY_CONFIRM_SIGNAL_THRESHOLD = 16
_REPLAY_BOUNDARY_FRAME_TYPES = frozenset(
    {
        "text-end",
        "reasoning-end",
        "tool-input-available",
        *AI_SDK_TOOL_OUTPUT_FRAME_TYPES,
        "finish-step",
    }
)
_DURABLE_DELTA_VALUE_FIELDS = {
    "reasoning-delta": "delta",
    "text-delta": "delta",
    "tool-input-delta": "inputTextDelta",
}
_DURABLE_DELTA_IDENTITY_FIELDS = {
    "reasoning-delta": ("id",),
    "text-delta": ("id",),
    "tool-input-delta": ("toolCallId", "toolName"),
}
_DURABLE_COALESCED_DELTA_MAX_CHARS = 64 * 1024


class _DurableSemanticFrameCoalescer:
    """Coalesce transport-sized delta frames into durable semantic records.

    Live SSE keeps the original chunk cadence. Durable history does not need
    to preserve the transport split, so consecutive deltas in the same AI SDK
    block are persisted as one larger delta before the next semantic boundary.
    """

    def __init__(self) -> None:
        self._pending: dict[str, Any] | None = None

    def ingest(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        frame_type = str(frame.get("type") or "").strip()
        value_field = _DURABLE_DELTA_VALUE_FIELDS.get(frame_type)
        if value_field is None:
            return [*self.flush(), dict(frame)]

        value = str(frame.get(value_field) or "")
        if not value:
            return []

        identity = self._identity_for(frame_type, frame)
        pending_identity = (
            self._pending.get("_identity")
            if isinstance(self._pending, dict)
            else None
        )
        emitted: list[dict[str, Any]] = []
        if self._pending is not None and pending_identity != identity:
            emitted.extend(self.flush())

        if self._pending is None:
            pending_frame = dict(frame)
            pending_frame[value_field] = value
            pending_frame["_identity"] = identity
            self._pending = pending_frame
        else:
            self._pending[value_field] = (
                f"{str(self._pending.get(value_field) or '')}{value}"
            )
            self._merge_latest_metadata(self._pending, frame)

        if len(str(self._pending.get(value_field) or "")) >= _DURABLE_COALESCED_DELTA_MAX_CHARS:
            emitted.extend(self.flush())
        return emitted

    def flush(self) -> list[dict[str, Any]]:
        if self._pending is None:
            return []
        frame = dict(self._pending)
        frame.pop("_identity", None)
        self._pending = None
        return [frame]

    @staticmethod
    def _identity_for(frame_type: str, frame: dict[str, Any]) -> tuple[Any, ...]:
        fields = _DURABLE_DELTA_IDENTITY_FIELDS.get(frame_type, ())
        return (frame_type, *[str(frame.get(field) or "") for field in fields])

    @staticmethod
    def _merge_latest_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key in (
            "__live_seq",
            "__source_kind",
            "__source_sandbox_turn_id",
            "__source_sandbox_seq",
            "__source_frame_index",
            "__remote_cursor_seq",
        ):
            if key in source:
                target[key] = source[key]

