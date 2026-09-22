"""Claude Code's interaction codec — vendor semantics for the interaction seam.

Everything here is Claude vocabulary: which native tools present as which
structural contract, how the SDK expects ``AskUserQuestion`` answers keyed,
which permission modes an approved plan may transition into, and the exact
model-facing reply copy. The platform carries the produced contract verbatim
and never consults these names; the runner consumes the encoded answer over
the existing wire (``allow``/``deny`` plus an effective tool input).
"""

from copy import deepcopy
from typing import Any, get_args

from claude_agent_sdk.types import PermissionMode

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_DECISION,
    PRESENTATION_FORM,
    PRESENTATION_TOOL_APPROVAL,
    is_denied_interaction_response,
    render_interaction_reply,
    validated_question_answer_rows,
)

#: The modes the plan-approval card offers, in display order. A subset of the
#: vendor set by product choice; membership is asserted below so an SDK
#: release that renames one fails at import instead of shipping a dead button.
_PLAN_APPROVE_MODE_CHOICES = ["bypassPermissions", "acceptEdits", "default"]
_PLAN_REVIEW_MODE = "plan"

_SDK_PERMISSION_MODES = frozenset(get_args(PermissionMode))
_UNDECLARED_MODES = (
    set(_PLAN_APPROVE_MODE_CHOICES) | {_PLAN_REVIEW_MODE}
) - _SDK_PERMISSION_MODES
if _UNDECLARED_MODES:
    raise RuntimeError(
        "claude interaction codec references permission modes the pinned SDK "
        f"does not declare: {sorted(_UNDECLARED_MODES)}"
    )


def derive_native_question_key(
    item: dict[str, Any],
    *,
    question_id: str,
    index: int,
) -> str:
    question_text = str(item.get("question") or "").strip()
    if question_text:
        return question_text
    header = str(item.get("header") or "").strip()
    if header:
        return header
    if question_id:
        return question_id
    return f"Question {index + 1}"


def normalize_pending_preset_answers(
    *,
    raw_questions: list[dict[str, Any]],
    raw_answers: dict[str, Any],
) -> dict[str, str] | None:
    normalized: dict[str, str] = {}
    for index, item in enumerate(raw_questions):
        question_id = str(item.get("id") or "").strip() or f"question_{index + 1}"
        candidates = [
            question_id,
            derive_native_question_key(item, question_id=question_id, index=index),
            str(item.get("header") or "").strip(),
        ]
        value = ""
        for candidate in candidates:
            if not candidate:
                continue
            current = str(raw_answers.get(candidate) or "").strip()
            if current:
                value = current
                break
        if value:
            normalized[question_id] = value
    return normalized or None


