"""Claude Code contracts for background-task discovery and terminal events.

The Claude client reduces its vendor messages to a neutral manifest before it
crosses the engine seam. Later, the platform hands persisted engine messages
back to the Claude adapter to decide whether a given event settles one. These
tests require:

- the claude_code adapter reads the CLI's OWN launch declaration — a tool
  result whose ``status`` is ``async_launched`` — in both serializations that
  reach it (the runner's ``__sdk_type``/``tool_use_result`` spelling on the
  live stream, the CLI JSONL ``type``/``toolUseResult`` spelling in the
  mirror). The Agent tool's *input* is deliberately not read: asking for
  ``run_in_background`` is a request, ``async_launched`` is the CLI stating a
  task exists — and naming it (``agentId``).
- terminal statuses come from both vendor vocabularies:
  a ``task_notification`` says ``stopped`` where a ``task_updated`` patch says
  ``killed``. Notifications may be suppressed, so either event must settle the
  task.
- the generic adapter seam has no raw-turn manifest parser, so the platform
  cannot accidentally teach another engine Claude's vocabulary.
"""

from __future__ import annotations

from typing import Any

import pytest

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401
from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
)
from astrabox.core.service.orchestrator.engine.claude_code_background import (
    build_background_task_manifest,
)
from astrabox.core.service.orchestrator.engine.base import EngineAdapter
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter


def _runner_launch(tool_use_id: str, transcript_ref: str) -> dict[str, Any]:
    """The runner-serialized UserMessage a live async launch produces.

    Shape verified against claude-agent-sdk 0.2.152: ``UserMessage`` is a
    dataclass with ``content``/``uuid``/``parent_tool_use_id``/
    ``tool_use_result``, and the runner's ``_jsonable`` stamps every dataclass
    (including nested blocks) with its class name.
    """
    return {
        "__sdk_type": "UserMessage",
        "content": [
            {
                "__sdk_type": "ToolResultBlock",
                "tool_use_id": tool_use_id,
                "content": "Async agent launched.",
                "is_error": None,
            }
        ],
        "uuid": None,
        "parent_tool_use_id": None,
        "tool_use_result": {
            "status": "async_launched",
            "agentId": transcript_ref,
            "isAsync": True,
            "description": "run the long thing",
        },
    }


def _mirror_launch(tool_use_id: str, transcript_ref: str) -> dict[str, Any]:
    """The CLI JSONL line the transcript mirror stores for the same launch.

    Shape captured verbatim from a real mirror, over a sample large enough to
    rule out a one-off: ``type: "user"``, blocks under ``message.content``,
    camelCase ``toolUseResult`` sibling.
    """
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "Async agent launched.",
                }
            ],
        },
        "toolUseResult": {
            "status": "async_launched",
            "agentId": transcript_ref,
            "isAsync": True,
            "description": "run the long thing",
        },
        "uuid": "u-1",
    }


def _mirror_notification(
    tool_use_id: str, task_id: str, *, status: str = "completed"
) -> dict[str, Any]:
    """The queued task-notification the CLI writes when the task settles."""
    return {
        "type": "user",
        "origin": {"kind": "task-notification"},
        "message": {
            "role": "user",
            "content": (
                "<task-notification>\n"
                f"<task-id>{task_id}</task-id>\n"
                f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<status>{status}</status>\n"
                "<summary>Agent finished</summary>\n"
                "<result>THE ANSWER IS 42</result>\n"
                "</task-notification>"
            ),
        },
        "uuid": "u-2",
    }


def test_the_runner_spelling_opens_the_manifest_with_the_mapping() -> None:
    manifest = build_background_task_manifest(
        [
            _runner_launch("call_00_a", "agent-session-aa"),
            _runner_launch("call_00_b", "agent-session-bb"),
        ]
    )

    assert manifest is not None
    assert set(manifest) == {
        "transcript_refs",
        "engine_refs",
        "transcript_to_engine_ref",
        "control_to_engine_ref",
        "activation_to_engine_ref",
    }
    assert manifest["engine_refs"] == ["agent-session-aa", "agent-session-bb"]
    assert manifest["transcript_refs"] == ["agent-session-aa", "agent-session-bb"]
    assert manifest["transcript_to_engine_ref"] == {
        "agent-session-aa": "agent-session-aa",
        "agent-session-bb": "agent-session-bb",
    }
    assert manifest["control_to_engine_ref"] == manifest["transcript_to_engine_ref"]
    assert manifest["activation_to_engine_ref"] == {
        "call_00_a": "agent-session-aa", "call_00_b": "agent-session-bb",
    }


