"""Structural interaction contracts — the platform half of the interaction seam.

An engine adapter that raises a pending interaction declares how the user can
answer it — a multi-question form, a decision among adapter-declared options,
or an approval bound to a tool call — never what the interaction means to its
vendor. The vendor identity (the exact native tool name, the native call id,
the raw native input) rides the record verbatim; platform code and the
frontend dispatch only on the structural ``presentation`` field. Everything
the answer lifecycle needs later — option semantics, transcript reply copy, a
requested permission-mode transition — is authored by the adapter into the
durable contract at declaration time, so no platform module ever consults a
vendor vocabulary to resolve an answer.
"""

from copy import deepcopy
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso

PRESENTATION_FORM = "form"
PRESENTATION_DECISION = "decision"
PRESENTATION_TOOL_APPROVAL = "tool_approval"

INTERACTION_PRESENTATIONS = frozenset(
    {PRESENTATION_FORM, PRESENTATION_DECISION, PRESENTATION_TOOL_APPROVAL}
)

#: Keys an adapter may declare on an ``interaction.request`` contract. The
#: seam is closed-world on purpose: a key outside this set is a drifting
#: adapter, not a forward-compatible extension.
_CONTRACT_KEYS_COMMON = frozenset({"tool_name", "presentation", "prompt", "raw_input"})
_CONTRACT_KEYS_BY_PRESENTATION = {
    PRESENTATION_FORM: _CONTRACT_KEYS_COMMON | {"questions", "preset_answers"},
    PRESENTATION_DECISION: _CONTRACT_KEYS_COMMON | {"body", "options"},
    PRESENTATION_TOOL_APPROVAL: _CONTRACT_KEYS_COMMON,
}

_QUESTION_ROW_KEYS = frozenset(
    {"id", "header", "question", "native_answer_key", "multi_select", "options",
     "allow_free_text", "allow_empty_text"}
)
_QUESTION_OPTION_KEYS = frozenset({"label", "description"})
_DECISION_OPTION_KEYS = frozenset(
    {
        "id",
        "denial",
        "reply",
        "comment_prefix",
        "permission_mode_choices",
        "default_permission_mode",
        "applies_permission_mode",
    }
)

_PUBLIC_INTERACTION_COMMON_KEYS = frozenset(
    {
        "interaction_id",
        "turn_id",
        "tool_call_id",
        "tool_name",
        "presentation",
        "prompt",
    }
)
_PUBLIC_QUESTION_KEYS = frozenset(
    {"id", "header", "question", "multi_select", "options",
     "allow_free_text", "allow_empty_text"}
)
_PUBLIC_DECISION_OPTION_KEYS = frozenset(
    {
        "id",
        "denial",
        "permission_mode_choices",
        "default_permission_mode",
        "applies_permission_mode",
    }
)


class InteractionContractError(RuntimeError):
    """An adapter declared a malformed interaction contract.

    This is an integration defect on the adapter side of the seam, so it
    fails the turn loudly instead of degrading to a guessed presentation.
    """


def _require_str(contract: dict[str, Any], key: str, *, where: str) -> str:
    value = contract.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InteractionContractError(
            f"interaction contract {where}: {key!r} must be a non-empty string"
        )
    return value.strip()


