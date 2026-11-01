"""Hermes behind the runtime contract (``engine/runtime.py``).

One conversation is one session of the box's Hermes gateway
(:mod:`.hermes_gateway`), created under a request id derived from the platform
Session and resumed by its session id. This client reads that session's events
and the gateway's requests to it, and reports them as runtime events. Vendor
references are to ``gateway/`` and ``tui_gateway/`` at the commit the image
pins (``containers/sandbox-hermes/Dockerfile``).

* **Cursor.** The gateway stamps every event of a session with a ``seq`` and
  the ``replay_epoch`` of that session's replay ring, and
  ``session.events.since`` returns the events after a ``seq`` of that epoch
  (``gateway/session_events.py``). The cursor's generation is the gateway
  instance and the epoch, which together name that numbering; its position is
  an event's ``seq`` and the translation state at that point. The ring keeps
  fewer events than a long reply streams, so the state travels in the cursor
  rather than being rebuilt from the reply's first event. A cursor the gateway
  answers with ``snapshot_required`` — another instance or epoch, or a
  position its ring has dropped — is expired, and the reader reconciles
  from the session's ``session.resume`` snapshot (:meth:`state`).
* **Inputs.** ``prompt.submit`` carries the input's id in the gateway's
  admission request id (``astrabox-input:<input_id>``). The gateway admits a
  request id once per session and principal into its durable ledger
  (``gateway/session_authority.py``), so handing an input over again — a
  reader that took over after a first delivery whose outcome it did not see —
  returns the first admission's receipt instead of running the input twice.
  The receipt means the ledger holds the input.
* **Responses.** Each admission is one turn of the session's FIFO. A response
  starts at the turn's ``message.start``, or at its first event the reader
  sees, and ends at its ``message.complete`` (``complete``, ``interrupted`` or
  ``error``). Every turn event names its ``admission_id``, and the admissions
  the gateway lists in ``session.info`` and in the ``session.resume`` snapshot
  name the request id each came from. Every admission has response identity
  ``hermes:<admission_id>``. Its input association is separate: learning which
  input it answers cannot rename the response. Admissions from other producers
  (a finished background delegation or process notification) are engine-originated.
* **Unknown admissions.** A gateway that restarts while a turn runs marks that
  admission ``unknown`` and holds the session's FIFO behind it until a client
  resolves it (``prompt.resolve_unknown``), which never runs it again. This
  client ends that turn's response as failed, saying so, and resolves the
  admission once the platform has recorded the report (:meth:`committed`), so
  the inputs queued behind it run.
* **Settled admissions.** A gateway that shuts down cleanly interrupts its
  running turn and settles it ``interrupted``; it never runs it again. An input
  handed over again after its turn settled in a gateway whose events no reader
  recorded is answered with that terminal admission. This client reports the
  turn from the outcome and result Hermes saved at settlement
  (``prompt.receipt``), once no other response is open, and the input's
  custody closes with that report.
* **Interactions.** Approvals and clarify questions are ``approval.request``
  and ``clarify.request`` events naming a prompt id and the execution
  generation they belong to (``gateway/session_pending_controls.py``); the
  prompt id is the interaction id, an answer returns through
  ``approval.respond`` or ``clarify.respond`` on a connection attached to the
  session, and ``*.settled`` closes the prompt. Password and secret prompts are
  requests the gateway asks a client; this host refuses them at once, so the
  agent does not wait out their deadline.
* **Engine responses.** Each run of the conversation's own cron jobs
  (:mod:`.hermes_cron`) is a response of origin ``engine`` too, reported once
  no other response is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineStreamDetached,
)
from astrabox.core.service.orchestrator.engine.hermes_client import (
    HermesGatewayFrame,
    HermesGatewayRpcError,
    _translate_hermes_child_run_event,
)
from astrabox.core.service.orchestrator.engine.hermes_cron import (
    CronRunOutput,
    CronRunReport,
    HermesCronWatch,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_DECISION,
    PRESENTATION_FORM,
    validate_interaction_contract,
    validated_question_answer_rows,
)
from astrabox.core.service.orchestrator.engine.runtime import (
    InputAdmitted,
    InputMessageObserved,
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

#: The prefix of the admission request id that carries a platform input id.
INPUT_REQUEST_PREFIX = "astrabox-input:"
#: The prefix of the ``session.create`` request id derived from a platform
#: Session: the gateway answers a repeated create with the same session.
SESSION_REQUEST_PREFIX = "astrabox-session:"
#: The launch surface this adapter creates its sessions as. Hermes shows a
#: ``tui`` session's reasoning by default (``display.show_reasoning``,
#: ``gateway/local_viewer_events.py``), asks its clarify questions one card at a
#: time (``clarify.request``), and gives it the toolsets of Hermes' own TUI.
SESSION_SOURCE = "tui"
#: ``session.detach``'s ``end_reason`` for a conversation the platform ended.
END_REASON = "conversation_ended"
#: The JSON-RPC code Hermes' guide names for a request method a host does not implement.
_METHOD_NOT_FOUND = -32601
#: Cron responses a cursor remembers, newest last; more than this between two
#: commits of the reader is not a pace cron jobs can reach.
_CRON_REMEMBERED = 16
#: How long ending a conversation waits for its gateway session to leave the
#: live list: the interrupted turn settles first.
_END_BUDGET_SECONDS = 60.0
_END_POLL_SECONDS = 0.25
#: The failure a turn lost across a gateway restart is reported with.
UNKNOWN_OUTCOME_MESSAGE = (
    "Hermes restarted while this message was running, so whether it finished is unknown. "
    "It was not run again."
)
#: A turn Hermes interrupted while no reader recorded it; Hermes never runs an
#: interrupted turn again. ``cause`` is Hermes' own interrupt reason, if saved.
SETTLED_INTERRUPTED_MESSAGE = "Hermes stopped this message{cause} and will not run it again."


#: Transcript copy for each approval choice Hermes offers (``PendingControls.register``).
_APPROVAL_REPLIES = {
    "once": "The user allowed this once.",
    "session": "The user allowed this for the rest of the session.",
    "always": "The user always allows this.",
    "deny": "The user denied this.",
}


class HermesGatewayPort(Protocol):
    """What this client needs from the profile's gateway handle."""

    profile_key: str

    @property
    def is_live(self) -> bool: ...

    @property
    def instance_id(self) -> str: ...

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = ...
    ) -> dict[str, Any]: ...

    async def respond_error(self, request_id: str, *, code: int, message: str) -> None: ...

    def subscribe(self, session_id: str, *, after_offset: int = 0) -> Any: ...

    def unsubscribe(self, subscription: Any) -> None: ...

    def subscribed(self, session_id: str) -> bool: ...


