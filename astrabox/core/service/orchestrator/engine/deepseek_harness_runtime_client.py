"""DeepSeek Harness behind the runtime contract (``engine/runtime.py``).

One harness process in the box serves the conversation through its Web
profile's Remote API (:mod:`.deepseek_harness_link`). This client reads three
of the harness's own channels and reports them as runtime events:

* the conversation's durable Session log (``session/follow`` and
  ``session/page``), whose events carry contiguous ``seq`` numbers in a log
  that only appends;
* the process-local assistant-stream frames of a model attempt, whose durable
  ``assistant/message`` or ``assistant/attempt`` settlement repeats the same
  chunk sequence;
* the ``$events`` stream: approval and question requests, their cancellation,
  and child Session status.

**Responses.** A response is one harness turn, ``turn/start`` to ``turn/end``;
its id is ``<session>:<turn>:<seq of turn/start>``. At a turn's start the
harness claims, before anything else can run, every pending next-step message
and the first queued prompt (``@deepseek-ai/dsh-agent-loop``,
``ReactLoopAgent.turn`` and ``ReactLoopInbox.claim``). The client folds the
inbox from its durable ``agent/inbox/spliced`` events exactly as the vendor's
``inbox`` projection does, so the inputs a response answers are the platform
inputs in that claim. A turn that claims none is one the harness started
itself (origin ``engine``): a sub-agent notice, a goal round, a reminder.

**Admission.** :meth:`DeepSeekHarnessRuntimeClient.submit` sends
``session/prompt`` in ``queue`` mode with ``requestId`` =
``astrabox-input:<input_id>``; the harness answers a repeated ``requestId`` that
is still queued or already logged with the original acceptance and inserts
nothing. An input is admitted when its ``agent/inbox/spliced`` insertion,
whose message ``source.rpcId`` is that request id, is in the log: the vendor
commits every inbox mutation as that durable event and rebuilds the inbox
from it when a stored Session resumes, so the input outlives the harness
process. A queued prompt restored that way runs at the Session's next waking
delivery; resuming starts no turn by itself.

**Cursor.** The generation is the harness process (a digest of its launch
token, :attr:`DshApiLink.process_identity`). The position holds two
watermarks — the last emitted event of a durable log event ``[seq, index]`` and
of an attempt's chunk ``[turn, step, ordinal, chunk, index]`` — plus the state
the log alone cannot rebuild for the open turn: the folded inbox and the open
turn's claimed inputs. Resuming reads the log from the open turn's start (or
from the watermark when no turn is open), translates it again, and skips
whatever the watermarks cover. An attempt is named by its place in its step,
which the live frames and the durable settlement both give, so a chunk has
the same position whether it is read live or from history. A log event that
lands while an attempt streams (an inbox insertion, a title) is read before
the attempt's chunks from history and between them live; it carries no
browser part, so a response's parts keep one order either way. Approval and
question requests are not in the log: the harness sends every pending one
again to each new ``$events`` client under the same ``eventId``, and the
position records which ones were reported and whether the response's message
was closed at one.

**Status.** ``waiting`` while a reported request is still pending in the
harness, ``working`` while a turn is open or a child Session runs, ``idle``
otherwise. The harness's own
``AgentStatus`` counts a turn waiting on a person as running and covers only
the one Session, so the status is read from the turn boundaries in the log,
the requests, and the children's running state.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, cast, runtime_checkable

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineStreamDetached,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_child_runs import (
    DSH_ENGINE_KIND,
    DeepSeekHarnessChildResources,
    interrupt_dsh_child,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_events import (
    AttemptBlocks,
    DeepSeekHarnessProtocolError,
    FileTool,
    approval_response_value,
    assistant_stream_chunks,
    build_approval_contract,
    build_question_contract,
    question_response_value,
    tool_call_parts,
    tool_result_parts,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_history import (
    open_turn_events,
    read_history_events,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_link import DshApiError
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_FORM,
    PRESENTATION_TOOL_APPROVAL,
    build_tool_approval_request_frame,
    interaction_presentation,
    is_denied_interaction_response,
    validate_interaction_contract,
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
from astrabox.seams.model import ModelEndpointProvider

logger = get_logger(__name__)

ENGINE_KIND = DSH_ENGINE_KIND

#: The pinned web profile's bootstrap permission presets and the command that
#: selects them. The adapter declaration requires these so its default remains
#: usable; each live Session reads the complete current list from the vendor's
#: permission catalog, including additive presets.
DSH_PERMISSION_PRESETS = ("read-only", "workspace-write", "danger-full-access")
DSH_PERMISSION_COMMAND = "/permission"

#: The harness's DeepSeek route. ``@deepseek-ai/dsh-llm-deepseek`` owns it and
#: reads its key from ``DEEPSEEK_API_KEY`` and its endpoint from
#: ``DEEPSEEK_BASE_URL``, the two variables the adapter declares. The vendor
#: scopes it to DeepSeek models, optionally behind a gateway: its requests
#: use the Messages API and a 256,000-token output cap by default,
#: which another model's endpoint refuses.
DSH_MODEL_PROVIDER = "deepseek-official"

#: The route for every other model. The vendor points OpenAI-compatible
#: gateways at ``@deepseek-ai/dsh-llm-pi-ai``: its web profile mounts it with
#: no routes, and a ``llm-pi-ai`` user-settings section declares them live
#: (what the harness's own Models page writes). The declared route sends
#: standard Chat Completions to the platform gateway with the adapter's
#: defaults for a model it does not describe: a 262,144-token context window
#: and a 32,768-token output cap.
DSH_GATEWAY_PROVIDER = "astrabox-gateway"
DSH_GATEWAY_SETTINGS_NS = "llm-pi-ai"
DSH_GATEWAY_API = "openai-completions"

#: Prefix of the ``requestId`` this client mints for a platform input. The
#: harness persists the id on the queued and the admitted message as
#: ``source.rpcId``.
_REQUEST_ID_PREFIX = "astrabox-input:"

#: An index past every event one record can produce: "the whole record".
_WHOLE = 1 << 30

#: Log events the platform shows nothing for: it holds the prompt, the
#: queue, the title, the permission and model controls itself, and renders
#: an interaction from the live request rather than from its audit pair.
_SILENT_EVENT_TYPES = frozenset(
    {
        "agent-preset/selected",
        "approval/asked",
        "approval/decided",
        "approval/policy",
        "command/done",
        "command/run",
        "model/selection",
        "permission/preset",
        "permissionPresets/preset",
        "request/context",
        "request/header",
        "sandbox/mode",
        "session/title",
        "session/title-llm-request",
        "subagent/catalog",
        "user/message",
    }
)

#: ``turn/end`` reason kinds (``TurnEndReasonMap`` in ``@deepseek-ai/dsh-session``)
#: → the platform's outcome and the AI SDK finish reason. ``interrupted`` is a
#: resume closing a turn its crashed process left open and ``forked`` a fork
#: seed closing one at the fork boundary: nobody asked for either, so both
#: are failures, not cancellations.
_TURN_ENDINGS: dict[str, tuple[ResponseOutcome, str]] = {
    "completed": ("completed", "stop"),
    "max-tokens": ("completed", "length"),
    "aborted": ("cancelled", "other"),
    "blocked": ("failed", "error"),
    "error": ("failed", "error"),
    "interrupted": ("failed", "error"),
    "forked": ("failed", "error"),
}



def request_id(input_id: str) -> str:
    """The ``requestId`` that identifies one platform input to the harness."""
    return f"{_REQUEST_ID_PREFIX}{input_id}"


def input_id_of(message: Any) -> str | None:
    """The platform input a harness ``UserMessage`` carries, if this client sent it."""
    source = message.get("source") if isinstance(message, dict) else None
    if not isinstance(source, dict) or source.get("kind") != "user":
        return None
    rpc_id = str(source.get("rpcId") or "")
    if not rpc_id.startswith(_REQUEST_ID_PREFIX) or len(rpc_id) == len(_REQUEST_ID_PREFIX):
        return None
    return rpc_id[len(_REQUEST_ID_PREFIX):]


def response_id_for(session_id: str, turn: int, start_seq: int) -> str:
    """A response id from the harness's own turn identity."""
    return f"{session_id}:{turn}:{start_seq}"


