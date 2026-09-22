from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401
import pytest
from pydantic import ValidationError

from astrabox.api.routes.sessions import ChildRunRecord
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.child_runs import (
    public_child_message_id,
    public_child_run_id,
)
from astrabox.core.service.orchestrator.engine.emissions import ChildResourceFact
from astrabox.core.service.orchestrator.engine.frame_scope import pop_engine_frame_scope
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.session_child_run_view import (
    ChildRunProjectionError,
    SessionChildRunView,
    project_session_child_runs,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.child_runs import (
    ChildRunProjectionMixin,
)


def _frame(
    seq: int,
    *,
    turn_id: str | None,
    data: dict,
    frame_id: str,
) -> dict:
    return {
        "session_id": "session-1",
        "frame_seq": seq,
        "turn_id": turn_id,
        "scope": "session",
        "payload": {
            "type": "data-subagent",
            "id": frame_id,
            "transient": True,
            "data": data,
        },
    }


def _lifecycle(
    engine_ref: str,
    *,
    event: str = "updated",
    engine_event: str = "task_updated",
    engine_status: str | None = None,
    engine_reason: str | None = None,
    operations: list[str] | None = None,
    control_ref: str | None = None,
    parent_engine_ref: str | None = None,
    **extra: object,
) -> dict:
    data: dict = {
        "kind": "lifecycle",
        "engineRef": engine_ref,
        "engineKind": "claude_code",
        "event": event,
        "engineEvent": engine_event,
        "operations": list(operations or []),
        **extra,
    }
    if engine_status is not None:
        data["engineStatus"] = engine_status
    if engine_reason is not None:
        data["engineReason"] = engine_reason
    if control_ref is not None:
        data["controlRef"] = control_ref
    if parent_engine_ref is not None:
        data["parentEngineRef"] = parent_engine_ref
    return data


def _project(*, frames: list[dict], events: list[dict] | None = None, **kwargs: object):
    return project_session_child_runs(
        session_id="session-1",
        events=events or [],
        frames=frames,
        **kwargs,
    )


def test_child_run_lifecycle_is_session_scoped_across_root_turns() -> None:
    child_runs = _project(
        frames=[
            _frame(
                1,
                turn_id=None,
                frame_id="child:start",
                data=_lifecycle(
                    "tool-use-1",
                    event="opened",
                    engine_event="task_started",
                    engine_status="running",
                    operations=["stop"],
                    control_ref="task-1",
                    description="Research",
                ),
            ),
            _frame(
                9,
                turn_id=None,
                frame_id="child:done",
                data=_lifecycle(
                    "tool-use-1",
                    event="closed",
                    engine_event="task_updated",
                    engine_status="completed",
                    engine_reason="end_turn",
                    control_ref="task-1",
                    summary="Finished",
                ),
            ),
        ]
    )

    assert child_runs == [
        {
            "child_run_id": public_child_run_id(
                session_id="session-1",
                engine_kind="claude_code",
                engine_ref="tool-use-1",
            ),
            "engine_kind": "claude_code",
            "depth": 1,
            "closed": True,
            "operations": [],
            "tool_call_ids": [],
            "description": "Research",
            "engine_event": "task_updated",
            "engine_status": "completed",
            "engine_reason": "end_turn",
            "summary": "Finished",
        }
    ]


def test_public_child_run_id_is_session_scoped_and_not_the_engine_reference() -> None:
    first = public_child_run_id(
        session_id="session-1",
        engine_kind="claude_code",
        engine_ref="task-native-secret",
    )

    assert first == public_child_run_id(
        session_id="session-1",
        engine_kind="claude_code",
        engine_ref="task-native-secret",
    )
    assert first != "task-native-secret"
    assert first != public_child_run_id(
        session_id="session-2",
        engine_kind="claude_code",
        engine_ref="task-native-secret",
    )


@pytest.mark.parametrize("engine_status", ["pending", "running", "paused"])
def test_projection_preserves_native_status_without_platform_mapping(
    engine_status: str,
) -> None:
    projected = _project(
        frames=[
            _frame(
                1,
                turn_id=None,
                frame_id=engine_status,
                data=_lifecycle(
                    "tool-use-native-secret",
                    engine_event="task_updated",
                    engine_status=engine_status,
                ),
            )
        ]
    )

    assert projected[0]["engine_event"] == "task_updated"
    assert projected[0]["engine_status"] == engine_status
    assert projected[0]["closed"] is False
    assert "phase" not in projected[0]
    assert "engine_ref" not in projected[0]


def test_projection_does_not_collapse_distinct_native_terminal_statuses() -> None:
    projected = _project(
        frames=[
            _frame(
                1,
                turn_id=None,
                frame_id="killed",
                data=_lifecycle(
                    "tool-use-killed",
                    event="closed",
                    engine_status="killed",
                ),
            ),
            _frame(
                2,
                turn_id=None,
                frame_id="stopped",
                data=_lifecycle(
                    "tool-use-stopped",
                    event="closed",
                    engine_status="stopped",
                ),
            ),
        ]
    )

    assert [child["engine_status"] for child in projected] == ["killed", "stopped"]
    assert all(child["closed"] for child in projected)


def test_dsh_continuable_child_can_become_inactive_then_running() -> None:
    projected = _project(
        frames=[
            _frame(
                1,
                turn_id=None,
                frame_id="dsh:inactive",
                data={
                    **_lifecycle(
                        "dsh-child-session",
                        engine_event="subagent.finished",
                        engine_status="inactive",
                    ),
                    "engineKind": "dsh",
                },
            ),
            _frame(
                2,
                turn_id=None,
                frame_id="dsh:running-again",
                data={
                    **_lifecycle(
                        "dsh-child-session",
                        engine_event="subagent.started",
                        engine_status="running",
                        operations=["stop"],
                        control_ref="opaque-parent-and-child-address",
                    ),
                    "engineKind": "dsh",
                },
            ),
        ]
    )

    assert projected[0]["closed"] is False
    assert projected[0]["engine_status"] == "running"
    assert projected[0]["operations"] == ["stop"]


def test_child_run_projection_uses_terminal_event_when_live_reader_was_absent() -> None:
    child_runs = _project(
        frames=[],
        events=[
            {
                "session_id": "session-1",
                "event_seq": 7,
                "turn_id": "launcher-turn",
                "event_type": "turn.background_tasks_materialized",
                "payload": {
                    "blocks": [
                        {
                            "type": "subagent",
                            "id": "child:materialized",
                            "data": _lifecycle(
                                "tool-use-1",
                                event="closed",
                                engine_event="result",
                                engine_status="completed",
                            ),
                        },
                        {
                            "type": "subagent",
                            "id": "child:message",
                            "data": {
                                "kind": "message",
                                "engineRef": "tool-use-1",
                                "engineKind": "claude_code",
                                "role": "assistant",
                                "messageId": "message-1",
                                "content": [{"type": "text", "text": "answer"}],
                            },
                        },
                    ]
                },
            }
        ],
        include_messages=True,
    )

    assert child_runs[0]["engine_status"] == "completed"
    assert child_runs[0]["messages"] == [
        {
            "role": "assistant",
            "message_id": public_child_message_id(
                session_id="session-1",
                engine_kind="claude_code",
                engine_ref="tool-use-1",
                message_ref="message-1",
            ),
            "content": [{"type": "text", "text": "answer"}],
        }
    ]


def test_child_run_projection_merges_fragments_of_one_engine_message() -> None:
    child_runs = _project(
        frames=[
            _frame(
                1,
                turn_id=None,
                frame_id="child:lifecycle",
                data=_lifecycle("tool-use-1", engine_status="running"),
            ),
            _frame(
                2,
                turn_id=None,
                frame_id="child:thinking",
                data={
                    "kind": "message",
                    "engineRef": "tool-use-1",
                    "engineKind": "claude_code",
                    "role": "assistant",
                    "messageId": "engine-message-1",
                    "content": [{"type": "thinking", "thinking": "checking"}],
                },
            ),
            _frame(
                3,
                turn_id=None,
                frame_id="child:tool-use",
                data={
                    "kind": "message",
                    "engineRef": "tool-use-1",
                    "engineKind": "claude_code",
                    "role": "assistant",
                    "messageId": "engine-message-1",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "bash-1",
                            "name": "Bash",
                            "input": {"command": "printf answer"},
                        }
                    ],
                },
            ),
        ],
        include_messages=True,
    )

    assert child_runs[0]["messages"] == [
        {
            "role": "assistant",
            "message_id": public_child_message_id(
                session_id="session-1",
                engine_kind="claude_code",
                engine_ref="tool-use-1",
                message_ref="engine-message-1",
            ),
            "content": [
                {"type": "thinking", "thinking": "checking"},
                {
                    "type": "tool_use",
                    "id": "bash-1",
                    "name": "Bash",
                    "input": {"command": "printf answer"},
                },
            ],
        }
    ]