def platform_input_id(request_id: Any) -> str | None:
    """The platform input an admission request id carries, or ``None`` for another producer's."""

    if isinstance(request_id, str) and request_id.startswith(INPUT_REQUEST_PREFIX):
        return request_id[len(INPUT_REQUEST_PREFIX):] or None
    return None


def _snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """A ``session.resume`` / ``session.create`` snapshot, refused when it lacks what this client reads."""

    if (
        not isinstance(value.get("session_id"), str) or not value["session_id"]
        or not isinstance(value.get("replay_epoch"), str) or not value["replay_epoch"]
        or type(value.get("last_sequence")) is not int
        or not isinstance(value.get("subscription_id"), str)
        or type(value.get("execution_generation")) is not int
        or not isinstance(value.get("pending"), list)
        or not isinstance(value.get("prompts"), list)
    ):
        raise RuntimeProtocolError(
            "Hermes returned a session snapshot without its session id, replay position, "
            "subscription, execution generation or pending work."
        )
    return value


# --------------------------------------------------------------------------
# Interactions
# --------------------------------------------------------------------------


def interaction_contract(prompt: dict[str, Any]) -> dict[str, Any]:
    """The platform contract for one Hermes prompt, built from the prompt alone.

    An approval is a decision among the choices Hermes offers for it, under
    Hermes' own names; a clarify prompt is a form with its one question.
    ``raw_input`` is the prompt as Hermes sent it.
    """

    kind = str(prompt.get("kind") or "")
    raw = {key: value for key, value in prompt.items() if key != "session_id"}
    if kind == "approval":
        choices = [str(choice) for choice in prompt.get("choices") or []]
        unknown = [choice for choice in choices if choice not in _APPROVAL_REPLIES]
        if not choices or unknown:
            raise ValueError(f"Hermes approval offers no choice the adapter knows: {choices!r}")
        description = str(prompt.get("description") or "").strip()
        contract: dict[str, Any] = {
            "tool_name": kind,
            "presentation": PRESENTATION_DECISION,
            "prompt": description or "Hermes asks for approval.",
            "raw_input": raw,
            "body": str(prompt.get("command") or ""),
            "options": [
                {"id": choice, "denial": choice == "deny", "reply": _APPROVAL_REPLIES[choice]}
                for choice in choices
            ],
        }
    elif kind == "clarify":
        choices = prompt.get("choices") if isinstance(prompt.get("choices"), list) else []
        contract = {
            "tool_name": kind,
            "presentation": PRESENTATION_FORM,
            "prompt": str(prompt.get("question") or "").strip() or "Hermes has a question for you.",
            "raw_input": raw,
            "questions": [
                {
                    "id": "answer",
                    "question": str(prompt.get("question") or ""),
                    "native_answer_key": "answer",
                    "multi_select": bool(prompt.get("multi_select")),
                    "options": [{"label": str(choice)} for choice in choices if str(choice).strip()],
                    "allow_free_text": True,
                    "allow_empty_text": True,
                }
            ],
        }
    else:
        raise ValueError(f"Hermes prompt kind {kind!r} is not one this host answers")
    validate_interaction_contract(contract)
    return contract


def interaction_record(*, prompt: dict[str, Any], response_id: str) -> dict[str, Any]:
    """The platform interaction record for one Hermes prompt."""

    return {
        "interaction_id": str(prompt["prompt_id"]),
        "response_id": response_id,
        **interaction_contract(prompt),
    }


def answer_result(record: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """The field Hermes reads for a person's answer to ``record``.

    An approval answers ``{choice}`` and a clarify prompt ``{answer}``, where
    ``''`` skips the question (``ClarifyRespondParams``). A comment on an
    approval has no field to travel in and is not sent.
    """

    if record["presentation"] == PRESENTATION_FORM:
        if response.get("decline") is True:
            return {"answer": ""}
        rows = validated_question_answer_rows(record, response)
        return {"answer": rows[0]["response_text"]}
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
    #: The gateway admission whose turn this is; ``None`` for output no admission names.
    admission: str | None = None
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
class _State:
    response: _Response | None = None
    #: Admission id -> the platform input it carries, for admissions whose turn has not ended.
    admissions: dict[str, str] = field(default_factory=dict)
    #: Open interaction (prompt) id -> the response it belongs to.
    interactions: dict[str, str] = field(default_factory=dict)
    running: bool = False
    status: RuntimeStatus = "idle"
    #: Cron responses already reported, newest last.
    cron: list[str] = field(default_factory=list)
    #: Unknown admissions reported failed -> their execution generation, which
    #: ``prompt.resolve_unknown`` names once the platform recorded the report.
    unknown: dict[str, int] = field(default_factory=dict)
    #: Settled admissions no reader saw run, oldest first, each waiting to be
    #: reported until no other response is open: ``{"input_id", "admission",
    #: "outcome", "result", "usage"}`` from the gateway's receipt.
    settled: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "response": dataclasses.asdict(self.response) if self.response else None,
            "admissions": dict(self.admissions),
            "interactions": dict(self.interactions),
            "running": self.running,
            "status": self.status,
            "cron": list(self.cron),
            "unknown": dict(self.unknown),
            "settled": [dict(item) for item in self.settled],
        }

    @classmethod
    def from_json(cls, value: Any) -> _State:
        if not isinstance(value, dict):
            raise ValueError("a Hermes cursor carries its translation state")
        response = value.get("response")
        return cls(
            response=_Response(**response) if isinstance(response, dict) else None,
            admissions={str(k): str(v) for k, v in (value.get("admissions") or {}).items()},
            interactions={str(k): str(v) for k, v in (value.get("interactions") or {}).items()},
            running=bool(value.get("running")),
            status=value.get("status") or "idle",
            cron=[str(item) for item in value.get("cron") or []],
            unknown={str(k): int(v) for k, v in (value.get("unknown") or {}).items()},
            settled=[dict(item) for item in value.get("settled") or [] if isinstance(item, dict)],
        )


