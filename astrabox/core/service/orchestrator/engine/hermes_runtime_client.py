"""Hermes behind the runtime contract (``engine/runtime.py``).

One conversation is one session of the profile's resident ``hermes serve``
backend (:mod:`.hermes_gateway`), resumed by its stored key. This client reads
that session's events and the backend's requests to it, and reports them as
runtime events. Vendor references are to ``tui_gateway/`` at the commit the
image pins (``containers/sandbox-hermes/Dockerfile``).

* **Cursor.** Hermes stamps every event of a session with a ``seq`` and keeps
  the newest in a replay ring that ``session.events.since`` reads back after
  any ``seq`` (``event_replay.py``). The cursor's generation is the backend's
  ``replay_epoch`` and the live session id, which together name that
  numbering; its position is an event's ``seq`` and the translation state at
  that point. The ring keeps fewer events than a long reply streams, so the
  state travels in the cursor rather than being rebuilt from the reply's
  first event. Reading the ring again after a cursor and translating from its
  state yields the same events; a cursor whose events the ring has evicted
  (``truncated``), or one from another epoch or session, is expired.
* **Responses.** A response starts at ``message.start``, or at the first
  event of a turn whose start the reader did not see. It ends at
  ``message.complete`` (``complete``, ``interrupted`` or ``error``), or at an
  ``error`` event when a turn failed before the agent ran.
* **Inputs.** ``prompt.submit`` answers ``streaming`` when the input started a
  turn and ``queued`` when a turn was running: the profile sets Hermes'
  busy-input mode to ``queue`` (``engine/hermes.py``), so a message sent
  during a turn waits for it. A ``streaming`` receipt with ``user_row_id``
  names the row Hermes wrote for the input in its own transcript before the
  agent could consume it, and the input is admitted there. A queued input is
  admitted when the turn Hermes drains it into starts, which is when Hermes
  writes its row. Hermes merges queued text into one turn, so that turn
  answers every input queued before it.
* **Interactions.** Approvals and clarify questions are requests the backend
  sends this client (``server_requests.py``); the request id is the
  interaction id, and the answer returns through ``request.answer``.
  Requests this host cannot answer are refused at once, which the vendor's
  integration guide asks of a host so the agent does not wait out a
  deadline.
* **Engine responses.** A turn no input started (a finished delegation, a
  process notification, a goal continuation) is a response of origin
  ``engine``, and so is each run of the conversation's own cron jobs
  (:mod:`.hermes_cron`), reported once no other response is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineStreamDetached,
)
from astrabox.core.service.orchestrator.engine.hermes_client import (
    HermesTuiRpcError,
    HermesTuiWireEvent,
    _translate_hermes_child_run_event,
)
from astrabox.core.service.orchestrator.engine.hermes_cron import (
    CronRunOutput,
    CronRunReport,
    HermesCronWatch,
)
from astrabox.core.service.orchestrator.engine.input_ids import input_response_message_id
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_DECISION,
    PRESENTATION_FORM,
    validate_interaction_contract,
    validated_question_answer_rows,
)
from astrabox.core.service.orchestrator.engine.runtime import (
    InputAdmitted,
    InteractionClosed,
    InteractionNotOpen,
    InteractionOpened,
    OpenResponse,
    PendingInteraction,
    ResponseEnded,
    ResponseOrigin,
    ResponseOutcome,
    ResponseOutput,
    ResponseStarted,
    RuntimeCursor,
    RuntimeCursorExpired,
    RuntimeDiagnostic,
    RuntimeEvent,
    RuntimeInput,
    RuntimeInputRefused,
    RuntimeProtocolError,
    RuntimeState,
    RuntimeStatus,
    StatusChanged,
)
from astrabox.persistence.repository.hermes_cron_repository import HermesCronJobRepository

logger = get_logger(__name__)

#: The requests this host answers; every other request method is refused.
ANSWERED_REQUESTS = frozenset({"approval", "clarify"})
#: The JSON-RPC code Hermes' guide names for a request method a host does not implement.
_METHOD_NOT_FOUND = -32601
#: A ``session.events.since`` watermark past any ``seq``: the reply carries no
#: events, only the ring's ``latest_seq``, ``epoch`` and ``open_requests``.
_LATEST = 2**53
#: Hermes' own bound on building a session's agent (``_wait_agent`` in
#: ``server.py``): a prompt sent later than this fails with its timeout.
_AGENT_BUILD_TIMEOUT_SECONDS = 30.0
#: Context occupancy, in percent of the model's window, at which an idle
#: conversation is compressed ahead of need. Hermes compresses inside a turn
#: at 50% of the window, or 75% for windows under 512K tokens
#: (``agent/context_compressor.py``), and the user waits for that.
#: Compressing between turns, well below it, leaves every turn the headroom
#: it needs to run without compressing itself.
_IDLE_COMPACT_AT_PERCENT = 40
#: How long a conversation must stay quiet after a reply before it is compressed.
_IDLE_COMPACT_QUIET_SECONDS = 15.0
_COMPRESS_TIMEOUT_SECONDS = 900.0
#: Cron responses a cursor remembers, newest last; more than this between two
#: commits of the reader is not a pace cron jobs can reach.
_CRON_REMEMBERED = 16
#: ``prompt.submit`` errors that are Hermes declining this message, as
#: opposed to the session or the backend being unavailable: an active-session
#: limit (``4090``) and a transcript it could not write (``5070``-``5072``),
#: in ``methods_prompt.py``.
_REFUSING_SUBMIT_CODES = frozenset({4090, 5070, 5071, 5072})

#: ``request.cancel`` reasons (``RequestCancelReason``) as contract reasons.
_CANCEL_REASONS: dict[str, Literal["answered", "withdrawn", "expired"]] = {
    "timeout": "expired",
    "resolved": "answered",
}

#: Transcript copy for each approval choice Hermes offers (``ApprovalChoice``).
_APPROVAL_REPLIES = {
    "once": "The user allowed this once.",
    "session": "The user allowed this for the rest of the session.",
    "always": "The user always allows this.",
    "deny": "The user denied this.",
}


class HermesGatewayPort(Protocol):
    """What this client needs from the profile's resident gateway handle."""

    profile_key: str

    @property
    def is_live(self) -> bool: ...

    @property
    def replay_epoch(self) -> str | None: ...

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = ...
    ) -> dict[str, Any]: ...

    async def respond_error(self, request_id: str, *, code: int, message: str) -> None: ...

    def subscribe(self, tui_session_id: str, *, after_offset: int = 0) -> Any: ...

    def unsubscribe(self, subscription: Any) -> None: ...


# --------------------------------------------------------------------------
# Interactions
# --------------------------------------------------------------------------