def test_the_mirror_spelling_opens_the_same_manifest() -> None:
    manifest = build_background_task_manifest(
        [_mirror_launch("call_00_c", "agent-session-cc")]
    )

    assert manifest is not None
    assert manifest["transcript_to_engine_ref"] == {
        "agent-session-cc": "agent-session-cc"
    }
    assert manifest["activation_to_engine_ref"] == {"call_00_c": "agent-session-cc"}


def test_a_foreground_tool_result_opens_nothing() -> None:
    """Only the CLI's async_launched answer opens the lane.

    A foreground Agent (or any other tool) produces an ordinary tool result;
    opening a continuation for it would make the platform wait for work that
    already finished inside the turn.
    """
    raw = _runner_launch("call_00_d", "agent-session-dd")
    raw["tool_use_result"] = {"status": "success"}

    assert build_background_task_manifest([raw]) is None


def test_a_task_killed_via_task_updated_settles_inside_the_turn() -> None:
    """The SDK-documented case a notification-only terminal set misses.

    A TaskStop'd task reports ``status="killed"`` via ``task_updated`` and the
    matching notification is sometimes suppressed (claude-agent-sdk types.py,
    TaskUpdatedMessage). If ``killed`` is not terminal, this manifest opens and
    nothing can ever close it.
    """
    killed = {
        "__sdk_type": "TaskUpdatedMessage",
        "subtype": "task_updated",
        "task_id": "agent-session-ee",
        "patch": {"status": "killed"},
        "status": "running",
    }
    started = {
        "__sdk_type": "TaskStartedMessage",
        "subtype": "task_started",
        "task_id": "agent-session-ee",
        "tool_use_id": "call_00_e",
    }

    assert (
        build_background_task_manifest(
            [_runner_launch("call_00_e", "agent-session-ee"), started, killed]
        )
        is None
    )
    send_call = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "name": "SendMessage", "id": "call_00_resume",
            "input": {"to": "agent-session-ee", "message": "Run the next task"},
        }]},
    }
    resumed = _runner_launch("call_00_resume", "agent-session-ee")
    resumed["tool_use_result"] = {"success": True, "resumedAgentId": "agent-session-ee"}
    reactivated = [
        _runner_launch("call_00_e", "agent-session-ee"), started, killed,
        send_call, resumed,
        _mirror_notification("call_00_e", "agent-session-ee"),
    ]
    manifest = build_background_task_manifest(reactivated)
    assert manifest == {
        "transcript_refs": ["agent-session-ee"],
        "engine_refs": ["agent-session-ee"],
        "transcript_to_engine_ref": {"agent-session-ee": "agent-session-ee"},
        "control_to_engine_ref": {"agent-session-ee": "agent-session-ee"},
        "activation_to_engine_ref": {"call_00_resume": "agent-session-ee"},
    }
    assert build_background_task_manifest([
        *reactivated, {**started, "tool_use_id": "call_00_resume"}, killed,
    ]) is None


def test_terminal_matching_reads_the_mirrored_notification() -> None:
    """The materializer's path: manifest ids vs the mirrored XML notification."""
    adapter = get_engine_adapter("claude_code")
    record = adapter.detached_child_run_terminal(
        _mirror_notification("call_00_f", "agent-session-ff"),
        transcript_refs={"agent-session-ff"},
        engine_refs={"agent-session-ff"},
        transcript_to_engine_ref={"agent-session-ff": "agent-session-ff"},
        activation_to_engine_ref={"call_00_f": "agent-session-ff"},
        observed_activations={},
        control_to_engine_ref={},
    )

    assert record is not None
    assert set(record) == {
        "transcript_ref",
        "engine_ref",
        "control_ref",
        "event",
        "engine_event",
        "engine_status",
        "summary",
        "result",
    }
    assert record["transcript_ref"] == "agent-session-ff"
    assert record["engine_ref"] == "agent-session-ff"
    assert record["control_ref"] == "agent-session-ff"
    assert record["event"] == "closed"
    assert record["engine_event"] == "task_notification"
    assert record["engine_status"] == "completed"
    assert record["result"] == "THE ANSWER IS 42"

    unrelated = adapter.detached_child_run_terminal(
        _mirror_notification("call_other", "task_other"),
        transcript_refs={"agent-session-ff"},
        engine_refs={"agent-session-ff"},
        transcript_to_engine_ref={"agent-session-ff": "agent-session-ff"},
        activation_to_engine_ref={"call_00_f": "agent-session-ff"},
        observed_activations={},
        control_to_engine_ref={},
    )
    assert unrelated is None, "a notification for a task outside the manifest must not settle it"