def _finish(reason: str) -> dict[str, Any]:
    return {"type": "finish", "finishReason": reason}


def _pending_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _tool_result(value: Any) -> Any:
    """``tool.complete``'s ``result``: Hermes sends the tool's output as JSON text when it is JSON."""

    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return json.loads(value)
    return value


class _Translation:
    """Hermes events to runtime events, holding only what the cursor carries."""

    def __init__(self, generation: Callable[[], str], answering: Callable[[str], bool]) -> None:
        self._generation = generation
        #: Whether this client is answering a prompt: Hermes settles a prompt
        #: (``*.settled``) before it replies to the answer that settled it.
        self._answering = answering
        self.state = _State()

    # -- one ring event ------------------------------------------------------

    def event(self, params: dict[str, Any], seq: int) -> list[RuntimeEvent]:
        kind = str(params.get("type") or "")
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        assert isinstance(payload, dict)
        admission = params.get("admission_id") if isinstance(params.get("admission_id"), str) else None
        if kind == "session.info":
            return self.pending(payload.get("pending"), running=payload.get("running"))
        if kind == "message.start":
            response = self.state.response
            if response is not None and (admission is None or response.admission == admission):
                # The same turn announced again: a claim and its turn runner both start it.
                return []
            if response is not None:
                raise RuntimeProtocolError(
                    f"Hermes started admission {admission!r} while the turn of "
                    f"{response.admission!r} had not ended."
                )
            self.state.running = True
            return self._open(seq, admission) + self._status()
        if kind == "message.delta":
            return self._text(seq, admission, str(payload.get("text") or ""), delta=True)
        if kind == "reasoning.delta":
            return self._reasoning(seq, admission, str(payload.get("text") or ""))
        if kind == "message.interim":
            # Commentary beside tool calls; streamed text already reached the
            # browser as deltas, so only text that was not streamed is shown.
            if payload.get("already_streamed") is True or not str(payload.get("text") or ""):
                return []
            return self._text(seq, admission, str(payload["text"]), delta=False)
        if kind == "tool.start":
            return self._tool_start(seq, admission, payload)
        if kind == "tool.complete":
            return self._tool_complete(seq, admission, payload)
        if kind == "message.complete":
            # The payload names its own admission: a completion published for a
            # resolved admission can carry another running turn's stamp.
            completed = payload.get("admission_id") if isinstance(payload.get("admission_id"), str) else admission
            return self._complete(seq, completed, payload)
        if kind in {"approval.request", "clarify.request"}:
            return self._prompt(seq, admission, {**payload, "kind": kind.split(".", 1)[0]})
        if kind in {"approval.settled", "clarify.settled"}:
            prompt_id = str(payload.get("prompt_id") or "")
            return self.closed(prompt_id, "answered" if self._answering(prompt_id) else "withdrawn")
        if kind.startswith("subagent."):
            response_id = self.state.response.id if self.state.response else None
            return [
                ResponseOutput(None, response_id, frame)
                for frame in _translate_hermes_child_run_event(kind, payload)
            ]
        # ``thinking.delta`` carries a spinner label or its reset and
        # ``reasoning.available`` a tool-progress preview, not reasoning
        # (``gateway/local_viewer_events.py``); every other event is session
        # telemetry the browser does not render.
        return []

    # -- the gateway's own account of pending work -----------------------------

    def pending(self, rows: Any, *, running: Any = None) -> list[RuntimeEvent]:
        """Learn the admissions the gateway lists, and report the ones it lost.

        ``session.info`` and the ``session.resume`` snapshot list every pending
        admission with the request id it came from (``_publish_pending`` in
        ``gateway/session_authority.py``). An ``unknown`` one is a turn a
        gateway restart cut off: its response ends failed here, once. Only an
        event carrying the list says what is pending; one without it changes
        nothing here but ``running``.
        """

        state = self.state
        events: list[RuntimeEvent] = []
        if isinstance(rows, list):
            listed = _pending_rows(rows)
            for row in listed:
                input_id = platform_input_id(row.get("input_id"))
                if input_id is not None and isinstance(row.get("admission_id"), str):
                    state.admissions.setdefault(row["admission_id"], input_id)
            for row in listed:
                admission = row.get("admission_id")
                generation = row.get("execution_generation")
                if (
                    row.get("status") == "unknown" and isinstance(admission, str)
                    and type(generation) is int and admission not in state.unknown
                ):
                    events += self._lost(admission, generation)
            # A reported admission absent from the gateway's list has been resolved.
            still = {row.get("admission_id") for row in listed}
            state.unknown = {key: value for key, value in state.unknown.items() if key in still}
        if isinstance(running, bool):
            state.running = running
        return events + self._status()

    def _lost(self, admission: str, generation: int) -> list[RuntimeEvent]:
        state = self.state
        events: list[RuntimeEvent] = []
        if state.response is None or state.response.admission != admission:
            if state.response is not None:
                raise RuntimeProtocolError(
                    f"Hermes lost admission {admission!r} while the turn of "
                    f"{state.response.admission!r} had not ended."
                )
            events = self._open(None, admission, observed=False)
        response = state.response
        assert response is not None
        error = {"message": UNKNOWN_OUTCOME_MESSAGE}
        events += self._end(
            "failed", "unknown", usage=None, error=error,
            parts=[{"type": "error", "errorText": UNKNOWN_OUTCOME_MESSAGE}, _finish("error")],
        )
        state.unknown[admission] = generation
        return events

    # -- inputs ----------------------------------------------------------------

    def receipt(
        self, input_id: str, receipt: dict[str, Any], settled: dict[str, Any] | None = None
    ) -> list[RuntimeEvent]:
        """``prompt.submit`` answered: the gateway's ledger holds the input.

        A ``terminal`` receipt answers an input handed over again after its
        turn already ran and settled in a gateway whose events no reader
        recorded; ``settled`` is that admission's ``prompt.receipt`` with its
        saved result, and the turn is reported from it.
        """

        admission = receipt.get("admission_id")
        if isinstance(admission, str) and receipt.get("status") != "terminal":
            self.state.admissions.setdefault(admission, input_id)
        # The receipt is written to the server log before the admission is
        # recorded, so a reader stopped between the two still leaves it. A
        # settled admission closes the input's custody with its report.
        events: list[RuntimeEvent] = [
            RuntimeDiagnostic(None, "hermes.prompt.submit", {"input_id": input_id, "receipt": receipt}),
        ]
        if receipt.get("status") != "terminal":
            events.append(InputAdmitted(None, input_id))
        generation = receipt.get("execution_generation")
        if (
            receipt.get("status") == "unknown" and isinstance(admission, str)
            and type(generation) is int and admission not in self.state.unknown
        ):
            events += self._lost(admission, generation)
        elif receipt.get("status") == "terminal" and isinstance(admission, str):
            saved = (settled or {}).get("result")
            self.state.settled.append({
                "input_id": input_id,
                "admission": admission,
                "outcome": receipt.get("outcome"),
                "result": saved if isinstance(saved, dict) else {},
                "usage": (settled or {}).get("usage") if isinstance((settled or {}).get("usage"), dict) else None,
            })
            events += self._report_settled()
        return events

    def _report_settled(self) -> list[RuntimeEvent]:
        """Report the settled admissions waiting, while no other response is open.

        The gateway saved each one's outcome and result at settlement
        (``prompt.receipt`` with ``include_result``, ``gateway/session_results.py``);
        its outcome reads as ``message.complete`` would have
        (``completion_payload`` in ``gateway/session_settlement_recovery.py``).
        """

        events: list[RuntimeEvent] = []
        while self.state.response is None and self.state.settled:
            item = self.state.settled.pop(0)
            native = item.get("outcome")
            outcome: ResponseOutcome
            if native == "completed":
                outcome, reason, finish = "completed", "complete", "stop"
            elif native == "interrupted":
                outcome, reason, finish = "cancelled", "interrupted", "other"
            elif native == "cancelled":
                outcome, reason, finish = "cancelled", "cancelled", "other"
            elif native == "failed":
                outcome, reason, finish = "failed", "error", "error"
            else:
                raise RuntimeProtocolError(f"Hermes settled admission {item.get('admission')!r} as {native!r}")
            self.state.admissions[str(item["admission"])] = str(item["input_id"])
            events.append(InputAdmitted(None, str(item["input_id"])))
            events += self._open(None, str(item["admission"]), observed=False)
            result = item.get("result") or {}
            text = result.get("final_response")
            parts: list[dict[str, Any]] = []
            if outcome != "failed" and isinstance(text, str) and text:
                block = self._block_id("text")
                parts += [
                    {"type": "text-start", "id": block},
                    {"type": "text-delta", "id": block, "delta": text},
                    {"type": "text-end", "id": block},
                ]
            error: dict[str, Any] | None = None
            if outcome == "failed":
                message = str(result.get("failure_reason") or result.get("error") or text or "Hermes failed")
                error = {"message": message}
            elif native == "interrupted":
                cause = result.get("interrupt_message")
                message = SETTLED_INTERRUPTED_MESSAGE.format(
                    cause=f" ({cause})" if isinstance(cause, str) and cause else ""
                )
                error = {"message": message}
            if error is not None:
                parts.append({"type": "error", "errorText": error["message"]})
            parts.append(_finish(finish))
            events += self._end(outcome, reason, usage=item.get("usage"), error=error, parts=parts)
        return events

    def closed(self, prompt_id: str, reason: Literal["answered", "withdrawn", "expired"]) -> list[RuntimeEvent]:
        if prompt_id not in self.state.interactions:
            return []
        del self.state.interactions[prompt_id]
        return [InteractionClosed(None, prompt_id, reason)] + self._status()

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

    def _open(self, seq: int | None, admission: str | None, *, observed: bool = True) -> list[RuntimeEvent]:
        """Start the response for the turn of ``admission``, which began at event ``seq``.

        A turn the reader saw start has taken its input into the conversation,
        so the input is observed and admitted here; a turn reported only as
        lost (``observed=False``) is neither, and the input's custody closes
        with the gateway's receipt to its next submit.
        """

        input_id = self.state.admissions.get(admission) if admission else None
        events: list[RuntimeEvent] = []
        if input_id is not None:
            origin: ResponseOrigin = "input"
            inputs: tuple[str, ...] = (input_id,)
            if observed:
                events += [InputMessageObserved(None, input_id), InputAdmitted(None, input_id)]
        else:
            origin, inputs = "engine", ()
        response_id = f"hermes:{admission}" if admission else f"hermes:{self._generation()}:{seq}"
        self.state.response = _Response(id=response_id, origin=origin, inputs=list(inputs), admission=admission)
        events.append(ResponseStarted(None, response_id, origin, inputs))
        events.append(ResponseOutput(None, response_id, self._start_part(response_id, origin, inputs)))
        return events

    @staticmethod
    def _start_part(response_id: str, origin: ResponseOrigin, input_ids: tuple[str, ...]) -> dict[str, Any]:
        return {
            "type": "start",
            "messageId": response_id,
            "messageMetadata": {"response_id": response_id, "input_ids": list(input_ids), "origin": origin},
        }

    def _ensure(self, seq: int, admission: str | None) -> list[RuntimeEvent]:
        """Open a response for turn content that arrived without one."""

        return [] if self.state.response is not None else self._open(seq, admission)

    def _complete(self, seq: int, admission: str | None, payload: dict[str, Any]) -> list[RuntimeEvent]:
        if admission is not None and admission in self.state.unknown:
            # Already ended here as lost; resolving it can publish its completion.
            return []
        response = self.state.response
        if response is not None and admission is not None and response.admission != admission:
            raise RuntimeProtocolError(
                f"Hermes completed admission {admission!r} while the turn of "
                f"{response.admission!r} had not ended."
            )
        status = str(payload.get("status") or "")
        outcome: ResponseOutcome
        if status == "complete":
            outcome, reason = "completed", "stop"
        elif status == "interrupted":
            outcome, reason = "cancelled", "other"
        elif status == "error":
            outcome, reason = "failed", "error"
        else:
            raise RuntimeProtocolError(f"Hermes message.complete has an unknown status {status!r}")
        events = self._ensure(seq, admission)
        response = self.state.response
        assert response is not None
        text = payload.get("text")
        parts: list[dict[str, Any]] = []
        if not response.streamed and outcome != "failed" and isinstance(text, str) and text:
            events += self._reopen() + self._close_blocks()
            block = self._block_id("text")
            parts += [
                {"type": "text-start", "id": block},
                {"type": "text-delta", "id": block, "delta": text},
                {"type": "text-end", "id": block},
            ]
        error: dict[str, Any] | None = None
        if outcome == "failed":
            # A failed turn's ``text`` is the gateway's own account of the failure
            # (``completion_payload`` in ``gateway/session_settlement_recovery.py``).
            message = str(payload.get("failure_reason") or text or "Hermes failed")
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
        if response.admission is not None:
            self.state.admissions.pop(response.admission, None)
        self.state.response = None
        return events + self._status() + self._report_settled()

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

    def _prompt(self, seq: int, admission: str | None, prompt: dict[str, Any]) -> list[RuntimeEvent]:
        prompt_id = str(prompt.get("prompt_id") or "")
        if not prompt_id or prompt_id in self.state.interactions:
            return []
        events = self._ensure(seq, admission)
        response = self.state.response
        assert response is not None
        record = interaction_record(prompt=prompt, response_id=response.id)
        events += self._reopen()
        events += self._close_blocks()
        events.append(InteractionOpened(None, response.id, prompt_id, record))
        events.append(ResponseOutput(None, response.id, {"type": "data-interaction", "data": dict(record)}))
        events.append(ResponseOutput(None, response.id, _finish("tool-calls")))
        response.closed = True
        self.state.interactions[prompt_id] = response.id
        return events + self._status()

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

    def _text(self, seq: int, admission: str | None, text: str, *, delta: bool) -> list[RuntimeEvent]:
        if not text:
            return []
        events = self._ensure(seq, admission) + self._reopen()
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

    def _reasoning(self, seq: int, admission: str | None, text: str) -> list[RuntimeEvent]:
        if not text:
            return []
        events = self._ensure(seq, admission) + self._reopen()
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

    def _tool_start(self, seq: int, admission: str | None, payload: dict[str, Any]) -> list[RuntimeEvent]:
        events = self._ensure(seq, admission) + self._reopen() + self._close_blocks()
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

    def _tool_complete(self, seq: int, admission: str | None, payload: dict[str, Any]) -> list[RuntimeEvent]:
        events = self._ensure(seq, admission) + self._reopen()
        response = self.state.response
        assert response is not None
        tool_id = str(payload.get("tool_id") or response.tool or "").strip()
        is_error = payload.get("is_error")
        if not isinstance(is_error, bool):
            raise RuntimeProtocolError("Hermes tool.complete omitted its native boolean is_error classification. "
                                       "Align the sandbox image with the core protocol before recovering this conversation.")
        result = payload.get("result")
        if is_error:
            error_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return events + [ResponseOutput(None, response.id, {
                "type": "tool-output-error", "toolCallId": tool_id, "errorText": error_text,
            })]
        output = {key: value for key, value in payload.items() if key not in {"tool_id", "name"}}
        if "result" in output:
            output["result"] = _tool_result(result)
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
    """One reader's connection to one conversation on the profile's Hermes gateway.

    The gateway and this host's connection to it belong to :mod:`.hermes_gateway`
    and are shared with the profile's other conversations; this client owns one
    gateway session on it. :meth:`close` leaves that session running for the
    next reader, and :meth:`dispose` ends it.
    """

    engine_kind = "assistant"

    def __init__(
        self,
        *,
        gateway: HermesGatewayPort,
        platform_session_id: str,
        snapshot: dict[str, Any],
        subscription: Any,
    ) -> None:
        self._gateway = gateway
        self._platform_session_id = str(platform_session_id)
        self._sid = str(snapshot["session_id"])
        self._instance = gateway.instance_id
        self._epoch = str(snapshot["replay_epoch"])
        #: The latest attach snapshot: pending admissions, prompts, server info.
        self._snapshot = snapshot
        self._subscription = subscription
        #: Prompts this client is answering.
        self._answering: set[str] = set()
        self._translation = _Translation(lambda: self._generation, self._answering.__contains__)
        self._inbox: asyncio.Queue[_Item] = asyncio.Queue()
        #: The highest event ``seq`` translated.
        self._last_seq = 0
        #: Inputs this client handed to the gateway, and in-flight submits of them.
        self._submitted: dict[str, asyncio.Future[None]] = {}
        #: Requests already refused: a request can reach this client again.
        self._refused: set[str] = set()
        #: Unknown admissions whose resolution this client already requested.
        self._resolving: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._reading = False
        self._closed = False
        self._forwarder: asyncio.Task[None] | None = None
        self._cron: HermesCronWatch | None = None
        self._cron_reports: list[CronRunReport] = []
        self._cron_unrecorded: dict[str, CronRunReport] = {}

    @property
    def _generation(self) -> str:
        return f"{self._instance}:{self._epoch}"

    # -- connecting --------------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        *,
        gateway: HermesGatewayPort,
        platform_session_id: str,
        resume_session_key: str | None,
        read_cron_run_output: Callable[[str], Awaitable[CronRunOutput | None]] | None = None,
        cron_store: HermesCronJobRepository | None = None,
    ) -> HermesRuntimeClient:
        """Open the conversation's gateway session and start following it.

        A new conversation is created under a request id derived from its
        platform Session, so a create repeated after its reply was lost
        returns the same session. Both create and resume attach this host's
        connection to the session, which a submit and an answer require.
        Conversations share the Assistant profile's native environment.
        """

        if not gateway.is_live:
            raise EngineStreamDetached("Hermes gateway is not connected for this profile")
        key = str(resume_session_key or "").strip()
        subscription = gateway.subscribe(key) if key else None
        try:
            if key:
                snapshot = await gateway.request("session.resume", {"session_id": key})
            else:
                snapshot = await gateway.request("session.create", {
                    "request_id": SESSION_REQUEST_PREFIX + platform_session_id,
                    "source": SESSION_SOURCE,
                })
            _snapshot(snapshot)
        except BaseException:
            if subscription is not None:
                gateway.unsubscribe(subscription)
            raise
        if subscription is None or subscription.session_id != snapshot["session_id"]:
            # A created session's id is the gateway's; frames sent before this
            # subscription existed are read back from its ring by events().
            if subscription is not None:
                gateway.unsubscribe(subscription)
            subscription = gateway.subscribe(snapshot["session_id"])
        client = cls(
            gateway=gateway,
            platform_session_id=platform_session_id,
            snapshot=snapshot,
            subscription=subscription,
        )
        client._forwarder = asyncio.create_task(client._forward(), name=f"hermes-session-{client._sid}")
        if read_cron_run_output is not None:
            client._start_cron(cron_store or HermesCronJobRepository(), read_cron_run_output)
        return client

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
                wire: HermesGatewayFrame = await self._subscription.next_event()
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
        return self._sid

    async def submit(self, item: RuntimeInput) -> None:
        if self._closed:
            raise EngineStreamDetached("Hermes runtime client is closed")
        sent = self._submitted.get(item.input_id)
        if sent is not None:
            await sent
            return
        if any(block.get("type") != "text" for block in item.content_blocks):
            raise RuntimeInputRefused(code="ENGINE_CAPABILITY_UNAVAILABLE", message="Hermes takes text messages only")
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._submitted[item.input_id] = done
        try:
            receipt = await self._gateway.request("prompt.submit", {
                "session_id": self._sid,
                "text": item.content,
                "input_id": INPUT_REQUEST_PREFIX + item.input_id,
            })
        except HermesGatewayRpcError as exc:
            self._submitted.pop(item.input_id, None)
            done.set_result(None)
            if exc.reason == "admission_conflict":
                raise RuntimeInputRefused(
                    code="HERMES_INPUT_REFUSED",
                    message="Hermes already holds a different message under this input id",
                ) from exc
            raise
        except BaseException:
            self._submitted.pop(item.input_id, None)
            done.set_result(None)
            raise
        done.set_result(None)
        if receipt.get("voice_stopped") is True:
            raise RuntimeInputRefused(
                code="HERMES_INPUT_REFUSED",
                message="Hermes took this message as the stop phrase of its voice mode",
            )
        if receipt.get("status") not in {"queued", "started", "unknown", "terminal"} or not isinstance(
            receipt.get("admission_id"), str
        ):
            raise RuntimeProtocolError(f"Hermes answered prompt.submit with no admission receipt: {receipt!r}")
        settled = None
        if receipt["status"] == "terminal":
            settled = await self._gateway.request("prompt.receipt", {
                "session_id": self._sid, "admission_id": receipt["admission_id"], "include_result": True,
            })
        self._inbox.put_nowait(("receipt", (item.input_id, receipt, settled)))

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        # A snapshot read and the event subscription share one connection.
        # Either can observe its loss; both end this stream so the reader
        # reattaches from its last committed cursor.
        try:
            async for event in self._events(after=after):
                yield event
        except EngineStreamDetached as exc:
            logger.info("Hermes session %s stream ended: %s", self._sid, exc)

    async def _events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
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
            "session.events.since",
            {"session_id": self._sid, "last_sequence": last_seen, "replay_epoch": self._epoch},
        )
        replayed = [item for item in reply.get("events") or [] if isinstance(item, dict)]
        if (
            reply.get("snapshot_required") is not False
            or reply.get("replay_epoch") != self._epoch
            or (skip is not None and (not replayed or replayed[0].get("seq") != last_seen + 1))
        ):
            # The state of an expired cursor is not this session's any more:
            # what happened in the gap is unknown, so ``state`` starts afresh.
            raise RuntimeCursorExpired(f"Hermes holds none of the events of session {self._sid} after {last_seen}")
        if restored is not None:
            self._translation.state = restored
        self._reading = True
        self._last_seq = last_seen
        # What the gateway held when this client attached — the admissions its
        # ring may not name, and any turn a restart lost — read at the
        # snapshot's place in the stream, unless the cursor is already past it.
        attached_at = int(self._snapshot["last_sequence"])
        attached = last_seen > attached_at
        for index, params in enumerate(replayed):
            seq = params.get("seq")
            if not attached and type(seq) is int and seq > attached_at:
                attached = True
                for event in self._attached_snapshot():
                    yield event
            for event in await self._ring(params, skip=skip if index == 0 else None):
                yield event
        if not attached:
            for event in self._attached_snapshot():
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
                    await self._refuse(str(item.event.get("id") or ""), str(item.event.get("method") or ""))
                    continue
                if item.session_id != self._sid:
                    await self._broadcast(item.event)
                    continue
                if str(item.event.get("type") or "") == "session.replay_gap":
                    # The gateway retired this connection's subscription: frames
                    # after the gap never reached it. The next reader reattaches
                    # and replays from its cursor.
                    logger.info("Hermes session %s subscription overflowed; ending the stream", self._sid)
                    return
                epoch = item.event.get("replay_epoch")
                if isinstance(epoch, str) and epoch and epoch != self._epoch:
                    # The gateway renumbered the session (its shared replay cache
                    # dropped the ring): this live stream continues unbroken, in
                    # the new numbering.
                    self._epoch, self._last_seq = epoch, 0
                batch = await self._ring(item.event, skip=None)
            elif kind == "receipt":
                input_id, receipt, settled = item
                batch = self._synthetic(lambda: self._translation.receipt(input_id, receipt, settled))
            elif kind == "answered":
                batch = self._synthetic(lambda: self._translation.closed(item, "answered"))
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
        pre = self._translation.state.to_json()
        events = self._translation.event(params, seq)
        post = self._translation.state.to_json()
        self._last_seq = seq
        if self._cron is not None:
            await self._cron.observe(params)
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

    def _attached_snapshot(self) -> list[RuntimeEvent]:
        snapshot = self._snapshot
        return self._synthetic(
            lambda: self._translation.pending(snapshot.get("pending"), running=snapshot.get("running"))
        )

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

    async def _refuse(self, request_id: str, method: str) -> None:
        """Answer a request this host does not implement (a password or secret prompt)."""

        if not request_id or request_id in self._refused:
            return
        self._refused.add(request_id)
        with contextlib.suppress(Exception):
            await self._gateway.respond_error(
                request_id,
                code=_METHOD_NOT_FOUND,
                message=f"AstraBox does not answer {method!r} requests",
            )

    async def _broadcast(self, event: dict[str, Any]) -> None:
        """A process-wide event, such as ``cron.changed``: no ``seq``, nothing to translate."""

        if self._cron is not None:
            await self._cron.observe(event)

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

    async def _attach(self) -> dict[str, Any]:
        """Re-attach this connection to the session and return the gateway's snapshot of it."""

        snapshot = _snapshot(await self._gateway.request("session.resume", {"session_id": self._sid}))
        self._snapshot = snapshot
        return snapshot

    async def state(self) -> RuntimeState:
        snapshot = await self._attach()
        # Reconciling starts the session's numbering from the snapshot's.
        self._epoch = str(snapshot["replay_epoch"])
        current = self._translation.state
        rebuilt = _State(cron=list(current.cron), unknown=dict(current.unknown),
                         settled=[dict(item) for item in current.settled], running=snapshot.get("running") is True)
        rows = _pending_rows(snapshot["pending"])
        for row in rows:
            input_id = platform_input_id(row.get("input_id"))
            if input_id is not None and isinstance(row.get("admission_id"), str):
                rebuilt.admissions[row["admission_id"]] = input_id
        running = next((row for row in rows if row.get("status") == "started"), None)
        open_responses: list[OpenResponse] = []
        if running is not None:
            admission = str(running.get("admission_id") or "")
            if current.response is not None and current.response.admission == admission:
                rebuilt.response = current.response
            else:
                input_id = rebuilt.admissions.get(admission)
                rebuilt.response = _Response(
                    id=f"hermes:{admission}",
                    origin="input" if input_id else "engine",
                    inputs=[input_id] if input_id else [],
                    admission=admission,
                )
            response = rebuilt.response
            open_responses.append(OpenResponse(response.id, response.origin, tuple(response.inputs)))
        for row in rows:
            admission = row.get("admission_id")
            if row.get("status") == "unknown" and isinstance(admission, str) and admission not in rebuilt.unknown:
                # Ended, with the reason, by the events that follow this snapshot.
                input_id = rebuilt.admissions.get(admission)
                open_responses.append(OpenResponse(
                    f"hermes:{admission}",
                    "input" if input_id else "engine",
                    (input_id,) if input_id else (),
                ))
        pending: list[PendingInteraction] = []
        if rebuilt.response is not None:
            for prompt in _pending_rows(snapshot["prompts"]):
                if prompt.get("kind") not in {"approval", "clarify"} or not prompt.get("prompt_id"):
                    continue
                record = interaction_record(prompt=prompt, response_id=rebuilt.response.id)
                pending.append(PendingInteraction(rebuilt.response.id, str(prompt["prompt_id"]), record))
                rebuilt.interactions[str(prompt["prompt_id"])] = rebuilt.response.id
        status: RuntimeStatus = "waiting" if pending else ("working" if rebuilt.running else "idle")
        rebuilt.status = status
        self._translation.state = rebuilt
        # No admitted inputs are reported here: custody closes on the gateway's
        # receipt to a submit, or on a turn start, so an input the platform
        # still holds is handed over again and answered with its admission.
        return RuntimeState(
            status=status,
            open_responses=tuple(open_responses),
            pending_interactions=tuple(pending),
            queued_input_ids=tuple(
                rebuilt.admissions[str(row["admission_id"])]
                for row in rows
                if row.get("status") == "queued" and str(row.get("admission_id") or "") in rebuilt.admissions
            ),
            cursor=RuntimeCursor(
                self._generation, {"seq": int(snapshot["last_sequence"]), "state": rebuilt.to_json()}
            ),
        )

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        # Hermes accepts an answer only from a connection attached to the session.
        snapshot = await self._attach()
        prompt = next(
            (
                item for item in _pending_rows(snapshot["prompts"])
                if item.get("prompt_id") == interaction_id and item.get("kind") in {"approval", "clarify"}
            ),
            None,
        )
        if prompt is None or type(prompt.get("execution_generation")) is not int:
            raise InteractionNotOpen(interaction_id)
        record = interaction_record(prompt=prompt, response_id="")
        method = "approval.respond" if prompt["kind"] == "approval" else "clarify.respond"
        answer = answer_result(record, response)
        self._answering.add(interaction_id)
        try:
            reply = await self._gateway.request(method, {
                "session_id": self._sid,
                "execution_generation": prompt["execution_generation"],
                "prompt_id": interaction_id,
                **answer,
            })
        except HermesGatewayRpcError as exc:
            self._answering.discard(interaction_id)
            if exc.reason in {"stale_generation", "not_found"}:
                raise InteractionNotOpen(interaction_id) from exc
            raise
        except BaseException:
            self._answering.discard(interaction_id)
            raise
        if reply.get("status") != "resolved":
            self._answering.discard(interaction_id)
            raise InteractionNotOpen(interaction_id)
        self._inbox.put_nowait(("answered", interaction_id))

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        """Stop the running turn; the inputs queued behind it stay queued.

        This is Hermes' own Stop (``interrupt`` in
        ``gateway/session_authority.py``): it stops the turn of the execution
        generation it is told, read here from the session's snapshot, and ends
        the session's background delegations. The session's queued admissions
        stay in its FIFO and run after the stopped turn settles.
        """

        if cancel_queued:
            raise NotImplementedError("the runtime contract reports no input a runtime discards")
        snapshot = await self._attach()
        try:
            await self._gateway.request(
                "session.interrupt",
                {"session_id": self._sid, "execution_generation": snapshot["execution_generation"]},
            )
        except HermesGatewayRpcError as exc:
            # The turn that snapshot named ended before the request reached Hermes.
            if exc.reason != "stale_generation":
                raise

    async def committed(self, cursor: RuntimeCursor) -> None:
        if cursor.generation != self._generation:
            return
        state = cursor.position.get("state") or {}
        recorded = set(state.get("cron") or [])
        for response_id in [key for key in self._cron_unrecorded if key in recorded]:
            report = self._cron_unrecorded.pop(response_id)
            if self._cron is not None:
                await self._cron.delivered(report)
        for admission, generation in (state.get("unknown") or {}).items():
            if admission not in self._resolving:
                # The platform recorded the lost turn's failure; the session's
                # FIFO behind it may run now. Off the commit, which awaits this.
                self._resolving.add(admission)
                task = asyncio.create_task(
                    self._resolve_unknown(str(admission), int(generation)),
                    name=f"hermes-resolve-unknown-{admission}",
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _resolve_unknown(self, admission: str, generation: int) -> None:
        try:
            await self._gateway.request("prompt.resolve_unknown", {
                "session_id": self._sid, "admission_id": admission, "execution_generation": generation,
            })
        except (HermesGatewayRpcError, EngineStreamDetached) as exc:
            # Another viewer resolved it first, or the connection ended; the
            # next reader's snapshot lists it again if it is still unknown.
            self._resolving.discard(admission)
            logger.info(
                "Hermes unknown admission not resolved: session=%s admission=%s error=%s",
                self._platform_session_id, admission, exc,
            )

    async def capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind="assistant",
            input_content_types=["text"],
            permission_modes=[],
            supports_interaction=True,
            supports_child_run_control=True,
            supports_server_info=True,
            extra={"transport": "hermes_gateway_v1_websocket_through_the_image_bridge"},
        )

    async def stop_child_run(self, control_id: str) -> None:
        await self._gateway.request("subagent.interrupt", {"session_id": self._sid, "subagent_id": str(control_id)})

    async def get_server_info(self) -> dict[str, Any] | None:
        info = self._snapshot.get("info")
        return dict(info) if isinstance(info, dict) else None

    async def close(self) -> None:
        """Stop following the session. Its turns keep running in the gateway.

        The session's subscription belongs to this host's connection, which
        sibling clients of the profile share: it is detached once no client of
        this connection follows the session, so the gateway stops sending it
        here and a later end of the conversation is not held open by it.
        """

        if self._closed:
            return
        self._closed = True
        for task in (self._forwarder, *self._tasks):
            if task is not None and not task.done():
                task.cancel()
        if self._cron is not None:
            await self._cron.stop()
        self._gateway.unsubscribe(self._subscription)
        if self._gateway.is_live and not self._gateway.subscribed(self._sid):
            with contextlib.suppress(Exception):
                await self._gateway.request("session.detach", {
                    "session_id": self._sid, "subscription_id": self._snapshot["subscription_id"],
                })
        self._inbox.put_nowait(("end", EngineStreamDetached("Hermes runtime client closed")))

    async def dispose(self) -> bool:
        """End this conversation's gateway session; the gateway keeps serving its siblings."""

        ended = False
        if self._gateway.is_live:
            try:
                ended = await end_native_session(self._gateway, self._sid)
            except Exception as exc:  # noqa: BLE001 — the session ends with the gateway
                logger.warning(
                    "Hermes session end failed on dispose: platform_session=%s session=%s err=%s",
                    self._platform_session_id,
                    self._sid,
                    exc,
                )
        await self.close()
        return ended


