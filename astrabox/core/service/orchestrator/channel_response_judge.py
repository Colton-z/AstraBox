"""The platform's model judge for ordinary channel messages, and judge selection.

The judgement is one strict JSON decision on the deployment's title model.
An invalid decision is an error, never an answer, so the message is retried
and stays context.
"""
from __future__ import annotations

import json
from typing import Any

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.seams.channel_response import (
    MAX_JUDGED_CONTEXT_MESSAGES,
    ChannelResponseContext,
    ChannelResponseDecision,
    ChannelResponseJudge,
    channel_response_judge,
)

_MAX_MESSAGE_CHARS = 2000

_SYSTEM_PROMPT = """You decide whether an AI teammate should send an unprompted reply in a work channel.
Silence is the default. Return respond=true only when replying now is clearly useful and relevant.
The configured channel instruction is binding.
For a participated topic, a direct continuation, question, correction, or required next action is a strong reason to reply.
For a proactive-policy topic with no prior participation, reply only when the assistant can materially advance the work; ignore chatter, acknowledgements, social messages, and topics outside the instruction.
Judge only whether to reply. Do not answer the message itself.
Return exactly one JSON object matching {"respond": boolean, "reason": string}."""


class ChannelResponseJudgmentError(RuntimeError):
    pass


def _truncate(value: Any, limit: int = _MAX_MESSAGE_CHARS) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


class ModelChannelResponseJudge(ChannelResponseJudge):
    """Side-effect-free selection call on the title model."""

    name = "model"

    def __init__(self, model: Any | None = None) -> None:
        self._model = model

    async def judge(self, *, context: ChannelResponseContext) -> ChannelResponseDecision:
        from astrabox.core.service.orchestrator.session_title_service import ConversationTitleModel

        model = self._model or ConversationTitleModel()
        prompt = json.dumps(
            {
                "selection_reason": context.selection_reason,
                "channel_instruction": context.instruction,
                "prior_messages": [
                    {
                        "actor": row.participant,
                        "text": _truncate(row.content),
                        "timestamp": _truncate(row.timestamp, 100),
                    }
                    for row in context.context[-MAX_JUDGED_CONTEXT_MESSAGES:]
                ],
                "current_message": {
                    "actor": context.message.participant,
                    "text": _truncate(context.message.content),
                    "timestamp": _truncate(context.message.timestamp, 100),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw = await model.complete_json(system_prompt=_SYSTEM_PROMPT, user_prompt=prompt)
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise ChannelResponseJudgmentError(f"response judge returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict) or set(payload) != {"respond", "reason"}:
            raise ChannelResponseJudgmentError("response judge decision fields are invalid")
        if not isinstance(payload["respond"], bool) or not isinstance(payload["reason"], str):
            raise ChannelResponseJudgmentError("response judge decision types are invalid")
        if not payload["reason"].strip():
            raise ChannelResponseJudgmentError("response judge decision reason is empty")
        return ChannelResponseDecision(respond=payload["respond"], reason=payload["reason"].strip())


def active_channel_response_judge() -> ChannelResponseJudge | None:
    """The installed judge, else the built-in one when the title model is on."""
    installed = channel_response_judge()
    if installed is not None:
        return installed
    if bool(getattr(load_astrabox_settings(), "title_model_enabled", True)):
        return ModelChannelResponseJudge()
    return None
