from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    UnknownWireEvent,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
    canonicalize_child_run_blocks,
)
from astrabox.core.service.orchestrator.engine.claude_message_blocks import (
    collect_message_blocks_from_raw_events,
)
from astrabox.core.service.orchestrator.engine.claude_child_runs import ClaudeChildIdentities


def _task_started(child_run_id: str, uuid: str) -> dict:
    return {
        "__sdk_type": "TaskStartedMessage",
        "subtype": "task_started",
        "data": {
            "type": "system",
            "subtype": "task_started",
            "tool_use_id": child_run_id,
        },
        "task_id": f"task-{child_run_id}",
        "description": f"run {child_run_id}",
        "uuid": uuid,
        "session_id": "sdk-session",
        "tool_use_id": child_run_id,
    }


def test_claude_child_user_string_crosses_the_live_seam_as_text() -> None:
    identities = ClaudeChildIdentities()
    identities.bind("child-tool", "child-agent")
    task = "  Process CHILD_claude_code_9eaa622e9c18402f985ca2c441a6d05a.\n"
    frames = list(
        translate_claude_sdk_message(
            {
                "__sdk_type": "UserMessage",
                "parent_tool_use_id": "child-tool",
                "uuid": "5c8e2e27-9c19-4921-8d2c-bdb2d702f3ee",
                "content": task,
            },
            envelope_seq=1,
            cursor=ClaudeStreamCursor(child_identities=identities),
        )
    )
    assert len(frames) == 1
    assert frames[0]["type"] == "data-subagent"
    assert frames[0]["data"]["engineRef"] == "child-agent"
    assert frames[0]["data"]["role"] == "user"
    assert frames[0]["data"]["content"] == [{"type": "text", "text": task}]


def test_typed_claude_lifecycle_and_nested_agent_project_generic_lineage() -> None:
    cursor = ClaudeStreamCursor()

    root = list(
        translate_claude_sdk_message(
            _task_started("root-child", "u-root"), envelope_seq=1, cursor=cursor
        )
    )
    parent_message = list(
        translate_claude_sdk_message(
            {
                "__sdk_type": "AssistantMessage",
                "parent_tool_use_id": "root-child",
                "content": [
                    {
                        "__sdk_type": "ToolUseBlock",
                        "id": "nested-child",
                        "name": "Agent",
                        "input": {"subagent_type": "general-purpose"},
                    }
                ],
                "message_id": "message-parent",
                "uuid": "u-parent-message",
            },
            envelope_seq=2,
            cursor=cursor,
        )
    )
    nested = list(
        translate_claude_sdk_message(
            _task_started("nested-child", "u-nested"),
            envelope_seq=3,
            cursor=cursor,
        )
    )

    assert root == [
        {
            "type": "data-subagent",
            "id": "subagent:lifecycle:u-root",
            "data": {
                "kind": "lifecycle",
                "engineRef": "task-root-child",
                "event": "opened",
                "engineEvent": "task_started",
                "operations": ["stop"],
                "controlRef": "task-root-child",
                "toolCallId": "root-child",
                "description": "run root-child",
                "engineKind": "claude_code",
            },
            "__engine_frame_scope": "session",
            "transient": True,
        }
    ]
    assert parent_message[0]["__engine_frame_scope"] == "session"
    assert parent_message[0]["transient"] is True
    assert parent_message[0]["data"] == {
        "kind": "message",
        "engineRef": "task-root-child",
        "role": "assistant",
        "content": [
            {
                "id": "nested-child",
                "name": "Agent",
                "input": {"subagent_type": "general-purpose"},
                "type": "tool_use",
            }
        ],
        "messageId": "message-parent",
        "engineKind": "claude_code",
    }
    assert nested[0]["data"] == {
        "kind": "lifecycle",
        "engineRef": "task-nested-child",
        "parentEngineRef": "task-root-child",
        "event": "opened",
        "engineEvent": "task_started",
        "operations": ["stop"],
        "controlRef": "task-nested-child",
        "toolCallId": "nested-child",
        "description": "run nested-child",
        "engineKind": "claude_code",
    }
    assert nested[0]["__engine_frame_scope"] == "session"
    assert nested[0]["transient"] is True


