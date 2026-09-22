from __future__ import annotations

import pytest

from astrabox.core.service.orchestrator.session_public_projection import (
    PublicProjectionError,
    project_owner_session,
    project_public_message_page,
    project_shared_session,
)


_PRIVATE_MARKERS = {
    "native-session-secret",
    "native-control-secret",
    "native-transcript-secret",
    "share-token-secret",
    "callback-secret",
}


def _assert_private_markers_absent(value: object) -> None:
    rendered = repr(value)
    for marker in _PRIVATE_MARKERS:
        assert marker not in rendered


def _internal_session() -> dict[str, object]:
    return {
        "session_id": "session-public",
        "user_id": "owner-public",
        "title": "Visible title",
        "state": "READY",
        "delivery_state": "RECEIVED",
        "engine_session_key": "native-session-secret",
        "claude_session_id": "native-session-secret",
        "sandbox_backend": "private-backend",
        "runtime_identity": {"resume": "native-session-secret"},
        "owner_type": "assistant_workspace",
        "owner_id": "native-transcript-secret",
        "hidden": False,
        "sandbox_callback_token": "callback-secret",
        "share": {"token": "share-token-secret", "created_by": "owner-private"},
        "runtime_binding": {
            "authority_kind": "assistant_workspace",
            "authority_id": "native-transcript-secret",
            "state": "READY",
            "can_dispatch": True,
            "sandbox_id": "private-binding-sandbox",
            "expires_at": None,
            "reason_code": None,
            "reason_message": None,
            "owner_runtime_identity": {"resume": "native-session-secret"},
        },
        "agent_runtime": {
            "agent_id": "agent-public",
            "state": "ACTIVE",
            "sandbox_id": "sandbox-public",
            "runtime_identity": {"resume": "native-session-secret"},
        },
        "background_task_state": {
            "state": "OPEN",
            "pending_manifest_count": 1,
            "pending_task_count": 1,
            "source_turn_id": "turn-public",
            "control_ref": "native-control-secret",
        },
        "delivery_failure": {
            "turn_id": "turn-public",
            "client_message_id": "client-public",
            "text": "Visible text",
            "summary": "Visible summary",
            "control_ref": "native-control-secret",
        },
        "pending_interaction": {
            "interaction_id": "interaction-public",
            "session_id": "session-public",
            "turn_id": "turn-public",
            "tool_call_id": "tool-public",
            "tool_name": "ExitPlanMode",
            "presentation": "decision",
            "prompt": "Approve?",
            "raw_input": {"plan": "visible input"},
            "body": "Visible body",
            "options": [
                {
                    "id": "approve",
                    "denial": False,
                    "reply": "native-control-secret",
                    "comment_prefix": "native-transcript-secret",
                    "permission_mode_choices": ["default"],
                }
            ],
            "sandbox_turn_id": 12,
            "last_sandbox_seq": 7,
            "response": {"private": "native-control-secret"},
        },
        "pending_inputs": [
            {
                "command_id": "command-public",
                "input_id": "input-public",
                "client_message_id": "client-public",
                "content": "Visible queued input",
                "sequence": 1,
                "status": "pending",
                "resume_ref": "native-session-secret",
            }
        ],
    }


def test_owner_session_is_an_allowlist_not_a_persistence_row_blacklist() -> None:
    public = project_owner_session(_internal_session())

    assert public["session_id"] == "session-public"
    assert public["runtime_binding"] == {
        "state": "READY",
        "can_dispatch": True,
        "reason_code": None,
        "reason_message": None,
    }
    assert public["pending_interaction"]["options"] == [
        {
            "id": "approve",
            "denial": False,
            "permission_mode_choices": ["default"],
        }
    ]
    assert public["pending_inputs"][0]["content"] == "Visible queued input"
    _assert_private_markers_absent(public)


def test_shared_session_is_narrower_than_the_owner_projection() -> None:
    shared = project_shared_session(_internal_session(), allow_download=True)

    assert set(shared) == {
        "title",
        "delivery_state",
        "delivery_failure",
        "pending_interaction",
        "share_allow_download",
    }
    assert shared["share_allow_download"] is True
    assert "user_id" not in shared
    assert "sandbox_id" not in shared
    assert "raw_input" not in shared["pending_interaction"]
    _assert_private_markers_absent(shared)