def _validate_form_contract(contract: dict[str, Any]) -> None:
    questions = contract.get("questions")
    if not isinstance(questions, list) or not questions:
        raise InteractionContractError(
            "interaction contract form: 'questions' must be a non-empty list"
        )
    seen_ids: set[str] = set()
    for row in questions:
        if not isinstance(row, dict):
            raise InteractionContractError(
                "interaction contract form: question rows must be dicts"
            )
        unknown = set(row) - _QUESTION_ROW_KEYS
        if unknown:
            raise InteractionContractError(
                f"interaction contract form: unknown question keys {sorted(unknown)}"
            )
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id.strip():
            raise InteractionContractError(
                "interaction contract form: question 'id' must be a non-empty string"
            )
        if row_id in seen_ids:
            raise InteractionContractError(
                f"interaction contract form: duplicate question id {row_id!r}"
            )
        seen_ids.add(row_id)
        for key in ("allow_free_text", "allow_empty_text"):
            if not isinstance(row.get(key), bool):
                raise InteractionContractError(
                    f"interaction contract form: question {key!r} must be a bool"
                )
        if row["allow_empty_text"] and not row["allow_free_text"]:
            raise InteractionContractError(
                "interaction contract form: empty text requires free text"
            )
        options = row.get("options")
        if not isinstance(options, list):
            raise InteractionContractError(
                "interaction contract form: question 'options' must be a list"
            )
        for option in options:
            if not isinstance(option, dict) or set(option) - _QUESTION_OPTION_KEYS:
                raise InteractionContractError(
                    "interaction contract form: question options carry only "
                    "'label' and 'description'"
                )
            if not isinstance(option.get("label"), str) or not option["label"].strip():
                raise InteractionContractError(
                    "interaction contract form: option 'label' must be a "
                    "non-empty string"
                )
    preset_answers = contract.get("preset_answers")
    if preset_answers is not None and not isinstance(preset_answers, dict):
        raise InteractionContractError(
            "interaction contract form: 'preset_answers' must be a dict or None"
        )


def _validate_decision_contract(contract: dict[str, Any]) -> None:
    body = contract.get("body")
    if body is not None and not isinstance(body, str):
        raise InteractionContractError(
            "interaction contract decision: 'body' must be a string or None"
        )
    options = contract.get("options")
    if not isinstance(options, list) or not options:
        raise InteractionContractError(
            "interaction contract decision: 'options' must be a non-empty list"
        )
    seen_ids: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            raise InteractionContractError(
                "interaction contract decision: options must be dicts"
            )
        unknown = set(option) - _DECISION_OPTION_KEYS
        if unknown:
            raise InteractionContractError(
                f"interaction contract decision: unknown option keys {sorted(unknown)}"
            )
        option_id = option.get("id")
        if not isinstance(option_id, str) or not option_id.strip():
            raise InteractionContractError(
                "interaction contract decision: option 'id' must be a non-empty string"
            )
        if option_id in seen_ids:
            raise InteractionContractError(
                f"interaction contract decision: duplicate option id {option_id!r}"
            )
        seen_ids.add(option_id)
        if not isinstance(option.get("denial"), bool):
            raise InteractionContractError(
                f"interaction contract decision: option {option_id!r} must declare "
                "'denial' as a bool"
            )
        if not isinstance(option.get("reply"), str) or not option["reply"].strip():
            raise InteractionContractError(
                f"interaction contract decision: option {option_id!r} must declare "
                "a non-empty 'reply' — the transcript copy is the adapter's to "
                "author, and the platform has no vocabulary to invent one from"
            )
        mode_choices = option.get("permission_mode_choices")
        if mode_choices is not None:
            if (
                not isinstance(mode_choices, list)
                or not mode_choices
                or any(not isinstance(m, str) or not m.strip() for m in mode_choices)
            ):
                raise InteractionContractError(
                    f"interaction contract decision: option {option_id!r} "
                    "'permission_mode_choices' must be a non-empty list of "
                    "non-empty strings"
                )
        for key in ("comment_prefix", "default_permission_mode", "applies_permission_mode"):
            value = option.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise InteractionContractError(
                    f"interaction contract decision: option {option_id!r} {key!r} "
                    "must be a non-empty string or None"
                )


def validate_interaction_contract(contract: dict[str, Any]) -> str:
    """Validate an adapter-declared contract; return its presentation.

    Raises :class:`InteractionContractError` on any malformed declaration —
    before the record is persisted, so a drifting adapter fails its turn
    instead of seeding the stores with a shape no consumer can answer.
    """
    if not isinstance(contract, dict):
        raise InteractionContractError("interaction contract must be a dict")
    presentation = _require_str(contract, "presentation", where="common")
    if presentation not in INTERACTION_PRESENTATIONS:
        raise InteractionContractError(
            f"interaction contract: unknown presentation {presentation!r} "
            f"(expected one of {sorted(INTERACTION_PRESENTATIONS)})"
        )
    _require_str(contract, "tool_name", where="common")
    _require_str(contract, "prompt", where="common")
    if not isinstance(contract.get("raw_input"), dict):
        raise InteractionContractError(
            "interaction contract: 'raw_input' must be a dict"
        )
    allowed = _CONTRACT_KEYS_BY_PRESENTATION[presentation]
    unknown = set(contract) - allowed
    if unknown:
        raise InteractionContractError(
            f"interaction contract {presentation}: unknown keys {sorted(unknown)}"
        )
    if presentation == PRESENTATION_FORM:
        _validate_form_contract(contract)
    elif presentation == PRESENTATION_DECISION:
        _validate_decision_contract(contract)
    return presentation