def test_a_subagents_own_background_launch_opens_nothing() -> None:
    """A subagent-owned background launch cannot open the parent turn's lane.

    The inner launch emits the same ``async_launched`` receipt as a parent-lane
    Agent launch, stamped with the subagent's context (``parent_tool_use_id``
    on the runner envelope, ``isSidechain`` on the mirror line). Its terminal
    returns to the sidechain rather than the parent turn, so admitting the launch
    to the parent manifest would leave that manifest unsettled. Subagent context
    must be invisible here.
    """
    inner_runner = _runner_launch("call_00_inner", "agent-session-inner")
    inner_runner["parent_tool_use_id"] = "call_00_parent_agent"
    assert build_background_task_manifest([inner_runner]) is None

    inner_mirror = _mirror_launch("call_00_inner", "agent-session-inner")
    inner_mirror["isSidechain"] = True
    assert build_background_task_manifest([inner_mirror]) is None


def test_task_lifecycle_alone_opens_nothing() -> None:
    """Only the launch receipt opens the lane — lifecycle merely names tasks.

    The vendor's TaskStartedMessage/TaskProgressMessage carry NO parent-context
    field (verified against claude-agent-sdk types), so a subagent's inner task
    and a different turn's still-running task both present the same lifecycle
    to whichever turn window is open. Accepting task_started alone can attach
    that inner task to an unrelated foreground turn and leave it in
    BACKGROUND_RUNNING. The async_launched receipt is the only statement that
    identifies which turn expects a continuation.
    """
    started = {
        "__sdk_type": "TaskStartedMessage",
        "subtype": "task_started",
        "task_id": "task_inner_2",
        "tool_use_id": "call_00_inner_2",
        "description": "inner bash",
    }
    progress = {
        "__sdk_type": "TaskProgressMessage",
        "subtype": "task_progress",
        "task_id": "task_inner_2",
        "tool_use_id": "call_00_inner_2",
    }
    assert build_background_task_manifest([started, progress]) is None

    own_started = {
        **started,
        "task_id": "agent-session-outer",
        "tool_use_id": "call_00_outer",
    }
    with_launch = build_background_task_manifest(
        [
            _runner_launch("call_00_outer", "agent-session-outer"),
            started,
            own_started,
        ]
    )
    assert with_launch is not None
    assert with_launch["engine_refs"] == ["agent-session-outer"], (
        "a lifecycle record must not widen a manifest beyond what the turn launched"
    )
    assert with_launch["control_to_engine_ref"] == {
        "agent-session-outer": "agent-session-outer"
    }, "an unrelated lifecycle must not enter the launched child's identity mapping"
    assert with_launch["activation_to_engine_ref"] == {"call_00_outer": "agent-session-outer"}


def test_a_subagents_own_task_terminal_settles_nothing() -> None:
    """The other half of the same invisibility: an inner task's terminal must
    not close a parent-lane manifest entry, even on an id collision."""
    adapter = get_engine_adapter("claude_code")
    inner_terminal = _mirror_notification("call_00_h", "agent-session-hh")
    inner_terminal["isSidechain"] = True

    record = adapter.detached_child_run_terminal(
        inner_terminal,
        transcript_refs={"agent-session-hh"},
        engine_refs={"agent-session-hh"},
        transcript_to_engine_ref={"agent-session-hh": "agent-session-hh"},
        activation_to_engine_ref={"call_00_h": "agent-session-hh"},
        observed_activations={},
        control_to_engine_ref={},
    )
    assert record is None