@runtime_checkable
class DeepSeekHarnessLink(Protocol):
    """One connection to one in-box harness gateway."""

    @property
    def is_live(self) -> bool: ...

    @property
    def process_identity(self) -> str: ...

    async def call(self, method: str, payload: dict[str, Any], *, rpc_id: str | None = None) -> Any: ...

    async def respond(self, rpc_id: str, result: dict[str, Any]) -> bool: ...

    async def follow_session(self, address: dict[str, Any]) -> dict[str, Any]: ...

    def iter_frames(self) -> AsyncGenerator[dict[str, Any], None]: ...

    async def close(self) -> None: ...


async def create_harness_session(
    link: Any, *, cwd: str | None, session_create: dict[str, Any] | None
) -> str:
    """Create one harness conversation and return the id it answers under.

    The configuration is a property of the Agent, which lets a prepared box
    create its conversation before any Session claims it: the claim then names
    this id and rejoins rather than creating a second one, composed exactly
    like the one a cold start makes.
    """

    payload = dict(session_create or {})
    conflicts = set(payload) & {"cwd", "workspaceId", "sessionId"}
    if conflicts:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"DSH session_create contains platform-managed fields: {sorted(conflicts)}",
            status_code=400,
        )
    if cwd:
        payload["cwd"] = cwd
    created = await link.call("session/create", {"args": {"request": payload}})
    native = str((created or {}).get("sessionId") or "").strip()
    if not native:
        raise RuntimeError("deepseek_harness session/create returned no sessionId")
    return native


async def resolve_model_route(
    link: Any,
    model: str,
    *,
    gateway_base_url: str,
    gateway_key_env: str,
    model_endpoint_provider: ModelEndpointProvider | None,
) -> str:
    """Name the harness LLM route that serves ``model`` through the gateway.

    A model the DeepSeek route advertises in the harness's own catalog
    (``session/modelCatalog``) runs on that route. Any other model runs on
    a generic route this call declares for the platform gateway: the same
    base URL and credential variable the DeepSeek route reads, and the
    model as its only entry. The section is replaced whole, so re-asserting
    a changed model leaves no stale entry. The harness validates the
    section when it is written and answers ``settings/rejected`` for one
    it cannot serve.
    """

    catalog = await link.call("session/modelCatalog", {"args": {}})
    groups = catalog.get("groups") if isinstance(catalog, dict) else None
    if not isinstance(groups, list):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"deepseek_harness returned no model catalog: {json.dumps(catalog)[:200]}",
            status_code=502,
        )
    deepseek_models = {
        str(entry.get("id") or "")
        for group in groups
        if isinstance(group, dict) and group.get("id") == DSH_MODEL_PROVIDER
        for entry in group.get("models") or []
        if isinstance(entry, dict)
    }
    if model in deepseek_models:
        return DSH_MODEL_PROVIDER
    if not gateway_base_url or not gateway_key_env:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"deepseek_harness cannot route {model!r}: the platform "
                "gateway base URL or credential variable is empty"
            ),
            status_code=500,
        )
    model_entry: dict[str, Any] = {"id": model}
    if model_endpoint_provider is not None:
        modalities = await model_endpoint_provider.model_input_modalities(model_name=model)
        if modalities is not None:
            model_entry["input"] = list(modalities)
    await link.call(
        "settings/replace",
        {
            "args": {
                "ns": DSH_GATEWAY_SETTINGS_NS,
                "section": {
                    "providers": {
                        DSH_GATEWAY_PROVIDER: {
                            "displayName": "AstraBox model gateway",
                            "api": DSH_GATEWAY_API,
                            "baseURL": gateway_base_url,
                            "apiKeyEnv": gateway_key_env,
                            "models": [model_entry],
                        }
                    }
                },
            }
        },
    )
    return DSH_GATEWAY_PROVIDER


# --------------------------------------------------------------------------
# Translation: the harness's records → runtime events
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Turn:
    """The open harness turn and what translating its records needs."""

    turn: int
    seq: int
    inputs: tuple[str, ...]
    response_id: str
    usage: dict[str, Any] | None = None
    files: dict[str, FileTool] = field(default_factory=dict)
    #: (turn, step) → attempts settled so far, which names the next attempt.
    settled: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def origin(self) -> Literal["input", "engine"]:
        return "input" if self.inputs else "engine"


@dataclass(slots=True)
class _Attempt:
    """A model attempt read from live frames or from the follow baseline."""

    blocks: AttemptBlocks
    #: The vendor's process-local attempt id; live chunks name it.
    live_id: str
    started_after: int
    settlement: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _Unit:
    """The events one record translates to, and where that record sits.

    ``channel`` is ``"d"`` for a durable log event (``key`` = its seq) or
    ``"a"`` for an attempt's chunk or end (``key`` = turn, step, ordinal,
    chunk index, with :data:`_WHOLE` as the end).
    """

    channel: Literal["d", "a"]
    key: tuple[int, ...]
    events: list[RuntimeEvent]