def test_background_snapshot_replaces_live_fragment_arrival_order() -> None:
    engine_ref = "background-agent-1"
    tool_result = {
        "kind": "message",
        "engineRef": engine_ref,
        "engineKind": "claude_code",
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "bash-1",
                "content": "done",
                "is_error": False,
            }
        ],
    }
    materialized = {
        "session_id": "session-1",
        "event_seq": 9,
        "event_type": "turn.background_tasks_materialized",
        "payload": {
            "blocks": [
                {
                    "type": "subagent",
                    "id": "child:closed",
                    "data": _lifecycle(
                        engine_ref,
                        event="closed",
                        engine_status="completed",
                    ),
                },
                {
                    "type": "subagent",
                    "id": "child:tool-use",
                    "data": {
                        "kind": "message",
                        "engineRef": engine_ref,
                        "engineKind": "claude_code",
                        "role": "assistant",
                        "messageId": "assistant-tool-call",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "bash-1",
                                "name": "Bash",
                                "input": {"command": "printf done"},
                            }
                        ],
                    },
                },
                {
                    "type": "subagent",
                    "id": "child:tool-result",
                    "data": tool_result,
                },
            ]
        },
    }

    child_runs = _project(
        frames=[
            _frame(
                1,
                turn_id=None,
                frame_id="child:opened",
                data=_lifecycle(engine_ref, event="opened", engine_status="running"),
            ),
            # The live bridge can publish the result before the detached
            # transcript reader later supplies the complete prefix.
            _frame(
                5,
                turn_id=None,
                frame_id="child:tool-result",
                data=tool_result,
            ),
        ],
        events=[materialized],
        include_messages=True,
    )

    assert [
        block["type"]
        for message in child_runs[0]["messages"]
        for block in message["content"]
    ] == ["tool_use", "tool_result"]


