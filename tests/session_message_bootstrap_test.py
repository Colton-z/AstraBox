from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_read import (
    SessionReadRenderingMixin,
)


@pytest.mark.asyncio
async def test_first_message_page_reads_pending_after_overlay_cursor() -> None:
    renderer = SessionReadRenderingMixin()
    events: list[str] = []
    pending_reads = iter(
        [
            {"interaction_id": "initial", "turn_id": "turn-1"},
            {"interaction_id": "final", "turn_id": "turn-1"},
        ]
    )

    renderer._must_get_owned_session = AsyncMock(  # type: ignore[attr-defined]
        return_value={"session_id": "session-1"},
    )
    renderer._touch_projection_session = AsyncMock(  # type: ignore[attr-defined]
        return_value={"session_id": "session-1"},
    )
    renderer._reconcile_runtime_binding = AsyncMock(  # type: ignore[attr-defined]
        return_value={"session_id": "session-1"},
    )
    renderer._get_kernel_session_snapshot = AsyncMock(  # type: ignore[method-assign]
        return_value={},
    )
    renderer._get_messages_page = AsyncMock(return_value=([], False))  # type: ignore[attr-defined]
    renderer._session_service = SimpleNamespace(  # type: ignore[attr-defined]
        _sanitize_message=lambda message: message,
    )
    renderer._resolve_active_turn_id_for_messages = (  # type: ignore[method-assign]
        lambda **_kwargs: "turn-1"
    )

    async def build_overlay(
        _session_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        events.append("overlay")
        return {
            "turn_id": "turn-1",
            "role": "assistant",
            "blocks": [],
            "source_frame_seq_applied": 16,
        }

    renderer._build_active_turn_overlay_message = build_overlay  # type: ignore[method-assign]

    async def get_pending_interaction(
        _session_id: str,
        **_kwargs: object,
    ) -> dict[str, str]:
        events.append("pending")
        return next(pending_reads)

    async def get_max_session_frame_seq(_session_id: str) -> int:
        events.append("cursor")
        return 17

    renderer._get_pending_interaction = get_pending_interaction  # type: ignore[method-assign]
    renderer._session_events_repo = SimpleNamespace(  # type: ignore[attr-defined]
        get_max_session_frame_seq=get_max_session_frame_seq,
    )

    result = await renderer.get_messages(object(), "session-1")  # type: ignore[arg-type]

    assert events == ["pending", "overlay", "cursor", "pending"]
    assert result["session_frame_seq"] == 17
    assert result["pending_interaction"] == {
        "interaction_id": "final",
        "turn_id": "turn-1",
    }