async def end_native_session(
    gateway: HermesGatewayPort,
    session_id: str,
    *,
    budget_seconds: float = _END_BUDGET_SECONDS,
    poll_seconds: float = _END_POLL_SECONDS,
) -> bool:
    """End one conversation's gateway session, leaving its siblings running.

    Hermes ends a local session when its last viewer detaches with an
    ``end_reason``, once the session's FIFO is idle
    (``gateway/session_viewer_close.py``). So the session's queued admissions
    are cancelled, an admission a restart left ``unknown`` is resolved, the
    running turn is interrupted, and this connection detaches with
    :data:`END_REASON`; the session then leaves ``session.active_list``, which
    this waits for. Returns ``False`` when the gateway holds no such session.
    """

    try:
        snapshot = _snapshot(await gateway.request("session.resume", {"session_id": session_id}))
    except HermesGatewayRpcError as exc:
        if exc.reason == "not_found":
            return False
        raise
    for row in _pending_rows(snapshot["pending"]):
        admission = row.get("admission_id")
        if row.get("status") == "queued" and isinstance(admission, str):
            await gateway.request("prompt.cancel", {"session_id": session_id, "admission_id": admission})
        elif row.get("status") == "unknown" and isinstance(admission, str):
            await gateway.request("prompt.resolve_unknown", {
                "session_id": session_id, "admission_id": admission,
                "execution_generation": row.get("execution_generation"),
            })
    if snapshot.get("running") is True:
        try:
            await gateway.request(
                "session.interrupt",
                {"session_id": session_id, "execution_generation": snapshot["execution_generation"]},
            )
        except HermesGatewayRpcError as exc:
            if exc.reason != "stale_generation":
                raise
    detached = await gateway.request("session.detach", {
        "session_id": session_id, "subscription_id": snapshot["subscription_id"], "end_reason": END_REASON,
    })
    if detached.get("detached") is not True:
        raise RuntimeError(f"Hermes did not detach this connection from session {session_id}")
    deadline = time.monotonic() + budget_seconds
    while True:
        listed = await gateway.request("session.active_list", {})
        row = next(
            (item for item in listed.get("sessions") or [] if isinstance(item, dict) and item.get("id") == session_id),
            None,
        )
        if row is None:
            return True
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Hermes still lists session {session_id} as {row.get('status')!r} {budget_seconds:.0f}s "
                "after its last viewer ended it; another viewer may still be attached"
            )
        await asyncio.sleep(poll_seconds)


__all__ = [
    "END_REASON",
    "HermesGatewayPort",
    "HermesRuntimeClient",
    "INPUT_REQUEST_PREFIX",
    "SESSION_REQUEST_PREFIX",
    "answer_result",
    "end_native_session",
    "interaction_contract",
    "interaction_record",
    "platform_input_id",
]
