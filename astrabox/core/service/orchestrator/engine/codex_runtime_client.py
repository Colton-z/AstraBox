"""Codex behind the runtime contract (``engine/runtime.py``).

The box runs ``codex app-server``. This client holds one JSON-RPC link to it
(:class:`CodexAppServerLink`) and drives one Codex **thread**, the vendor's
conversation; the thread id is the engine session key a replacement box
resumes.

* **A response is a Codex turn.** Its id is the turn id. It starts at the
  turn's first item: a ``userMessage`` carrying a ``clientId`` makes it the
  answer to that platform input (``origin="input"``); any other first item,
  or none for a turn that ends empty, means Codex started the turn itself
  (``origin="engine"``: a queued goal round, a sub-agent's mail). It ends at
  ``turn/completed``, whose ``status`` is the native reason: ``completed`` is
  read as completed, ``interrupted`` as cancelled, ``failed`` as failed with
  the vendor's ``TurnError``.
* **An input is admitted at its ``userMessage`` item.** Codex appends an
  event to the thread's canonical history, the rollout file the platform
  mirrors out of the box, before it delivers the event to a client
  (``send_event_raw_with_persistence`` in codex-rs core: "canonical append,
  and delivery in the same order"). The item's ``clientId`` is the
  ``clientUserMessageId`` this client sent, which is the platform input id.
  The answer to ``turn/start`` is not admission: Codex discards input steered
  into a turn that is interrupted before it drains it. ``thread/queue/add`` is
  not admission either: the queue lives in the box's SQLite, which a
  replacement box does not receive.
* **Submission follows the thread's turns.** With no turn in progress an input
  starts one (``turn/start``). While a turn runs it joins Codex's durable
  user-message queue (``thread/queue/add``), which Codex starts as its own
  turn when the thread next goes idle; after an interrupt Codex holds the
  queue until a turn completes normally. Codex keeps no id-based dedup, so a
  second submission of an input found in the thread's history, its queue or
  this client's own submissions is a no-op.
* **Status is Codex's.** ``thread/status/changed`` reports ``active`` with a
  waiting flag as ``waiting``, ``active`` as ``working``, anything else as
  ``idle``.
* **Interactions are Codex's server requests**: command and file-change
  approvals, permission grants, MCP elicitations and questions. Codex re-sends
  a pending request, with its original id, to a connection that resumes the
  thread, and announces with ``serverRequest/resolved`` that it has stopped
  waiting on one. The interaction id is ``codex:{turnId}:{id}``:
  request ids restart with the app-server process, turn ids do not.

**The cursor.** Codex keeps no notification journal, so a cursor cannot name a
place in the notification stream. It names a place in the thread's persisted
record instead. ``generation`` is the thread id. ``position`` names the turn
whose response started last and whether it ended; for a response still open
it also holds what was shown of it: its opening and ending parts, the items
shown to their end, the prose blocks still open with the characters each has
shown, the tool parts shown of items still running, and whether its browser
message is closed at an interaction. It also holds the status last reported,
since Codex announces only changes of it. ``events(after=)`` first reads the
thread back from Codex's paginated history (``thread/turns/list``,
``thread/items/list``), which Codex projects from the rollout as it appends
it, and translates everything after the position; then it follows the live
notifications. The persisted record is append-only and in the order Codex
delivered it, and its translation depends only on that record and the
position, so the events after a cursor are the same each time they are read.

Deltas are not persisted: a block that was streaming when the previous reader
stopped is finished with the completed item's text after the characters
already shown. Two kinds of event are read live rather than from that record:
interactions, which Codex re-sends to a resuming connection and whose
``data-interaction`` part carries the interaction id so a second showing
updates the first, and sub-agent facts, session output read from each child's
own thread.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineStreamDetached,
)
from astrabox.core.service.orchestrator.engine.codex_child_runs import (
    COLLAB_ITEM_TYPE,
    CodexChildResources,
)
from astrabox.core.service.orchestrator.engine.codex_events import (
    DELTA_FAMILIES,
    INTERACTION_METHODS,
    QUESTION_METHOD,
    CodexProtocolError,
    CodexTurnTranslator,
    approval_response_value,
    build_approval_contract,
    build_question_contract,
    question_response_value,
)
from astrabox.core.service.orchestrator.engine.codex_link import (
    CodexAppServerLink,
    CodexRpcError,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    InteractionContractError,
    validate_interaction_contract,
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
    RuntimeState,
    RuntimeStatus,
    StatusChanged,
)

logger = get_logger(__name__)

ENGINE_KIND = "codex"

#: The marketplace Codex knows the platform's plugins by.
CODEX_PLUGIN_MARKETPLACE = "astrabox"

#: Codex's own `SandboxMode`, verbatim. These are the names the CLI's `-s`
#: flag takes and the strings `thread/start` accepts, so the console offers
#: the engine's vocabulary rather than a set invented here.
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

#: The `SandboxPolicy` Codex itself derives from each mode. `thread/start`
#: takes the mode by name; `thread/settings/update` takes the structured
#: policy, so a switch after the thread exists needs the expansion. Every entry
#: was read off a running server's `thread/start` response for that mode
#: rather than assembled from the type definition.
CODEX_SANDBOX_POLICIES: dict[str, dict[str, Any]] = {
    "read-only": {"type": "readOnly", "networkAccess": False},
    "workspace-write": {
        "type": "workspaceWrite",
        "writableRoots": [],
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    },
    "danger-full-access": {"type": "dangerFullAccess"},
}

#: The app-server's largest page for `thread/turns/list` and `thread/items/list`.
_HISTORY_PAGE = 100

#: `TurnStatus` at `turn/completed` → this adapter's reading of it, and the AI
#: SDK finish reason the response's browser message ends with. `inProgress` is
#: absent: on a completion it would mean the vendor changed what that
#: notification means.
_TURN_ENDINGS: dict[str, tuple[ResponseOutcome, str]] = {
    "completed": ("completed", "stop"),
    "interrupted": ("cancelled", "other"),
    "failed": ("failed", "error"),
}

#: `ThreadActiveFlag` values that mean the turn waits on a person.
_WAITING_FLAGS = frozenset({"waitingOnApproval", "waitingOnUserInput"})

#: A child thread's notifications that change what the console shows of it.
_CHILD_METHODS = frozenset(
    {"thread/status/changed", "turn/started", "item/started", "item/completed", "turn/completed"}
)


def interaction_id_for(request: dict[str, Any]) -> str:
    """The platform id of one Codex server request.

    Codex numbers requests per app-server process and re-sends a pending one
    with the same number to a resuming connection; the turn the request
    belongs to keeps the id unique across processes.
    """

    params = request.get("params")
    params = params if isinstance(params, dict) else {}
    scope = str(params.get("turnId") or params.get("threadId") or "").strip()
    if not scope or request.get("id") is None:
        raise CodexProtocolError(f"codex server request names no turn or thread: {request!r}")
    return f"codex:{scope}:{request['id']}"


def interaction_record(request: dict[str, Any], response_id: str) -> dict[str, Any]:
    """The platform interaction record for one Codex server request.

    Built only from the request, so a request Codex re-sends builds the same
    record.
    """

    method = str(request.get("method") or "")
    params = request.get("params")
    params = params if isinstance(params, dict) else {}
    contract = (
        build_question_contract(params)
        if method == QUESTION_METHOD
        else build_approval_contract(method, params)
    )
    tool_call_id = str(contract.pop("tool_use_id", "") or "").strip()
    validate_interaction_contract(contract)
    record: dict[str, Any] = {
        "interaction_id": interaction_id_for(request),
        "response_id": response_id,
        **contract,
    }
    if tool_call_id:
        record["tool_call_id"] = tool_call_id
    return record


def runtime_status(status: Any) -> tuple[RuntimeStatus, bool]:
    """A `ThreadStatus` as the runtime contract's status, and whether a turn runs."""

    if not isinstance(status, dict):
        raise CodexProtocolError(f"codex thread status is not an object: {status!r}")
    kind = str(status.get("type") or "")
    if kind == "active":
        flags = status.get("activeFlags") or []
        return ("waiting" if _WAITING_FLAGS.intersection(flags) else "working"), True
    if kind in {"idle", "notLoaded", "systemError"}:
        return "idle", False
    raise CodexProtocolError(f"codex thread status has unknown type {kind!r}")