def test_child_run_projection_rejects_role_changes_within_one_engine_message() -> None:
    shared_message = {
        "kind": "message",
        "engineRef": "tool-use-1",
        "engineKind": "claude_code",
        "messageId": "engine-message-1",
        "content": [{"type": "text", "text": "content"}],
    }

    with pytest.raises(ChildRunProjectionError, match="changed role"):
        _project(
            frames=[
                _frame(
                    1,
                    turn_id=None,
                    frame_id="child:lifecycle",
                    data=_lifecycle("tool-use-1", engine_status="running"),
                ),
                _frame(
                    2,
                    turn_id=None,
                    frame_id="child:assistant-fragment",
                    data={**shared_message, "role": "assistant"},
                ),
                _frame(
                    3,
                    turn_id=None,
                    frame_id="child:user-fragment",
                    data={**shared_message, "role": "user"},
                ),
            ],
            include_messages=True,
        )


def test_child_run_projection_orders_nested_runs_and_rejects_parent_conflicts() -> None:
    root = _frame(
        1,
        turn_id=None,
        frame_id="root",
        data=_lifecycle("root-engine-ref", engine_status="running"),
    )
    nested = _frame(
        2,
        turn_id=None,
        frame_id="nested",
        data=_lifecycle(
            "nested-engine-ref",
            parent_engine_ref="root-engine-ref",
            engine_status="completed",
        ),
    )

    projected = _project(frames=[nested, root])
    root_public_id = public_child_run_id(
        session_id="session-1",
        engine_kind="claude_code",
        engine_ref="root-engine-ref",
    )
    nested_public_id = public_child_run_id(
        session_id="session-1",
        engine_kind="claude_code",
        engine_ref="nested-engine-ref",
    )
    assert [
        (row["child_run_id"], row.get("parent_child_run_id"), row["depth"])
        for row in projected
    ] == [
        (root_public_id, None, 1),
        (nested_public_id, root_public_id, 2),
    ]

    conflicting = {
        **nested,
        "frame_seq": 3,
        "payload": {
            **nested["payload"],
            "id": "nested-conflict",
            "data": {
                **nested["payload"]["data"],
                "parentEngineRef": "different-parent",
            },
        },
    }
    with pytest.raises(ChildRunProjectionError, match="changed parent"):
        _project(frames=[root, nested, conflicting])


