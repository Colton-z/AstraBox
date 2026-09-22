from __future__ import annotations

import pytest

from astrabox.core.service.orchestrator.engine.frame_scope import (
    PublicEngineFrameError,
    public_engine_frame_payload,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _ResumeCursorTracker,
    _emit_resumable_payloads,
)


def _project(frame: dict[str, object]) -> dict[str, object] | None:
    return public_engine_frame_payload(frame, frame_seq=7, scope="turn")


def test_raw_engine_event_has_no_public_frame() -> None:
    assert _project(
        {
            "type": "data-raw-event",
            "data": {
                "raw": {
                    "session_id": "native-session-secret",
                    "task_id": "native-control-secret",
                }
            },
        }
    ) is None


def test_adapter_declared_public_payload_is_open_to_additive_fields() -> None:
    frame = {
        "type": "data-engine-metrics",
        "id": "metrics:turn-1",
        "providerMetadata": {"cacheReadTokens": 12},
        "data": {
            "usage": {
                "input_tokens": 1,
                "cache_read_tokens": 12,
            },
            "newVendorMetric": 7,
        },
        "__engine_sequence_number": 9,
    }

    assert _project(frame) == {
        key: value for key, value in frame.items() if not key.startswith("__")
    }


def test_api_retry_is_typed_instead_of_a_raw_vendor_envelope() -> None:
    assert _project(
        {
            "type": "data-api-retry",
            "id": "api-retry",
            "data": {
                "attempt": 2,
                "max_retries": 10,
                "error_status": 401,
                "error": "authentication_failed",
            },
        }
    ) == {
        "type": "data-api-retry",
        "id": "api-retry",
        "data": {
            "attempt": 2,
            "max_retries": 10,
            "error_status": 401,
            "error": "authentication_failed",
        },
    }


def test_interaction_drops_engine_control_fields() -> None:
    projected = _project(
        {
            "type": "data-interaction",
            "data": {
                "interaction_id": "interaction-1",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "created_at": "2026-08-17T00:00:00Z",
                "tool_call_id": "tool-1",
                "tool_name": "ExitPlanMode",
                "presentation": "decision",
                "prompt": "Continue?",
                "raw_input": {"plan": "Do the work"},
                "body": "Do the work",
                "options": [
                    {
                        "id": "approve",
                        "denial": False,
                        "reply": "vendor transcript reply",
                        "comment_prefix": "vendor prefix",
                    }
                ],
                "engine_anchor": {"native_id": "native-control-secret"},
            },
        }
    )

    assert projected == {
        "type": "data-interaction",
        "data": {
            "interaction_id": "interaction-1",
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "tool_name": "ExitPlanMode",
            "presentation": "decision",
            "prompt": "Continue?",
            "raw_input": {"plan": "Do the work"},
            "body": "Do the work",
            "options": [{"id": "approve", "denial": False}],
        },
    }


def test_session_scoped_fact_is_only_an_invalidation() -> None:
    assert public_engine_frame_payload(
        {
            "type": "data-engine-private-fact",
            "data": {"controlRef": "native-control-secret"},
        },
        frame_seq=8,
        scope="session",
    ) == {
        "type": "data-child-runs-changed",
        "transient": True,
        "data": {"frameSeq": 8},
    }


def test_frame_type_is_the_only_structural_requirement_at_public_egress() -> None:
    assert _project(
        {
            "type": "data-new-engine-extension",
            "data": {"shape": "adapter-owned"},
        }
    ) == {
        "type": "data-new-engine-extension",
        "data": {"shape": "adapter-owned"},
    }
    with pytest.raises(PublicEngineFrameError, match="type is required"):
        _project({"data": {}})


def test_hidden_durable_frame_still_advances_the_public_resume_cursor() -> None:
    emitted, terminal = _emit_resumable_payloads(
        cursor_tracker=_ResumeCursorTracker(),
        payload={
            "type": "data-raw-event",
            "data": {"raw": {"session_id": "native-session-secret"}},
        },
        frame_seq=11,
        payload_scope="turn",
        payload_turn_id="turn-1",
        include_resume_cursor=True,
    )

    assert terminal is False
    assert emitted == [
        {
            "type": "data-resume-cursor",
            "transient": True,
            "data": {
                "frameSeq": 11,
                "turnId": "turn-1",
            },
        }
    ]
