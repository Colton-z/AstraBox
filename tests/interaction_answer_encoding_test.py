"""An interaction answer as the model will experience it.

The answer path has two halves and this pins both. The platform half
(``interaction_contract``) checks the answer against the presentation the
adapter declared and renders the transcript reply from the record's own
structure; the Claude half (``claude_interaction_codec``) encodes that result
for the runner wire — ``allow`` with the tool's effective input, or ``deny``
with model-facing feedback.

The rendered text is a contract, not a formatting detail: the model reacts to
it, ``tool_result_semantics`` classifies a denied tool result by matching it,
and the console localizes it by exact string. The expected strings below are
therefore byte-exact on purpose.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.claude_interaction_codec import (
    build_claude_interaction_contract,
    claude_answer_fields,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_pending_interaction_record,
    render_interaction_reply,
    validate_interaction_contract,
    validate_interaction_response,
)

_ASK_QUESTIONS = [
    {
        "header": "Database",
        "question": "Which db?",
        "multiSelect": False,
        "options": [{"label": "sqlite", "description": "Local database"}],
    },
    {
        "header": "Region",
        "question": "Which region?",
        "multiSelect": False,
        "options": [],
    },
]


def _pending(*, tool_name: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    """A durable record the way the seam builds it, adapter declaration first."""
    contract = build_claude_interaction_contract(
        tool_name=tool_name,
        input_payload=input_payload,
    )
    validate_interaction_contract(contract)
    return build_pending_interaction_record(
        contract=contract,
        session_id="session-1",
        turn_id="turn-1",
        interaction_id="interaction-1",
        tool_call_id="toolu_01",
    )


def _permission_pending() -> dict[str, Any]:
    return _pending(tool_name="Bash", input_payload={"command": "rm -rf build"})


def _ask_pending() -> dict[str, Any]:
    return _pending(
        tool_name="AskUserQuestion",
        input_payload={"questions": _ASK_QUESTIONS},
    )


def _plan_pending() -> dict[str, Any]:
    return _pending(tool_name="ExitPlanMode", input_payload={"plan": "1. do the thing"})


def test_approve_is_a_bare_allow() -> None:
    choice, updated_input, message = claude_answer_fields(
        _permission_pending(), {"decision": "approve"}
    )
    assert (choice, updated_input, message) == ("allow", None, None)


def test_reject_denies_with_the_model_facing_feedback() -> None:
    choice, updated_input, message = claude_answer_fields(
        _permission_pending(), {"decision": "reject", "comment": "use tar instead"}
    )
    assert choice == "deny"
    assert updated_input is None
    assert message == (
        "Response to your pending tool confirmation:\n"
        "The user doesn't want to proceed with this tool use.\n"
        "Additional notes: use tar instead"
    )


def test_ask_user_question_answers_ride_in_updated_input() -> None:
    pending = _ask_pending()
    # The browser answers by the platform question id; the SDK's answer map is
    # keyed by the native key the record carries next to it.
    assert [question["id"] for question in pending["questions"]] == [
        "question_1",
        "question_2",
    ]
    assert [question["native_answer_key"] for question in pending["questions"]] == [
        "Which db?",
        "Which region?",
    ]
    choice, updated_input, message = claude_answer_fields(
        pending,
        {
            "answers": [
                {"question_id": "question_1", "option_label": "sqlite"},
                {"question_id": "question_2", "free_text": "eu-west-1"},
            ]
        },
    )
    assert choice == "allow"
    assert message is None
    assert updated_input is not None
    assert updated_input["questions"] == _ASK_QUESTIONS
    assert updated_input["answers"] == {
        "Which db?": "sqlite",
        "Which region?": "eu-west-1",
    }


def test_answered_questions_render_the_shared_reply() -> None:
    reply = render_interaction_reply(
        _ask_pending(),
        {
            "answers": [
                {"question_id": "question_1", "option_label": "sqlite"},
                {"question_id": "question_2", "free_text": "eu-west-1"},
            ],
            "notes": "keep it cheap",
        },
    )
    assert reply == (
        "Response to your pending questions:\n"
        "- Database: sqlite\n"
        "- Region: eu-west-1\n"
        "\n"
        "Additional notes: keep it cheap"
    )


def test_ask_user_question_decline_denies_with_feedback() -> None:
    choice, updated_input, message = claude_answer_fields(
        _ask_pending(), {"decline": True}
    )
    assert choice == "deny"
    assert updated_input is None
    assert message == "User declined to answer questions."


def test_plan_approval_is_a_bare_allow() -> None:
    choice, updated_input, message = claude_answer_fields(
        _plan_pending(), {"decision": "approve"}
    )
    assert (choice, updated_input, message) == ("allow", None, None)


def test_plan_revision_denies_with_its_declared_reply() -> None:
    choice, updated_input, message = claude_answer_fields(
        _plan_pending(), {"decision": "revise", "comment": "the migration step"}
    )
    assert choice == "deny"
    assert updated_input is None
    assert message == (
        "Response to your pending plan confirmation:\n"
        "Do not exit plan mode yet. Stay in plan mode and continue refining the plan.\n"
        "Focus next on: the migration step"
    )


def test_plan_rejection_denies_with_its_declared_reply() -> None:
    choice, updated_input, message = claude_answer_fields(
        _plan_pending(), {"decision": "reject", "comment": "wrong direction"}
    )
    assert choice == "deny"
    assert updated_input is None
    assert message == (
        "Response to your pending plan confirmation:\n"
        "User rejected Claude's plan.\n"
        "Additional notes: wrong direction"
    )


def test_invalid_decision_fails_loud() -> None:
    # Both halves refuse it: the platform gate runs first in the answer path,
    # and the codec never encodes an answer the presentation cannot represent.
    with pytest.raises(APIError):
        validate_interaction_response(_permission_pending(), {"decision": "maybe"})
    with pytest.raises(APIError):
        claude_answer_fields(_permission_pending(), {"decision": "maybe"})


def test_a_decision_outside_the_declared_options_fails_loud() -> None:
    with pytest.raises(APIError):
        validate_interaction_response(_plan_pending(), {"decision": "approve_all"})
    with pytest.raises(APIError):
        claude_answer_fields(_plan_pending(), {"decision": "approve_all"})


def test_missing_answers_fail_loud() -> None:
    with pytest.raises(APIError):
        validate_interaction_response(_ask_pending(), {"answers": []})
    with pytest.raises(APIError):
        claude_answer_fields(_ask_pending(), {"answers": []})