def interaction_contract(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """The platform contract for one Hermes request, built from the request alone.

    An approval is a decision among the choices Hermes offers for it, under
    Hermes' own names; a clarify request is a form with one question, or one
    per question of a batch. ``raw_input`` is the request as Hermes sent it.
    """

    raw = {key: value for key, value in params.items() if key != "session_id"}
    if method == "approval":
        choices = [str(choice) for choice in params.get("choices") or []]
        unknown = [choice for choice in choices if choice not in _APPROVAL_REPLIES]
        if not choices or unknown:
            raise ValueError(f"Hermes approval offers no choice the adapter knows: {choices!r}")
        command = str(params.get("command") or "")
        description = str(params.get("description") or "").strip()
        contract: dict[str, Any] = {
            "tool_name": str(params.get("tool_name") or "").strip() or method,
            "presentation": PRESENTATION_DECISION,
            "prompt": description or "Hermes asks for approval.",
            "raw_input": raw,
            "body": command,
            "options": [
                {"id": choice, "denial": choice == "deny", "reply": _APPROVAL_REPLIES[choice]}
                for choice in choices
            ],
        }
    elif method == "clarify":
        batch = params.get("questions")
        if isinstance(batch, list) and batch:
            questions = [
                _clarify_question(str(item.get("qid") or ""), item) for item in batch if isinstance(item, dict)
            ]
            prompt = "Hermes has questions for you."
        else:
            questions = [_clarify_question("answer", params)]
            prompt = str(params.get("question") or "").strip() or "Hermes has a question for you."
        contract = {
            "tool_name": method,
            "presentation": PRESENTATION_FORM,
            "prompt": prompt,
            "raw_input": raw,
            "questions": questions,
        }
    else:
        raise ValueError(f"Hermes request {method!r} is not one this host answers")
    validate_interaction_contract(contract)
    return contract


def _clarify_question(question_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """One form question for a clarify question; ``''`` is Hermes' skip."""

    if not question_id:
        raise ValueError("Hermes clarify batch question has no qid")
    choices = item.get("choices") if isinstance(item.get("choices"), list) else []
    return {
        "id": question_id,
        "question": str(item.get("question") or ""),
        "native_answer_key": question_id,
        "multi_select": bool(item.get("multi_select")),
        "options": [{"label": str(choice)} for choice in choices if str(choice).strip()],
        "allow_free_text": True,
        "allow_empty_text": True,
    }


def interaction_record(
    *, request_id: str, method: str, params: dict[str, Any], response_id: str
) -> dict[str, Any]:
    """The platform interaction record for one Hermes request."""

    return {
        "interaction_id": request_id,
        "response_id": response_id,
        **interaction_contract(method, params),
    }


def answer_result(record: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """The JSON-RPC result Hermes reads for a person's answer to ``record``.

    An approval answers ``{choice}``. A clarify request answers ``{answer}``
    for one question (``''`` skips it) or ``{answers}`` for a batch, where no
    ``answers`` cancels the batch (``ClarifyResult``). A comment on an
    approval has no field to travel in and is not sent.
    """

    if record["presentation"] == PRESENTATION_FORM:
        questions = record["questions"]
        single = len(questions) == 1 and questions[0]["id"] == "answer"
        if response.get("decline") is True:
            return {"answer": ""} if single else {}
        rows = validated_question_answer_rows(record, response)
        if single:
            return {"answer": rows[0]["response_text"]}
        return {"answers": {row["question"]["id"]: row["response_text"] for row in rows}}
    choice = str(response.get("decision") or "").strip()
    if choice not in {option["id"] for option in record["options"]}:
        raise ValueError(f"Hermes approval has no choice {choice!r}")
    return {"choice": choice}


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------


@dataclass
class _Response:
    """The response a turn is producing, and the browser blocks it has open."""

    id: str
    origin: ResponseOrigin
    inputs: list[str]
    text: str | None = None
    reasoning: str | None = None
    #: Text and reasoning blocks opened so far; block ids are numbered by it.
    blocks: int = 0
    tool: str | None = None
    #: A ``message.delta`` reached this response, so ``message.complete``'s
    #: full text is not shown again.
    streamed: bool = False
    #: The browser message was closed at an interaction and reopens with the
    #: response's next part.
    closed: bool = False


@dataclass
class _Pending:
    """An input handed to Hermes whose response has not started."""

    input: str
    #: ``sending`` until ``prompt.submit`` answers, then Hermes' status.
    receipt: Literal["sending", "streaming", "queued"]
    #: The highest event ``seq`` this client had received when it sent the
    #: input: no event at or below it can be the input's turn.
    after: int
    admitted: bool = False


@dataclass
class _State:
    response: _Response | None = None
    pending: list[_Pending] = field(default_factory=list)
    #: Open interaction id -> the response it belongs to.
    interactions: dict[str, str] = field(default_factory=dict)
    running: bool = False
    status: RuntimeStatus = "idle"
    #: Cron responses already reported, newest last.
    cron: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "response": dataclasses.asdict(self.response) if self.response else None,
            "pending": [dataclasses.asdict(item) for item in self.pending],
            "interactions": dict(self.interactions),
            "running": self.running,
            "status": self.status,
            "cron": list(self.cron),
        }

    @classmethod
    def from_json(cls, value: Any) -> _State:
        if not isinstance(value, dict):
            raise ValueError("a Hermes cursor carries its translation state")
        response = value.get("response")
        return cls(
            response=_Response(**response) if isinstance(response, dict) else None,
            pending=[_Pending(**item) for item in value.get("pending") or []],
            interactions={str(k): str(v) for k, v in (value.get("interactions") or {}).items()},
            running=bool(value.get("running")),
            status=value.get("status") or "idle",
            cron=[str(item) for item in value.get("cron") or []],
        )


def _finish(reason: str) -> dict[str, Any]:
    return {"type": "finish", "finishReason": reason}


class _Translation:
    """Hermes events to runtime events, holding only what the cursor carries."""

    def __init__(self, generation: str) -> None:
        self.generation = generation
        self.state = _State()

    # -- one ring event ------------------------------------------------------

    def event(self, params: dict[str, Any], seq: int) -> list[RuntimeEvent]:
        kind = str(params.get("type") or "")
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        assert isinstance(payload, dict)
        if kind == "message.start":
            if self.state.response is not None:
                # A goal continuation announces its turn before the turn runner
                # announces it again (``prompt_turn._dispatch_followup_turn``).
                return []
            self.state.running = True
            return self._open(seq) + self._status()
        if kind == "message.delta":
            return self._text(seq, str(payload.get("text") or ""), delta=True)
        if kind == "reasoning.delta":
            return self._reasoning(seq, str(payload.get("text") or ""))
        if kind == "message.interim":
            # Commentary beside tool calls; streamed text already reached the
            # browser as deltas, so only text that was not streamed is shown.
            if payload.get("already_streamed") is True or not str(payload.get("text") or ""):
                return []
            return self._text(seq, str(payload["text"]), delta=False)
        if kind == "tool.start":
            return self._tool_start(seq, payload)
        if kind == "tool.complete":
            return self._tool_complete(seq, payload)
        if kind == "message.complete":
            return self._complete(seq, payload)
        if kind == "error":
            return self._error(seq, str(payload.get("message") or "Hermes failed"))
        if kind == "request.cancel":
            return self._cancel(payload)
        if kind == "session.info":
            self.state.running = payload.get("running") is True
            return self._status()
        if kind.startswith("subagent."):
            response_id = self.state.response.id if self.state.response else None
            return [
                ResponseOutput(None, response_id, frame)
                for frame in _translate_hermes_child_run_event(kind, payload)
            ]
        # ``thinking.delta`` carries a spinner label or its reset and
        # ``reasoning.available`` a tool-progress preview, not reasoning
        # (``agent_callbacks.py``, ``tool_progress.py``); every other event
        # is session telemetry the browser does not render.
        return []

    # -- a request from the backend -----------------------------------------

    def request(self, request: dict[str, Any], seq: int) -> list[RuntimeEvent]:
        request_id = str(request["id"])
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        assert isinstance(params, dict)
        events = [] if self.state.response is not None else self._open(seq, key=request_id)
        response = self.state.response
        assert response is not None
        record = interaction_record(
            request_id=request_id, method=str(request["method"]), params=params, response_id=response.id
        )
        events += self._reopen()
        events += self._close_blocks()
        events.append(InteractionOpened(None, response.id, request_id, record))
        events.append(ResponseOutput(None, response.id, {"type": "data-interaction", "data": dict(record)}))
        events.append(ResponseOutput(None, response.id, _finish("tool-calls")))
        response.closed = True
        self.state.interactions[request_id] = response.id
        return events + self._status()

    # -- inputs ----------------------------------------------------------------

    def receipt(self, input_id: str) -> list[RuntimeEvent]:
        """The input's ``prompt.submit`` answered; admit it if Hermes wrote its row."""

        for item in self.state.pending:
            if item.input == input_id and item.admitted:
                return [InputAdmitted(None, input_id)]
        return [RuntimeDiagnostic(None, "hermes.prompt.submit", {"input_id": input_id})]

    def unheld(self, input_ids: list[str]) -> list[RuntimeEvent]:
        """Queued inputs Hermes holds no turn for while idle with an empty queue.

        Hermes answered them inside a turn already running: it drops a queued
        copy of the prompt it is answering (``_enqueue_prompt``), and an input
        that reached its queue as the turn it merged into started is answered
        by that turn.
        """

        events: list[RuntimeEvent] = []
        for item in list(self.state.pending):
            if item.input in input_ids and item.receipt == "queued":
                self.state.pending.remove(item)
                if not item.admitted:
                    events.append(InputAdmitted(None, item.input))
        return events

    # -- engine responses --------------------------------------------------------

    def cron(self, report: CronRunReport) -> list[RuntimeEvent]:
        response_id = report.response_id
        block = f"hermes-cron-text:{report.job_id}:{report.run_at}"
        events: list[RuntimeEvent] = [
            ResponseStarted(None, response_id, "engine", ()),
            ResponseOutput(None, response_id, self._start_part(response_id, "engine", ())),
            ResponseOutput(None, response_id, {"type": "text-start", "id": block}),
            ResponseOutput(None, response_id, {"type": "text-delta", "id": block, "delta": report.text}),
            ResponseOutput(None, response_id, {"type": "text-end", "id": block}),
        ]
        if report.failed:
            events.append(ResponseOutput(None, response_id, {"type": "error", "errorText": report.text}))
        events.append(ResponseOutput(None, response_id, _finish("error" if report.failed else "stop")))
        events.append(
            ResponseEnded(
                None,
                response_id,
                "failed" if report.failed else "completed",
                report.status,
                error={"message": report.text} if report.failed else None,
            )
        )
        self.state.cron = (self.state.cron + [response_id])[-_CRON_REMEMBERED:]
        return events

    # -- response boundaries ---------------------------------------------------

    def _open(self, seq: int, *, key: str | None = None, streaming_only: bool = False) -> list[RuntimeEvent]:
        """Start the response for a turn Hermes began at event ``seq``.

        A ``streaming`` receipt is an input that started a turn of its own;
        otherwise the turn drained Hermes' queue and answers every queued
        input, or it is the runtime's own. Only inputs sent before event
        ``seq`` was received can be its inputs.
        """

        candidates = [item for item in self.state.pending if item.after < seq]
        chosen = [item for item in candidates if item.receipt == "streaming"][:1]
        if not chosen and not streaming_only:
            chosen = [item for item in candidates if item.receipt != "streaming"]
        if streaming_only and not chosen:
            return []
        for item in chosen:
            self.state.pending.remove(item)
        input_ids = tuple(item.input for item in chosen)
        # Replayed turn events can consume the pending entry before its
        # synthetic submit receipt is read. Starting the turn proves admission.
        events: list[RuntimeEvent] = [InputAdmitted(None, item.input) for item in chosen]
        if input_ids:
            origin: ResponseOrigin = "input"
            response_id = input_response_message_id(input_ids[0])
        else:
            origin = "engine"
            response_id = f"hermes:{self.generation}:{key or seq}"
        self.state.response = _Response(id=response_id, origin=origin, inputs=list(input_ids))
        events.append(ResponseStarted(None, response_id, origin, input_ids))
        events.append(ResponseOutput(None, response_id, self._start_part(response_id, origin, input_ids)))
        return events

    @staticmethod
    def _start_part(response_id: str, origin: ResponseOrigin, input_ids: tuple[str, ...]) -> dict[str, Any]:
        return {
            "type": "start",
            "messageId": response_id,
            "messageMetadata": {"response_id": response_id, "input_ids": list(input_ids), "origin": origin},
        }

    def _ensure(self, seq: int) -> list[RuntimeEvent]:
        """Open a response for turn content that arrived without one."""

        return [] if self.state.response is not None else self._open(seq)

    def _complete(self, seq: int, payload: dict[str, Any]) -> list[RuntimeEvent]:
        status = str(payload.get("status") or "complete")
        outcome: ResponseOutcome
        if status == "complete":
            outcome, reason = "completed", "stop"
        elif status == "interrupted":
            outcome, reason = "cancelled", "other"
        elif status == "error":
            outcome, reason = "failed", "error"
        else:
            raise ValueError(f"Hermes message.complete has an unknown status {status!r}")
        events = self._ensure(seq)
        response = self.state.response
        assert response is not None
        text = payload.get("text")
        parts: list[dict[str, Any]] = []
        shows_text = outcome != "failed" or payload.get("partial") is True
        if not response.streamed and shows_text and isinstance(text, str) and text:
            events += self._reopen() + self._close_blocks()
            block = self._block_id("text")
            parts += [
                {"type": "text-start", "id": block},
                {"type": "text-delta", "id": block, "delta": text},
                {"type": "text-end", "id": block},
            ]
        error: dict[str, Any] | None = None
        if outcome == "failed":
            message = str(payload.get("error") or text or "Hermes failed")
            error = {"message": message}
            if isinstance(payload.get("error_surface"), dict):
                error["error_surface"] = dict(payload["error_surface"])
            parts.append({"type": "error", "errorText": message})
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        if usage is not None:
            # Hermes' own usage words, shown with the response as Claude's result is.
            parts.append({"type": "data-result", "data": {"usage": dict(usage)}})
        parts.append(_finish(reason))
        return events + self._end(outcome, status, usage=usage, error=error, parts=parts)

    def _error(self, seq: int, message: str) -> list[RuntimeEvent]:
        """An ``error`` event: a session-level failure, or the end of a turn that failed.

        Hermes reports a turn that could not run (refused, cancelled before the
        agent was ready) with only this event, and a turn whose terminal frame
        could not be sent with this event in its place.
        """

        events: list[RuntimeEvent] = []
        if self.state.response is None:
            events = self._open(seq, streaming_only=True)
            if self.state.response is None:
                return []
        parts = [{"type": "error", "errorText": message}, _finish("error")]
        return events + self._end("failed", "error", usage=None, error={"message": message}, parts=parts)

    def _end(
        self,
        outcome: ResponseOutcome,
        native_reason: str | None,
        *,
        usage: dict[str, Any] | None,
        error: dict[str, Any] | None,
        parts: list[dict[str, Any]],
    ) -> list[RuntimeEvent]:
        response = self.state.response
        assert response is not None
        events = self._reopen() + self._close_blocks()
        events += [ResponseOutput(None, response.id, part) for part in parts]
        events.append(ResponseEnded(None, response.id, outcome, native_reason, usage, error))
        # The platform withdraws the response's open interactions when it ends.
        self.state.interactions = {
            key: value for key, value in self.state.interactions.items() if value != response.id
        }
        self.state.response = None
        return events + self._status()

    def _reopen(self) -> list[RuntimeEvent]:
        """Reopen the browser message an interaction closed, once the response continues."""

        response = self.state.response
        if response is None or not response.closed:
            return []
        response.closed = False
        return [ResponseOutput(None, response.id, {"type": "start", "messageId": response.id})]

    def _status(self) -> list[RuntimeEvent]:
        status: RuntimeStatus = (
            "waiting" if self.state.interactions else ("working" if self.state.running else "idle")
        )
        if status == self.state.status:
            return []
        self.state.status = status
        return [StatusChanged(None, status)]

    def _cancel(self, payload: dict[str, Any]) -> list[RuntimeEvent]:
        request_id = str(payload.get("id") or "")
        if request_id not in self.state.interactions:
            return []
        del self.state.interactions[request_id]
        reason = _CANCEL_REASONS.get(str(payload.get("reason") or ""), "withdrawn")
        return [InteractionClosed(None, request_id, reason)] + self._status()

    # -- content ---------------------------------------------------------------

    def _block_id(self, lane: str) -> str:
        response = self.state.response
        assert response is not None
        response.blocks += 1
        return f"hermes-{lane}:{response.id}:{response.blocks}"

    def _close_blocks(self) -> list[RuntimeEvent]:
        response = self.state.response
        if response is None:
            return []
        events: list[RuntimeEvent] = []
        if response.text is not None:
            events.append(ResponseOutput(None, response.id, {"type": "text-end", "id": response.text}))
            response.text = None
        if response.reasoning is not None:
            events.append(ResponseOutput(None, response.id, {"type": "reasoning-end", "id": response.reasoning}))
            response.reasoning = None
        return events

    def _text(self, seq: int, text: str, *, delta: bool) -> list[RuntimeEvent]:
        if not text:
            return []
        events = self._ensure(seq) + self._reopen()
        response = self.state.response
        assert response is not None
        if response.reasoning is not None:
            events.append(ResponseOutput(None, response.id, {"type": "reasoning-end", "id": response.reasoning}))
            response.reasoning = None
        if not delta:
            events += self._close_blocks()
            block = self._block_id("text")
            return events + [
                ResponseOutput(None, response.id, {"type": "text-start", "id": block}),
                ResponseOutput(None, response.id, {"type": "text-delta", "id": block, "delta": text}),
                ResponseOutput(None, response.id, {"type": "text-end", "id": block}),
            ]
        if response.text is None:
            response.text = self._block_id("text")
            events.append(ResponseOutput(None, response.id, {"type": "text-start", "id": response.text}))
        response.streamed = True
        events.append(ResponseOutput(None, response.id, {"type": "text-delta", "id": response.text, "delta": text}))
        return events

    def _reasoning(self, seq: int, text: str) -> list[RuntimeEvent]:
        if not text:
            return []
        events = self._ensure(seq) + self._reopen()
        response = self.state.response
        assert response is not None
        if response.text is not None:
            events.append(ResponseOutput(None, response.id, {"type": "text-end", "id": response.text}))
            response.text = None
        if response.reasoning is None:
            response.reasoning = self._block_id("reasoning")
            events.append(ResponseOutput(None, response.id, {"type": "reasoning-start", "id": response.reasoning}))
        events.append(
            ResponseOutput(None, response.id, {"type": "reasoning-delta", "id": response.reasoning, "delta": text})
        )
        return events

    def _tool_start(self, seq: int, payload: dict[str, Any]) -> list[RuntimeEvent]:
        events = self._ensure(seq) + self._reopen() + self._close_blocks()
        response = self.state.response
        assert response is not None
        tool_id = str(payload.get("tool_id") or "").strip()
        name = str(payload.get("name") or "tool").strip() or "tool"
        arguments = payload.get("args")
        if isinstance(arguments, dict):
            tool_input: dict[str, Any] = dict(arguments)
        else:
            # Hermes separates the native arguments from a display preview;
            # a preview is not presented as parsed arguments.
            context = payload.get("context")
            tool_input = {"preview": context} if isinstance(context, str) and context.strip() else {}
        response.tool = tool_id or None
        return events + [
            ResponseOutput(None, response.id, {"type": "tool-input-start", "toolCallId": tool_id, "toolName": name}),
            ResponseOutput(
                None,
                response.id,
                {"type": "tool-input-available", "toolCallId": tool_id, "toolName": name, "input": tool_input},
            ),
        ]

    def _tool_complete(self, seq: int, payload: dict[str, Any]) -> list[RuntimeEvent]:
        events = self._ensure(seq) + self._reopen()
        response = self.state.response
        assert response is not None
        tool_id = str(payload.get("tool_id") or response.tool or "").strip()
        is_error = payload.get("is_error")
        if not isinstance(is_error, bool):
            raise RuntimeProtocolError("Hermes tool.complete omitted its native boolean is_error classification. "
                                       "Align the sandbox image with the core protocol before recovering this conversation.")
        if is_error:
            result = payload.get("result")
            error_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return events + [ResponseOutput(None, response.id, {
                "type": "tool-output-error", "toolCallId": tool_id, "errorText": error_text,
            })]
        output = {key: value for key, value in payload.items() if key not in {"tool_id", "name"}}
        part = {
            "type": "tool-output-available",
            "toolCallId": tool_id,
            "output": output or str(payload.get("summary") or ""),
        }
        return events + [ResponseOutput(None, response.id, part)]


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


_Item = tuple[str, Any]


class HermesRuntimeClient:
    """One reader's connection to one conversation on the profile's Hermes backend.

    The backend and its attachment belong to :mod:`.hermes_gateway` and are
    shared with the profile's other conversations; this client owns one
    vendor session on it. :meth:`close` leaves that session running for the
    next reader, and :meth:`dispose` ends it.
    """

    engine_kind = "assistant"

    def __init__(
        self,
        *,
        gateway: HermesGatewayPort,
        platform_session_id: str,
        engine_session_key: str,
        tui_session_id: str,
        epoch: str,
        subscription: Any,
        server_info: dict[str, Any] | None,
    ) -> None:
        self._gateway = gateway
        self._platform_session_id = str(platform_session_id)
        self._engine_session_key = engine_session_key
        self._sid = tui_session_id
        self._epoch = epoch
        self._generation = f"{epoch}:{tui_session_id}"
        self._subscription = subscription
        self._server_info = server_info
        self._translation = _Translation(self._generation)
        self._inbox: asyncio.Queue[_Item] = asyncio.Queue()
        #: The highest event ``seq`` received from the backend.
        self._received_seq = 0
        #: The highest event ``seq`` translated.
        self._last_seq = 0
        self._inflight: dict[str, tuple[asyncio.Future[None], int]] = {}
        self._texts: dict[str, str] = {}
        #: Requests the backend withdrew while this client read, so a copy that
        #: arrives after the withdrawal opens nothing.
        self._withdrawn: set[str] = set()
        #: Requests already refused: a request reaches this client both live
        #: and in a replay's ``open_requests``.
        self._refused: set[str] = set()
        self._reading = False
        self._closed = False
        self._agent_built = asyncio.Event()
        self._forwarder: asyncio.Task[None] | None = None
        self._cron: HermesCronWatch | None = None
        self._cron_reports: list[CronRunReport] = []
        self._cron_unrecorded: dict[str, CronRunReport] = {}
        self._unheld_check: asyncio.Task[None] | None = None
        self._context_percent: int | None = None
        self._compaction: asyncio.Task[None] | None = None

    # -- connecting --------------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        *,
        gateway: HermesGatewayPort,
        platform_session_id: str,
        resume_session_key: str | None,
        session_env: dict[str, str] | None = None,
        read_cron_run_output: Callable[[str], Awaitable[CronRunOutput | None]] | None = None,
        cron_store: HermesCronJobRepository | None = None,
    ) -> HermesRuntimeClient:
        """Open the conversation's vendor session and start following it.

        A new conversation is persisted with its platform Session id as the
        native title. If attachment failed before the durable key was published,
        Hermes' exact-title lookup finds that row for native resume. A stored
        key keeps naming the whole conversation across compression.
        """

        if not gateway.is_live:
            raise EngineStreamDetached("Hermes resident gateway is not live for this profile")
        epoch = str(gateway.replay_epoch or "").strip()
        if not epoch:
            raise RuntimeError("the Hermes backend announced no replay epoch in gateway.ready")
        try:
            capabilities = await gateway.request("gateway.capabilities", {})
        except HermesTuiRpcError as exc:
            if exc.code != -32601:
                raise
            raise RuntimeProtocolError("Hermes does not expose the required gateway.capabilities protocol.") from exc
        if capabilities.get("execution_tool_completion_is_error") is not True:
            raise RuntimeProtocolError(
                "Hermes does not advertise native execution tool-completion classification. "
                "Deploy the sandbox image matching this core before starting or recovering conversations."
            )
        key = str(resume_session_key or "").strip()
        if not key:
            listed = await gateway.request("session.list", {"title": platform_session_id})
            sessions = listed.get("sessions")
            if not isinstance(sessions, list) or len(sessions) > 1:
                raise RuntimeError("Hermes returned an invalid exact-title session lookup")
            if sessions:
                row = sessions[0]
                if not isinstance(row, dict) or row.get("title") != platform_session_id:
                    raise RuntimeError("Hermes did not return the requested conversation title")
                key = str(row.get("id") or "").strip()
                if not key:
                    raise RuntimeError("Hermes title lookup returned no durable session key")
        created = not key
        context_params = {"tool_environment": dict(session_env)} if session_env is not None else {}
        if key:
            result = await gateway.request("session.resume", {"session_id": key, "cols": 120, **context_params})
        else:
            result = await gateway.request("session.create", {"cols": 120, **context_params})
        sid = str(result.get("session_id") or "").strip()
        if not sid:
            raise RuntimeError("Hermes did not return a TUI session id")
        # A created session builds its agent after the reply and announces it
        # with ``session.info``, which reaches only a subscriber, so the
        # subscription exists before anything else is awaited.
        subscription = gateway.subscribe(sid)
        try:
            if not key:
                title = await gateway.request("session.title", {"session_id": sid})
                key = str(title.get("session_key") or "").strip()
                if not key:
                    raise RuntimeError("Hermes did not return its durable session key")
                # Empty Hermes sessions are process-local drafts until an
                # explicit title or prompt persists their SessionDB row.
                titled = await gateway.request(
                    "session.title", {"session_id": sid, "title": platform_session_id}
                )
                if titled.get("pending") is not False:
                    raise RuntimeError("Hermes did not persist the new conversation")
            # Every event at or below this seq was sent before the subscription
            # could see it, so no input this client sends can be its turn.
            since = await gateway.request("session.events.since", {"session_id": sid, "last_seen": _LATEST})
            if since.get("epoch") != epoch:
                raise RuntimeError("the Hermes backend restarted while the conversation was opened")
        except BaseException:
            gateway.unsubscribe(subscription)
            if created:
                with contextlib.suppress(BaseException):
                    await gateway.request("session.close", {"session_id": sid})
            raise
        info = result.get("info")
        client = cls(
            gateway=gateway,
            platform_session_id=platform_session_id,
            engine_session_key=key,
            tui_session_id=sid,
            epoch=epoch,
            subscription=subscription,
            server_info=dict(info) if isinstance(info, dict) else None,
        )
        client._received_seq = int(since.get("latest_seq") or 0)
        if not (isinstance(info, dict) and info.get("lazy")):
            client._agent_built.set()
        client._forwarder = asyncio.create_task(client._forward(), name=f"hermes-session-{sid}")
        await client._await_agent_build()
        if read_cron_run_output is not None:
            client._start_cron(cron_store or HermesCronJobRepository(), read_cron_run_output)
        return client

    async def _await_agent_build(self) -> None:
        """Wait for Hermes to build this session's agent.

        Hermes builds an agent — its tools, Skills and model metadata — after
        ``session.create`` answers, and a prompt waits for that build. Waiting
        here makes the build part of activating the conversation, so the
        backend a workspace publishes as ready has already done it.
        """

        try:
            async with asyncio.timeout(_AGENT_BUILD_TIMEOUT_SECONDS):
                await self._agent_built.wait()
        except TimeoutError:
            # Hermes fails the first prompt with its own timeout, which is the
            # outcome the conversation reports.
            logger.warning(
                "Hermes did not build the agent of session=%s within %ss",
                self._sid,
                _AGENT_BUILD_TIMEOUT_SECONDS,
            )

    def _start_cron(
        self,
        store: HermesCronJobRepository,
        read_run_output: Callable[[str], Awaitable[CronRunOutput | None]],
    ) -> None:
        async def list_jobs() -> list[dict[str, Any]]:
            listed = await self._gateway.request("cron.manage", {"action": "list", "include_disabled": True})
            jobs = listed.get("jobs")
            return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []

        self._cron = HermesCronWatch(
            profile_key=self._gateway.profile_key,
            session_id=self._platform_session_id,
            store=store,
            list_jobs=list_jobs,
            read_run_output=read_run_output,
            report=lambda report: self._inbox.put_nowait(("cron", report)),
        )
        self._cron.start()

    async def _forward(self) -> None:
        """Move the session's frames into this client's inbox, in wire order."""

        try:
            while True:
                wire: HermesTuiWireEvent = await self._subscription.next_event()
                if not wire.is_request:
                    seq = wire.event.get("seq")
                    if isinstance(seq, int) and not isinstance(seq, bool):
                        self._received_seq = max(self._received_seq, seq)
                    kind = str(wire.event.get("type") or "")
                    if kind == "session.info" and isinstance(wire.event.get("payload"), dict):
                        self._server_info = dict(wire.event["payload"])
                    if kind == "session.info" or (kind == "error" and wire.session_id):
                        # A failed build is announced as an error; the prompt reports it.
                        self._agent_built.set()
                self._inbox.put_nowait(("wire", wire))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._inbox.put_nowait(("end", exc))

    # -- the contract ------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        return not self._closed and self._gateway.is_live

    @property
    def engine_session_key(self) -> str | None:
        return self._engine_session_key

    async def submit(self, item: RuntimeInput) -> None:
        if self._closed:
            raise EngineStreamDetached("Hermes runtime client is closed")
        if item.input_id in self._texts or any(p.input == item.input_id for p in self._translation.state.pending):
            return
        if any(block.get("type") != "text" for block in item.content_blocks):
            raise RuntimeInputRefused(code="ENGINE_CAPABILITY_UNAVAILABLE", message="Hermes takes text messages only")
        self._texts[item.input_id] = item.content
        await self._send(item.input_id, item.content)

    async def _send(self, input_id: str, text: str) -> None:
        entry = _Pending(input=input_id, receipt="sending", after=self._received_seq)
        self._translation.state.pending.append(entry)
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._inflight[input_id] = (done, entry.after)
        try:
            result = await self._gateway.request("prompt.submit", {"session_id": self._sid, "text": text})
            status = result.get("status")
            if status == "streaming":
                entry.receipt = "streaming"
                row = result.get("user_row_id")
                entry.admitted = isinstance(row, int) and not isinstance(row, bool)
            elif status == "queued":
                entry.receipt = "queued"
            elif status in {"steered", "redirected"}:
                # Folded into the running turn, which has started: the
                # profile's busy-input mode is not ``queue``. Hermes answers
                # it in that turn.
                logger.warning(
                    "Hermes %s an input into the running turn; the profile's "
                    "display.busy_input_mode is not queue: session=%s",
                    status,
                    self._platform_session_id,
                )
                self._translation.state.pending.remove(entry)
                self._inbox.put_nowait(("admit", input_id))
            elif result.get("voice_stopped") is True:
                raise RuntimeInputRefused(
                    code="HERMES_INPUT_REFUSED",
                    message="Hermes took this message as the stop phrase of its voice mode",
                )
            else:
                raise RuntimeError(f"Hermes answered prompt.submit with no status Hermes defines: {result!r}")
        except HermesTuiRpcError as exc:
            self._forget(entry)
            if exc.code in _REFUSING_SUBMIT_CODES:
                raise RuntimeInputRefused(
                    code="HERMES_INPUT_REFUSED", message=f"Hermes refused the message: {exc.message}"
                ) from exc
            raise
        except BaseException:
            self._forget(entry)
            raise
        finally:
            self._inflight.pop(input_id, None)
            done.set_result(None)
        self._inbox.put_nowait(("receipt", input_id))

    def _forget(self, entry: _Pending) -> None:
        with contextlib.suppress(ValueError):
            self._translation.state.pending.remove(entry)
        self._texts.pop(entry.input, None)

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        if self._reading:
            raise RuntimeError("a Hermes runtime client serves one event stream")
        skip: int | None = None
        restored: _State | None = None
        if after is None:
            last_seen = 0
        else:
            if after.generation != self._generation:
                raise RuntimeCursorExpired(
                    f"cursor from Hermes generation {after.generation!r}, the session is {self._generation!r}"
                )
            restored = _State.from_json(after.position.get("state"))
            seq = int(after.position["seq"])
            skip = after.position.get("skip")
            last_seen = seq - 1 if skip is not None else seq
        reply = await self._gateway.request(
            "session.events.since", {"session_id": self._sid, "last_seen": last_seen}
        )
        replayed = [item for item in reply.get("events") or [] if isinstance(item, dict)]
        if (
            reply.get("epoch") != self._epoch
            or reply.get("truncated") is True
            or int(reply.get("latest_seq") or 0) < last_seen
            or (skip is not None and (not replayed or replayed[0].get("seq") != last_seen + 1))
        ):
            # The state of an expired cursor is not this session's any more:
            # what happened in the gap is unknown, so ``state`` starts afresh.
            raise RuntimeCursorExpired(f"Hermes holds none of the events of session {self._sid} after {last_seen}")
        if restored is not None:
            # Replay lookup can overlap a submit. Its live native receipt
            # and sequence watermark must survive cursor restoration.
            local = {item.input: item for item in self._translation.state.pending}
            restored.pending = [local.pop(item.input, item) for item in restored.pending] + list(local.values())
            self._translation.state = restored
        self._reading = True
        self._last_seq = last_seen
        self._received_seq = max(self._received_seq, int(reply.get("latest_seq") or 0))
        for index, params in enumerate(replayed):
            for event in await self._ring(params, skip=skip if index == 0 else None):
                yield event
        for request in reply.get("open_requests") or []:
            if isinstance(request, dict):
                for event in await self._request(request):
                    yield event
        while True:
            kind, item = await self._inbox.get()
            if kind == "end":
                if isinstance(item, EngineStreamDetached):
                    logger.info("Hermes session %s stream ended: %s", self._sid, item)
                    return
                raise item
            if kind == "wire":
                if item.is_request:
                    batch = await self._request(item.event)
                elif item.session_id == self._sid:
                    batch = await self._ring(item.event, skip=None)
                else:
                    batch = await self._broadcast(item.event)
            elif kind == "receipt":
                batch = self._synthetic(lambda: self._translation.receipt(item))
            elif kind == "admit":
                batch = self._synthetic(lambda: [InputAdmitted(None, item)])
            elif kind == "unheld":
                batch = self._synthetic(lambda: self._translation.unheld(list(item)))
            elif kind == "cron":
                batch = await self._report_cron(item)
            else:  # pragma: no cover - the inbox kinds are closed
                raise AssertionError(f"unknown inbox item {kind!r}")
            for event in batch:
                yield event

    async def _ring(self, params: dict[str, Any], *, skip: int | None) -> list[RuntimeEvent]:
        """Translate one event of the session's ring, stamped with its cursor."""

        seq = params.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= self._last_seq:
            return []
        # An input sent before this event was received may still await its
        # receipt, which decides whether this event's turn is that input's.
        waits = [done for done, sent_after in self._inflight.values() if sent_after < seq]
        if waits:
            await asyncio.wait(waits)
        pre = self._translation.state.to_json()
        events = self._translation.event(params, seq)
        post = self._translation.state.to_json()
        self._last_seq = seq
        await self._observe(params)
        if not events:
            events = [RuntimeDiagnostic(None, f"hermes.{params.get('type') or 'event'}", params)]
        stamped: list[RuntimeEvent] = []
        last = len(events) - 1
        for index, event in enumerate(events):
            if skip is not None and index < skip:
                continue
            position: dict[str, Any] = (
                {"seq": seq, "state": post} if index == last else {"seq": seq, "skip": index + 1, "state": pre}
            )
            stamped.append(dataclasses.replace(event, cursor=RuntimeCursor(self._generation, position)))
        if self._translation.state.response is None and self._cron_reports:
            stamped += self._flush_cron()
        return stamped

    def _synthetic(self, translate: Callable[[], list[RuntimeEvent]]) -> list[RuntimeEvent]:
        """Stamp events that are not in the ring: re-derived, not re-read, after a cursor."""

        pre = self._translation.state.to_json()
        events = translate()
        post = self._translation.state.to_json()
        last = len(events) - 1
        return [
            dataclasses.replace(
                event,
                cursor=RuntimeCursor(self._generation, {"seq": self._last_seq, "state": post if index == last else pre}),
            )
            for index, event in enumerate(events)
        ]

    async def _request(self, request: dict[str, Any]) -> list[RuntimeEvent]:
        request_id = str(request.get("id") or "")
        method = str(request.get("method") or "")
        if not request_id or request_id in self._translation.state.interactions or request_id in self._withdrawn:
            return []
        if method not in ANSWERED_REQUESTS:
            await self._refuse(request_id, method)
            return []
        return self._synthetic(lambda: self._translation.request(request, self._last_seq))

    async def _refuse(self, request_id: str, method: str) -> None:
        if request_id in self._refused:
            return
        self._refused.add(request_id)
        with contextlib.suppress(Exception):
            await self._gateway.respond_error(
                request_id,
                code=_METHOD_NOT_FOUND,
                message=f"AstraBox does not answer {method!r} requests",
            )

    async def _broadcast(self, event: dict[str, Any]) -> list[RuntimeEvent]:
        """A process-wide event, such as ``cron.changed``: no ``seq``, nothing to translate."""

        if self._cron is not None:
            await self._cron.observe(event)
        return []

    async def _observe(self, params: dict[str, Any]) -> None:
        """What one event means beyond its translation: cron, compression, unheld inputs."""

        kind = str(params.get("type") or "")
        if kind == "request.cancel":
            payload = params.get("payload")
            if isinstance(payload, dict) and payload.get("id"):
                self._withdrawn.add(str(payload["id"]))
        if self._cron is not None:
            await self._cron.observe(params)
        self._observe_context(params)
        state = self._translation.state
        if (
            kind == "session.info"
            and not state.running
            and state.response is None
            and any(item.receipt == "queued" for item in state.pending)
            and (self._unheld_check is None or self._unheld_check.done())
        ):
            self._unheld_check = asyncio.create_task(
                self._check_unheld(), name=f"hermes-unheld-{self._sid}"
            )

    async def _check_unheld(self) -> None:
        """Admit queued inputs Hermes holds no turn for once it is idle with nothing queued.

        ``session.activate`` reads ``running`` and the queue under one lock, so
        an idle session with no ``queued`` prompt will start no turn for them.
        """

        try:
            live = await self._gateway.request("session.activate", {"session_id": self._sid, "omit_messages": True})
        except Exception as exc:  # noqa: BLE001 — the next idle report asks again
            logger.info("Hermes queue check skipped: session=%s error=%s", self._sid, exc)
            return
        if live.get("running") or live.get("queued"):
            return
        queued = [item.input for item in self._translation.state.pending if item.receipt == "queued"]
        if queued:
            self._inbox.put_nowait(("unheld", queued))

    async def _report_cron(self, report: CronRunReport) -> list[RuntimeEvent]:
        state = self._translation.state
        if report.response_id in state.cron:
            # Recorded by a reader before this one, which stopped before it
            # could move the watermark.
            if self._cron is not None:
                await self._cron.delivered(report)
            return []
        self._cron_reports.append(report)
        return [] if state.response is not None else self._flush_cron()

    def _flush_cron(self) -> list[RuntimeEvent]:
        """Report the waiting cron runs: between responses, never inside one."""

        reports, self._cron_reports = self._cron_reports, []
        events: list[RuntimeEvent] = []
        for report in reports:
            self._cron_unrecorded[report.response_id] = report
            events += self._synthetic(lambda report=report: self._translation.cron(report))
        return events

    async def state(self) -> RuntimeState:
        since = await self._gateway.request("session.events.since", {"session_id": self._sid, "last_seen": _LATEST})
        if since.get("epoch") != self._epoch:
            raise RuntimeCursorExpired("the Hermes backend restarted while this client was connected")
        live = await self._gateway.request("session.activate", {"session_id": self._sid, "omit_messages": True})
        translation = self._translation
        state = translation.state
        response_id = state.response.id if state.response is not None else ""
        pending: list[PendingInteraction] = []
        for request in since.get("open_requests") or []:
            request_id = str(request.get("id") or "")
            method = str(request.get("method") or "")
            if method not in ANSWERED_REQUESTS:
                await self._refuse(request_id, method)
                continue
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            record = interaction_record(request_id=request_id, method=method, params=params, response_id=response_id)
            pending.append(PendingInteraction(response_id, request_id, record))
            state.interactions.setdefault(request_id, response_id)
        state.running = live.get("running") is True
        status: RuntimeStatus = "waiting" if pending else ("working" if state.running else "idle")
        state.status = status
        return RuntimeState(
            status=status,
            open_responses=(
                (OpenResponse(state.response.id, state.response.origin, tuple(state.response.inputs)),)
                if state.response is not None
                else ()
            ),
            pending_interactions=tuple(pending),
            queued_input_ids=tuple(item.input for item in state.pending if item.receipt != "streaming"),
            admitted_input_ids=tuple(item.input for item in state.pending if item.admitted),
            cursor=RuntimeCursor(
                self._generation, {"seq": int(since.get("latest_seq") or 0), "state": state.to_json()}
            ),
        )

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        since = await self._gateway.request("session.events.since", {"session_id": self._sid, "last_seen": _LATEST})
        request = next(
            (
                item
                for item in since.get("open_requests") or []
                if isinstance(item, dict) and item.get("id") == interaction_id
            ),
            None,
        )
        if request is None or request.get("method") not in ANSWERED_REQUESTS:
            raise InteractionNotOpen(interaction_id)
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        record = interaction_record(
            request_id=interaction_id, method=str(request["method"]), params=params, response_id=""
        )
        reply = await self._gateway.request(
            "request.answer", {"id": interaction_id, "result": answer_result(record, response)}
        )
        if reply.get("status") != "ok":
            raise InteractionNotOpen(interaction_id)
        self._translation.state.interactions.pop(interaction_id, None)

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        """Stop the running turn, keeping the inputs queued behind it.

        ``session.interrupt`` drops Hermes' queue together with the turn
        (``session_lifecycle._interrupt_session_turn``). Those inputs never
        started, so they are handed to Hermes again in their order: sending
        them is delivery, not a replay.
        """

        if cancel_queued:
            raise NotImplementedError("the runtime contract reports no input a runtime discards")
        state = self._translation.state
        queued = [item for item in state.pending if item.receipt == "queued"]
        await self._gateway.request("session.interrupt", {"session_id": self._sid})
        for item in queued:
            with contextlib.suppress(ValueError):
                state.pending.remove(item)
            text = self._texts.get(item.input)
            if text is None:
                # Sent by an earlier reader of this session, which held the text.
                logger.warning(
                    "Hermes dropped queued input %s on interrupt and this client never held its text: session=%s",
                    item.input,
                    self._platform_session_id,
                )
                continue
            await self._send(item.input, text)

    async def committed(self, cursor: RuntimeCursor) -> None:
        if cursor.generation != self._generation or not self._cron_unrecorded:
            return
        recorded = set((cursor.position.get("state") or {}).get("cron") or [])
        for response_id in [key for key in self._cron_unrecorded if key in recorded]:
            report = self._cron_unrecorded.pop(response_id)
            if self._cron is not None:
                await self._cron.delivered(report)

    async def capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind="assistant",
            input_content_types=["text"],
            permission_modes=[],
            supports_interaction=True,
            supports_child_run_control=True,
            supports_server_info=True,
            extra={"transport": "hermes_tui_json_rpc_over_hermes_serve_websocket"},
        )

    async def stop_child_run(self, control_id: str) -> None:
        await self._gateway.request("subagent.interrupt", {"session_id": self._sid, "subagent_id": str(control_id)})

    async def get_server_info(self) -> dict[str, Any] | None:
        return dict(self._server_info) if isinstance(self._server_info, dict) else None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [self._forwarder, self._unheld_check, self._compaction]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        if self._cron is not None:
            await self._cron.stop()
        self._gateway.unsubscribe(self._subscription)
        self._inbox.put_nowait(("end", EngineStreamDetached("Hermes runtime client closed")))

    async def dispose(self) -> bool:
        """End this conversation's vendor session; the backend keeps serving its siblings."""

        closed = False
        if self._gateway.is_live:
            try:
                await self._gateway.request("session.close", {"session_id": self._sid})
                closed = True
            except Exception as exc:  # noqa: BLE001 — the session ends with the backend
                logger.warning(
                    "Hermes session.close failed on dispose: platform_session=%s tui_session=%s err=%s",
                    self._platform_session_id,
                    self._sid,
                    exc,
                )
        await self.close()
        return closed

    # -- idle compression ----------------------------------------------------------

    def _observe_context(self, event: dict[str, Any]) -> None:
        """Track context occupancy and compress after a reply once the conversation is quiet.

        Hermes' manual compression snapshots the history and summarizes it
        without holding the history lock, so a message sent while it runs is
        submitted and answered at once on the full history; Hermes then
        discards the compression result by its history-version fence and the
        next quiet period compresses again (``session_compression.py``).
        """

        payload = event.get("payload")
        usage = payload.get("usage") if isinstance(payload, dict) else None
        percent = usage.get("context_percent") if isinstance(usage, dict) else None
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            self._context_percent = int(percent)
        if str(event.get("type") or "") != "message.complete":
            return
        if (self._context_percent or 0) < _IDLE_COMPACT_AT_PERCENT:
            return
        if self._compaction is not None and not self._compaction.done():
            return
        self._compaction = asyncio.create_task(
            self._compact_when_quiet(), name=f"hermes-idle-compaction-{self._platform_session_id}"
        )

    def _quiet(self) -> bool:
        state = self._translation.state
        return not self._closed and state.response is None and not state.pending and not self._inflight

    async def _compact_when_quiet(self) -> None:
        await asyncio.sleep(_IDLE_COMPACT_QUIET_SECONDS)
        if not self._quiet():
            return
        try:
            result = await self._gateway.request(
                "session.compress", {"session_id": self._sid}, timeout=_COMPRESS_TIMEOUT_SECONDS
            )
            self._context_percent = None
            logger.info(
                "Hermes idle compression: session=%s status=%s before=%s after=%s",
                self._platform_session_id,
                result.get("status"),
                result.get("before_tokens"),
                result.get("after_tokens"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a later quiet period retries
            logger.info("Hermes idle compression skipped: session=%s error=%s", self._platform_session_id, exc)


__all__ = [
    "ANSWERED_REQUESTS",
    "HermesGatewayPort",
    "HermesRuntimeClient",
    "answer_result",
    "interaction_contract",
    "interaction_record",
]
