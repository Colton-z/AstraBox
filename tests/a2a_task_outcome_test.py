"""An Agent's reported outcome, as its A2A task ends.

A conversation an A2A context created reports what became of a task with the
``report_outcome`` tool. These cases build the response the session reader
records, with the tool card bound to the call, and read the task the A2A
service projects from it: the state and status message any A2A client reads,
and the task outcome extension's data only for a client that activated it. A
response without a report stays the completed task it is for every other
caller.
"""

from __future__ import annotations

from typing import Any

import pytest
from a2a.types import a2a_pb2 as a2a
from google.protobuf.json_format import MessageToDict

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent.a2a_task_outcome import (
    TASK_OUTCOME_CARD,
    TASK_OUTCOME_EXTENSION,
    task_outcome,
)
from astrabox.core.service.orchestrator.agent.agent_a2a_service import AgentA2AService
from astrabox.core.service.orchestrator.engine.frame_scope import mark_engine_public_ui_frame
from astrabox.core.service.orchestrator.tool_cards import CARD_PART_TYPE


def _reply(text: str, *outcomes: dict[str, Any]) -> list[dict[str, Any]]:
    """A response's recorded output: the reply's text, then each outcome card the reader bound."""
    chunks: list[dict[str, Any]] = [
        {"type": "text-start", "id": "t1"},
        {"type": "text-delta", "id": "t1", "delta": text},
        {"type": "text-end", "id": "t1"},
    ]
    for index, outcome in enumerate(outcomes):
        call = f"call-{index}"
        card = f"crd_{index:024x}"
        chunks += [
            {"type": "tool-input-available", "toolCallId": call, "toolName": "mcp__a2a__report_outcome",
             "dynamic": True, "input": outcome},
            {"type": "tool-output-available", "toolCallId": call, "output": {"card": card}},
            mark_engine_public_ui_frame({
                "type": CARD_PART_TYPE, "id": f"tool-card:{call}:{card}",
                "data": {"toolCallId": call, "cardId": card, "kind": TASK_OUTCOME_CARD, **task_outcome(outcome)},
            }),
        ]
    return chunks


def _task(chunks: list[dict[str, Any]], *, outcome: str = "completed", extensions: frozenset[str] = frozenset()) -> a2a.Task:
    service = AgentA2AService.__new__(AgentA2AService)
    row = {"reference_id": "a2a-task-1", "context_id": "ticket-1", "message_id": "m1",
           "created_at": "2026-11-14T00:00:00+00:00"}
    session = {"session_id": "s1", "title": "", "source_type": "a2a"}
    sent = {"input_id": "i1", "response_id": "r1", "content": "Do it", "state": "answered",
            "created_at": "2026-11-14T00:00:01+00:00"}
    response = {"response_id": "r1", "status": "ended", "outcome": outcome,
                "started_at": "2026-11-14T00:00:02+00:00", "ended_at": "2026-11-14T00:00:03+00:00"}
    return service._task("agent-1", row, session, sent, response, [], chunks, [], extensions)


def _status_text(task: a2a.Task) -> str:
    return "\n".join(part.text for part in task.status.message.parts if part.HasField("text"))


def test_a_reply_without_a_report_stays_completed_with_nothing_extra() -> None:
    task = _task(_reply("All done."), extensions=frozenset({TASK_OUTCOME_EXTENSION}))
    assert task.status.state == a2a.TASK_STATE_COMPLETED
    assert not task.status.message.extensions
    assert [list(artifact.extensions) for artifact in task.artifacts] == [[]]


def test_a_declined_request_is_rejected_with_the_agents_reason() -> None:
    """A client that knows no extension still reads the decline: REJECTED and why."""
    task = _task(_reply("I will not.", {"state": "TASK_STATE_REJECTED", "message": "No such service exists."}))
    assert task.status.state == a2a.TASK_STATE_REJECTED
    assert "No such service exists." in _status_text(task)
    assert not task.status.message.metadata


def test_a_handback_and_code_references_reach_only_a_client_that_activated_the_extension() -> None:
    handback = {"state": "TASK_STATE_REJECTED", "message": "This is the billing team's.", "handback": True}
    plain = _task(_reply("Not mine.", handback))
    assert plain.status.state == a2a.TASK_STATE_REJECTED and not plain.status.message.metadata

    active = _task(_reply("Not mine.", handback), extensions=frozenset({TASK_OUTCOME_EXTENSION}))
    assert list(active.status.message.extensions) == [TASK_OUTCOME_EXTENSION]
    assert MessageToDict(active.status.message.metadata) == {TASK_OUTCOME_EXTENSION: {"handback": True}}

    done = {"state": "TASK_STATE_COMPLETED", "message": "Added HELLO.md.", "repository": "acme/site",
            "branch": "agent/hello", "commit": "ABC1234", "pull_request": "https://github.com/acme/site/pull/3"}
    task = _task(_reply("Done.", done), extensions=frozenset({TASK_OUTCOME_EXTENSION}))
    assert task.status.state == a2a.TASK_STATE_COMPLETED
    assert "Added HELLO.md." in _status_text(task)
    assert list(task.artifacts[0].extensions) == [TASK_OUTCOME_EXTENSION]
    assert MessageToDict(task.artifacts[0].metadata)[TASK_OUTCOME_EXTENSION] == {
        "repository": "acme/site", "branch": "agent/hello", "commit": "abc1234",
        "pull_request": "https://github.com/acme/site/pull/3",
    }


def test_the_last_report_counts_and_a_failed_or_cancelled_response_keeps_its_state() -> None:
    chunks = _reply("Changed my mind.", {"state": "TASK_STATE_REJECTED", "message": "Out of scope."},
                    {"state": "TASK_STATE_COMPLETED", "message": "Did it after all."})
    assert _task(chunks).status.state == a2a.TASK_STATE_COMPLETED
    assert _task(chunks, outcome="failed").status.state == a2a.TASK_STATE_FAILED
    assert _task(chunks, outcome="cancelled").status.state == a2a.TASK_STATE_CANCELED


@pytest.mark.parametrize("arguments", [
    {"state": "TASK_STATE_FAILED", "message": "x"},
    {"state": "TASK_STATE_COMPLETED", "message": "  "},
    {"state": "TASK_STATE_COMPLETED", "message": "x", "handback": True},
    {"state": "TASK_STATE_REJECTED", "message": "x", "handback": "yes"},
    {"state": "TASK_STATE_COMPLETED", "message": "x", "commit": "not-a-commit"},
    {"state": "TASK_STATE_COMPLETED", "message": "x", "branch": "two words"},
    {"state": "TASK_STATE_COMPLETED", "message": "x", "pull_request": "github.com/acme/site/pull/3"},
])
def test_a_report_the_tool_does_not_describe_is_refused(arguments: dict[str, Any]) -> None:
    with pytest.raises(APIError) as refused:
        task_outcome(arguments)
    assert refused.value.code == "INVALID_REQUEST"