def test_terminal_without_tool_use_id_uses_the_ordered_control_alias() -> None:
    adapter = get_engine_adapter("claude_code")
    observed_activations: dict[str, str] = {}
    common = {
        "transcript_refs": {"agent-session-aa"},
        "engine_refs": {"agent-session-aa"},
        "transcript_to_engine_ref": {"agent-session-aa": "agent-session-aa"},
        "control_to_engine_ref": {"agent-session-aa": "agent-session-aa"},
        "activation_to_engine_ref": {"call_00_a": "agent-session-aa"},
        "observed_activations": observed_activations,
    }
    started = {
        "__sdk_type": "TaskStartedMessage",
        "subtype": "task_started",
        "task_id": "agent-session-aa",
        "tool_use_id": "call_00_a",
    }
    terminal = {
        "__sdk_type": "TaskUpdatedMessage",
        "subtype": "task_updated",
        "task_id": "agent-session-aa",
        "patch": {"status": "killed"},
    }
    assert adapter.detached_child_run_terminal(terminal, **common) is None
    old_started = {**started, "tool_use_id": "call_00_old"}
    assert adapter.detached_child_run_terminal(old_started, **common) is None
    assert observed_activations == {"agent-session-aa": "call_00_old"}
    old_terminal = _mirror_notification("call_00_old", "agent-session-aa")
    assert adapter.detached_child_run_terminal(old_terminal, **common) is None
    assert adapter.detached_child_run_terminal(terminal, **common) is None

    assert adapter.detached_child_run_terminal(started, **common) is None
    assert observed_activations == {"agent-session-aa": "call_00_a"}
    assert adapter.detached_child_run_terminal(old_terminal, **common) is None
    record = adapter.detached_child_run_terminal(terminal, **common)

    assert record is not None
    assert record["transcript_ref"] == "agent-session-aa"
    assert record["engine_ref"] == "agent-session-aa"
    assert record["control_ref"] == "agent-session-aa"
    assert record["engine_status"] == "killed"

    # A second source must establish its own activation before an id-less
    # terminal can settle anything; the first source's final alias is not proof.
    second_source = {**common, "observed_activations": {}}
    assert adapter.detached_child_run_terminal(terminal, **second_source) is None
    assert adapter.detached_child_run_terminal(
        _mirror_notification("call_00_a", "agent-session-aa"), **second_source,
    ) == {
        "transcript_ref": "agent-session-aa",
        "engine_ref": "agent-session-aa",
        "control_ref": "agent-session-aa",
        "event": "closed",
        "engine_event": "task_notification",
        "engine_status": "completed",
        "summary": "Agent finished",
        "result": "THE ANSWER IS 42",
    }


def test_a_notification_naming_several_tasks_is_refused_not_half_applied() -> None:
    """An individual notification cannot name several tasks.

    The XML reader took the first ``<task-id>`` and never looked at the rest,
    so a notification covering two tasks closed one and left the other
    running with no terminal left to report it: an Agents panel that reads
    finished while work is still going. There is no half of this that is
    correct, so the shape is refused where it arrives. The vendor's aggregate
    stopped control, without a tool-use-id, settles no individual child.
    """
    adapter = get_engine_adapter("claude_code")
    aggregate = _mirror_notification("call_00_a", "agent-session-aa", status="stopped")
    aggregate["message"]["content"] = (
        aggregate["message"]["content"].replace(
            "<tool-use-id>",
            "<task-id>agent-session-bb</task-id>\n<tool-use-id>",
        )
    )

    with pytest.raises(ChildRunProjectionError) as refused:
        adapter.detached_child_run_terminal(
            aggregate,
            transcript_refs={"agent-session-aa"},
            engine_refs={"agent-session-aa"},
            transcript_to_engine_ref={"agent-session-aa": "agent-session-aa"},
            activation_to_engine_ref={"call_00_a": "agent-session-aa"},
            observed_activations={},
            control_to_engine_ref={},
        )
    # Both names have to be readable, or the report cannot say what was left
    # unsettled.
    assert "agent-session-aa" in str(refused.value)
    assert "agent-session-bb" in str(refused.value)

    aggregate["message"]["content"] = aggregate["message"]["content"].replace(
        "<tool-use-id>call_00_a</tool-use-id>\n", "",
    )
    assert adapter.detached_child_run_terminal(
        aggregate,
        transcript_refs={"agent-session-aa"},
        engine_refs={"agent-session-aa"},
        transcript_to_engine_ref={"agent-session-aa": "agent-session-aa"},
        activation_to_engine_ref={"call_00_a": "agent-session-aa"},
        observed_activations={},
        control_to_engine_ref={},
    ) is None, "aggregate stopped control metadata is not an individual child terminal"


def test_a_notification_naming_one_task_still_settles_it() -> None:
    """The control for the refusal above: one task-id is the ordinary case."""
    adapter = get_engine_adapter("claude_code")
    record = adapter.detached_child_run_terminal(
        _mirror_notification("call_00_a", "agent-session-aa", status="stopped"),
        transcript_refs={"agent-session-aa"},
        engine_refs={"agent-session-aa"},
        transcript_to_engine_ref={"agent-session-aa": "agent-session-aa"},
        activation_to_engine_ref={"call_00_a": "agent-session-aa"},
        observed_activations={},
        control_to_engine_ref={},
    )

    assert record is not None
    assert record["engine_ref"] == "agent-session-aa"
    assert record["engine_status"] == "stopped"


def test_background_manifest_parser_is_not_part_of_the_platform_adapter_seam() -> None:
    """Claude vocabulary stays in its engine package.

    The client emits a neutral manifest; neither the base adapter nor another
    engine is asked to parse Claude's raw events.
    """
    assert not hasattr(EngineAdapter, "background_tasks_opened")
    assert not hasattr(get_engine_adapter("claude_code"), "background_tasks_opened")