def test_message_page_drops_raw_vendor_envelopes_and_internal_cursors() -> None:
    internal_message = {
        "session_id": "session-public",
        "message_id": "message-public",
        "turn_id": "turn-public",
        "role": "assistant",
        "content": "Visible answer",
        "created_at": "2026-08-17T00:00:00Z",
        "engine_session_key": "native-session-secret",
        "blocks": [
            {"type": "text", "text": "Visible answer", "private": "callback-secret"},
            {
                "type": "result",
                "result": "Visible result",
                "stop_reason": "completed",
                "sessionId": "native-session-secret",
                "deferredToolUse": {"id": "native-control-secret"},
            },
            {
                "type": "raw_event",
                "event_type": "claude_code.sdk",
                "raw": {"task_id": "native-control-secret"},
            },
        ],
    }
    page = project_public_message_page(
        {
            "messages": [internal_message],
            "has_more": False,
            "session_frame_seq": 9,
            "active_turn_overlay": {
                "turn_id": "turn-public",
                "message": internal_message,
                "resume_cursor": {"turn_id": "turn-public", "frame_seq": 9},
                "live_source_cursor": {"native": "native-transcript-secret"},
            },
            "pending_interaction": None,
            "private_page_state": "callback-secret",
        }
    )

    assert page["messages"][0]["blocks"] == [
        {"type": "text", "text": "Visible answer"},
        {
            "type": "result",
            "result": "Visible result",
            "stop_reason": "completed",
        },
    ]
    assert page["active_turn_overlay"] == {
        "turn_id": "turn-public",
        "message": page["messages"][0],
        "resume_cursor": {"turn_id": "turn-public", "frame_seq": 9},
    }
    _assert_private_markers_absent(page)


def test_unknown_message_blocks_fail_loudly() -> None:
    with pytest.raises(PublicProjectionError, match="no public projection"):
        project_public_message_page(
            {
                "messages": [
                    {
                        "session_id": "session-public",
                        "message_id": "message-public",
                        "turn_id": "turn-public",
                        "role": "assistant",
                        "content": "",
                        "blocks": [{"type": "new_vendor_block"}],
                    }
                ],
                "has_more": False,
            }
        )


def test_adapter_public_data_part_keeps_additive_fields() -> None:
    page = project_public_message_page(
        {
            "messages": [
                {
                    "message_id": "message-public",
                    "turn_id": "turn-public",
                    "role": "assistant",
                    "content": "",
                    "blocks": [
                        {
                            "type": "ui_data",
                            "part": {
                                "type": "data-engine-metrics",
                                "id": "metrics-1",
                                "data": {
                                    "tokens": 2,
                                    "newVendorMetric": 9,
                                },
                                "providerMetadata": {
                                    "cacheReadTokens": 14,
                                },
                            },
                        }
                    ],
                }
            ],
            "has_more": False,
        }
    )

    assert page["messages"][0]["blocks"] == [
        {
            "type": "ui_data",
            "part": {
                "type": "data-engine-metrics",
                "id": "metrics-1",
                "data": {"tokens": 2, "newVendorMetric": 9},
                "providerMetadata": {"cacheReadTokens": 14},
            },
        }
    ]


def test_adapter_public_data_part_cannot_restore_a_raw_diagnostic() -> None:
    with pytest.raises(PublicProjectionError, match="not public"):
        project_public_message_page(
            {
                "messages": [
                    {
                        "message_id": "message-public",
                        "turn_id": "turn-public",
                        "role": "assistant",
                        "content": "",
                        "blocks": [
                            {
                                "type": "ui_data",
                                "part": {
                                    "type": "data-raw-event",
                                    "data": {"nativeControl": "secret"},
                                },
                            }
                        ],
                    }
                ],
                "has_more": False,
            }
        )


def test_an_image_block_reaches_the_reader_without_carrying_extra_fields() -> None:
    """A conversation that received an image must still be readable.

    The durable message keeps the engine's own image block, and every block
    type needs a projection here or the whole page 500s — measured on a real
    box: one pasted image made ``GET /sessions/{id}/messages`` fail with
    ``message block has no public projection: 'image'`` for the rest of that
    conversation's life, while the turn itself had succeeded.

    The source is projected field by field like ``result.usage``, so a
    producer that later attaches something else to it does not leak through.
    """
    page = project_public_message_page(
        {
            "messages": [
                {
                    "message_id": "m1",
                    "role": "user",
                    "content": "look at this",
                    "blocks": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                                "internal_note": "native-session-secret",
                            },
                        }
                    ],
                }
            ]
        }
    )

    block = page["messages"][0]["blocks"][0]
    assert block == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "iVBORw0KGgo=",
        },
    }


def test_an_image_block_without_a_source_object_is_refused() -> None:
    with pytest.raises(PublicProjectionError, match="image source must be an object"):
        project_public_message_page(
            {
                "messages": [
                    {
                        "message_id": "m1",
                        "role": "user",
                        "blocks": [{"type": "image", "source": "not-an-object"}],
                    }
                ]
            }
        )