def _origin(item: dict[str, Any]) -> tuple[ResponseOrigin, tuple[str, ...]]:
    if str(item.get("type") or "") == "userMessage":
        client_id = str(item.get("clientId") or "").strip()
        if client_id:
            return "input", (client_id,)
    return "engine", ()


def _turn_of(params: dict[str, Any]) -> dict[str, Any]:
    turn = params.get("turn")
    if not isinstance(turn, dict) or not str(turn.get("id") or "").strip():
        raise CodexProtocolError("codex turn notification carries no turn")
    return turn


class _Stream:
    """One thread's messages → runtime events, holding the position they leave.

    Every method appends ``(event, position)`` pairs. A position is the cursor
    state after its event, built only from what was emitted, so a reader that
    commits after any event resumes exactly after it.
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.translator = CodexTurnTranslator()
        #: The open response, which is a turn id.
        self.response: str | None = None
        #: The turn whose response started last, and whether it ended.
        self.turn: str | None = None
        self.ended = True
        #: The open response's opening (who it answers, how many of its
        #: opening parts are shown), the items shown to their end, how many of
        #: its ending parts are shown, and whether its browser message is
        #: closed at an interaction.
        self.origin: ResponseOrigin = "engine"
        self.input_ids: tuple[str, ...] = ()
        self.opened = 0
        self.done: list[str] = []
        self._done: set[str] = set()
        self.ending = 0
        self.closed = False
        #: The status last reported; Codex announces only its changes.
        self.status: RuntimeStatus | None = None
        #: A turn Codex started whose first item has not arrived yet.
        self.pending_turn: str | None = None
        #: Platform inputs Codex holds in this thread's history.
        self.admitted: set[str] = set()
        #: Open interactions by id, and the interaction each request id opened.
        self.interactions: dict[str, dict[str, Any]] = {}
        self._requests: dict[Any, str] = {}
        self.answered: set[str] = set()
        #: Turns this client started with `turn/start`, and turns it interrupted.
        self.submitted_turns: set[str] = set()
        self.interrupted_turns: set[str] = set()
        self._started: set[str] = set()
        self._ended: set[str] = set()
        self._out: list[tuple[RuntimeEvent, dict[str, Any]]] = []

    # -- position ------------------------------------------------------------

    def position(self) -> dict[str, Any]:
        position: dict[str, Any] = {"turn": self.turn, "ended": self.ended, "status": self.status}
        if self.response is not None:
            position.update(
                origin=self.origin,
                input_ids=list(self.input_ids),
                opened=self.opened,
                done=list(self.done),
                ending=self.ending,
                closed=self.closed,
                **self.translator.state(),
            )
        return position

    def restore(self, position: dict[str, Any]) -> None:
        status = position.get("status")
        self.status = cast(RuntimeStatus, status) if status in {"idle", "working", "waiting"} else None
        turn = str(position.get("turn") or "").strip() or None
        self.turn = turn
        self.ended = bool(position.get("ended", True))
        if turn is None:
            return
        self._started.add(turn)
        if self.ended:
            self._ended.add(turn)
            return
        self.response = turn
        self.origin = "input" if position.get("origin") == "input" else "engine"
        self.input_ids = tuple(str(input_id) for input_id in position.get("input_ids") or ())
        self.opened = int(position.get("opened") or 0)
        self.done = [str(item_id) for item_id in position.get("done") or ()]
        self._done = set(self.done)
        self.ending = int(position.get("ending") or 0)
        self.closed = bool(position.get("closed", False))
        self.translator.restore(position)

    def drain(self) -> list[tuple[RuntimeEvent, dict[str, Any]]]:
        out, self._out = self._out, []
        return out

    def busy(self) -> bool:
        """Whether a turn is in progress as far as this client knows."""

        running = {self.response, self.pending_turn, *self.submitted_turns} - {None}
        return bool(running - self._ended - self.interrupted_turns)

    def open_turn(self) -> str | None:
        """The turn an interrupt stops: the open response's, or one about to produce output."""

        for turn in (self.response, self.pending_turn):
            if turn is not None and turn not in self._ended:
                return turn
        live = sorted(self.submitted_turns - self._ended)
        return live[-1] if live else None

    # -- notifications -------------------------------------------------------

    def notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "turn/started":
            self._turn_started(_turn_of(params))
        elif method == "turn/completed":
            self.turn_completed(_turn_of(params))
        elif method in {"item/started", "item/completed"}:
            self.item(method, params)
        elif method in DELTA_FAMILIES:
            self._delta(message, params)
        elif method == "thread/status/changed":
            self.set_status(params.get("status"))
        elif method == "serverRequest/resolved":
            self._resolved(params.get("requestId"))
        else:
            for part in self.translator.translate(message):
                self._diagnostic_or_fail(part)

    def set_status(self, status: Any) -> None:
        mapped, _running = runtime_status(status)
        if str((status or {}).get("type") or "") == "systemError":
            self._diagnostic("codex.thread_system_error", {"status": status})
        if mapped != self.status:
            self.status = mapped
            self._emit(StatusChanged(None, mapped))

    def _turn_started(self, turn: dict[str, Any]) -> None:
        turn_id = str(turn["id"])
        if turn_id in self._started or turn_id in self._ended or turn_id == self.pending_turn:
            return
        if self.response is not None:
            raise CodexProtocolError(f"codex started turn {turn_id} while turn {self.response} is open")
        self.pending_turn = turn_id

    def turn_completed(self, turn: dict[str, Any]) -> None:
        turn_id = str(turn["id"])
        if turn_id in self._ended:
            return
        status = str(turn.get("status") or "").strip()
        ending = _TURN_ENDINGS.get(status)
        if ending is None:
            raise CodexProtocolError(f"turn/completed carries unknown status {status!r}")
        if self.response != turn_id:
            if self.response is not None:
                raise CodexProtocolError(f"codex completed turn {turn_id} while turn {self.response} is open")
            # A turn that produced no item: Codex ran it and nothing answered an input.
            self._start(turn_id, "engine", ())
        outcome, finish_reason = ending
        error = turn.get("error") if isinstance(turn.get("error"), dict) else None
        parts: list[dict[str, Any]] = []
        if outcome == "failed":
            parts.append({"type": "error", "errorText": str((error or {}).get("message") or status)})
        parts.append({"type": "finish", "finishReason": finish_reason})
        for part in parts[self.ending:]:
            self._output(part, shown=self._ending_shown)
        self.response = None
        self.ended = True
        self._ended.add(turn_id)
        self.translator = CodexTurnTranslator()
        # Codex aborts a turn's pending requests with the turn.
        for interaction_id, record in list(self.interactions.items()):
            if record["response_id"] == turn_id:
                del self.interactions[interaction_id]
        self._requests = {
            request_id: interaction_id
            for request_id, interaction_id in self._requests.items()
            if interaction_id in self.interactions
        }
        self._emit(ResponseEnded(None, turn_id, outcome, status, None, error))

    def item(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            raise CodexProtocolError(f"{method} carries no item object")
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise CodexProtocolError(f"{method} for {item.get('type')!r} has no id")
        if item_id in self._done:
            return
        if not self._ensure_response(str(params.get("turnId") or ""), item):
            return
        completed = method == "item/completed"
        if str(item.get("type") or "") == "userMessage":
            if completed:
                self._item_done(item_id)
                client_id = str(item.get("clientId") or "").strip()
                if client_id:
                    self.admitted.add(client_id)
                    self._emit(InputAdmitted(None, client_id))
            return
        if completed:
            parts = self.translator.item_completed(item)
        else:
            parts = self.translator.translate({"method": method, "params": params})
        for index, part in enumerate(parts):
            if completed and index == len(parts) - 1:
                self._show(part, done=item_id)
            else:
                self._show(part)
        if completed and not parts:
            self._item_done(item_id)

    def _delta(self, message: dict[str, Any], params: dict[str, Any]) -> None:
        if str(params.get("itemId") or "") in self._done:
            return
        if self.response is None or str(params.get("turnId") or "") != self.response:
            # The item's start was not shown here; its completion shows it whole.
            return
        for part in self.translator.translate(message):
            self._show(part)

    # -- server requests -----------------------------------------------------

    def request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        if method not in INTERACTION_METHODS:
            self._diagnostic(f"codex.server_request.{method}", message)
            return
        interaction_id = interaction_id_for(message)
        if interaction_id in self.interactions:
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        turn_id = str(params.get("turnId") or "")
        own_thread = str(params.get("threadId") or "") == self.thread_id
        if (
            own_thread
            and turn_id
            and self.response is None
            and self.pending_turn in (None, turn_id)
            and turn_id not in self._ended
        ):
            # The turn asks before any item of it arrived: it is Codex's own.
            self._start(turn_id, "engine", ())
        response_id = self.response
        if response_id is None or (own_thread and turn_id and turn_id != response_id):
            # A sub-agent's request belongs to the parent turn that waits on
            # it; a request outside any open response has nothing to pause.
            self._diagnostic("codex.request_outside_response", message)
            return
        try:
            record = interaction_record(message, response_id)
        except (CodexProtocolError, InteractionContractError) as exc:
            # Raising would end the stream, and Codex re-sends the request to
            # the next reader. It stays pending until the turn is interrupted.
            logger.warning("codex request %s cannot be presented: %s", interaction_id, exc)
            self._diagnostic("codex.request_unpresentable", {"error": str(exc), "request": message})
            return
        self.interactions[interaction_id] = record
        self._requests[message.get("id")] = interaction_id
        self._emit(InteractionOpened(None, response_id, interaction_id, record))
        if self.closed:
            # A request while another still waits arrives in a message the
            # other one closed.
            self._emit(ResponseOutput(None, response_id, {"type": "start", "messageId": response_id}))
        # The id makes the part one part per interaction: a request Codex
        # re-sends to a resuming reader updates it rather than adding another.
        self._emit(
            ResponseOutput(None, response_id, {"type": "data-interaction", "id": interaction_id, "data": dict(record)})
        )
        self.closed = True
        self._emit(ResponseOutput(None, response_id, {"type": "finish", "finishReason": "tool-calls"}))

    def _resolved(self, request_id: Any) -> None:
        interaction_id = self._requests.pop(request_id, None)
        if interaction_id is None:
            return
        self.interactions.pop(interaction_id, None)
        if interaction_id in self.answered:
            # The reader records the answer itself once Codex has it.
            self.answered.discard(interaction_id)
            return
        self._emit(InteractionClosed(None, interaction_id, "withdrawn"))

    # -- the persisted record ------------------------------------------------

    def finish_opening(self) -> None:
        """Show the opening parts of a restored response that an earlier reader did not."""

        if self.response is not None:
            self._open_message()

    def persisted_item(self, turn_id: str, item: dict[str, Any]) -> None:
        self.item("item/completed", {"threadId": self.thread_id, "turnId": turn_id, "item": item})

    def persisted_turn(self, turn: dict[str, Any]) -> None:
        turn_id = str(turn["id"])
        if str(turn.get("status") or "") == "inProgress":
            if self.response is None and turn_id not in self._started:
                self.pending_turn = turn_id
            return
        self.turn_completed(turn)

    # -- output --------------------------------------------------------------

    def child_frames(self, frames: list[dict[str, Any]]) -> None:
        # A child run outlives the parent turn that spawned it, so its facts
        # are session output and belong to no response.
        for frame in frames:
            self._emit(ResponseOutput(None, None, frame))

    def _ensure_response(self, turn_id: str, item: dict[str, Any]) -> bool:
        if self.response is not None:
            if turn_id == self.response:
                return True
            self._diagnostic("codex.item_outside_open_response", {"turnId": turn_id, "item": item})
            return False
        if turn_id in self._ended:
            # A background terminal finishing under the turn that started it.
            self._diagnostic("codex.item_after_turn_end", {"turnId": turn_id, "item": item})
            return False
        if self.pending_turn not in (None, turn_id):
            self._diagnostic("codex.item_outside_pending_turn", {"turnId": turn_id, "item": item})
            return False
        origin, input_ids = _origin(item)
        self._start(turn_id, origin, input_ids)
        return True

    def _start(self, turn_id: str, origin: ResponseOrigin, input_ids: tuple[str, ...]) -> None:
        if not turn_id:
            raise CodexProtocolError("a codex response needs its turn id")
        self.response = turn_id
        self.turn = turn_id
        self.ended = False
        self.origin = origin
        self.input_ids = input_ids
        self.opened = 0
        self.done = []
        self._done = set()
        self.ending = 0
        self.closed = False
        self.pending_turn = None
        self._started.add(turn_id)
        self.translator = CodexTurnTranslator()
        self._emit(ResponseStarted(None, turn_id, origin, input_ids))
        self._open_message()

    def _open_message(self) -> None:
        response_id = self.response
        assert response_id is not None
        opening = [
            {
                "type": "start",
                "messageId": response_id,
                "messageMetadata": {"response_id": response_id, "input_ids": list(self.input_ids), "origin": self.origin},
            },
            {"type": "start-step"},
        ]
        for part in opening[self.opened:]:
            self.opened += 1
            self._emit(ResponseOutput(None, response_id, part))

    def _show(self, part: dict[str, Any], *, done: str | None = None) -> None:
        """Emit one translated part; ``done`` names the item it finishes."""

        if part.get("type") == "data-raw-event":
            if done is not None:
                self._item_done(done)
            data = part.get("data") or {}
            self._diagnostic(f"codex.{data.get('subtype')}", data.get("raw"))
            return
        if done is not None:
            self._output(part, shown=lambda: self._item_done(done))
        else:
            self._output(part, shown=lambda: self.translator.advance(part))

    def _item_done(self, item_id: str) -> None:
        self.translator.forget(item_id)
        self.done.append(item_id)
        self._done.add(item_id)

    def _ending_shown(self) -> None:
        self.ending += 1

    def _output(self, part: dict[str, Any], *, shown: Callable[[], None]) -> None:
        """Emit one part of the open response; ``shown`` records it in the position."""

        response_id = self.response
        if response_id is None:
            raise CodexProtocolError(f"codex output outside a response: {part!r}")
        if self.closed:
            # The browser message closed at an interaction reopens once the response continues.
            self.closed = False
            self._emit(ResponseOutput(None, response_id, {"type": "start", "messageId": response_id}))
        shown()
        self._emit(ResponseOutput(None, response_id, part))

    def _diagnostic_or_fail(self, part: dict[str, Any]) -> None:
        if part.get("type") != "data-raw-event":
            raise CodexProtocolError(f"codex notification translated outside an item: {part!r}")
        data = part.get("data") or {}
        self._diagnostic(f"codex.{data.get('subtype')}", data.get("raw"))

    def _diagnostic(self, event_type: str, raw: Any) -> None:
        self._emit(RuntimeDiagnostic(None, event_type, raw))

    def _emit(self, event: RuntimeEvent) -> None:
        self._out.append((event, self.position()))


class CodexRuntimeClient:
    """One reader's connection to one Codex thread in its box."""

    engine_kind = ENGINE_KIND

    def __init__(
        self,
        *,
        link: CodexAppServerLink,
        cwd: str | None = None,
        instructions: str | None = None,
        model: str | None = None,
        sandbox_mode: str | None = None,
        turn_options: dict[str, Any] | None = None,
        thread_config: dict[str, Any] | None = None,
    ) -> None:
        self._link = link
        self._cwd = str(cwd or "").strip() or None
        self._instructions = str(instructions or "").strip() or None
        self._model = str(model or "").strip() or None
        self._sandbox_mode = str(sandbox_mode or "").strip() or None
        self._turn_options = dict(turn_options or {})
        #: Config overrides that ride `thread/start` and `thread/resume`. The
        #: model gateway lives here rather than in the image: Codex takes a
        #: whole `model_providers` table per thread, so a deployment's address
        #: never has to be baked into a container that is not per-deployment.
        self._thread_config = dict(thread_config or {})
        self._effective_model = self._model
        self._thread_id: str | None = None
        #: Codex writes a thread to disk with its first user message; before
        #: that it has no history to list and nothing a resume can find.
        self._materialized = False
        self._opened_status: dict[str, Any] | None = None
        self._stream: _Stream | None = None
        self._ready = asyncio.Event()
        self._reading = False
        self._submit_lock = asyncio.Lock()
        #: Inputs Codex holds or was handed: its history, its queue, and this
        #: client's submissions.
        self._known_inputs: set[str] = set()
        self._children: CodexChildResources | None = None

    # -- identity --------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        return bool(self._link.is_live)

    @property
    def engine_session_key(self) -> str | None:
        return self._thread_id if self._materialized else None

    def _thread(self) -> str:
        if not self._thread_id:
            raise RuntimeError("the codex runtime client has no thread")
        return self._thread_id

    async def open(self, *, resume_thread_id: str | None) -> None:
        """Start the conversation's thread, or rejoin the one it has.

        Either call subscribes this connection to the thread's notifications,
        which the link queues until :meth:`events` reads them; rejoining a
        running thread also makes Codex re-send its pending requests here.
        """

        await self._trust_platform_plugin_hooks()
        if resume_thread_id:
            # The thread outlives the socket and is rejoined by name, so its
            # history and its permission profile stand. Its provider table does
            # not: `model_providers` is configuration the app-server process
            # holds, and a resume on a NEW box reaches a server that was never
            # told where the model is ("Model provider `astrabox` not found").
            # The model is not re-sent: it is thread state, and overriding it
            # on every rejoin would move a conversation to another model.
            params: dict[str, Any] = {"threadId": resume_thread_id, "excludeTurns": True}
            if self._thread_config:
                params["config"] = dict(self._thread_config)
            answered = await self._link.call("thread/resume", params)
        else:
            params = {"historyMode": "paginated"}
            if self._cwd:
                params["cwd"] = self._cwd
            if self._model:
                params["model"] = self._model
            if self._sandbox_mode:
                params["sandbox"] = self._sandbox_mode
            if self._thread_config:
                params["config"] = dict(self._thread_config)
            if self._instructions:
                # The Agent's instructions, delivered the way Codex takes them:
                # in place before the thread composes rather than racing it.
                params["developerInstructions"] = self._instructions
            answered = await self._link.call("thread/start", params)
        thread = (answered or {}).get("thread") if isinstance(answered, dict) else None
        thread_id = str((thread or {}).get("id") or "").strip()
        if not thread_id:
            raise RuntimeError("codex returned no thread id")
        if resume_thread_id and thread_id != resume_thread_id:
            raise RuntimeError(f"codex resumed thread {thread_id!r}, not {resume_thread_id!r}")
        history_mode = str((thread or {}).get("historyMode") or "")
        if history_mode != "paginated":
            # The event stream reads the thread back through Codex's paginated
            # history; a thread without it cannot be resumed by position.
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"codex thread {thread_id} keeps {history_mode or 'unknown'} history, not paginated",
                status_code=502,
            )
        model = str(cast(dict[str, Any], answered).get("model") or "").strip()
        if model:
            self._effective_model = model
        self._thread_id = thread_id
        self._materialized = bool(resume_thread_id)
        self._opened_status = cast(dict[str, Any], (thread or {}).get("status"))

    async def _trust_platform_plugin_hooks(self) -> None:
        """Trust the hooks of the plugins the platform installed, before a thread.

        Codex runs a hook from outside managed configuration only once its
        hash is recorded as trusted under ``hooks.state`` (codex-rs/hooks
        discovery at rust-v0.160.0); otherwise it lists the hook and skips
        it. Its TUI asks the person and records the answer with
        ``config/batchWrite``, and this writes the same record. A plugin in
        the platform's marketplace is one the Agent's author declared at a
        pinned revision, which is that review; any other hook, such as a
        project's own, still waits for one.
        """

        listed = await self._link.call("hooks/list", {"cwds": [self._cwd]} if self._cwd else {})
        trusted = {
            str(hook["key"]): {"trusted_hash": str(hook["currentHash"])}
            for entry in (listed or {}).get("data") or []
            for hook in entry.get("hooks") or []
            if hook.get("source") == "plugin"
            and str(hook.get("pluginId") or "").endswith(f"@{CODEX_PLUGIN_MARKETPLACE}")
            and hook.get("trustStatus") in {"untrusted", "modified"}
        }
        if trusted:
            await self._link.call(
                "config/batchWrite",
                {
                    "edits": [{"keyPath": "hooks.state", "value": trusted, "mergeStrategy": "upsert"}],
                    "reloadUserConfig": True,
                },
            )

    # -- sending ---------------------------------------------------------------

    async def submit(self, item: RuntimeInput) -> None:
        # The dedup sets are complete once the thread has been read back.
        await self._ready.wait()
        stream = self._stream
        assert stream is not None
        async with self._submit_lock:
            if item.input_id in self._known_inputs or item.input_id in stream.admitted:
                return
            text = [{"type": "text", "text": item.content, "text_elements": []}]
            try:
                if stream.busy():
                    # The native queue starts with default TurnStartOptions;
                    # outputSchema is start-only, not a thread setting.
                    if self._turn_options.get("outputSchema") is not None:
                        raise RuntimeInputRefused(
                            code="ENGINE_CAPABILITY_UNAVAILABLE",
                            message=(
                                "Codex cannot queue input with a configured outputSchema while a "
                                "response is running; send it after the current response ends."
                            ),
                        )
                    await self._link.call(
                        "thread/queue/add",
                        {"threadId": self._thread(), "input": text, "clientUserMessageId": item.input_id},
                    )
                else:
                    started = await self._link.call("turn/start", self._turn_start_params(item, text))
                    turn_id = str(((started or {}).get("turn") or {}).get("id") or "").strip()
                    if not turn_id:
                        raise CodexProtocolError("codex turn/start returned no turn")
                    stream.submitted_turns.add(turn_id)
                    self._materialized = True
            except CodexRpcError as exc:
                raise RuntimeInputRefused(
                    code="AGENT_RUNTIME_ERROR",
                    message=f"Codex refused the input: {exc.rpc_message}",
                    status_code=502,
                ) from exc
            self._known_inputs.add(item.input_id)

    def _turn_start_params(self, item: RuntimeInput, text: list[dict[str, Any]]) -> dict[str, Any]:
        params: dict[str, Any] = {
            **self._turn_options,
            "threadId": self._thread(),
            "clientUserMessageId": item.input_id,
            "input": text,
        }
        collaboration = params.get("collaborationMode")
        if isinstance(collaboration, dict):
            settings = collaboration.get("settings")
            if isinstance(settings, dict):
                if "model" in settings:
                    raise ValueError("codex collaborationMode.settings.model is managed by AstraBox")
                if not self._effective_model:
                    raise RuntimeError("codex did not report the thread's effective model")
                params["collaborationMode"] = {
                    **collaboration,
                    "settings": {**settings, "model": self._effective_model},
                }
        return params

    # -- reading ---------------------------------------------------------------

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        thread_id = self._thread()
        if after is not None and after.generation != thread_id:
            raise RuntimeCursorExpired(f"cursor from codex thread {after.generation!r}, this thread is {thread_id!r}")
        if self._reading:
            raise RuntimeError("a Codex runtime client serves one event stream")
        self._reading = True
        try:
            stream = _Stream(thread_id)
            if after is not None:
                stream.restore(after.position)
            stream.set_status(self._opened_status)
            await self._read_back(stream)
            self._stream = stream
            self._ready.set()
            for event, position in stream.drain():
                yield _at(thread_id, event, position)
            async for message in self._link.iter_inbound():
                if not await self._take(stream, message):
                    return
                for event, position in stream.drain():
                    yield _at(thread_id, event, position)
        except EngineStreamDetached:
            return
        finally:
            self._reading = False

    async def _read_back(self, stream: _Stream) -> None:
        """Translate the thread's persisted turns and items after the stream's position."""

        stream.finish_opening()
        if not self._materialized:
            return
        for turn in await self._turns_since(stream.turn):
            turn_id = str(turn["id"])
            if turn_id == stream.turn and stream.ended:
                continue
            for item in await self._turn_items(turn_id):
                stream.persisted_item(turn_id, item)
                stream.child_frames(await self._child_item_frames(item))
            stream.persisted_turn(turn)
        queued = await self._queued_submissions()
        self._known_inputs |= stream.admitted | {str(entry["clientUserMessageId"]) for entry in queued}

    async def _turns_since(self, turn_id: str | None) -> list[dict[str, Any]]:
        """``turn_id`` and every later turn, oldest first; every turn for ``None``.

        Raises :class:`RuntimeCursorExpired` when the thread holds no such
        turn: its history is not the one the cursor was taken from.
        """

        newest_first: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "threadId": self._thread(),
                "sortDirection": "desc",
                "itemsView": "notLoaded",
                "limit": _HISTORY_PAGE,
            }
            if cursor:
                params["cursor"] = cursor
            page = await self._link.call("thread/turns/list", params)
            for turn in (page or {}).get("data") or []:
                newest_first.append(turn)
                if turn_id is not None and turn.get("id") == turn_id:
                    return list(reversed(newest_first))
            cursor = (page or {}).get("nextCursor")
            if not cursor:
                break
        if turn_id is not None:
            raise RuntimeCursorExpired(f"codex thread {self._thread()} holds no turn {turn_id!r}")
        return list(reversed(newest_first))

    async def _turn_items(self, turn_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"threadId": self._thread(), "turnId": turn_id, "limit": _HISTORY_PAGE}
            if cursor:
                params["cursor"] = cursor
            page = await self._link.call("thread/items/list", params)
            items.extend(entry["item"] for entry in (page or {}).get("data") or [])
            cursor = (page or {}).get("nextCursor")
            if not cursor:
                return items

    async def _queued_submissions(self) -> list[dict[str, Any]]:
        """Codex's durable queue for the thread, first to start first."""

        submissions: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"threadId": self._thread()}
            if cursor:
                params["cursor"] = cursor
            page = await self._link.call("thread/queue/list", params)
            submissions.extend((page or {}).get("data") or [])
            cursor = (page or {}).get("nextCursor")
            if not cursor:
                return submissions

    async def _take(self, stream: _Stream, message: dict[str, Any]) -> bool:
        """Translate one live message; ``False`` when the thread closed under this reader."""

        if "id" in message:
            stream.request(message)
            return True
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        method = str(message.get("method") or "")
        thread_id = str(params.get("threadId") or "")
        if thread_id and thread_id != stream.thread_id:
            if method in _CHILD_METHODS:
                stream.child_frames(await self._child_thread_frames(thread_id, message))
            return True
        if method == "thread/closed":
            return False
        if method == "turn/completed":
            # A child spawned during the turn may have finished after its
            # announcement, and the parent stream says nothing more about it.
            stream.child_frames(await self._settle_children())
        stream.notification(message)
        item = params.get("item")
        if method == "item/completed" and isinstance(item, dict):
            stream.child_frames(await self._child_item_frames(item))
        return True

    # -- child runs ------------------------------------------------------------

    def _child_projector(self) -> CodexChildResources:
        if self._children is None:
            self._children = CodexChildResources(root_thread_id=self._thread(), call=self._link.call)
        return self._children

    async def _child_item_frames(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """Read the children one parent ``collabAgentToolCall`` item names."""

        if str(item.get("type") or "") != COLLAB_ITEM_TYPE:
            return []
        projector = self._child_projector()
        if not projector.observe_item(item):
            return []
        return await projector.refresh()

    async def _child_thread_frames(self, thread_id: str, message: dict[str, Any]) -> list[dict[str, Any]]:
        projector = self._children
        if projector is None or not projector.contains(thread_id):
            if message.get("method") != "turn/started":
                return []
            # Codex starts a child's turn before returning its spawn result;
            # reading the thread's metadata verifies the parent owns it.
            projector = self._child_projector()
        return await projector.refresh_thread(thread_id, notification=message)

    async def _settle_children(self) -> list[dict[str, Any]]:
        if self._children is None:
            return []
        return await self._children.refresh()

    async def stop_child_run(self, control_id: str) -> None:
        """Interrupt one spawned child, addressed by its own thread.

        Codex has no parent-side close for a sub-agent: the vendor's
        `closeAgent` and `interruptAgent` are tools the model calls. What the
        protocol offers a client is the ordinary per-thread interrupt, and a
        child thread takes it like any other.
        """

        thread_id = str(control_id or "").strip()
        if not thread_id:
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message="codex child-run control reference is empty",
                status_code=409,
            )
        value = await self._link.call("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = (value or {}).get("thread") if isinstance(value, dict) else None
        turns = thread.get("turns") if isinstance(thread, dict) else None
        live = [
            turn
            for turn in (turns or [])
            if isinstance(turn, dict) and str(turn.get("status") or "") == "inProgress"
        ]
        if not live:
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message="codex child run has no turn left to interrupt",
                status_code=409,
            )
        await self._link.call("turn/interrupt", {"threadId": thread_id, "turnId": str(live[-1].get("id") or "")})

    # -- state, answers, interrupts ----------------------------------------------

    async def state(self) -> RuntimeState:
        thread_id = self._thread()
        read = await self._link.call("thread/read", {"threadId": thread_id})
        status, _running = runtime_status(((read or {}).get("thread") or {}).get("status"))
        page = await self._link.call(
            "thread/turns/list",
            {"threadId": thread_id, "sortDirection": "desc", "itemsView": "summary", "limit": _HISTORY_PAGE},
        )
        turns = (page or {}).get("data") or []
        admitted = [
            client_id
            for turn in turns
            for item in turn.get("items") or []
            if item.get("type") == "userMessage" and (client_id := str(item.get("clientId") or "").strip())
        ]
        open_responses: tuple[OpenResponse, ...] = ()
        open_turn: str | None = None
        cursor_turn: str | None = turns[0]["id"] if turns else None
        if turns and turns[0].get("status") == "inProgress":
            open_turn = str(turns[0]["id"])
            # Events resume where the open response began, so its parts are read whole.
            cursor_turn = str(turns[1]["id"]) if len(turns) > 1 else None
            items = await self._turn_items(open_turn)
            admitted += [
                client_id
                for item in items
                if item.get("type") == "userMessage" and (client_id := str(item.get("clientId") or "").strip())
            ]
            if items:
                origin, input_ids = _origin(items[0])
                open_responses = (OpenResponse(open_turn, origin, input_ids),)
        pending: list[PendingInteraction] = []
        if open_responses:
            response_id = open_responses[0].response_id
            for request in self._link.pending_server_requests():
                params = request.get("params") or {}
                own_turn = params.get("threadId") != thread_id or params.get("turnId") in (None, response_id)
                if str(request.get("method") or "") in INTERACTION_METHODS and own_turn:
                    record = interaction_record(request, response_id)
                    pending.append(PendingInteraction(response_id, record["interaction_id"], record))
        return RuntimeState(
            status=status,
            open_responses=open_responses,
            pending_interactions=tuple(pending),
            queued_input_ids=tuple(str(entry["clientUserMessageId"]) for entry in await self._queued_submissions()),
            admitted_input_ids=tuple(dict.fromkeys(admitted)),
            cursor=RuntimeCursor(
                thread_id,
                {"turn": cursor_turn, "ended": True, "status": status},
            ),
        )

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        # The link holds every request Codex sent and has not resolved, those
        # it re-sent to this connection included, whether or not the event
        # stream has read them yet.
        request = next(
            (
                request
                for request in self._link.pending_server_requests()
                if str(request.get("method") or "") in INTERACTION_METHODS
                and interaction_id_for(request) == interaction_id
            ),
            None,
        )
        if request is None:
            raise InteractionNotOpen(interaction_id)
        method = str(request.get("method") or "")
        params = request.get("params")
        params = params if isinstance(params, dict) else {}
        value = (
            question_response_value(params, response)
            if method == QUESTION_METHOD
            else approval_response_value(method, params, response)
        )
        if self._stream is not None:
            self._stream.answered.add(interaction_id)
        await self._link.respond(request.get("id"), value)

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        await self._ready.wait()
        stream = self._stream
        assert stream is not None
        if cancel_queued:
            for submission in await self._queued_submissions():
                await self._link.call(
                    "thread/queue/delete", {"threadId": self._thread(), "queuedSubmissionId": str(submission["id"])}
                )
        turn_id = stream.open_turn()
        if turn_id is None:
            return
        try:
            # Answered once Codex has aborted the turn; its `turn/completed`
            # with status `interrupted` follows on the stream.
            await self._link.call("turn/interrupt", {"threadId": self._thread(), "turnId": turn_id})
        except CodexRpcError as exc:
            # A turn that ended first refuses the interrupt, and its own
            # ending is already on the stream.
            logger.warning("codex refused turn/interrupt for %s: %s", turn_id, exc.rpc_message)
            return
        stream.interrupted_turns.add(turn_id)

    async def committed(self, cursor: RuntimeCursor) -> None:
        # Codex keeps the thread's history itself; nothing waits on the platform to release it.
        return None

    # -- modes, capabilities, lifecycle ------------------------------------------

    async def set_permission_mode(self, mode: str) -> None:
        normalized = str(mode or "").strip()
        if normalized not in CODEX_SANDBOX_POLICIES:
            raise ValueError(f"codex does not offer sandbox mode {mode!r}")
        # "for subsequent turns" is the vendor's wording for this field: a
        # queued input that starts its own turn later runs under it too.
        await self._link.call(
            "thread/settings/update",
            {"threadId": self._thread(), "sandboxPolicy": CODEX_SANDBOX_POLICIES[normalized]},
        )

    async def capabilities(self) -> EngineCapabilityManifest:
        info = self._link.server_info or {}
        return EngineCapabilityManifest(
            engine_kind=ENGINE_KIND,
            permission_modes=list(CODEX_SANDBOX_MODES),
            supports_interaction=True,
            supports_server_info=True,
            # A spawned sub-agent is an ordinary thread, so the per-thread
            # interrupt the protocol already offers is a real stop.
            supports_child_run_control=True,
            # What `initialize` answered, verbatim. It names the binary the
            # box is actually running, which is the fact a support question
            # about "which Codex is this" needs.
            extra={
                key: info[key]
                for key in ("userAgent", "codexHome", "platformOs", "platformFamily")
                if key in info
            },
        )

    async def get_server_info(self) -> dict[str, Any] | None:
        return self._link.server_info

    async def close(self) -> None:
        await self._link.close()


def _at(thread_id: str, event: RuntimeEvent, position: dict[str, Any]) -> RuntimeEvent:
    return dataclasses.replace(event, cursor=RuntimeCursor(thread_id, position))
