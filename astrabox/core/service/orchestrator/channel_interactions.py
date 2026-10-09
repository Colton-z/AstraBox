"""Show a pending interaction in a channel and read a participant's answer.

The interaction record is the engine-neutral contract
(``engine/interaction_contract.py``); this module only projects its public view
into one channel message and turns a reply back into the contract's answer
shape. Which engine asked, and what an answer means to it, stays with the
engine adapter.

A form is shown one question at a time, so every reply is about exactly one
question: the one in the message it quotes, or the one shown last.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_DECISION,
    PRESENTATION_FORM,
    PRESENTATION_TOOL_APPROVAL,
)
from astrabox.seams.channel import ChannelChoice, ChannelInteraction

#: Prefix of every action id this module mints. Telegram limits a button's
#: callback data to 64 bytes; ``ix1:<16 hex>:<question>:<choice>`` stays far
#: below it.
_ACTION_PREFIX = "ix1"
_ACTION_PATTERN = re.compile(r"^ix1:([0-9a-f]{16}):(\d{1,2}):(\d{1,3})$")
#: A reply made only of option numbers, separated by commas or spaces.
_SELECTOR_PATTERN = re.compile(r"^\s*\d+(?:\s*[,，、\s]\s*\d+)*\s*$")
_TOOL_INPUT_LIMIT = 600
_APPROVAL_LABELS = ("Approve", "Reject")


def interaction_ref(deployment_id: str, session_id: str, interaction_id: str) -> str:
    """The short, stable name a delivered question's buttons carry."""
    digest = hashlib.sha256(
        f"{deployment_id}\0{session_id}\0{interaction_id}".encode()
    ).hexdigest()
    return digest[:16]


def question_count(view: dict[str, Any]) -> int:
    """How many questions the participant answers before the engine gets one answer."""
    if view.get("presentation") == PRESENTATION_FORM:
        return len(_questions(view))
    return 1


def channel_interaction(
    view: dict[str, Any],
    *,
    question_index: int,
    ref: str,
    actions: bool,
) -> ChannelInteraction:
    """Project question ``question_index`` (from 0) of a public interaction view."""
    presentation = str(view.get("presentation") or "")
    total = question_count(view)
    if not 0 <= question_index < total:
        raise ValueError(f"interaction has no question {question_index + 1}")
    interaction_id = str(view.get("interaction_id") or "")
    if presentation == PRESENTATION_FORM:
        question = _questions(view)[question_index]
        header = str(question.get("header") or "").strip()
        text = str(question.get("question") or "").strip()
        title = f"{header}: {text}" if header and text and header != text else (text or header)
        labels = [
            (str(option.get("label") or ""), str(option.get("description") or ""))
            for option in question.get("options") or []
            if isinstance(option, dict)
        ]
        multi_select = question.get("multi_select") is True
        free_text = question.get("allow_free_text") is True
        detail = ""
    elif presentation == PRESENTATION_DECISION:
        title = str(view.get("prompt") or "").strip()
        detail = str(view.get("body") or "").strip()
        labels = [
            (str(option.get("id") or ""), "")
            for option in view.get("options") or []
            if isinstance(option, dict)
        ]
        multi_select = False
        free_text = False
    elif presentation == PRESENTATION_TOOL_APPROVAL:
        title = str(view.get("prompt") or "").strip()
        detail = _tool_input_summary(view.get("raw_input"))
        labels = [(label, "") for label in _APPROVAL_LABELS]
        multi_select = False
        free_text = False
    else:
        raise ValueError(f"unknown interaction presentation {presentation!r}")
    choices = tuple(
        ChannelChoice(
            number=number,
            label=label,
            description=description,
            action=(
                f"{_ACTION_PREFIX}:{ref}:{question_index}:{number}"
                if actions and not multi_select
                else ""
            ),
        )
        for number, (label, description) in enumerate(labels, start=1)
    )
    position = (question_index + 1, total)
    return ChannelInteraction(
        interaction_id=interaction_id,
        presentation=presentation,
        title=title,
        detail=detail,
        choices=choices,
        multi_select=multi_select,
        free_text=free_text,
        position=position,
        text=_render(title, detail, choices, multi_select, free_text, position),
    )


def _render(
    title: str,
    detail: str,
    choices: tuple[ChannelChoice, ...],
    multi_select: bool,
    free_text: bool,
    position: tuple[int, int],
) -> str:
    lines = [f"Question {position[0]} of {position[1]}: {title}" if position[1] > 1 else title]
    if detail:
        lines += ["", detail]
    if choices:
        lines.append("")
        for choice in choices:
            suffix = f" - {choice.description}" if choice.description else ""
            lines.append(f"{choice.number}. {choice.label}{suffix}")
    lines.append("")
    if choices and multi_select:
        how = 'Reply with the numbers of your choices, for example "1, 3".'
    elif choices:
        how = "Reply with a number."
    else:
        how = "Reply to this message with your answer."
    if choices and free_text:
        how += " Or reply to this message with your own answer."
    lines.append(how)
    return "\n".join(lines)


def _questions(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in view.get("questions") or [] if isinstance(row, dict)]


def _tool_input_summary(raw_input: Any) -> str:
    if raw_input in (None, {}, ""):
        return ""
    if isinstance(raw_input, dict) and set(raw_input) == {"command"}:
        text = str(raw_input["command"])
    else:
        text = json.dumps(raw_input, ensure_ascii=False, indent=2, default=str)
    if len(text) > _TOOL_INPUT_LIMIT:
        text = text[:_TOOL_INPUT_LIMIT] + "..."
    return text