def test_durable_projection_links_nested_child_independent_of_event_order() -> None:
    blocks = collect_message_blocks_from_raw_events(
        [
            {
                "seq": 1,
                "raw": {
                    "type": "assistant",
                    "parent_tool_use_id": "nested-child",
                    "message": {
                        "id": "message-nested",
                        "content": [{"type": "text", "text": "nested result"}],
                    },
                    "uuid": "u-nested-message",
                },
            },
            {
                "seq": 2,
                "raw": {
                    "type": "assistant",
                    "parent_tool_use_id": "root-child",
                    "message": {
                        "id": "message-parent",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "nested-child",
                                "name": "Agent",
                                "input": {"subagent_type": "general-purpose"},
                            }
                        ],
                    },
                    "uuid": "u-parent-message",
                },
            },
            {"seq": 3, "raw": _task_started("root-child", "u-root")},
            {"seq": 4, "raw": _task_started("nested-child", "u-nested")},
        ]
    )

    child_blocks = [block for block in blocks if block.get("type") == "subagent"]
    nested_blocks = [
        block for block in child_blocks if block["data"]["engineRef"] == "task-nested-child"
    ]
    assert nested_blocks
    assert all(block["data"]["parentEngineRef"] == "task-root-child" for block in nested_blocks)
    assert all(block["data"]["engineKind"] == "claude_code" for block in child_blocks)


def test_generic_child_run_contract_rejects_a_parent_cycle() -> None:
    blocks = [
        {
            "type": "subagent",
            "id": "a-start",
            "data": {
                "kind": "lifecycle",
                "engineRef": "a",
                "parentEngineRef": "b",
                "event": "opened",
                "engineEvent": "test.opened",
                "operations": [],
            },
        },
        {
            "type": "subagent",
            "id": "b-start",
            "data": {
                "kind": "lifecycle",
                "engineRef": "b",
                "parentEngineRef": "a",
                "event": "opened",
                "engineEvent": "test.opened",
                "operations": [],
            },
        },
    ]

    with pytest.raises(ChildRunProjectionError, match="parent cycle"):
        canonicalize_child_run_blocks(blocks, engine_kind="test_engine")


@pytest.mark.parametrize(
    "vendor_status",
    ["pending", "running", "paused"],
)
def test_claude_nonterminal_statuses_cross_the_seam_verbatim(
    vendor_status: str,
) -> None:
    (frame,) = list(
        translate_claude_sdk_message(
            {
                "__sdk_type": "TaskUpdatedMessage",
                "subtype": "task_updated",
                "data": {},
                "task_id": "task-child",
                "patch": {"status": vendor_status},
                "uuid": f"u-{vendor_status}",
                "session_id": "sdk-session",
                "tool_use_id": "child",
            },
            envelope_seq=1,
            cursor=ClaudeStreamCursor(),
        )
    )

    assert frame["data"] == {
        "kind": "lifecycle",
        "engineRef": "task-child",
        "event": "updated",
        "engineEvent": "task_updated",
        "engineStatus": vendor_status,
        "toolCallId": "child",
        "operations": ["stop"],
        "controlRef": "task-child",
        "engineKind": "claude_code",
    }
    assert "phase" not in frame["data"]


def test_a_task_updated_patch_without_a_status_is_a_plain_update() -> None:
    """The vendor documents a patch of only end_time/result/error as a
    non-terminal update whose status is None, and never raises on one. A turn
    must not fail because the engine sent exactly what it says it sends."""

    (frame,) = list(
        translate_claude_sdk_message(
            {
                "__sdk_type": "TaskUpdatedMessage",
                "subtype": "task_updated",
                "data": {},
                "task_id": "task-child",
                "patch": {"end_time": 1_788_319_240, "result": "done"},
                "uuid": "u-no-status",
                "session_id": "sdk-session",
                "tool_use_id": "child",
            },
            envelope_seq=1,
            cursor=ClaudeStreamCursor(),
        )
    )

    assert frame["data"]["event"] == "updated"
    assert "engineStatus" not in frame["data"]


def test_a_task_notification_without_a_status_still_fails_loud() -> None:
    """Unlike task_updated, the SDK reads task_notification's status
    unconditionally, so its absence must be rejected as an unknown event."""

    with pytest.raises(UnknownWireEvent, match="task_notification lacks"):
        list(
            translate_claude_sdk_message(
                {
                    "__sdk_type": "TaskNotificationMessage",
                    "subtype": "task_notification",
                    "data": {},
                    "task_id": "task-child",
                    "uuid": "u-no-status",
                    "session_id": "sdk-session",
                    "tool_use_id": "child",
                },
                envelope_seq=1,
                cursor=ClaudeStreamCursor(),
            )
        )