def _finish(reason: str) -> dict[str, Any]:
    return {"type": "finish", "finishReason": reason}


class _Translation:
    """Durable log events and attempt chunks → runtime events, deterministically.

    Everything here derives from the log read from the open turn's start, the
    attempt chunks, and the base a cursor records (:meth:`base`): the folded
    inbox, the seq it is folded through, and the open turn's claimed inputs.
    Translating the same records from the same base yields the same events.
    """

    def __init__(self, session_id: str, base: dict[str, Any]) -> None:
        self.session_id = session_id
        inbox = base.get("inbox") or [[], []]
        self.next_turn: list[str | None] = list(inbox[0])
        self.next_step: list[str | None] = list(inbox[1])
        self.inbox_seq = int(base.get("inbox_seq", -1))
        turn = base.get("turn")
        #: The open turn a resume starts at; its claim has already left the folded inbox.
        self._claimed: dict[str, Any] | None = dict(turn) if isinstance(turn, dict) else None
        self.turn: _Turn | None = None
        self.attempt: _Attempt | None = None
        #: (session, tool call id) → tool name, for a question that names its tool call.
        self.tool_names: dict[tuple[str, str], str] = {}

    def base(self) -> dict[str, Any]:
        """What a cursor records for resuming at the current point."""
        turn = None
        if self.turn is not None:
            turn = {"turn": self.turn.turn, "seq": self.turn.seq, "inputs": list(self.turn.inputs)}
        return {
            "inbox": [list(self.next_turn), list(self.next_step)],
            "inbox_seq": self.inbox_seq,
            "turn": turn,
        }

    # -- durable log events --------------------------------------------------

    def durable(self, event: dict[str, Any]) -> list[_Unit]:
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise DeepSeekHarnessProtocolError("session event lacks a non-negative seq")
        kind = str(event.get("type") or "")
        data = event.get("data")
        if not isinstance(data, dict):
            raise DeepSeekHarnessProtocolError(f"session event {kind!r} has no data object")
        units: list[_Unit] = []
        events: list[RuntimeEvent] = []
        if kind == "agent/inbox/spliced":
            events = self._splice(seq, data)
        elif kind == "turn/start":
            events = self._turn_start(seq, data)
        elif kind in {"assistant/message", "assistant/attempt"}:
            units = self._settlement(seq, kind, event, data)
        elif kind == "step/start":
            events = self._output({"type": "start-step"})
        elif kind == "step/end":
            events = self._output({"type": "finish-step"})
        elif kind == "tool/call":
            events = self._tool_call(data)
        elif kind == "tool/result":
            events = self._tool_result(data)
        elif kind == "turn/end":
            events = self._turn_end(data)
        elif kind not in _SILENT_EVENT_TYPES:
            events = [RuntimeDiagnostic(None, f"deepseek_harness.event.{kind}", dict(event))]
        return [_Unit("d", (seq,), events), *units]

    def _splice(self, seq: int, data: dict[str, Any]) -> list[RuntimeEvent]:
        """Fold one inbox mutation the way the vendor's ``inbox`` projection does."""
        target = data.get("target")
        inserted = data.get("inserted")
        if target not in {"next-turn", "next-step"} or not isinstance(inserted, list):
            raise DeepSeekHarnessProtocolError("agent/inbox/spliced has no target or inserted list")
        ids = [input_id_of(message) for message in inserted]
        if seq > self.inbox_seq:
            pending = self.next_turn if target == "next-turn" else self.next_step
            start = data.get("start")
            removed = data.get("removedCount", 0)
            if (
                not isinstance(start, int) or isinstance(start, bool) or not 0 <= start <= len(pending)
                or not isinstance(removed, int) or isinstance(removed, bool) or removed < 0
                or start + removed > len(pending)
            ):
                raise DeepSeekHarnessProtocolError(f"agent/inbox/spliced at {seq} does not fit the folded inbox")
            discarded = [item for item in pending[start:start + removed] if item]
            pending[start:start + removed] = ids
            self.inbox_seq = seq
            if discarded and data.get("outcome") == "canceled":
                # The harness dropped queued inputs it had admitted (a disposal
                # clears the inbox); nothing will answer them.
                logger.warning(
                    "deepseek_harness discarded admitted inputs session=%s inputs=%s", self.session_id, discarded
                )
        return [InputAdmitted(None, input_id) for input_id in ids if input_id]

    def _turn_start(self, seq: int, data: dict[str, Any]) -> list[RuntimeEvent]:
        number = data.get("turn")
        if not isinstance(number, int) or isinstance(number, bool):
            raise DeepSeekHarnessProtocolError("turn/start carries no turn number")
        if self.turn is not None:
            raise DeepSeekHarnessProtocolError(f"turn/start {number} while turn {self.turn.turn} is open")
        claimed = self._claimed
        if claimed is not None and claimed.get("seq") == seq:
            inputs = tuple(str(item) for item in claimed.get("inputs") or [])
        else:
            # The claim the loop makes right after opening the turn: all
            # next-step input, then the first queued prompt.
            inputs = tuple(item for item in [*self.next_step, *self.next_turn[:1]] if item)
        self._claimed = None
        response_id = response_id_for(self.session_id, number, seq)
        self.turn = _Turn(turn=number, seq=seq, inputs=inputs, response_id=response_id)
        self.attempt = None
        start = {
            "type": "start",
            "messageId": response_id,
            "messageMetadata": {"response_id": response_id, "input_ids": list(inputs), "origin": self.turn.origin},
        }
        return [
            *[InputMessageObserved(None, input_id) for input_id in inputs],
            ResponseStarted(None, response_id, self.turn.origin, inputs),
            ResponseOutput(None, response_id, start),
        ]

    def _turn_end(self, data: dict[str, Any]) -> list[RuntimeEvent]:
        turn = self.turn
        if turn is None:
            raise DeepSeekHarnessProtocolError("turn/end without an open turn")
        if data.get("turn") != turn.turn:
            raise DeepSeekHarnessProtocolError(f"turn/end {data.get('turn')!r} closes turn {turn.turn}")
        reason = data.get("reason")
        kind = str(reason.get("kind") or "") if isinstance(reason, dict) else ""
        ending = _TURN_ENDINGS.get(kind)
        if ending is None:
            raise DeepSeekHarnessProtocolError(f"turn/end carries unknown reason kind {kind!r}")
        outcome, finish_reason = ending
        parts: list[dict[str, Any]] = []
        if self.attempt is not None:
            # An attempt whose live end never came (abandoned while unread).
            parts += self.attempt.blocks.close()
            self.attempt = None
        error: dict[str, Any] | None = None
        if outcome == "failed":
            failure = reason.get("error") if isinstance(reason, dict) else None
            message = str(failure.get("message") or kind) if isinstance(failure, dict) else kind
            error = {"code": "DEEPSEEK_HARNESS_TURN_FAILED", "message": message, "vendor_reason": dict(reason or {})}
            parts.append({"type": "error", "errorText": message})
        parts.append(_finish(finish_reason))
        events: list[RuntimeEvent] = [ResponseOutput(None, turn.response_id, part) for part in parts]
        # Verbatim from the last usage the turn reported; a multi-step turn's
        # earlier step usages are not summed into a total the vendor never gave.
        events.append(ResponseEnded(None, turn.response_id, outcome, kind, turn.usage, error))
        self.turn = None
        return events

    def _tool_call(self, data: dict[str, Any]) -> list[RuntimeEvent]:
        parts, file_tool = tool_call_parts(data)
        call_id = str(data.get("callId") or "")
        if call_id and data.get("name"):
            self.tool_names[(self.session_id, call_id)] = str(data["name"])
        if file_tool is not None and self.turn is not None:
            self.turn.files[call_id] = file_tool
        return self._outputs(parts)

    def _tool_result(self, data: dict[str, Any]) -> list[RuntimeEvent]:
        message = data.get("message")
        call_id = str(message.get("toolCallId") or "") if isinstance(message, dict) else ""
        file_tool = self.turn.files.pop(call_id, None) if self.turn is not None else None
        return self._outputs(tool_result_parts(data, file_tool))

    # -- attempts ------------------------------------------------------------

    def _next_attempt_key(self, turn: int, step: int) -> tuple[int, int, int]:
        open_turn = self._require_turn(turn)
        return (turn, step, open_turn.settled.get((turn, step), 0) + 1)

    def _require_turn(self, turn: int) -> _Turn:
        if self.turn is None or self.turn.turn != turn:
            raise DeepSeekHarnessProtocolError(f"assistant attempt of turn {turn} outside that turn")
        return self.turn

    def _settlement(self, seq: int, kind: str, event: dict[str, Any], data: dict[str, Any]) -> list[_Unit]:
        """Account one attempt's durable settlement; read its chunks when they were not live."""
        turn = data.get("turn")
        step = data.get("step")
        if not isinstance(turn, int) or not isinstance(step, int):
            raise DeepSeekHarnessProtocolError(f"{kind} has no turn or step")
        open_turn = self._require_turn(turn)
        key = self._next_attempt_key(turn, step)
        open_turn.settled[(turn, step)] = key[2]
        usage = data.get("usage")
        if isinstance(usage, dict):
            open_turn.usage = dict(usage)
        attempt = self.attempt
        if (
            attempt is not None and attempt.blocks.key == key and seq > attempt.started_after
            and (kind != "assistant/message" or event.get("surfaceOp") == "append")
        ):
            if attempt.settlement is not None:
                raise DeepSeekHarnessProtocolError("assistant attempt has multiple durable settlements")
            # Its chunks arrived live; the live end frame closes it.
            attempt.settlement = event
            return []
        blocks = AttemptBlocks(self.session_id, key)
        units = [
            _Unit("a", (*key, index), self._outputs(blocks.chunk(chunk)))
            for index, chunk in enumerate(assistant_stream_chunks(data.get("stream")))
        ]
        units.append(_Unit("a", (*key, _WHOLE), self._outputs(blocks.close())))
        return units

    def attempt_frame(self, frame: dict[str, Any]) -> list[_Unit]:
        """One live assistant-stream frame (``SessionAssistantStreamFrame``)."""
        kind = frame.get("type")
        if kind == "start":
            if self.attempt is not None:
                raise DeepSeekHarnessProtocolError("assistant start overlaps an active attempt")
            self.attempt = self._begin(frame)
            return []
        if kind not in {"chunk", "end"}:
            raise DeepSeekHarnessProtocolError(f"unknown assistant stream frame {kind!r}")
        attempt = self.attempt
        # A reconnect can miss a start: the vendor's own client ignores frames
        # of an unknown attempt and shows its durable settlement instead.
        if attempt is None or frame.get("attemptId") != attempt.live_id:
            return []
        index = frame.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index != attempt.blocks.chunks:
            raise DeepSeekHarnessProtocolError("assistant stream chunk index is not contiguous")
        key = attempt.blocks.key
        if kind == "chunk":
            chunk = frame.get("chunk")
            if not isinstance(chunk, dict):
                raise DeepSeekHarnessProtocolError("assistant stream chunk is not an object")
            return [_Unit("a", (*key, index), self._outputs(attempt.blocks.chunk(chunk)))]
        outcome = frame.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("kind") not in {"committed", "abandoned"}:
            raise DeepSeekHarnessProtocolError("assistant stream end has no native outcome")
        settlement = attempt.settlement
        if outcome["kind"] == "committed":
            if settlement is None or (settlement.get("type"), settlement.get("seq")) != (
                outcome.get("eventType"), outcome.get("seq"),
            ):
                raise DeepSeekHarnessProtocolError("assistant stream end does not match its durable settlement")
            if len(assistant_stream_chunks(settlement["data"].get("stream"))) != attempt.blocks.chunks:
                raise DeepSeekHarnessProtocolError("assistant settlement disagrees with the live chunk count")
        elif settlement is not None:
            raise DeepSeekHarnessProtocolError("abandoned assistant attempt has a durable settlement")
        self.attempt = None
        return [_Unit("a", (*key, _WHOLE), self._outputs(attempt.blocks.close()))]

    def baseline(self, baseline: dict[str, Any]) -> list[_Unit]:
        """The follow opening's active attempt, read from its accumulated stream."""
        active = baseline.get("activeAttempt")
        if active is None:
            return []
        if not isinstance(active, dict):
            raise DeepSeekHarnessProtocolError("assistant baseline has an invalid activeAttempt")
        if self.attempt is not None:
            raise DeepSeekHarnessProtocolError("assistant baseline overlaps an active attempt")
        attempt = self._begin(active)
        chunks = assistant_stream_chunks(active.get("stream"))
        if len(chunks) != active.get("nextIndex"):
            raise DeepSeekHarnessProtocolError("assistant baseline stream does not match nextIndex")
        self.attempt = attempt
        key = attempt.blocks.key
        return [
            _Unit("a", (*key, index), self._outputs(attempt.blocks.chunk(chunk)))
            for index, chunk in enumerate(chunks)
        ]

    def _begin(self, frame: dict[str, Any]) -> _Attempt:
        live_id = frame.get("attemptId")
        turn = frame.get("turn")
        step = frame.get("step")
        started_after = frame.get("startedAfterSeq")
        if (
            not isinstance(live_id, str) or not live_id
            or not isinstance(turn, int) or not isinstance(step, int)
            or not isinstance(started_after, int) or isinstance(started_after, bool)
        ):
            raise DeepSeekHarnessProtocolError("assistant attempt lacks its id, turn, step or opening cursor")
        key = self._next_attempt_key(turn, step)
        return _Attempt(blocks=AttemptBlocks(self.session_id, key), live_id=live_id, started_after=started_after)

    # -- output ----------------------------------------------------------------

    def _output(self, part: dict[str, Any]) -> list[RuntimeEvent]:
        return self._outputs([part])

    def _outputs(self, parts: list[dict[str, Any]]) -> list[RuntimeEvent]:
        if not parts:
            return []
        if self.turn is None:
            raise DeepSeekHarnessProtocolError(f"response output outside a turn: {parts[0].get('type')!r}")
        return [ResponseOutput(None, self.turn.response_id, part) for part in parts]