@dataclass(frozen=True)
class ActionTarget:
    """What a pressed button names."""

    ref: str
    question_index: int
    number: int


def parse_action(action: str) -> ActionTarget | None:
    """Read an action id minted by :func:`channel_interaction`; other strings are not actions."""
    match = _ACTION_PATTERN.match(str(action or ""))
    if match is None:
        return None
    return ActionTarget(match.group(1), int(match.group(2)), int(match.group(3)))


@dataclass(frozen=True)
class QuestionAnswer:
    """One question answered, in the contract's answer-row shape."""

    row: dict[str, Any]
    #: The choice's label for a decision or approval, which decides the
    #: engine answer; ``None`` for a form row.
    choice: str | None = None


@dataclass(frozen=True)
class Refusal:
    """A reply that tried to answer and could not be used."""

    message: str


def read_reply(
    view: dict[str, Any],
    *,
    question_index: int,
    text: str,
    quoted: bool,
) -> QuestionAnswer | Refusal | None:
    """Turn a text reply into an answer, a refusal, or ``None`` (not an answer).

    A message answers when it is nothing but option numbers or an exact
    option label, or when it quotes the question's message and the question
    accepts the participant's own words. Everything else, quoted or not, is a
    new message for the conversation: on platforms where replying to the bot
    is how people talk, "no, use staging" is a redirection, not a bad answer.
    """
    shown = channel_interaction(view, question_index=question_index, ref="0" * 16, actions=False)
    reply = str(text or "").strip()
    numbers = _selected_numbers(reply)
    if numbers is None:
        by_label = [
            choice.number for choice in shown.choices
            if reply and choice.label.strip().casefold() == reply.casefold()
        ]
        numbers = by_label or None
    if numbers is not None:
        return _choose(view, shown, question_index, numbers)
    if quoted and shown.free_text and reply:
        return QuestionAnswer(
            row={"question_id": _question_id(view, question_index), "free_text": reply}
        )
    return None


def answer_action(
    view: dict[str, Any], *, target: ActionTarget
) -> QuestionAnswer | Refusal:
    """The answer a pressed button gives to the question it was shown with."""
    shown = channel_interaction(
        view, question_index=target.question_index, ref="0" * 16, actions=False
    )
    return _choose(view, shown, target.question_index, [target.number])


def _selected_numbers(reply: str) -> list[int] | None:
    if not _SELECTOR_PATTERN.match(reply):
        return None
    numbers: list[int] = []
    for part in re.split(r"[,，、\s]+", reply.strip()):
        if part and int(part) not in numbers:
            numbers.append(int(part))
    return numbers


def _choose(
    view: dict[str, Any],
    shown: ChannelInteraction,
    question_index: int,
    numbers: list[int],
) -> QuestionAnswer | Refusal:
    count = len(shown.choices)
    if not count or any(not 1 <= number <= count for number in numbers):
        return Refusal(_how_to_answer(shown))
    if len(numbers) > 1 and not shown.multi_select:
        return Refusal(_how_to_answer(shown))
    labels = [shown.choices[number - 1].label for number in numbers]
    if shown.presentation != PRESENTATION_FORM:
        return QuestionAnswer(row={}, choice=labels[0])
    question_id = _question_id(view, question_index)
    if shown.multi_select:
        return QuestionAnswer(row={"question_id": question_id, "option_labels": labels})
    return QuestionAnswer(row={"question_id": question_id, "option_label": labels[0]})


def _question_id(view: dict[str, Any], question_index: int) -> str:
    return str(_questions(view)[question_index].get("id") or "")


def _how_to_answer(shown: ChannelInteraction) -> str:
    count = len(shown.choices)
    if not count:
        return "Reply to the question's message with your answer."
    if shown.multi_select:
        return f'Reply with numbers from 1 to {count}, for example "1, 3".'
    if count == 1:
        return "Reply with 1."
    return f"Reply with a number from 1 to {count}."


def engine_answer(
    view: dict[str, Any], answers: list[QuestionAnswer]
) -> dict[str, Any]:
    """The contract answer for a fully answered interaction."""
    presentation = str(view.get("presentation") or "")
    if presentation == PRESENTATION_FORM:
        return {"answers": [answer.row for answer in answers]}
    choice = str(answers[-1].choice or "")
    if presentation == PRESENTATION_TOOL_APPROVAL:
        return {"decision": "approve" if choice == _APPROVAL_LABELS[0] else "reject"}
    option = next(
        option for option in view.get("options") or []
        if isinstance(option, dict) and str(option.get("id") or "") == choice
    )
    answer: dict[str, Any] = {"decision": choice}
    default_mode = str(option.get("default_permission_mode") or "").strip()
    if option.get("permission_mode_choices") and default_mode:
        # The console asks which mode to continue in; a channel has no such
        # control, so the adapter's declared default is the answer.
        answer["permission_mode"] = default_mode
    return answer


def answered_summary(view: dict[str, Any], answers: list[QuestionAnswer]) -> str:
    """One line confirming what the engine was told, for the conversation."""
    parts: list[str] = []
    for answer in answers:
        if answer.choice is not None:
            parts.append(answer.choice)
        elif "option_labels" in answer.row:
            parts.append(", ".join(answer.row["option_labels"]))
        else:
            parts.append(str(answer.row.get("option_label") or answer.row.get("free_text") or ""))
    return "Answered: " + "; ".join(part for part in parts if part)


__all__ = [
    "ActionTarget",
    "QuestionAnswer",
    "Refusal",
    "answer_action",
    "answered_summary",
    "channel_interaction",
    "engine_answer",
    "interaction_ref",
    "parse_action",
    "question_count",
    "read_reply",
]
