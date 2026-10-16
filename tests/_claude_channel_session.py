"""A Session's Claude runtime for tests, recorded the way the Session's reader records it.

Runner frames are built from the Claude Agent SDK's own message types and
serialized by the runner's own serializer (``sandbox_runner._jsonable``). The
platform's Claude adapter translates them into runtime events
(``astrabox/core/service/orchestrator/engine/claude_runtime_client.py``). Each
frame's events are committed as one batch through
:meth:`SessionRuntimeRepository.record` under a reader lease, mapped the way
``SessionReader._take`` maps them, and the committed batch is handed to the
Session's fact consumers, as the reader's ``committed`` hook does.

The lease stays held, so the Session has a live reader for what asks one
(an interrupt request).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from astrabox.core.service.orchestrator.engine.claude_runtime_client import _Translation
from astrabox.core.service.orchestrator.engine.runtime import (
    InputAdmitted,
    InteractionClosed,
    InteractionOpened,
    ResponseEnded,
    ResponseOutput,
    ResponseStarted,
    RuntimeEvent,
    StatusChanged,
)
from astrabox.core.service.orchestrator.sandbox_runner import _jsonable
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch,
    InputsAdmitted,
    InteractionClose,
    InteractionOpen,
    OutputPart,
    ResponseEnd,
    ResponseStart,
    SessionRuntimeRepository,
)

FactConsumer = Callable[[str, FactBatch], Awaitable[None]]

_READER = "claude-session-test"
_TTL_S = 30.0
_MODEL = "claude-sonnet-4-5"


class ClaudeSession:
    """One Session's Claude runtime: each method is what Claude's runner journals."""

    def __init__(self, session_id: str, *, consumers: Sequence[FactConsumer] = ()) -> None:
        self.session_id = session_id
        self.consumers = list(consumers)
        self._repo = SessionRuntimeRepository()
        self._translation = _Translation()
        self._runner_seq = 0

    @property
    def response_id(self) -> str:
        """The response in progress."""
        response_id = self._translation.response_id
        assert response_id is not None, "no response is in progress"
        return response_id

    async def take(self, input_id: str, content: str) -> str:
        """Claude takes an accepted input; returns the response it starts."""
        await self._event(UserMessage(content=content), uuid=input_id)
        return self.response_id

    async def take_pending(self) -> list[str]:
        """Claude takes every input the Session holds for it, in acceptance order."""
        taken = []
        for row in await self._repo.undelivered_inputs(self.session_id):
            await self.take(str(row["input_id"]), str(row["content"] or ""))
            taken.append(str(row["input_id"]))
        return taken

    async def say(self, text: str) -> None:
        """One assistant message of one text block."""
        await self._event(AssistantMessage(content=[TextBlock(text=text)], model=_MODEL))

    async def reply(self, input_id: str, content: str, *texts: str) -> str:
        """Claude answers an input with ``texts`` and ends the response; returns it."""
        response_id = await self.take(input_id, content)
        for text in texts:
            await self.say(text)
        await self.finish()
        return response_id

    async def reply_on_its_own(self, *texts: str) -> str:
        """Claude answers without an input (a finished background task); returns the response."""
        response_id = str(uuid.uuid4())
        first, *rest = texts
        await self._event(
            AssistantMessage(content=[TextBlock(text=first)], model=_MODEL, uuid=response_id),
            engine_boundary=True,
        )
        for text in rest:
            await self.say(text)
        await self.finish()
        return response_id

    async def work_on_its_own(self, command: str, output: str) -> str:
        """Claude runs one command without an input and says nothing; returns the response."""
        response_id = str(uuid.uuid4())
        tool_use_id = f"toolu_{response_id[:8]}"
        await self._event(
            AssistantMessage(
                content=[ToolUseBlock(id=tool_use_id, name="Bash", input={"command": command})],
                model=_MODEL,
                uuid=response_id,
            ),
            engine_boundary=True,
        )
        await self._event(UserMessage(content=[ToolResultBlock(tool_use_id=tool_use_id, content=output)]))
        await self.finish()
        return response_id

    async def ask(self, interaction_id: str, question: str, options: Sequence[str]) -> None:
        """Claude asks one ``AskUserQuestion`` question and waits on its gate."""
        tool_input = {
            "questions": [
                {
                    "question": question,
                    "header": question,
                    "multiSelect": False,
                    "options": [{"label": label, "description": ""} for label in options],
                }
            ]
        }
        tool_use_id = f"toolu_{interaction_id}"
        await self._event(
            AssistantMessage(
                content=[ToolUseBlock(id=tool_use_id, name="AskUserQuestion", input=tool_input)],
                model=_MODEL,
            )
        )
        await self._frame({
            "op": "interaction",
            "interaction_id": interaction_id,
            "tool_use_id": tool_use_id,
            "tool_name": "AskUserQuestion",
            "tool_input": tool_input,
        })

    async def answer_taken(self, interaction_id: str) -> None:
        """The Session's reader handed a stored answer to Claude, which took it."""
        await self._commit([], facts=[InteractionClose(interaction_id, "answered")])

    async def finish(self) -> None:
        """Claude's ResultMessage: the response in progress ends completed."""
        await self._event(
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="native-session",
                stop_reason="end_turn",
            )
        )

    async def interrupted(self) -> None:
        """The runner stopped the response in progress on an interrupt."""
        await self._frame({"op": "turn_interrupted", "command_id": self._translation.interrupt_target})

    # -- recording -----------------------------------------------------------

    async def _event(self, message: Any, *, uuid: str | None = None, engine_boundary: bool = False) -> None:
        payload = _jsonable(message)
        if uuid is not None:
            # The runner names a consumed root UserMessage by the platform input it carries.
            payload["uuid"] = uuid
        self._runner_seq += 1
        frame: dict[str, Any] = {"op": "event", "seq": self._runner_seq, "message": payload}
        if engine_boundary:
            frame["engine_boundary"] = True
        await self._frame(frame)

    async def _frame(self, frame: dict[str, Any]) -> None:
        await self._commit(self._translation.translate(frame))

    async def _commit(self, events: Sequence[RuntimeEvent], *, facts: Sequence[Any] = ()) -> None:
        lease = await self._repo.claim_reader(self.session_id, owner=_READER, ttl_s=_TTL_S)
        assert lease is not None, "another reader holds the Session"
        seq, _cursor = await self._repo.resume_point(self.session_id)
        batch = FactBatch(facts=list(facts))
        for event in events:
            if isinstance(event, ResponseOutput):
                seq += 1
                batch.output.append(OutputPart(seq, event.response_id, event.frame))
            elif isinstance(event, ResponseStarted):
                batch.facts.append(ResponseStart(event.response_id, event.origin, tuple(event.input_ids)))
            elif isinstance(event, ResponseEnded):
                batch.facts.append(
                    ResponseEnd(event.response_id, event.outcome, event.native_reason, event.usage, event.error)
                )
            elif isinstance(event, InteractionOpened):
                batch.facts.append(InteractionOpen(event.response_id, event.interaction_id, event.contract))
            elif isinstance(event, InteractionClosed):
                batch.facts.append(InteractionClose(event.interaction_id, event.reason))
            elif isinstance(event, InputAdmitted):
                batch.facts.append(InputsAdmitted((event.input_id,)))
            elif isinstance(event, StatusChanged):
                batch.status = event.status
        assert await self._repo.record(lease, batch, ttl_s=_TTL_S)
        for consumer in self.consumers:
            await consumer(self.session_id, batch)