# --------------------------------------------------------------------------
# Cursor positions
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Watermarks:
    """The last emitted event of each translated channel.

    An index of :data:`_WHOLE` means every event of that record was emitted.
    """

    durable: tuple[int, ...]
    attempt: tuple[int, ...] | None

    def covers(self, unit: _Unit, index: int) -> bool:
        if unit.channel == "d":
            return (*unit.key, index) <= self.durable
        return self.attempt is not None and (*unit.key, index) <= self.attempt

    def advance(self, unit: _Unit, index: int) -> None:
        if unit.channel == "d":
            self.durable = (*unit.key, index)
        else:
            self.attempt = (*unit.key, index)


@dataclass(slots=True)
class _Live:
    """State the log cannot rebuild: the interactions reported open, and
    whether the open response's message was closed at one."""

    open: dict[str, str] = field(default_factory=dict)
    closed: bool = False


def _replay_start(position: dict[str, Any]) -> int:
    """The first log event a resume reads: the open turn's start, else the watermark."""
    turn = position.get("turn")
    if isinstance(turn, dict):
        return int(turn["seq"])
    seq, index = position["d"]
    return int(seq) if index < _WHOLE else int(seq) + 1


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Request:
    """One live approval or question request the harness waits on."""

    contract: dict[str, Any]
    tool_call_id: str | None
    session_id: str


