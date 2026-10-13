"""Whether the agent should answer an ordinary channel message nobody addressed to it.

Core owns the decision's place in the inbound state machine: which messages
are candidates, the frozen policy, the topic's participation and unsubmitted
context, persistence, deduplication and recovery. A judge only answers the
question for one candidate. It is called again for the same message after a
host failure, so it must be side-effect free.

A deployment may install one judge; otherwise the platform's built-in model
judge answers with the deployment's title model.
"""
from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

#: Only participated topics are judged; other ordinary messages stay context.
RESPONSE_POLICY_PARTICIPATED = "participated"
#: Every ordinary message is judged.
RESPONSE_POLICY_PROACTIVE = "proactive"
RESPONSE_POLICIES = (RESPONSE_POLICY_PARTICIPATED, RESPONSE_POLICY_PROACTIVE)

#: Most context rows a judge receives, oldest first; earlier rows are dropped.
MAX_JUDGED_CONTEXT_MESSAGES = 50
MAX_RESPONSE_INSTRUCTION_CHARS = 4000


@dataclass(frozen=True)
class ChannelContextMessage:
    participant: str
    content: str
    timestamp: str


@dataclass(frozen=True)
class ChannelResponseContext:
    """One candidate message with the state frozen when it was first recorded."""
    deployment_id: str
    channel_name: str
    conversation: str
    message: ChannelContextMessage
    policy: Literal["participated", "proactive"]
    instruction: str
    binding_revision: str
    #: ``participated_topic`` or ``proactive_policy``.
    selection_reason: str
    participated: bool
    #: Unsubmitted messages of the topic before this one, oldest first.
    context: tuple[ChannelContextMessage, ...] = field(default=())


@dataclass(frozen=True)
class ChannelResponseDecision:
    respond: bool
    reason: str

    def __post_init__(self) -> None:
        if type(self.respond) is not bool or not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("a response decision needs a boolean respond and a non-empty reason")


class ChannelResponseJudge(ABC):
    """Decide one candidate; raise when no decision can be made.

    An exception is never read as a decision: the message is retried with
    backoff and stays context. Do not post, record or dispatch anything here.
    """
    name: str

    @abstractmethod
    async def judge(self, *, context: ChannelResponseContext) -> ChannelResponseDecision:
        """Return whether to answer, with a reason that is kept on the record."""


_JUDGE: ChannelResponseJudge | None = None


def register_channel_response_judge(judge: ChannelResponseJudge) -> None:
    global _JUDGE
    if (
        not isinstance(judge, ChannelResponseJudge)
        or not isinstance(getattr(judge, "name", None), str)
        or not judge.name.strip()
        or not inspect.iscoroutinefunction(getattr(judge, "judge", None))
    ):
        raise TypeError("a channel response judge needs a name and an async judge method")
    if _JUDGE is not None and _JUDGE is not judge:
        raise RuntimeError("a channel response judge is already registered")
    _JUDGE = judge


def channel_response_judge() -> ChannelResponseJudge | None:
    return _JUDGE