def test_child_run_projection_refuses_a_turn_scoped_child_frame() -> None:
    invalid = _frame(
        1,
        turn_id="turn-1",
        frame_id="wrong-scope",
        data=_lifecycle("child-1", event="opened", engine_event="task_started"),
    )
    invalid["scope"] = "turn"

    with pytest.raises(ChildRunProjectionError, match="session scoped"):
        _project(frames=[invalid])


def test_child_run_projection_respects_the_shared_session_sequence() -> None:
    earlier_event = {
        "session_id": "session-1",
        "event_seq": 5,
        "event_type": "turn.background_tasks_materialized",
        "payload": {
            "blocks": [
                {
                    "type": "subagent",
                    "id": "earlier-event",
                    "data": _lifecycle(
                        "child-1",
                        engine_status="running",
                        description="earlier description",
                    ),
                }
            ]
        },
    }
    later_progress = _frame(
        9,
        turn_id=None,
        frame_id="late-progress",
        data=_lifecycle(
            "child-1",
            engine_status="running",
            description="later description",
        ),
    )

    projected = _project(events=[earlier_event], frames=[later_progress])
    assert projected[0]["description"] == "later description"


def test_child_run_projection_follows_an_engine_that_reopens_a_child() -> None:
    """The engine decides whether a child run lives again; this carries it.

    Refusing the reopen failed the whole projection with 409 — the child-run
    list a person reads, gone because one background subagent reported
    activity after it had gone quiet (p180). The engine's own adapter stopped
    enforcing the same rule for the same reason.
    """

    closed = _frame(
        1,
        turn_id=None,
        frame_id="closed",
        data=_lifecycle("child-1", event="closed", engine_status="completed"),
    )
    reopened = _frame(
        2,
        turn_id=None,
        frame_id="reopened",
        data=_lifecycle("child-1", engine_status="running"),
    )

    projected = _project(frames=[closed, reopened])

    assert len(projected) == 1
    assert projected[0]["closed"] is False
    assert projected[0]["engine_status"] == "running"


def test_child_run_projection_keeps_a_close_that_is_never_contradicted() -> None:
    """The reopen above must not have retired closure itself."""

    opened = _frame(
        1,
        turn_id=None,
        frame_id="opened",
        data=_lifecycle("child-1", engine_status="running"),
    )
    closed = _frame(
        2,
        turn_id=None,
        frame_id="closed",
        data=_lifecycle("child-1", event="closed", engine_status="completed"),
    )

    projected = _project(frames=[opened, closed])

    assert projected[0]["closed"] is True


def test_child_run_projection_rejects_public_projection_fields_in_private_fact() -> None:
    invalid = _frame(
        1,
        turn_id=None,
        frame_id="invalid",
        data={
            **_lifecycle("child-1", engine_status="paused"),
            "childRunId": "public-id-from-adapter",
            "controlId": "vendor-private-control",
        },
    )

    with pytest.raises(ChildRunProjectionError, match="public projection fields"):
        _project(frames=[invalid])


def test_child_run_control_handle_is_private_and_only_explicitly_exposed_internally() -> None:
    running = _frame(
        1,
        turn_id=None,
        frame_id="running",
        data=_lifecycle(
            "engine-child",
            event="opened",
            engine_event="task_started",
            engine_status="running",
            operations=["stop"],
            control_ref="vendor-private-control",
        ),
    )

    public = _project(frames=[running])
    expected_id = public_child_run_id(
        session_id="session-1",
        engine_kind="claude_code",
        engine_ref="engine-child",
    )
    assert public[0]["child_run_id"] == expected_id
    assert public[0]["operations"] == ["stop"]
    assert "engine_ref" not in public[0]
    assert "control_ref" not in public[0]

    internal = _project(frames=[running], include_control=True)
    assert internal[0]["child_run_id"] == expected_id
    assert internal[0]["control_ref"] == "vendor-private-control"
    assert "engine_ref" not in internal[0]