def _form_contract(tool_name: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    questions = []
    raw_preset_answers = input_payload.get("answers")
    raw_questions = input_payload.get("questions")
    normalized_raw_questions: list[dict[str, Any]] = []
    if isinstance(raw_questions, list):
        for index, item in enumerate(raw_questions):
            if not isinstance(item, dict):
                continue
            normalized_raw_questions.append(item)
            question_id = str(item.get("id") or "").strip() or f"question_{index + 1}"
            native_answer_key = derive_native_question_key(
                item,
                question_id=question_id,
                index=index,
            )
            options = []
            raw_options = item.get("options")
            if isinstance(raw_options, list):
                for option in raw_options:
                    if not isinstance(option, dict):
                        continue
                    label = str(option.get("label") or "").strip()
                    if not label:
                        continue
                    options.append(
                        {
                            "label": label,
                            "description": str(option.get("description") or "").strip() or None,
                        }
                    )
            questions.append(
                {
                    "id": question_id,
                    "header": str(item.get("header") or "").strip() or None,
                    "question": str(item.get("question") or "").strip() or None,
                    "native_answer_key": native_answer_key,
                    "multi_select": bool(item.get("multiSelect")) if "multiSelect" in item else None,
                    "allow_free_text": True,
                    "allow_empty_text": False,
                    "options": options,
                }
            )
    return {
        "tool_name": tool_name,
        "presentation": PRESENTATION_FORM,
        "prompt": "Please answer the following questions",
        "raw_input": deepcopy(input_payload),
        "questions": questions,
        "preset_answers": (
            normalize_pending_preset_answers(
                raw_questions=normalized_raw_questions,
                raw_answers=raw_preset_answers,
            )
            if isinstance(raw_preset_answers, dict)
            else None
        ),
    }


def _plan_decision_contract(tool_name: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "presentation": PRESENTATION_DECISION,
        "prompt": "Please confirm whether to exit plan mode",
        "raw_input": deepcopy(input_payload),
        "body": str(input_payload.get("plan") or "").strip() or None,
        "options": [
            {
                "id": "approve",
                "denial": False,
                "reply": (
                    "Response to your pending plan confirmation:\n"
                    "I approve exiting plan mode. Proceed with the plan you proposed."
                ),
                "permission_mode_choices": list(_PLAN_APPROVE_MODE_CHOICES),
                "default_permission_mode": "default",
            },
            {
                "id": "revise",
                "denial": True,
                "reply": (
                    "Response to your pending plan confirmation:\n"
                    "Do not exit plan mode yet. Stay in plan mode and continue "
                    "refining the plan."
                ),
                "comment_prefix": "Focus next on",
                "applies_permission_mode": _PLAN_REVIEW_MODE,
            },
            {
                "id": "reject",
                "denial": True,
                "reply": (
                    "Response to your pending plan confirmation:\n"
                    "User rejected Claude's plan."
                ),
                "applies_permission_mode": _PLAN_REVIEW_MODE,
            },
        ],
    }


def build_claude_interaction_contract(
    *,
    tool_name: str,
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    """Map one gated Claude tool onto its structural contract.

    ``AskUserQuestion`` and ``ExitPlanMode`` are the two tools the CLI routes
    through interactive flows of their own; every other gate is an approval
    over the tool call. This is the only place that mapping exists.
    """
    if tool_name == "AskUserQuestion":
        return _form_contract(tool_name, input_payload)
    if tool_name == "ExitPlanMode":
        return _plan_decision_contract(tool_name, input_payload)
    return {
        "tool_name": tool_name,
        "presentation": PRESENTATION_TOOL_APPROVAL,
        "prompt": f"Allow {tool_name} to continue?",
        "raw_input": deepcopy(input_payload),
    }


def _ask_user_updated_input(
    *,
    input_payload: dict[str, Any],
    pending: dict[str, Any],
    interaction_response: dict[str, Any],
) -> dict[str, Any]:
    raw_questions = input_payload.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise APIError(
            code="INVALID_REQUEST",
            message="pending interaction raw questions are missing",
            status_code=409,
        )

    sdk_answers: dict[str, str] = {}
    for row in validated_question_answer_rows(pending, interaction_response):
        question = row["question"]
        question_id = str(question.get("id") or "").strip()
        answer_key = str(question.get("native_answer_key") or "").strip()
        if not answer_key:
            answer_key = derive_native_question_key(
                question,
                question_id=question_id,
                index=len(sdk_answers),
            )
        sdk_answers[answer_key] = str(row["response_text"])

    return {
        "questions": deepcopy(raw_questions),
        "answers": sdk_answers,
    }


def claude_answer_fields(
    pending: dict[str, Any],
    interaction_response: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Encode a structurally validated answer for the Claude runner wire.

    An approval becomes ``allow``; ``AskUserQuestion`` answers ride inside the
    tool's effective input per the SDK contract (``{"questions": …, "answers":
    {question: response}}``); every deny carries the rendered reply as the
    model-facing feedback — the phrasing the model reacts to is a contract,
    not a formatting detail.
    """
    presentation = str(pending.get("presentation") or "").strip()
    if presentation == PRESENTATION_FORM:
        if interaction_response.get("decline") is True:
            return (
                "deny",
                None,
                render_interaction_reply(pending, interaction_response),
            )
        raw_input = pending.get("raw_input")
        if not isinstance(raw_input, dict):
            raise APIError(
                code="INVALID_REQUEST",
                message="pending interaction raw input is missing",
                status_code=409,
            )
        return (
            "allow",
            _ask_user_updated_input(
                input_payload=raw_input,
                pending=pending,
                interaction_response=interaction_response,
            ),
            None,
        )
    if is_denied_interaction_response(pending, interaction_response):
        return (
            "deny",
            None,
            render_interaction_reply(pending, interaction_response),
        )
    return "allow", None, None