class DeepSeekHarnessRuntimeClient:
    """One reader's connection to one harness conversation in its box."""

    engine_kind = ENGINE_KIND

    def __init__(
        self,
        *,
        link: DeepSeekHarnessLink,
        native_session_id: str,
        model_endpoint_provider: ModelEndpointProvider | None = None,
    ) -> None:
        native = str(native_session_id or "").strip()
        if not native:
            raise ValueError("a harness runtime client needs the conversation's session id")
        self._link = link
        self._session_id = native
        self._address = {"kind": "session", "sessionId": native}
        self._model_endpoint_provider = model_endpoint_provider
        self._snapshot: dict[str, Any] | None = None
        self._reading = False
        self._live = _Live()
        #: Requests seen on this connection, by ``eventId``, until answered or withdrawn.
        self._requests: dict[str, _Request] = {}
        #: Requests not reported yet: no response of this conversation is open.
        self._held: list[str] = []
        self._translation = _Translation(native, {})
        self._children: DeepSeekHarnessChildResources | None = None
        self._status_sent: RuntimeStatus | None = None

    @property
    def is_live(self) -> bool:
        return bool(self._link.is_live)

    @property
    def engine_session_key(self) -> str | None:
        return self._session_id

    # -- input -----------------------------------------------------------------

    async def submit(self, item: RuntimeInput) -> None:
        """Queue one input as the harness's own ``session/prompt``; a resend is a no-op."""
        request = request_id(item.input_id)
        try:
            value = await self._link.call(
                "session/prompt",
                {"args": {"request": {
                    "requestId": request,
                    "sessionId": self._session_id,
                    # "queue" is the harness's own word for an ordinary
                    # delivery: the prompt becomes the next turn. "steer"
                    # would splice it into the running turn instead.
                    "mode": "queue",
                    "content": [{"type": "text", "text": item.content}],
                }}},
                rpc_id=request,
            )
        except DshApiError as exc:
            raise RuntimeInputRefused(exc.code, exc.api_message) from exc
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DeepSeekHarnessProtocolError(f"session/prompt answered without accepting: {value!r}")

    # -- events ------------------------------------------------------------------

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        if after is not None and after.generation != self._link.process_identity:
            raise RuntimeCursorExpired(
                f"cursor from harness process {after.generation!r}, the harness is {self._link.process_identity!r}"
            )
        if self._reading:
            raise RuntimeError("a harness runtime client serves one event stream at a time")
        self._reading = True
        try:
            async for event in self._stream(after):
                yield event
        finally:
            self._reading = False

    async def _stream(self, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        position: dict[str, Any] = (
            deepcopy(after.position) if after is not None
            else {"d": [-1, _WHOLE], "a": None, "inbox": [[], []], "inbox_seq": -1, "turn": None}
        )
        snapshot = await self._open_follow()
        through = int(snapshot["cursor"])
        start = _replay_start(position)
        if start > through + 1:
            raise RuntimeCursorExpired(f"cursor at seq {start - 1} is past the harness log's end {through}")
        self._translation = translation = _Translation(self._session_id, position)
        marks = _Watermarks(
            durable=tuple(int(value) for value in position["d"]),
            attempt=tuple(int(value) for value in position["a"]) if position.get("a") else None,
        )
        self._live = _Live(
            open={str(k): str(v) for k, v in (position.get("open") or {}).items()},
            closed=bool(position.get("closed")),
        )
        self._children = DeepSeekHarnessChildResources(
            root_session_id=self._session_id,
            call=self._link.call,
            history_positions={str(k): int(v) for k, v in (position.get("children") or {}).items()},
        )
        expected = start
        for event in await read_history_events(
            self._link.call, address=self._address, through_seq=through, after_seq=start - 1,
        ):
            for out in self._durable(event, marks):
                yield out
            expected = event["seq"] + 1
        for out in self._live_events(await self._children.refresh(), marks):
            yield out
        status = self._status()
        self._status_sent = status
        yield StatusChanged(self._cursor(marks), status)
        frames = self._link.iter_frames()
        try:
            while True:
                try:
                    frame = await anext(frames)
                except (EngineStreamDetached, StopAsyncIteration):
                    # The connection ended; the runtime itself may still run.
                    return
                outputs, expected = await self._frame(frame, marks, expected)
                for out in outputs:
                    yield out
                for out in self._report_held(marks):
                    yield out
                status = self._status()
                if status != self._status_sent:
                    self._status_sent = status
                    yield StatusChanged(self._cursor(marks), status)
        finally:
            await frames.aclose()

    async def _frame(
        self, frame: dict[str, Any], marks: _Watermarks, expected: int,
    ) -> tuple[list[RuntimeEvent], int]:
        """Route one downlink frame; ``expected`` is the next root log seq."""
        kind = str(frame.get("type") or "")
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        session = str(payload.get("sessionId") or "")
        children = self._children
        assert children is not None
        translation = self._translation
        if session == self._session_id and kind == "session/event":
            event = payload.get("event")
            if not isinstance(event, dict) or event.get("seq") != expected:
                raise DeepSeekHarnessProtocolError(
                    f"harness log event {event.get('seq') if isinstance(event, dict) else None!r} "
                    f"does not follow seq {expected - 1}"
                )
            outputs = self._durable(event, marks)
            if event.get("type") == "subagent/catalog":
                _, facts = await children.observe_mux_frame(frame)
                outputs += self._live_events(facts, marks)
            return outputs, expected + 1
        if session == self._session_id and kind == "session/assistant-stream":
            outputs = []
            for unit in translation.attempt_frame(payload.get("frame") or {}):
                outputs += self._emit(unit, marks, translation.base())
            return outputs, expected
        if session == self._session_id and kind == "session/assistant-stream-snapshot":
            if payload.get("cursor") != expected - 1:
                raise DeepSeekHarnessProtocolError("assistant baseline is not at the followed log's cut")
            outputs = []
            for unit in translation.baseline(payload.get("baseline") or {}):
                outputs += self._emit(unit, marks, translation.base())
            return outputs, expected
        if kind == "waterfall":
            return self._request(frame, marks), expected
        if kind == "cancel":
            return self._withdraw(str(frame.get("eventId") or ""), marks), expected
        if kind == "emit" and frame.get("event") == "api-session/error" and (frame.get("args") or [None])[0] == self._session_id:
            # A failure outside a durable turn position; a turn's own failure
            # also ends it as turn/end with reason error.
            return [RuntimeDiagnostic(self._cursor(marks), "deepseek_harness.agent_error", dict(frame))], expected
        if children.may_own(frame):
            owned, facts = await children.observe_mux_frame(frame)
            if owned:
                return self._live_events(facts, marks), expected
        if kind == "emit":
            # Host notifications about other Sessions, plugins or settings
            # carry nothing for this conversation; its own status is read
            # from its log.
            return [], expected
        return [RuntimeDiagnostic(self._cursor(marks), f"deepseek_harness.frame.{kind}", dict(frame))], expected

    def _durable(self, event: dict[str, Any], marks: _Watermarks) -> list[RuntimeEvent]:
        """One log event's units, emitted past the cursor."""
        translation = self._translation
        before = translation.base()
        outputs: list[RuntimeEvent] = []
        for unit in translation.durable(event):
            outputs += self._emit(unit, marks, before if unit.channel == "d" else translation.base())
        return outputs

    def _emit(self, unit: _Unit, marks: _Watermarks, before: dict[str, Any]) -> list[RuntimeEvent]:
        """The events of one unit a cursor has not covered, each carrying its cursor.

        Every event but a unit's last carries the base from ``before`` the
        unit, so resuming between them translates the unit again from the
        same state and skips what was emitted.
        """
        events = unit.events
        fresh = [index for index in range(len(events)) if not marks.covers(unit, index)]
        if not fresh:
            return []
        outputs: list[RuntimeEvent] = []
        if fresh[0] == 0:
            outputs += self._before_unit(unit, marks, before)
        after = self._translation.base()
        for index in fresh:
            last = index == len(events) - 1
            marks.advance(unit, _WHOLE if last else index)
            position = self._position(marks, after if last else before)
            outputs.append(_with_cursor(events[index], RuntimeCursor(self._link.process_identity, position)))
        return outputs

    def _before_unit(self, unit: _Unit, marks: _Watermarks, before: dict[str, Any]) -> list[RuntimeEvent]:
        """Live-state steps a unit's first event needs: closing a turn's
        requests, and reopening a message an interaction closed.

        A reopen is its own event at the position before the unit, so a
        resume after it does not reopen again and one before it does.
        """
        outputs: list[RuntimeEvent] = []
        if any(isinstance(event, ResponseEnded) for event in unit.events):
            ended = next(event.response_id for event in unit.events if isinstance(event, ResponseEnded))
            # The response's end withdraws its open interactions in the
            # platform's record; the requests are gone with the turn.
            for interaction_id in [i for i, r in self._live.open.items() if r == ended]:
                self._live.open.pop(interaction_id, None)
                self._requests.pop(interaction_id, None)
        response_id = next(
            (event.response_id for event in unit.events if isinstance(event, ResponseOutput) and event.response_id),
            None,
        )
        if self._live.closed and response_id is not None:
            self._live.closed = False
            cursor = RuntimeCursor(self._link.process_identity, self._position(marks, before))
            outputs.append(ResponseOutput(cursor, response_id, {"type": "start", "messageId": response_id}))
        return outputs

    def _live_events(self, facts: list[dict[str, Any]], marks: _Watermarks) -> list[RuntimeEvent]:
        """Child-resource facts and evidence the child projector produced."""
        outputs: list[RuntimeEvent] = []
        response_id = self._translation.turn.response_id if self._translation.turn is not None else None
        for fact in facts:
            if fact.get("type") == "data-raw-event":
                data = fact.get("data") or {}
                outputs.append(RuntimeDiagnostic(
                    self._cursor(marks), f"deepseek_harness.child.{data.get('subtype')}", data.get("raw"),
                ))
                continue
            if response_id is not None and self._live.closed:
                self._live.closed = False
                outputs.append(ResponseOutput(self._cursor(marks), response_id, {"type": "start", "messageId": response_id}))
            outputs.append(ResponseOutput(self._cursor(marks), response_id, fact))
        return outputs

    # -- interactions --------------------------------------------------------------

    def _request(self, frame: dict[str, Any], marks: _Watermarks) -> list[RuntimeEvent]:
        """An approval or question request the harness is waiting on."""
        event_id = str(frame.get("eventId") or "").strip()
        agent_id = str(frame.get("agentId") or "").strip()
        children = self._children
        assert children is not None
        if frame.get("event") not in {"approval/request", "user-questions/request"} or not event_id or not (
            agent_id == self._session_id or children.contains(agent_id)
        ):
            return [RuntimeDiagnostic(self._cursor(marks), "deepseek_harness.request.unrouted", dict(frame))]
        payload = dict(frame.get("request") or {})
        payload["sessionId"] = agent_id
        if frame["event"] == "approval/request":
            contract = build_approval_contract(payload)
        else:
            contract = build_question_contract(payload)
            questions = payload.get("questions") or []
            if len(questions) == 1 and isinstance(questions[0], dict):
                intent = questions[0].get("intent")
                call_id = str(intent.get("callId") or "").strip() if isinstance(intent, dict) else ""
                if isinstance(intent, dict) and intent.get("kind") == "plan-review" and call_id:
                    owner = self._translation.tool_names.get((agent_id, call_id))
                    if not owner:
                        raise DeepSeekHarnessProtocolError("plan review names an unobserved tool call")
                    contract.update(tool_name=owner, tool_use_id=call_id)
        tool_call_id = str(contract.pop("tool_use_id", "") or "") or None
        validate_interaction_contract(contract)
        self._requests[event_id] = _Request(contract=contract, tool_call_id=tool_call_id, session_id=agent_id)
        if event_id in self._live.open or event_id in self._held:
            # Sent again to this new connection; already reported or waiting.
            return []
        self._held.append(event_id)
        return []

    def _report_held(self, marks: _Watermarks) -> list[RuntimeEvent]:
        """Report the held requests once a response of this conversation is open."""
        turn = self._translation.turn
        if turn is None or not self._held:
            return []
        outputs: list[RuntimeEvent] = []
        for event_id in list(self._held):
            self._held.remove(event_id)
            request = self._requests.get(event_id)
            if request is None:
                continue
            record = self._record(event_id, turn.response_id, request)
            parts: list[dict[str, Any]] = []
            if self._live.closed:
                parts.append({"type": "start", "messageId": turn.response_id})
            approval = build_tool_approval_request_frame(record)
            if approval is not None:
                parts.append(approval)
            parts += [{"type": "data-interaction", "data": dict(record)}, _finish("tool-calls")]
            self._live.open[event_id] = turn.response_id
            self._live.closed = True
            cursor = self._cursor(marks)
            outputs.append(InteractionOpened(cursor, turn.response_id, event_id, record))
            outputs += [ResponseOutput(cursor, turn.response_id, part) for part in parts]
        return outputs

    def _record(self, event_id: str, response_id: str, request: _Request) -> dict[str, Any]:
        record: dict[str, Any] = {"interaction_id": event_id, "response_id": response_id, **deepcopy(request.contract)}
        if request.tool_call_id:
            record["tool_call_id"] = request.tool_call_id
        return record

    def _withdraw(self, event_id: str, marks: _Watermarks) -> list[RuntimeEvent]:
        """The harness stopped waiting on a request (its turn was cancelled, or it settled elsewhere)."""
        self._requests.pop(event_id, None)
        if event_id in self._held:
            self._held.remove(event_id)
        if self._live.open.pop(event_id, None) is None:
            return []
        return [InteractionClosed(self._cursor(marks), event_id, "withdrawn")]

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        request = self._requests.get(interaction_id)
        response_id = self._live.open.get(interaction_id)
        if request is None or response_id is None:
            raise InteractionNotOpen(interaction_id)
        pending = self._record(interaction_id, response_id, request)
        presentation = interaction_presentation(pending)
        result: dict[str, Any]
        if presentation == PRESENTATION_TOOL_APPROVAL:
            result = {"ok": True, "value": approval_response_value(
                denied=is_denied_interaction_response(pending, response),
            )}
        elif presentation == PRESENTATION_FORM and is_denied_interaction_response(pending, response):
            # The harness's own decline for a question: its ask_user_question
            # raises ASK_CANCELLED, which the agent reads as the person
            # declining rather than as a malformed answer.
            result = {"ok": False, "error": {
                "name": "UserQuestionError", "code": "ASK_CANCELLED",
                "message": "the user cancelled ask_user_question", "details": {},
            }}
        elif presentation == PRESENTATION_FORM:
            result = {"ok": True, "value": question_response_value(pending, response)}
        else:
            raise InteractionNotOpen(interaction_id)
        await self._link.respond(interaction_id, result)
        self._requests.pop(interaction_id, None)
        self._live.open.pop(interaction_id, None)

    # -- positions -------------------------------------------------------------------

    def _position(self, marks: _Watermarks, base: dict[str, Any]) -> dict[str, Any]:
        children = self._children.history_positions() if self._children is not None else {}
        return {
            "d": list(marks.durable),
            "a": list(marks.attempt) if marks.attempt is not None else None,
            **base,
            "open": dict(self._live.open),
            "closed": self._live.closed,
            "children": children,
        }

    def _cursor(self, marks: _Watermarks) -> RuntimeCursor:
        return RuntimeCursor(self._link.process_identity, self._position(marks, self._translation.base()))

    def _status(self) -> RuntimeStatus:
        # A reported request counts while the harness still sends it to this
        # connection; one answered just before an earlier reader stopped is
        # never sent again.
        if any(interaction_id in self._requests for interaction_id in self._live.open):
            return "waiting"
        children = self._children
        if self._translation.turn is not None or (children is not None and children.any_running()):
            return "working"
        return "idle"

    # -- state -----------------------------------------------------------------------

    async def _open_follow(self) -> dict[str, Any]:
        if self._snapshot is None:
            snapshot = await self._link.follow_session(self._address)
            cursor = snapshot.get("cursor")
            if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < -1:
                raise DeepSeekHarnessProtocolError("follow snapshot has no log cursor")
            self._snapshot = snapshot
        return self._snapshot

    async def state(self) -> RuntimeState:
        snapshot = await self._open_follow()
        through = int(snapshot["cursor"])
        projections = snapshot.get("projections")
        values = projections.get("values") if isinstance(projections, dict) else None
        inbox = values.get("inbox") if isinstance(values, dict) else None
        if not isinstance(inbox, dict):
            raise DeepSeekHarnessProtocolError("follow snapshot carries no inbox projection")
        next_turn = [input_id_of(message) for message in inbox.get("next-turn") or []]
        next_step = [input_id_of(message) for message in inbox.get("next-step") or []]
        queued = tuple(item for item in [*next_turn, *next_step] if item)
        turn_events = await open_turn_events(self._link.call, address=self._address, through_seq=through)
        open_responses: tuple[OpenResponse, ...] = ()
        turn: dict[str, Any] | None = None
        claimed: tuple[str, ...] = ()
        if turn_events:
            start = turn_events[0]
            number = (start.get("data") or {}).get("turn")
            if not isinstance(number, int):
                raise DeepSeekHarnessProtocolError("turn/start carries no turn number")
            # The messages that entered the turn's first step are its claim
            # (``user/message`` "records the messages entering the step").
            claimed = tuple(dict.fromkeys(
                item for item in (
                    input_id_of(event.get("data")) for event in turn_events if event.get("type") == "user/message"
                ) if item
            ))
            response_id = response_id_for(self._session_id, number, int(start["seq"]))
            open_responses = (OpenResponse(response_id, "input" if claimed else "engine", claimed),)
            turn = {"turn": number, "seq": int(start["seq"]), "inputs": list(claimed)}
        pending = tuple(
            PendingInteraction(response_id, interaction_id, self._record(interaction_id, response_id, request))
            for interaction_id, response_id in self._live.open.items()
            if (request := self._requests.get(interaction_id)) is not None
        )
        status: RuntimeStatus = "waiting" if pending else ("working" if turn_events else "idle")
        position = {
            # Events resume where the open turn began, so its parts are read whole.
            "d": [int(turn["seq"]) - 1 if turn else through, _WHOLE],
            "a": None,
            "inbox": [next_turn, next_step],
            "inbox_seq": through,
            "turn": turn,
            "open": dict(self._live.open),
            "closed": self._live.closed,
            "children": {},
        }
        return RuntimeState(
            status=status,
            open_responses=open_responses,
            pending_interactions=pending,
            queued_input_ids=queued,
            admitted_input_ids=tuple(dict.fromkeys([*queued, *claimed])),
            cursor=RuntimeCursor(self._link.process_identity, position),
        )

    # -- controls ----------------------------------------------------------------------

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        """Cancel the running turn with the harness's own ``session/cancel``.

        The harness ends the turn ``aborted`` and keeps queued prompts, which
        wait for the Session's next waking delivery
        (``SessionController.cancel`` passes ``keepInbox``).
        """
        if cancel_queued:
            raise NotImplementedError("deepseek_harness interrupts only the turn in progress")
        try:
            await self._link.call("session/cancel", {"args": {"request": {"sessionId": self._session_id}}})
        except DshApiError as exc:
            # Cancellation needs a live Agent; without one nothing is running.
            if exc.code != "session/not-found":
                raise

    async def committed(self, cursor: RuntimeCursor) -> None:
        """Nothing to release: the harness keeps its whole log."""
        _ = cursor

    async def capabilities(self) -> EngineCapabilityManifest:
        # Selectable presets belong to the supplier's live process catalog.
        catalog = await self._link.call("permissionPresets/catalog", {"args": {}})
        options = catalog.get("options") if isinstance(catalog, dict) else None
        if not isinstance(options, list):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="deepseek_harness permission catalog has no options",
                status_code=502,
            )
        permission_modes: list[str] = []
        for index, option in enumerate(options):
            value = str(option.get("value") or "").strip() if isinstance(option, dict) else ""
            if not value or value in permission_modes:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message=f"deepseek_harness permission catalog has an invalid or repeated option at {index}",
                    status_code=502,
                )
            permission_modes.append(value)
        if not permission_modes:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="deepseek_harness permission catalog has no presets",
                status_code=502,
            )
        return EngineCapabilityManifest(
            engine_kind=ENGINE_KIND,
            permission_modes=permission_modes,
            supports_interaction=True,
            supports_child_run_control=True,
            extra={"transport": "dsh_apiproxy_over_opensandbox_endpoint"},
        )

    async def set_permission_mode(self, mode: str) -> None:
        """Select one of the harness's own permission presets.

        The harness bundles its two mechanism knobs, the sandbox mode and the
        approval policy, into named presets (``@deepseek-ai/dsh-permission-presets``),
        so AstraBox's flat mode list is this engine's own shape. Its API
        exposes no setter; the preset is switched by the harness's own
        ``/permission`` command through ``commands/execute``.
        """
        value = await self._link.call(
            "commands/execute",
            {"args": {
                "agentId": self._session_id,
                "line": f"{DSH_PERMISSION_COMMAND} {mode}",
                # The native descriptor requires submittedAttachments even
                # for commands with no attachments, such as /permission.
                "submittedAttachments": [],
            }},
        )
        result = (value or {}).get("result") if isinstance(value, dict) else None
        kind = str((result or {}).get("kind") or "") if isinstance(result, dict) else ""
        if kind != "success":
            # The command endpoint answers ok for a line it did not recognise,
            # so the envelope is not evidence — the command's own verdict is.
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"deepseek_harness refused permission preset {mode!r}: {json.dumps(value)[:200]}",
                status_code=502,
            )

    async def select_model(
        self,
        model: str,
        *,
        gateway_base_url: str,
        gateway_key_env: str,
        prepared_route: str | None = None,
    ) -> None:
        """Install the platform's model choice on this conversation.

        ``session/create`` takes no model, so a conversation runs the image's
        default until this selects one; it is asserted on every activation so
        a model changed on the Agent reaches conversations that already exist.
        The reply is the vendor's normalized selection and is checked, because
        a normalization that landed on another model would otherwise look like
        success. ``prepared_route`` is the route a prepared box resolved for
        this model against the same catalog and gateway settings.
        """
        route = prepared_route or await resolve_model_route(
            self._link,
            model,
            gateway_base_url=gateway_base_url,
            gateway_key_env=gateway_key_env,
            model_endpoint_provider=self._model_endpoint_provider,
        )
        value = await self._link.call(
            "session/selectModel",
            {"args": {"request": {"sessionId": self._session_id, "provider": route, "model": model}}},
        )
        selected = (value or {}).get("selected") if isinstance(value, dict) else None
        if not isinstance(selected, dict):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"deepseek_harness returned no model selection for {model!r}: {json.dumps(value)[:200]}",
                status_code=502,
            )
        installed = str(selected.get("model") or "")
        provider = str(selected.get("provider") or "")
        if installed != model or provider != route:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    "deepseek_harness installed a different model than the platform "
                    f"selected: asked {route}/{model!r}, installed {provider}/{installed!r}"
                ),
                status_code=502,
            )

    async def stop_child_run(self, control_id: str) -> None:
        """Interrupt one continuable child through its private harness address."""
        await interrupt_dsh_child(self._link.call, control_id)

    async def close(self) -> None:
        await self._link.close()


def _with_cursor(event: RuntimeEvent, cursor: RuntimeCursor) -> RuntimeEvent:
    return cast(RuntimeEvent, replace(event, cursor=cursor))