@pytest.mark.parametrize(
    "private_field,private_value",
    [
        ("engine_ref", "native-child"),
        ("engineRef", "native-child"),
        ("control_ref", "native-control"),
        ("controlRef", "native-control"),
    ],
)
def test_public_child_run_schema_rejects_adapter_private_fields(
    private_field: str,
    private_value: str,
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ChildRunRecord.model_validate(
            {
                "child_run_id": "public-child",
                "engine_kind": "claude_code",
                "depth": 1,
                "engine_event": "task_updated",
                "engine_status": "running",
                "closed": False,
                "active": True,
                "operations": ["stop"],
                private_field: private_value,
            }
        )


def _stop_harness(child_run: dict) -> ChildRunProjectionMixin:
    class _Harness(ChildRunProjectionMixin):
        pass

    harness = _Harness()
    harness._must_get_projection_backed_session = AsyncMock(
        return_value={
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "state": "READY",
        }
    )
    harness._require_turn_eligible = Mock()
    harness._child_run_view = SimpleNamespace(
        get_child_run_control=AsyncMock(return_value=child_run)
    )
    harness._turn_service = SimpleNamespace(
        reconcile_engine_child_resources=AsyncMock(return_value=True),
        stop_engine_child_run=AsyncMock(),
    )
    return harness


@pytest.mark.asyncio
async def test_stop_child_run_sends_only_the_private_native_control_reference() -> None:
    harness = _stop_harness(
        {
            "child_run_id": "public-child",
            "control_ref": "vendor-private-control",
            "closed": False,
            "operations": ["stop"],
        }
    )

    result = await harness.stop_child_run(
        UserContext("user-1"),
        "session-1",
        "public-child",
    )

    assert result == {
        "session_id": "session-1",
        "child_run_id": "public-child",
        "status": "accepted",
    }
    harness._child_run_view.get_child_run_control.assert_awaited_once_with(
        "session-1", "public-child"
    )
    harness._turn_service.reconcile_engine_child_resources.assert_awaited_once()
    harness._turn_service.stop_engine_child_run.assert_awaited_once_with(
        {
            "session_id": "session-1",
            "sandbox_id": "sandbox-1",
            "state": "READY",
        },
        "vendor-private-control",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_run", "error_code"),
    [
        (
            {
                "child_run_id": "public-child",
                "control_ref": "vendor-private-control",
                "closed": False,
                "operations": [],
            },
            "CHILD_RUN_CONTROL_UNAVAILABLE",
        ),
        (
            {
                "child_run_id": "public-child",
                "control_ref": "vendor-private-control",
                "closed": True,
                "operations": [],
            },
            "CHILD_RUN_ALREADY_TERMINAL",
        ),
    ],
)
async def test_stop_child_run_requires_stop_operation_on_an_open_child(
    child_run: dict,
    error_code: str,
) -> None:
    harness = _stop_harness(child_run)

    with pytest.raises(APIError) as exc_info:
        await harness.stop_child_run(
            UserContext("user-1"),
            "session-1",
            "public-child",
        )

    assert exc_info.value.code == error_code
    harness._turn_service.stop_engine_child_run.assert_not_awaited()


def _durable_claude_task_messages() -> list[dict]:
    """A background Agent task, journalled as the in-box runner serializes it."""
    from claude_agent_sdk.types import TaskStartedMessage, TaskUpdatedMessage

    from astrabox.core.service.orchestrator.sandbox_runner import _jsonable

    started = {
        "task_id": "task-aa",
        "tool_use_id": "call-aa",
        "description": "run the long thing",
        "task_type": "local_agent",
        "uuid": "started-aa",
        "session_id": "sdk-session",
    }
    backgrounded = {
        "task_id": "task-aa",
        "patch": {"is_backgrounded": True},
        "uuid": "backgrounded-aa",
        "session_id": "sdk-session",
    }
    completed = {
        "task_id": "task-aa",
        "patch": {"status": "completed"},
        "uuid": "completed-aa",
        "session_id": "sdk-session",
    }
    return [
        _jsonable(TaskStartedMessage(subtype="task_started", data=dict(started), **started)),
        _jsonable(
            TaskUpdatedMessage(subtype="task_updated", data=dict(backgrounded), **backgrounded)
        ),
        _jsonable(
            TaskUpdatedMessage(
                subtype="task_updated", data=dict(completed), status="completed", **completed
            )
        ),
    ]


def test_claude_adapter_recovers_durable_child_facts_with_one_cursor() -> None:
    indexed_facts = get_engine_adapter("claude_code").durable_child_resource_facts(
        _durable_claude_task_messages()
    )

    assert [message_index for message_index, _fact in indexed_facts] == [0, 1, 2]
    assert all(isinstance(fact, ChildResourceFact) for _index, fact in indexed_facts)
    assert indexed_facts[1][1]["data"] == {
        "kind": "lifecycle",
        "engineRef": "task-aa",
        "engineKind": "claude_code",
        "event": "updated",
        "engineEvent": "task_updated",
        "operations": ["stop"],
        "controlRef": "task-aa",
    }
    assert indexed_facts[2][1]["data"]["event"] == "closed"
    assert indexed_facts[2][1]["data"]["engineStatus"] == "completed"
    assert indexed_facts[2][1]["data"]["operations"] == []


@pytest.mark.asyncio
async def test_child_run_view_closes_from_a_post_turn_durable_terminal() -> None:
    messages = _durable_claude_task_messages()
    indexed_facts = get_engine_adapter("claude_code").durable_child_resource_facts(messages)
    live_frames: list[dict] = []
    for frame_seq, (_message_index, fact) in zip((2, 4), indexed_facts[:2], strict=True):
        payload = fact.as_frame()
        assert pop_engine_frame_scope(payload) == "session"
        live_frames.append(
            _frame(
                frame_seq,
                turn_id=None,
                frame_id=str(payload["id"]),
                data=dict(payload["data"]),
            )
        )
    events = [
        {
            "event_seq": event_seq,
            "event_type": "engine.message",
            "payload": {"engine_kind": "claude_code", "message": message},
        }
        for event_seq, message in zip((1, 3, 6), messages, strict=True)
    ]
    events.append(
        {
            "event_seq": 5,
            "event_type": "turn.completed",
            "payload": {"blocks": []},
        }
    )

    class _Repo:
        async def list_events(self, _session_id: str, **kwargs: object) -> list[dict]:
            after_seq = int(kwargs.get("after_seq") or 0)
            event_types = kwargs.get("event_types")
            return [
                dict(event)
                for event in events
                if int(event["event_seq"]) > after_seq
                and (
                    not isinstance(event_types, (set, frozenset))
                    or event["event_type"] in event_types
                )
            ]

        async def list_frames(self, _session_id: str, **kwargs: object) -> list[dict]:
            after_seq = int(kwargs.get("after_seq") or -1)
            return [dict(frame) for frame in live_frames if int(frame["frame_seq"]) > after_seq]

    child_runs = await SessionChildRunView(_Repo()).list_child_runs("session-1")

    assert len(child_runs) == 1
    assert child_runs[0]["closed"] is True
    assert child_runs[0]["engine_event"] == "task_updated"
    assert child_runs[0]["engine_status"] == "completed"
    assert child_runs[0]["operations"] == []


@pytest.mark.asyncio
async def test_child_run_view_reads_session_frames_and_durable_engine_messages() -> None:
    event_calls: list[dict] = []
    frame_calls: list[dict] = []

    class _Repo:
        async def list_events(self, _session_id: str, **kwargs: object) -> list[dict]:
            event_calls.append(dict(kwargs))
            return []

        async def list_frames(self, _session_id: str, **kwargs: object) -> list[dict]:
            frame_calls.append(dict(kwargs))
            return []

    assert await SessionChildRunView(_Repo()).list_child_runs("session-1") == []
    assert event_calls == [
        {
            "after_seq": 0,
            "event_types": frozenset(
                {
                    "engine.message",
                    "turn.completed",
                    "turn.failed",
                    "turn.recovered",
                    "turn.background_tasks_materialized",
                }
            ),
            "limit": 500,
        }
    ]
    assert frame_calls == [{"scope": "session", "after_seq": -1, "limit": 500}]