def build_pending_interaction_record(
    *,
    contract: dict[str, Any],
    session_id: str,
    turn_id: str,
    interaction_id: str,
    tool_call_id: str | None,
) -> dict[str, Any]:
    """Wrap a validated contract in the platform envelope.

    The contract fields are carried verbatim; the platform adds only identity
    and timing it owns. Callers validate first — this function trusts its
    input exactly because :func:`validate_interaction_contract` already
    refused every malformed shape.
    """
    record: dict[str, Any] = {
        "interaction_id": interaction_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "created_at": utcnow_iso(),
        **deepcopy(contract),
    }
    normalized_tool_call_id = str(tool_call_id or "").strip() or None
    if normalized_tool_call_id:
        record["tool_call_id"] = normalized_tool_call_id
    return record


def public_interaction_view(
    value: Any,
    *,
    include_tool_input: bool = True,
) -> dict[str, Any] | None:
    """Project the browser-facing half of a durable interaction record.

    Adapter-authored transcript replies, native answer keys and settlement
    watermarks are control data. The owner UI may see the tool input it is
    being asked to approve; read-only shares do not need it.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise InteractionContractError("pending interaction must be an object")
    presentation = interaction_presentation(value)
    public = {
        key: deepcopy(value[key])
        for key in _PUBLIC_INTERACTION_COMMON_KEYS
        if key in value
    }
    if include_tool_input and "raw_input" in value:
        public["raw_input"] = deepcopy(value.get("raw_input"))

    if presentation == PRESENTATION_FORM:
        questions: list[dict[str, Any]] = []
        for row in value.get("questions") or []:
            if not isinstance(row, dict):
                raise InteractionContractError("interaction question must be an object")
            question = {
                key: deepcopy(row[key])
                for key in _PUBLIC_QUESTION_KEYS
                if key in row
            }
            options: list[dict[str, Any]] = []
            for option in row.get("options") or []:
                if not isinstance(option, dict):
                    raise InteractionContractError(
                        "interaction question option must be an object"
                    )
                options.append(
                    {
                        key: deepcopy(option[key])
                        for key in _QUESTION_OPTION_KEYS
                        if key in option
                    }
                )
            question["options"] = options
            questions.append(question)
        public["questions"] = questions
        if "preset_answers" in value:
            public["preset_answers"] = deepcopy(value.get("preset_answers"))
    elif presentation == PRESENTATION_DECISION:
        if "body" in value:
            public["body"] = deepcopy(value.get("body"))
        public["options"] = [
            {
                key: deepcopy(option[key])
                for key in _PUBLIC_DECISION_OPTION_KEYS
                if key in option
            }
            for option in value.get("options") or []
            if isinstance(option, dict)
        ]
    return public


def interaction_presentation(pending: dict[str, Any]) -> str:
    """Read the structural presentation off a durable record, loudly.

    A record without one is not answerable — no consumer can render it and no
    validator can check a response against it — so absence is an error, never
    a guessed default.
    """
    presentation = str(pending.get("presentation") or "").strip()
    if presentation not in INTERACTION_PRESENTATIONS:
        raise APIError(
            code="INVALID_REQUEST",
            message=(
                "pending interaction carries no structural presentation "
                f"(got {presentation!r})"
            ),
            status_code=409,
        )
    return presentation


def validated_question_answer_rows(
    pending: dict[str, Any],
    interaction_response: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_answers = interaction_response.get("answers")
    if not isinstance(raw_answers, list) or not raw_answers:
        raise APIError(
            code="INVALID_REQUEST",
            message="interaction answers are required",
            status_code=400,
        )

    answer_by_question: dict[str, dict[str, Any]] = {}
    for item in raw_answers:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("question_id") or "").strip()
        if question_id:
            answer_by_question[question_id] = item

    questions = pending.get("questions")
    if not isinstance(questions, list) or not questions:
        raise APIError(
            code="INVALID_REQUEST",
            message="pending interaction questions are missing",
            status_code=409,
        )

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("id") or "").strip()
        answer = answer_by_question.get(question_id)
        if answer is None:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"missing answer for question '{question_id or index + 1}'",
                status_code=400,
            )
        option_labels = answer.get("option_labels")
        if isinstance(option_labels, list):
            labels = [value for value in option_labels if isinstance(value, str)]
        else:
            labels = []
        option_label = answer.get("option_label")
        free_text = answer.get("free_text")
        if isinstance(free_text, str) and item.get("allow_free_text") is not True:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"question '{question_id}' does not accept free text",
                status_code=400,
            )
        if labels:
            response_text = ", ".join(labels)
        elif isinstance(option_label, str):
            response_text = option_label
        elif isinstance(free_text, str):
            response_text = free_text
        else:
            response_text = ""
        if not response_text and not (
            isinstance(free_text, str) and item.get("allow_empty_text") is True
        ):
            raise APIError(
                code="INVALID_REQUEST",
                message=f"answer for question '{question_id or index + 1}' is empty",
                status_code=400,
            )
        prompt = (
            str(item.get("header") or "").strip()
            or str(item.get("question") or "").strip()
            or question_id
            or f"Question {index + 1}"
        )
        rows.append(
            {
                "question": item,
                "answer": answer,
                "prompt": prompt,
                "response_text": response_text,
            }
        )
    return rows


def selected_decision_option(
    pending: dict[str, Any],
    interaction_response: dict[str, Any],
) -> dict[str, Any]:
    """Find the declared option an answer names, matching the id exactly.

    Case is part of the id, not formatting: the ids are the vocabulary the
    adapter declared and the engine will be answered in, and Codex's are
    camelCase (`acceptForSession`). Folding case here would refuse an answer
    the console had rendered correctly, so the comparison is exact.
    """

    decision = str(interaction_response.get("decision") or "").strip()
    options = [
        option for option in (pending.get("options") or []) if isinstance(option, dict)
    ]
    for option in options:
        if str(option.get("id") or "").strip() == decision:
            return option
    option_ids = ", ".join(str(option.get("id") or "") for option in options)
    raise APIError(
        code="INVALID_REQUEST",
        message=f"interaction decision must be one of: {option_ids}",
        status_code=400,
    )


def _validated_tool_approval_decision(interaction_response: dict[str, Any]) -> str:
    decision = str(interaction_response.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject"}:
        raise APIError(
            code="INVALID_REQUEST",
            message="interaction decision must be approve or reject",
            status_code=400,
        )
    return decision


def validate_interaction_response(
    pending: dict[str, Any],
    interaction_response: dict[str, Any],
) -> None:
    """Check a browser answer against the record's declared structure.

    Vendor meaning is not judged here — that is the adapter's half. This
    gate only refuses answers the declared presentation cannot represent.
    """
    presentation = interaction_presentation(pending)
    if presentation == PRESENTATION_FORM:
        if interaction_response.get("decline") is True:
            return
        validated_question_answer_rows(pending, interaction_response)
        return
    if presentation == PRESENTATION_DECISION:
        selected_decision_option(pending, interaction_response)
        return
    _validated_tool_approval_decision(interaction_response)


def is_denied_interaction_response(
    pending: dict[str, Any],
    interaction_response: dict[str, Any],
) -> bool:
    presentation = interaction_presentation(pending)
    if presentation == PRESENTATION_FORM:
        return interaction_response.get("decline") is True
    if presentation == PRESENTATION_DECISION:
        option = selected_decision_option(pending, interaction_response)
        return bool(option.get("denial"))
    return _validated_tool_approval_decision(interaction_response) == "reject"


def resolve_interaction_permission_mode(
    pending: dict[str, Any] | None,
    interaction_response: dict[str, Any] | None,
) -> str | None:
    """Read the answer's permission-mode transition off the declared option.

    The mode strings are the engine's exact vocabulary, authored into the
    contract by its adapter; PermissionLifecycle still validates the returned
    name against that engine's manifest before applying it.
    """
    if not pending or not interaction_response:
        return None
    if interaction_presentation(pending) != PRESENTATION_DECISION:
        return None
    option = selected_decision_option(pending, interaction_response)
    mode_choices = option.get("permission_mode_choices")
    if isinstance(mode_choices, list) and mode_choices:
        requested = str(interaction_response.get("permission_mode") or "").strip()
        fallback = str(option.get("default_permission_mode") or "").strip()
        return requested or fallback or None
    applies = str(option.get("applies_permission_mode") or "").strip()
    return applies or None


def render_interaction_reply(
    pending: dict[str, Any],
    interaction_response: dict[str, Any],
) -> str:
    """Render the transcript-facing text for an answered interaction.

    Form and tool-approval copy is structural and shared across engines; a
    decision's copy comes verbatim from the answered option's declared
    ``reply``. The exact phrasing is a contract with adapter-side transcript
    parsers (see ``tool_result_semantics``), not a formatting preference —
    change it only together with every consumer that matches on it.
    """
    presentation = interaction_presentation(pending)

    if presentation == PRESENTATION_FORM:
        if interaction_response.get("decline") is True:
            return "User declined to answer questions."
        lines = ["Response to your pending questions:"]
        for row in validated_question_answer_rows(pending, interaction_response):
            lines.append(f"- {row['prompt']}: {row['response_text']}")
        notes = str(interaction_response.get("notes") or "").strip()
        if notes:
            lines.append("")
            lines.append(f"Additional notes: {notes}")
        return "\n".join(lines)

    if presentation == PRESENTATION_DECISION:
        option = selected_decision_option(pending, interaction_response)
        lines = [str(option.get("reply") or "")]
        comment = str(interaction_response.get("comment") or "").strip()
        if comment:
            prefix = str(option.get("comment_prefix") or "").strip() or "Additional notes"
            lines.append(f"{prefix}: {comment}")
        return "\n".join(lines)

    decision = _validated_tool_approval_decision(interaction_response)
    lines = [
        "Response to your pending tool confirmation:",
        (
            "The user approved this tool use."
            if decision == "approve"
            else "The user doesn't want to proceed with this tool use."
        ),
    ]
    comment = str(interaction_response.get("comment") or "").strip()
    if comment:
        lines.append(f"Additional notes: {comment}")
    return "\n".join(lines)


def build_tool_approval_request_frame(pending: dict[str, Any]) -> dict[str, Any] | None:
    """Map a pending tool-approval interaction onto the AI SDK
    ``tool-approval-request`` stream frame.

    Only the ``tool_approval`` presentation participates in the native
    approval lifecycle; form and decision interactions are delivered as
    ``data-interaction`` parts and answered through their own flows. A
    tool approval without a model-tracked tool call id has no tool part for
    the client to attach the approval to, so it stays on the
    ``data-interaction`` representation as well.
    """
    if str(pending.get("presentation") or "") != PRESENTATION_TOOL_APPROVAL:
        return None
    tool_call_id = str(pending.get("tool_call_id") or "").strip()
    if not tool_call_id:
        return None
    approval_id = str(pending.get("interaction_id") or "").strip() or tool_call_id
    return {
        "type": "tool-approval-request",
        "approvalId": approval_id,
        "toolCallId": tool_call_id,
    }


def build_tool_approval_response_frame(
    pending: dict[str, Any],
    interaction_response: dict[str, Any],
) -> dict[str, Any] | None:
    """Map an answered tool-approval interaction onto the AI SDK
    ``tool-approval-response`` stream frame.

    The approval id mirrors :func:`build_tool_approval_request_frame` so the
    client resolves the response against the request it already holds. The
    operator comment travels as the approval ``reason`` when present. The
    tool call itself still settles through a later ``tool-output-*`` frame;
    this frame only records the approval decision.
    """
    request_frame = build_tool_approval_request_frame(pending)
    if request_frame is None:
        return None
    frame: dict[str, Any] = {
        "type": "tool-approval-response",
        "approvalId": request_frame["approvalId"],
        "approved": not is_denied_interaction_response(pending, interaction_response),
    }
    comment = str(interaction_response.get("comment") or "").strip()
    if comment:
        frame["reason"] = comment
    return frame