def _activity_adapter(engine_kind: str):
    from astrabox.core.service.orchestrator.engine.claude_code import (
        ClaudeCodeEngineAdapter,
    )
    from astrabox.core.service.orchestrator.engine.codex import CodexEngineAdapter
    from astrabox.core.service.orchestrator.engine.deepseek_harness import (
        DeepSeekHarnessEngineAdapter,
    )
    from astrabox.core.service.orchestrator.engine.hermes import HermesEngineAdapter
    from astrabox.core.service.orchestrator.engine.pi import PiEngineAdapter

    return {
        "claude_code": ClaudeCodeEngineAdapter,
        "codex": CodexEngineAdapter,
        "deepseek_harness": DeepSeekHarnessEngineAdapter,
        "pi": PiEngineAdapter,
        "assistant": HermesEngineAdapter,
    }[engine_kind]()


@pytest.mark.parametrize(
    ("engine_kind", "field", "cases"),
    [
        (
            "claude_code",
            "engine_status",
            {
                "pending": True,
                "running": True,
                "paused": False,
                "completed": False,
                "failed": False,
                "stopped": False,
                "killed": False,
            },
        ),
        (
            "codex",
            "engine_status",
            {
                None: False,
                "inProgress": True,
                "completed": False,
                "interrupted": False,
                "failed": False,
            },
        ),
        ("deepseek_harness", "engine_status", {"running": True, "inactive": False}),
        (
            "pi",
            "engine_status",
            {
                "queued": True,
                "running": True,
                "paused": False,
                "complete": False,
                "failed": False,
                "partial": False,
                "stopped": False,
                "rejected": False,
            },
        ),
        (
            "assistant",
            "engine_event",
            {
                "subagent.spawn_requested": True,
                "subagent.start": True,
                "subagent.thinking": True,
                "subagent.tool": True,
                "subagent.progress": True,
                "subagent.complete": False,
            },
        ),
    ],
)
def test_child_activity_uses_native_work_not_resource_or_stop_availability(
    engine_kind: str, field: str, cases: dict[str | None, bool]
) -> None:
    adapter = _activity_adapter(engine_kind)
    for native_value, expected in cases.items():
        child: dict[str, Any] = {
            "closed": False,
            "operations": ["stop"],
            field: native_value,
        }
        assert adapter.child_run_is_active(child) is expected, native_value
        # Removal can close a resource without overwriting its last status.
        assert adapter.child_run_is_active({**child, "closed": True}) is False


def test_claude_partial_lifecycle_uses_preserved_status_before_event() -> None:
    adapter = _activity_adapter("claude_code")
    for event in ("task_started", "task_progress", "task_updated"):
        assert adapter.child_run_is_active({"engine_event": event}) is True
    assert (
        adapter.child_run_is_active(
            {"engine_event": "task_updated", "engine_status": "paused", "closed": False}
        )
        is False
    )
    assert (
        adapter.child_run_is_active(
            {"engine_event": "task_updated", "engine_status": "killed", "closed": True}
        )
        is False
    )


@pytest.mark.parametrize(
    "engine_kind", ["claude_code", "codex", "deepseek_harness", "pi", "assistant"]
)
def test_child_activity_rejects_an_unknown_native_contract(engine_kind: str) -> None:
    with pytest.raises(ChildRunProjectionError, match="unknown"):
        _activity_adapter(engine_kind).child_run_is_active(
            {"engine_event": "unknown", "engine_status": "unknown", "closed": False}
        )


def test_claude_nested_transcript_activity_reuses_native_stop_reason() -> None:
    from astrabox.core.service.orchestrator.engine.claude_code_background import (
        _agent_transcript_lifecycle,
    )

    adapter = _activity_adapter("claude_code")
    for stop_reason, expected in ((None, True), ("tool_use", True), ("end_turn", False)):
        event, reason = _agent_transcript_lifecycle(
            [{"type": "assistant", "message": {"stop_reason": stop_reason}}]
        )
        assert adapter.child_run_is_active(
            {
                "engine_event": "session_store.stop_reason",
                "engine_reason": reason,
                "closed": event == "closed",
            }
        ) is expected
    assert adapter.child_run_is_active(
        {"engine_event": "task_started", "closed": True}
    ) is False


def test_child_activity_base_requires_an_engine_answer() -> None:
    from astrabox.core.service.orchestrator.engine.base import EngineAdapter

    with pytest.raises(NotImplementedError, match="does not declare child-run activity"):
        EngineAdapter.child_run_is_active(_activity_adapter("claude_code"), {})
