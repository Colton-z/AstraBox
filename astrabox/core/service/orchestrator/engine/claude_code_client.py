"""Claude Code ``EngineClient`` — drives one box's runner over ``RunnerLink``.

The translation-shell counterpart of :class:`HermesEngineClient`: turns are
driven through the engine seam (durable FIFO delivery / ``iter_turn_events``), not
through the platform's sidecar dispatch machinery. The runner owns the SDK
and the session; this client owns nothing but the wire and the mapping onto
the AI SDK frame contract:

* runner ``event`` frames → ``translate_claude_sdk_message`` (per-engine
  translator seam; unknown types hard-fail);
* runner ``interaction`` frames → ``interaction.request`` AI SDK frames; the
  pending interaction id is remembered per turn so ``submit_interaction_response`` can
  route the answer without the caller holding runner identifiers;
* a turn ends at the translator's ``result`` frame. ``cancelled`` is decided
  by this client: the SDK reports an interrupted run as an error subtype, and
  only this client knows the interrupt was its own ``cancel_turn``;
* a link that dies mid-turn raises ``EngineStreamDetached`` — a transport
  statement, never a turn terminal: the box may still be running the turn, so
  the worker abandons without settling and the recovery lane judges from
  durable evidence. (A cancelled turn's EOF still settles as ``cancelled`` —
  the interrupt tears the stream down by design.)

An unfinished turn is recovered from Claude's durable transcript by the
adapter. This client therefore does not claim resident-turn continuation;
platform frame replay remains the independent ``session_events`` read path.

The link is one ordered stream with one reader at a time. A turn consumer
holds the read position from its own root input to its Result. Between turns
the client's resident observer reads instead, so a response the engine
starts on its own — Claude Code answering a queued ``<task-notification>``
with no input pending, which this SDK does not echo as a UserMessage — is
recognised at its first root assistant activity, translated by the same
translator, and handed to the platform's :class:`ResidentOutputSink` under
that activity envelope's uuid. A consumer
that attaches while such a response is running feeds its frames to the same
observation until its own input's receipt, so every frame has one publisher.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, get_args

from claude_agent_sdk import get_subagent_messages_from_store, list_subagents_from_store
from claude_agent_sdk.types import PermissionMode, SessionStore

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.claude_code_background import (
    build_background_task_manifest,
)
from astrabox.core.service.orchestrator.engine.claude_child_runs import ClaudeChildIdentities
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineInputCommand,
    EngineStreamDetached,
    EngineTurnReceipt,
    ResidentOutputSink,
    ResidentResponseHandle,
)
from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    claude_result_data,
    rebuild_claude_stream_cursor,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    EngineTurnEmission,
    InputConsumed,
    InteractionRequested,
    PrivateDiagnostic,
    ResponseCompleted,
    TurnTerminal,
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine.claude_message_blocks import parse_tool_event_blocks
from astrabox.core.service.orchestrator.engine.frame_scope import session_scoped_engine_frame
from astrabox.core.service.orchestrator.engine.claude_interaction_codec import (
    build_claude_interaction_contract,
    claude_answer_fields,
)
from astrabox.core.service.orchestrator.engine.runner_link import (
    DeliveryCommand as RunnerDeliveryCommand,
    RunnerLink,
    RunnerLinkError,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HistoryStoreSequence:
    """Typed durable-store coordinate from one Result checkpoint."""

    value: int


@dataclass(frozen=True, slots=True)
class HistoryLiveSequence:
    """Typed runner-journal coordinate from one Result checkpoint."""

    value: int


@dataclass(frozen=True, slots=True)
class ResultHistoryCheckpoint:
    """Independent coordinates sampled atomically at one Result boundary."""

    store: HistoryStoreSequence
    live: HistoryLiveSequence


def require_result_store_sequence(frame: dict[str, Any]) -> int:
    """The store coordinate a Result must carry, or a wire violation.

    A Result is the only frame where the coordinate exists, so a Result
    without one is a contract failure rather than a field to default.
    Defaulting to 0 would publish "nothing is durable yet" — a legitimate
    lower bound in shape — and let a consumer trim the journal from its
    start. Booleans are refused explicitly because ``True`` is an ``int``.

    :class:`RunnerLink` accepts a ``hello`` only when its protocol exactly
    matches the host wire, before routing replay frames. This function therefore
    validates only the Result shape.
    """
    value = frame.get("store_sequence")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunnerLinkError(f"runner Result carries no usable store_sequence: {value!r}")
    return value


def require_result_history_checkpoint(
    frame: dict[str, Any],
) -> ResultHistoryCheckpoint:
    """Decode one atomic Result checkpoint without comparing its coordinates."""

    live_value = frame.get("history_live_sequence")
    envelope_sequence = frame.get("seq")
    if (
        isinstance(live_value, bool)
        or not isinstance(live_value, int)
        or live_value < 0
        or live_value != envelope_sequence
    ):
        raise RunnerLinkError(
            "runner Result carries no atomic history_live_sequence "
            f"for envelope seq {envelope_sequence!r}: {live_value!r}"
        )
    return ResultHistoryCheckpoint(
        store=HistoryStoreSequence(require_result_store_sequence(frame)),
        live=HistoryLiveSequence(live_value),
    )


def session_store_reload_frame(gap: dict[str, Any]) -> dict[str, Any]:
    """Translate an expired runner cursor into one authoritative rebuild."""

    after = gap.get("after_sequence")
    first = gap.get("first_retained_sequence")
    last = gap.get("last_sequence")
    if (
        isinstance(after, bool)
        or not isinstance(after, int)
        or after < 0
        or isinstance(last, bool)
        or not isinstance(last, int)
        or last < 0
        or last <= after
        or (
            first is not None
            and (
                isinstance(first, bool)
                or not isinstance(first, int)
                or first < 1
                or first > last
                or after >= first - 1
            )
        )
    ):
        raise RunnerLinkError(f"runner journal gap is malformed: {gap!r}")
    resume_sequence = first - 1 if isinstance(first, int) else last
    return {
        "type": "data-session-store-reload",
        "transient": True,
        "data": {"resumeSequence": max(0, resume_sequence)},
    }


#: How often the observer refreshes the platform's liveness stamp for an
#: engine-owned response it is publishing. The turn worker uses the same
#: cadence; the reconciler treats a stamp older than 20s as an absent owner.
_RESIDENT_HEARTBEAT_INTERVAL_S = 5.0
#: While the platform still owns a turn on this conversation the observer
#: stays out of the stream and re-reads the platform's verdict at this
#: interval, so a turn settled without a consumer on this client does not
#: leave later engine-owned output unobserved.
_RESIDENT_RESTORE_INTERVAL_S = 30.0
_HANDOFF = object()
#: Control frames a consumer hands to the resident observation while the
#: stream has not yet reached that consumer's own input receipt.
_PRE_BOUNDARY_CONTROL_OPS = frozenset({"interaction", "turn_interrupted", "error", "gap"})
#: The vendor's own in-band word for a run ended by interrupt, documented on
#: ``claude_agent_sdk.types.ResultMessage.terminal_reason``.
_ABORTED_TERMINAL_REASONS = frozenset({"aborted_streaming", "aborted_tools"})


@dataclass
class _ResidentResponse:
    """One engine-owned response the observer is currently following.

    ``handle`` is ``None`` when the platform already settled this response
    and the frames are a replay: they are translated for translator state
    and never published again. ``after_sequence`` is the runner sequence the
    platform has already journaled through; frames at or before it rebuild
    the translator silently. ``boundary_sequence`` is the runner sequence of
    the first root activity that opened the response; a replayed frame
    before it belongs to something older and never enters this translator.
    """

    response_id: str
    handle: ResidentResponseHandle | None
    cursor: ClaudeStreamCursor
    boundary_sequence: int
    after_sequence: int | None = None
    raw_messages: list[dict[str, Any]] = field(default_factory=list)
    #: The tool call the model most recently declared; a permission gate
    #: that omits its tool_use_id belongs to it (same rule as the consumer).


class ClaudeCodeEngineClient:
    """One instance per (session, box); the platform holds it on the runtime."""

    def __init__(
        self,
        link: RunnerLink,
        *,
        session_id: str,
        transcript_store: SessionStore,
        workspace_dir: str,
        resume_session_key: str | None = None,
        resident_output_sink: ResidentOutputSink | None = None,
    ) -> None:
        self._link = link
        self._session_id = session_id
        self._transcript_store = transcript_store
        self._resident_output_sink = resident_output_sink
        self._workspace_dir = str(workspace_dir or "").strip()
        if not self._workspace_dir:
            raise ValueError(
                "Claude child transcript reads require the runtime workspace directory"
            )
        self._resume_session_key = str(resume_session_key or "").strip() or None
        self._child_identities = ClaudeChildIdentities()
        self._child_resource_lock = asyncio.Lock()
        self._child_resource_frames: dict[str, dict[str, Any]] = {}
        self._pending_interaction_id: str | None = None
        self._answered_interaction_ids: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._active_receipt: EngineTurnReceipt | None = None
        self._delivery_sequence = 0
        self._receipts: dict[str, EngineTurnReceipt] = {}
        self._pending_input_ids: deque[str] = deque()
        # Link-local replay boundary, deliberately separate from durable input
        # consumption.  A replacement host may know an input was consumed and
        # still have to skip an older retained Result until it observes that
        # input's root UserMessage.  Conversely, answering a live interaction
        # proves this consumer is already beyond the root boundary even when
        # the replacement host never saw the original UserMessage.
        self._stream_boundary_turn_ids: set[str] = set()
        # One reader of the link at a time. A consumer names itself here and
        # waits for the observer to step out; the observer reads only while
        # nobody owns the stream and no platform input is in flight.
        self._stream_owner: str | None = None
        self._handoff_requested = asyncio.Event()
        self._observer_idle = asyncio.Event()
        self._observer_idle.set()
        self._observer_wake = asyncio.Event()
        self._observer_task: asyncio.Task[None] | None = None
        # Frames the observer read but that belong to the next consumer: the
        # receipt of a platform input it saw first. Read before the link.
        self._pushback: deque[dict[str, Any]] = deque()
        self._resident: _ResidentResponse | None = None
        self._resident_heartbeat: asyncio.Task[None] | None = None
        # A replayed platform turn is in flight: skip its frames until its
        # Result; they were published by the worker that owned that turn.
        self._external_active = False
        # The platform still owns a turn nobody on this client is consuming
        # (a reattach during recovery). Stay out until a consumer finishes or
        # the platform's verdict changes.
        self._await_consumer = False
        self._closed = False

    @property
    def is_live(self) -> bool:
        """The link's own verdict — see :meth:`RunnerLink.is_live`."""
        return self._link.is_live

    @property
    def engine_session_key(self) -> str | None:
        return self._resume_session_key

    @property
    def active_receipt(self) -> EngineTurnReceipt | None:
        """The receipt of the turn currently holding the engine stream, or
        ``None`` between turns. The answer continuation resumes this receipt:
        the original consumer parked at the interaction boundary and exited,
        and the continuation segment re-enters the same stream."""
        return self._active_receipt

    async def bind_conversation(
        self,
        binding: EngineConversationBinding,
    ) -> None:
        """Prove that the configured runner resumed this conversation."""

        if binding.platform_session_id != self._session_id:
            raise RuntimeError(
                "Claude runner conversation identity mismatch: "
                f"client={self._session_id!r} "
                f"binding={binding.platform_session_id!r}"
            )
        durable_key = str(binding.engine_session_key or "").strip() or None
        if durable_key != self._resume_session_key:
            raise RuntimeError(
                "Claude runner resume key does not match the durable conversation: "
                f"configured={self._resume_session_key!r} durable={durable_key!r}"
            )

    @staticmethod
    def _runner_command(command: EngineInputCommand) -> RunnerDeliveryCommand:
        return RunnerDeliveryCommand(
            command_id=command.command_id,
            session_id=command.session_id,
            sequence=command.sequence,
            sdk_input={
                "type": "user",
                # Blocks when the input has some, so the model sees the image
                # the user pasted; the vendor reads both shapes on this field.
                "message": {
                    "role": "user",
                    "content": command.content_blocks or command.content,
                },
                "parent_tool_use_id": None,
                "session_id": command.session_id,
                "uuid": command.input_id,
            },
        )

    async def submit(self, command: EngineInputCommand) -> EngineTurnReceipt:
        existing = self._receipts.get(command.command_id)
        if existing is not None:
            return existing
        input_id = self._input_id(command)
        self._delivery_sequence = max(self._delivery_sequence, command.sequence)
        self._pending_input_ids.append(input_id)
        try:
            await self._link.deliver(self._runner_command(command))
        except BaseException:
            if input_id in self._pending_input_ids:
                self._pending_input_ids.remove(input_id)
            self._observer_wake.set()
            raise
        receipt = self._receipt(
            command,
            input_id=input_id,
            input_consumed=False,
        )
        self._receipts[command.command_id] = receipt
        return receipt

    @staticmethod
    def _input_id(command: EngineInputCommand) -> str:
        input_id = str(command.input_id or "").strip()
        try:
            return str(uuid.UUID(input_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("SDK input uuid must be a valid UUID") from exc

    def _receipt(
        self,
        command: EngineInputCommand,
        *,
        input_id: str,
        input_consumed: bool,
    ) -> EngineTurnReceipt:
        return EngineTurnReceipt(
            engine_turn_id=command.command_id,
            # The engine's own session key is the SDK conversation id, which
            # is unknown until the SDK reports it on the terminal
            # (ResultMessage.session_id → persisted by engine_turn). The
            # platform session id is not a valid resume key for an SDK
            # conversation file.
            engine_session_key=self._resume_session_key,
            started_at_monotonic_ns=time.monotonic_ns(),
            input_id=input_id,
            input_consumed=input_consumed,
        )

    async def deliver(self, command: EngineInputCommand) -> None:
        await self.submit(command)

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        if consumption_confirmed:
            input_id = self._input_id(command)
            self._delivery_sequence = max(
                self._delivery_sequence,
                command.sequence,
            )
            receipt = self._receipts.get(command.command_id)
            if receipt is None or receipt.input_consumed is not True:
                receipt = self._receipt(
                    command,
                    input_id=input_id,
                    input_consumed=True,
                )
                self._receipts[command.command_id] = receipt
            # Durable consumption says the SDK already accepted this input, so
            # it must not be delivered again. It does not say this host has
            # reached that input's UserMessage in the runner replay. Keep the
            # UUID as the stream boundary; otherwise an old retained Result can
            # terminate the new turn immediately after a host restart.
            if input_id not in self._pending_input_ids:
                self._pending_input_ids.append(input_id)
        else:
            receipt = await self.submit(command)
        self._active_receipt = receipt
        return receipt

    async def iter_turn_events(
        self,
        receipt: EngineTurnReceipt,
    ) -> AsyncIterator[EngineTurnEmission]:
        async for frame in self._iter_translated_frames(receipt):
            yield emission_from_translated_frame(frame)

    async def _iter_translated_frames(
        self,
        receipt: EngineTurnReceipt,
    ) -> AsyncIterator[dict[str, Any]]:
        await self._claim_stream(receipt)
        try:
            async for frame in self._iter_claimed_frames(receipt):
                yield frame
        finally:
            self._release_stream()

    async def _claim_stream(self, receipt: EngineTurnReceipt) -> None:
        """Take the link's read position from the resident observer.

        A consumer whose receipt names the response the observer is following
        is that response's own continuation — the AnswerInteraction worker
        re-entering the stream after a gate the observer parked it on. From
        here the platform turn owns the response to its terminal; the observer
        stops following it and never settles it.
        """

        self._stream_owner = "consumer"
        self._handoff_requested.set()
        await self._observer_idle.wait()
        resident = self._resident
        if resident is not None and resident.response_id == receipt.engine_turn_id:
            self._resident = None
            await self._stop_resident_heartbeat()
            logger.info(
                "resident response adopted by its platform continuation: "
                "session=%s response=%s",
                self._session_id,
                resident.response_id,
            )

    def _release_stream(self) -> None:
        self._stream_owner = None
        self._handoff_requested.clear()
        self._await_consumer = False
        self._observer_wake.set()

    async def _consumer_frames(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            if self._pushback:
                yield self._pushback.popleft()
                continue
            frame = await self._link.next_frame()
            if frame is None:
                return
            yield frame

    def _observe_native_session_key(self, message: dict[str, Any]) -> None:
        observed_session_key = str(message.get("session_id") or "").strip()
        if not observed_session_key and isinstance(message.get("data"), dict):
            observed_session_key = str(message["data"].get("session_id") or "").strip()
        if not observed_session_key:
            return
        if (
            self._resume_session_key is not None
            and self._resume_session_key != observed_session_key
        ):
            raise RuntimeError(
                "Claude SDK changed native conversation identity: "
                f"expected={self._resume_session_key!r} "
                f"actual={observed_session_key!r}"
            )
        self._resume_session_key = observed_session_key

    async def _iter_claimed_frames(
        self,
        receipt: EngineTurnReceipt,
    ) -> AsyncIterator[dict[str, Any]]:
        # Per-turn live-stream state (in-memory only): block ids for token
        # streaming, and which tool calls already streamed so the complete
        # AssistantMessage doesn't double-render them.
        cursor = ClaudeStreamCursor(child_identities=self._child_identities)
        raw_turn_messages: list[dict[str, Any]] = []
        input_boundary_observed = receipt.engine_turn_id in self._stream_boundary_turn_ids
        receipt_input_id = str(receipt.input_id or "").strip()
        async for frame in self._consumer_frames():
            op = frame.get("op")
            if not input_boundary_observed and op in _PRE_BOUNDARY_CONTROL_OPS:
                # Who is reading the link does not decide whose output this
                # is. A gate raised before this turn's own receipt belongs to
                # the response the engine is still producing, not to an input
                # the vendor has not taken yet; the same observation that
                # publishes that response parks it. The runner's error and gap
                # reach both: they end the observed response and this turn.
                await self._observe_frame(frame, from_consumer=True)
                if op == "interaction":
                    continue
            if op == "event":
                message = frame.get("message")
                if not isinstance(message, dict):
                    # Non-dataclass SDK object serialized as repr — a runner
                    # serializer gap. Surface it, don't guess.
                    yield _raw(frame, subtype="unserialized_event")
                    continue
                self._observe_native_session_key(message)
                if frame.get("message_type") == "ResultMessage":
                    # A Result is where the store coordinate exists, so a
                    # Result without one is a contract violation rather than a
                    # field to default. Defaulting would publish 0 as "nothing
                    # is durable yet", which reads as a legitimate lower bound
                    # and would let a consumer trim from the start of the
                    # journal. RunnerLink accepted this connection's exact
                    # protocol before routing replay, so this check governs only
                    # the Result shape.
                    require_result_history_checkpoint(frame)
                if frame.get("message_type") == "UserMessage":
                    parent_tool_use_id = message.get("parent_tool_use_id")
                    input_id = str(message.get("uuid") or "").strip()
                    if parent_tool_use_id is None and isinstance(message.get("content"), str):
                        try:
                            input_id = str(uuid.UUID(input_id))
                        except (AttributeError, TypeError, ValueError):
                            pass
                        if input_id in self._pending_input_ids:
                            # A queued platform input reached the engine
                            # under this receipt's FIFO stream.
                            self._pending_input_ids.remove(input_id)
                            input_boundary_observed = True
                        if input_id == receipt_input_id:
                            input_boundary_observed = True
                            self._stream_boundary_turn_ids.add(receipt.engine_turn_id)
                if not input_boundary_observed:
                    # Before this turn's own root input the stream is the
                    # engine's: the retained suffix of an earlier turn, or a
                    # response the engine started on its own that this
                    # platform input is queued behind. The official root UUID
                    # is the consumption boundary; Result ordering is not an
                    # input-correlation signal. The resident observation
                    # publishes engine-owned output under its own address;
                    # this turn publishes nothing before its boundary.
                    await self._observe_frame(frame, from_consumer=True)
                    continue
                raw_turn_messages.append(dict(message))
                for translated in translate_claude_sdk_message(
                    message, envelope_seq=int(frame.get("seq") or 0), cursor=cursor
                ):
                    if translated.get("type") == "result":
                        # Cancellation is decided by the vendor's own in-band
                        # answer when the CLI reports one: `terminal_reason`
                        # of `aborted_streaming` / `aborted_tools` means the
                        # run was cancelled via interrupt (documented on
                        # ``claude_agent_sdk.types.ResultMessage.terminal_reason``)
                        # — authoritative even for an interrupt this client did
                        # not issue. The local `_cancel_requested` set stays as
                        # the fallback for a CLI too old to report one.
                        aborted = str(translated.get("terminal_reason") or "") in (
                            "aborted_streaming",
                            "aborted_tools",
                        )
                        if aborted or receipt.engine_turn_id in self._cancel_requested:
                            translated = {**translated, "finishReason": "cancelled"}
                            translated.pop("error", None)
                        if self._pending_input_ids:
                            self._cancel_requested.discard(receipt.engine_turn_id)
                            yield {
                                "type": "response-result",
                                "data": claude_result_data(message),
                            }
                            cursor = ClaudeStreamCursor(child_identities=self._child_identities)
                            # Until the queued input's own receipt the stream
                            # is the engine's again: a task notification the
                            # CLI dequeues first is answered before the queued input
                            # and published under its own address.
                            input_boundary_observed = False
                            continue
                        if self._active_receipt is receipt:
                            self._active_receipt = None
                        self._answered_interaction_ids.clear()
                        background_tasks = build_background_task_manifest(raw_turn_messages)
                        if background_tasks is not None:
                            yield {
                                "type": "background-tasks-opened",
                                "manifest": background_tasks,
                            }
                        yield {
                            "type": "data-result",
                            "data": claude_result_data(message),
                        }
                        self._stream_boundary_turn_ids.discard(receipt.engine_turn_id)
                        yield translated
                        return
                    yield translated
            elif op == "interaction":
                interaction_id = str(frame.get("interaction_id") or "")
                if interaction_id in self._answered_interaction_ids:
                    logger.info(
                        "dropping an answered interaction replay: session=%s interaction=%s",
                        self._session_id,
                        interaction_id,
                    )
                    continue
                self._pending_interaction_id = interaction_id
                tool_input_raw = frame.get("tool_input")
                contract = build_claude_interaction_contract(
                    tool_name=str(frame.get("tool_name") or "").strip(),
                    input_payload=(
                        dict(tool_input_raw) if isinstance(tool_input_raw, dict) else {}
                    ),
                )
                yield {
                    "type": "interaction.request",
                    "interactionId": self._pending_interaction_id,
                    "payload": {
                        **contract,
                        "tool_use_id": frame.get("tool_use_id"),
                    },
                }
            elif op == "error":
                # The runner's in-band failure report — the pump died, and the
                # SDK's own exception class rides as the code (ProcessError,
                # CLIJSONDecodeError and CLINotFoundError call for three
                # different operator actions; a bare string would collapse
                # them into one).
                if self._active_receipt is receipt:
                    self._active_receipt = None
                self._answered_interaction_ids.clear()
                self._stream_boundary_turn_ids.discard(receipt.engine_turn_id)
                yield {
                    "type": "result",
                    "finishReason": "error",
                    "error": {
                        "code": str(frame.get("error_class") or "RUNNER_ERROR"),
                        "message": str(frame.get("detail") or "runner reported an error"),
                    },
                }
                return
            elif op == "gap":
                # The journal is a disposable live cache. Ask the browser to
                # rebuild from SessionStore and end this segment. The missing
                # interval is never manufactured from the retained suffix.
                yield session_store_reload_frame(frame)
                raise EngineStreamDetached(
                    "runner journal cursor expired; SessionStore rebuild required"
                )
            elif op == "turn_interrupted":
                # Claude Code can acknowledge interrupt after it has written
                # its local transcript without emitting another ResultMessage.
                # The runner's correlated control event is the terminal fact
                # in that race; a different command's replay is not.
                if str(frame.get("command_id") or "") != receipt.engine_turn_id:
                    continue
                continues_fifo = frame.get("continues_fifo")
                if not isinstance(continues_fifo, bool):
                    raise RunnerLinkError("runner interrupt acknowledgement has no FIFO verdict")
                if continues_fifo:
                    # The interrupt ended only the SDK response that was active
                    # when it was requested. A platform input already queued in
                    # the resident SDK belongs to the same native FIFO stream;
                    # keep reading until that successor reaches its own Result.
                    self._cancel_requested.discard(receipt.engine_turn_id)
                    self._pending_interaction_id = None
                    self._answered_interaction_ids.clear()
                    cursor = ClaudeStreamCursor(child_identities=self._child_identities)
                    continue
                if self._active_receipt is receipt:
                    self._active_receipt = None
                self._cancel_requested.discard(receipt.engine_turn_id)
                self._answered_interaction_ids.clear()
                self._stream_boundary_turn_ids.discard(receipt.engine_turn_id)
                yield {"type": "result", "finishReason": "cancelled"}
                return
            elif op in ("status", "input_ack"):
                continue
            else:
                yield _raw(frame, subtype=f"unknown_op:{op}")
        # Link closed without a terminal. For a cancelled turn that is the
        # expected shape (the interrupt tears the stream down), so settle it.
        # For anything else the turn did not end — the box may still be
        # running it — so this is a transport statement, raised as one, never
        # a synthesized result: a result frame here would become a durable
        # `turn.failed` written by a SIGTERM'd worker off its own closing
        # link, leaving the restarted process nothing to recover.
        if self._active_receipt is receipt:
            self._active_receipt = None
        if receipt.engine_turn_id in self._cancel_requested:
            self._stream_boundary_turn_ids.discard(receipt.engine_turn_id)
            yield {
                "type": "result",
                "finishReason": "cancelled",
            }
            return
        raise EngineStreamDetached("runner link closed before the turn's result frame")

    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        self._cancel_requested.add(receipt.engine_turn_id)
        await self._link.interrupt(receipt.engine_turn_id)
        return True

    async def stop_child_run(self, control_id: str) -> None:
        await self._link.stop_task(control_id)

    async def reconcile_child_resources(self) -> list[ChildResourceFact | PrivateDiagnostic]:
        """Publish only new or changed native child transcript facts.

        Store reads return complete history, not an event delta. Serialize
        observations as the DSH projector does so concurrent readers cannot
        repeatedly invalidate one another with unchanged history.
        """
        async with self._child_resource_lock:
            facts = await self._read_child_resources()
            changed: list[ChildResourceFact | PrivateDiagnostic] = []
            for fact in facts:
                frame = fact.as_frame()
                identity = str(frame["id"])
                if self._child_resource_frames.get(identity) != frame:
                    changed.append(fact)
                    self._child_resource_frames[identity] = frame
            return changed

    async def _read_child_resources(self) -> list[ChildResourceFact]:
        """Read current child messages through Claude's public Store reader.

        Messages are observable before tools finish. Lifecycle still comes
        from native task events, never from a transcript's last stop reason.
        """
        native_session_id = self._resume_session_key
        if native_session_id is None:
            return []
        agent_ids = await list_subagents_from_store(
            self._transcript_store, native_session_id, directory=self._workspace_dir
        )
        facts: list[ChildResourceFact] = []
        for agent_id in agent_ids:
            messages = await get_subagent_messages_from_store(
                self._transcript_store,
                native_session_id,
                agent_id,
                directory=self._workspace_dir,
            )
            for message in messages:
                if message.parent_tool_use_id:
                    self._child_identities.bind(message.parent_tool_use_id, agent_id)
                if message.parent_agent_id:
                    self._child_identities.bind_parent(agent_id, message.parent_agent_id)
                raw = {
                    "type": message.type,
                    "uuid": message.uuid,
                    "message": message.message,
                    "parent_tool_use_id": message.parent_tool_use_id,
                }
                for block in parse_tool_event_blocks(
                    raw,
                    identities=self._child_identities,
                    child_engine_ref=agent_id,
                ):
                    if block.get("type") != "subagent":
                        raise RuntimeError("Claude child Store reader emitted a non-child block")
                    facts.append(
                        ChildResourceFact(
                            session_scoped_engine_frame(
                                {"type": "data-subagent", "id": block["id"], "data": block["data"]}
                            )
                        )
                    )
        return facts

    async def set_permission_mode(self, mode: str) -> None:
        await self._link.set_permission_mode(mode)

    async def get_server_info(self) -> dict[str, Any] | None:
        return await self._link.get_init_info()

    async def interrupt_active_turn(self) -> bool:
        # The session-level interrupt entry (see the EngineClient protocol):
        # the receipt lives with the generator that started the turn, so the
        # platform's interrupt command cannot present one. No active turn is
        # a no-op — the interrupt raced a turn that already settled.
        receipt = self._active_receipt
        if receipt is None:
            resident = self._resident
            if resident is None or resident.handle is None:
                return False
            # A stop aimed at the response the engine is producing on its
            # own. Its address is the runner's handle for it too; the vendor
            # answers with its aborted Result, or the runner with the
            # correlated control event, and the observer closes the interval.
            await self._link.interrupt(resident.response_id)
            return True
        return await self.cancel_turn(receipt)

    async def submit_interaction_response(
        self,
        receipt: EngineTurnReceipt,
        *,
        pending: dict[str, Any],
        response: dict[str, Any],
    ) -> bool:
        # The caller's pending record holds the interaction id it is
        # answering; this client must not re-derive it from
        # `_pending_interaction_id`, which only a process that observed the
        # gate open ever has. After a restart the reattached client has none,
        # so re-deriving it refuses every answer without so much as contacting
        # the box, surfacing as INTERACTION_EXPIRED with no trace of the
        # answer in the box's own log.
        interaction_id = str(pending.get("interaction_id") or "").strip()
        if not interaction_id:
            return False
        choice, updated_input, message = claude_answer_fields(pending, response)
        if choice == "allow":
            # updated_input carries approve-with-changes and AskUserQuestion
            # answer sets alike — the runner's PreToolUse hook passes it to
            # the SDK as the tool's effective input.
            accepted = await self._link.answer(interaction_id, "allow", updated_input=updated_input)
        else:
            accepted = await self._link.answer(
                interaction_id, "deny", message=message or "user denied"
            )
        if accepted:
            # A host restart attaches from the runner journal before this
            # process answers the durable gate. The replay can therefore still
            # contain the interaction frame that this acknowledgement just
            # resolved. Keep its identity until the turn terminal so the
            # continuation consumes new output instead of parking on the old
            # question again. A terminal clears the set; until then it may
            # contain more than one answered gate from the same turn.
            self._answered_interaction_ids.add(interaction_id)
            self._stream_boundary_turn_ids.add(receipt.engine_turn_id)
        self._pending_interaction_id = None
        return accepted

    async def get_capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind="claude_code",
            # The pinned Claude input protocol accepts Anthropic image blocks;
            # _runner_command below is the adapter that carries them into it.
            input_content_types=["text", "image"],
            # Read from the pinned SDK, never transcribed: the vendor's set
            # grows without notice, and a copy kept in this tree would drift
            # silently the first time it does.
            permission_modes=list(get_args(PermissionMode)),
            # The two SDK tools whose calls change a file, under the names
            # the SDK gives them.
            supports_interaction=True,
            supports_child_run_control=True,
            supports_server_info=True,
        )

    async def close(self) -> None:
        self._closed = True
        self._stream_boundary_turn_ids.clear()
        self._observer_wake.set()
        self._handoff_requested.set()
        await self._stop_resident_heartbeat()
        observer = self._observer_task
        self._observer_task = None
        if observer is not None and not observer.done():
            observer.cancel()
            await asyncio.wait({observer})
        await self._link.close()

    # ── resident observation ─────────────────────────────────────────────
    #
    # The runner keeps ``receive_messages()`` running after every Result and
    # journals whatever Claude does next. Nothing platform-side reads that
    # journal between turns unless this observer does; the platform sink it
    # publishes through is the same journal, projection and follower a turn
    # consumer's frames reach.

    def start_resident_observation(self) -> None:
        """Begin reading the link between turns. Idempotent per client."""

        if self._observer_task is not None or self._closed:
            return
        if self._resident_output_sink is None:
            logger.warning(
                "resident observation disabled: no resident output sink for "
                "session=%s; output the engine produces between turns will "
                "not be published",
                self._session_id,
            )
            return
        self._observer_task = asyncio.create_task(
            self._observe_resident_output(),
            name=f"claude-resident-observer-{self._session_id}",
        )
        self._observer_task.add_done_callback(self._log_observer_exit)

    def _log_observer_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "resident observation stopped on error session=%s: %s",
                self._session_id,
                exc,
                exc_info=exc,
            )

    def _observer_gate_closed(self) -> bool:
        return (
            self._closed
            or self._await_consumer
            or self._stream_owner is not None
            or self._active_receipt is not None
            or bool(self._pending_input_ids)
        )

    async def _observe_resident_output(self) -> None:
        await self._restore_resident_output()
        while not self._closed:
            if self._observer_gate_closed():
                self._observer_wake.clear()
                if self._observer_gate_closed():
                    if self._await_consumer:
                        try:
                            await asyncio.wait_for(
                                self._observer_wake.wait(),
                                timeout=_RESIDENT_RESTORE_INTERVAL_S,
                            )
                        except TimeoutError:
                            await self._restore_resident_output()
                    else:
                        await self._observer_wake.wait()
                continue
            self._observer_idle.clear()
            try:
                while not self._observer_gate_closed():
                    frame = await self._next_frame_or_handoff()
                    if frame is _HANDOFF:
                        break
                    if frame is None:
                        # The link is dead. The runtime manager evicts this
                        # client; whatever response was open is restored by
                        # the client that replaces it.
                        return
                    verdict = await self._observe_frame(frame, from_consumer=False)
                    if verdict == "await_consumer":
                        self._await_consumer = True
                        break
            finally:
                self._observer_idle.set()

    async def _next_frame_or_handoff(self) -> Any:
        if self._handoff_requested.is_set():
            return _HANDOFF
        if self._pushback:
            return self._pushback.popleft()
        frame_task: asyncio.Task[Any] = asyncio.create_task(self._link.next_frame())
        handoff_task: asyncio.Task[Any] = asyncio.create_task(
            self._handoff_requested.wait()
        )
        try:
            done, _pending = await asyncio.wait(
                {frame_task, handoff_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            # Cancelled from outside while both waits were pending. Neither
            # may outlive this call: a read left running would take a frame
            # nobody hands on.
            frame_task.cancel()
            handoff_task.cancel()
            await asyncio.wait({frame_task, handoff_task})
            self._reclaim_frame(frame_task)
            raise
        if not handoff_task.done():
            handoff_task.cancel()
            await asyncio.wait({handoff_task})
        if frame_task in done:
            return frame_task.result()
        # A handoff arrived while the read was pending. Cancelling a read
        # that has not returned leaves the frame in the link; a read that
        # completed meanwhile is kept for the next reader.
        frame_task.cancel()
        await asyncio.wait({frame_task})
        self._reclaim_frame(frame_task)
        return _HANDOFF

    def _reclaim_frame(self, frame_task: asyncio.Task[Any]) -> None:
        if not frame_task.done() or frame_task.cancelled() or frame_task.exception() is not None:
            return
        frame = frame_task.result()
        if frame is not None:
            self._pushback.append(frame)

    async def _restore_resident_output(self) -> None:
        sink = self._resident_output_sink
        assert sink is not None
        checkpoint = await sink.restore_resident_output(engine_kind="claude_code")
        self._await_consumer = checkpoint.external_turn_active
        response_id = str(checkpoint.open_response_id or "").strip()
        if not response_id:
            return
        if self._resident is not None and self._resident.response_id == response_id:
            return
        if checkpoint.after_sequence is None or checkpoint.boundary_sequence is None:
            raise RuntimeError(
                "platform reports an open resident response with no journaled "
                f"output: session={self._session_id} response={response_id}"
            )
        # Translator state comes from what the platform already journaled for
        # this response, never from the runner's retained prefix: a complete
        # AssistantMessage must not re-render text that streamed and was
        # published, a tool that already opened must not open twice, and a
        # delta for a block still open lands on the id the platform holds.
        cursor = rebuild_claude_stream_cursor(
            checkpoint.committed_frames,
            child_identities=self._child_identities,
        )
        self._resident = _ResidentResponse(
            response_id=response_id,
            handle=ResidentResponseHandle(response_id=response_id, owns_slot=True),
            cursor=cursor,
            boundary_sequence=int(checkpoint.boundary_sequence),
            after_sequence=int(checkpoint.after_sequence),
        )
        self._start_resident_heartbeat()
        logger.info(
            "resident observation resumed: session=%s response=%s after_sequence=%s",
            self._session_id,
            response_id,
            checkpoint.after_sequence,
        )

    async def _observe_frame(
        self,
        frame: dict[str, Any],
        *,
        from_consumer: bool,
    ) -> str | None:
        """Apply one link frame to the resident observation.

        Returns ``"await_consumer"`` when the frame is the receipt of a
        platform input this client is delivering: the observer puts it back
        and steps out until that input's consumer has taken the stream.
        """

        op = frame.get("op")
        if op == "event":
            return await self._observe_event(frame, from_consumer=from_consumer)
        if op == "error":
            self._external_active = False
            if self._resident is not None:
                await self._close_resident_response(
                    {
                        "type": "result",
                        "finishReason": "error",
                        "error": {
                            "code": str(frame.get("error_class") or "RUNNER_ERROR"),
                            "message": str(
                                frame.get("detail") or "runner reported an error"
                            ),
                        },
                    },
                    engine_sequence_number=int(frame.get("seq") or 0),
                )
            return None
        if op == "gap":
            # The runner compacted a prefix this client never saw. Anything
            # the platform journaled before is the authority the translator
            # was restored from; a delta whose start fell in the hole opens
            # its own block. Missing native lines remain in the vendor
            # transcript the platform mirrors.
            logger.warning(
                "resident observation crossed a compacted runner prefix "
                "session=%s after=%s first_retained=%s open_response=%s",
                self._session_id,
                frame.get("after_sequence"),
                frame.get("first_retained_sequence"),
                self._resident.response_id if self._resident is not None else None,
            )
            return None
        if op == "turn_interrupted":
            resident = self._resident
            if (
                resident is not None
                and str(frame.get("command_id") or "") == resident.response_id
            ):
                # The runner's correlated control event for a stop aimed at
                # this response: Claude Code can acknowledge an interrupt
                # without a further ResultMessage, and this is the terminal
                # fact in that race.
                await self._close_resident_response(
                    {"type": "result", "finishReason": "cancelled"},
                    engine_sequence_number=int(frame.get("seq") or 0),
                )
            elif frame.get("continues_fifo") is not True:
                self._external_active = False
            return None
        if op == "interaction":
            await self._observe_interaction(frame)
        return None

    async def _observe_interaction(self, frame: dict[str, Any]) -> None:
        """Park an engine-owned response on the platform's interaction path.

        The gate is the same vendor PreToolUse / AskUserQuestion wait a
        platform turn raises, so it takes the same route: the same pending
        record, ``turn.awaiting_interaction`` event, WAITING_FOR_INTERACTION
        snapshot and approval frames. The user answers it through the same
        command, and that command's continuation consumer claims this stream
        with the response id as its engine turn id (see ``_claim_stream``).
        """

        interaction_id = str(frame.get("interaction_id") or "").strip()
        if interaction_id in self._answered_interaction_ids:
            logger.info(
                "dropping an answered interaction replay: session=%s interaction=%s",
                self._session_id,
                interaction_id,
            )
            return
        resident = self._resident
        if resident is None or resident.handle is None:
            # No response is open on this observer — the gate belongs to a
            # replayed turn some worker owned, or to a response the platform
            # already settled. Nothing here can park it a second time.
            logger.warning(
                "interaction observed outside any open engine-owned response "
                "session=%s interaction=%s tool=%s",
                self._session_id,
                interaction_id,
                frame.get("tool_name"),
            )
            return
        sink = self._resident_output_sink
        assert sink is not None
        tool_input_raw = frame.get("tool_input")
        contract = build_claude_interaction_contract(
            tool_name=str(frame.get("tool_name") or "").strip(),
            input_payload=dict(tool_input_raw) if isinstance(tool_input_raw, dict) else {},
        )
        parked = await sink.open_resident_interaction(
            resident.handle,
            interaction_id=interaction_id,
            contract={**contract, "tool_use_id": frame.get("tool_use_id")},
            engine_session_key=self._resume_session_key,
            engine_sequence_number=int(frame.get("seq") or 0),
        )
        self._pending_interaction_id = interaction_id
        logger.info(
            "resident response parked on an interaction: session=%s response=%s "
            "interaction=%s tool=%s waiting=%s",
            self._session_id,
            resident.response_id,
            interaction_id,
            frame.get("tool_name"),
            parked,
        )

    async def _observe_event(
        self,
        frame: dict[str, Any],
        *,
        from_consumer: bool,
    ) -> str | None:
        message = frame.get("message")
        if not isinstance(message, dict):
            return None
        self._observe_native_session_key(message)
        sequence = int(frame.get("seq") or 0)
        message_type = str(frame.get("message_type") or "")
        resident = self._resident
        if (
            resident is not None
            and resident.after_sequence is not None
            and sequence < resident.boundary_sequence
        ):
            # Replay older than the restored response: whatever it was, it
            # settled before this response opened and never enters its
            # translator.
            resident = None
        is_input_receipt = (
            message_type == "UserMessage"
            and message.get("parent_tool_use_id") is None
            and isinstance(message.get("content"), str)
        )
        if is_input_receipt:
            # The only root string UserMessage this SDK emits is a platform
            # input's receipt (the runner drops its own). A queued task
            # notification is not one: the engine answers it without echoing
            # a prompt, and its response is recognised below by its first
            # root assistant activity.
            input_id = str(message.get("uuid") or "").strip()
            with contextlib.suppress(AttributeError, TypeError, ValueError):
                input_id = str(uuid.UUID(input_id))
            if not from_consumer and (
                input_id in self._pending_input_ids or self._active_receipt is not None
            ):
                self._pushback.appendleft(frame)
                return "await_consumer"
            # A platform turn this client did not consume: replayed, or owned
            # by a worker that is gone. Its worker published it; skip to its
            # Result.
            self._child_identities.observe(message, include_messages=False)
            self._external_active = True
            return None
        if self._external_active:
            self._child_identities.observe(message, include_messages=False)
            if message_type == "ResultMessage":
                self._external_active = False
            return None
        if frame.get("engine_boundary") is True and self._resident is None:
            # The runner's declaration that a root assistant response began
            # with nothing platform-owned in flight: the first root
            # `message_start`, or the complete AssistantMessage when partial
            # events are off. Open the response at it and translate this very
            # frame below — the boundary is the first model step, not a
            # prelude to it. A later `message_start` inside the response is
            # another step of the same response; the runner does not flag it.
            await self._open_resident_response(frame, message)
            resident = self._resident
        if resident is None:
            self._child_identities.observe(message, include_messages=False)
            return None

        if message_type == "ResultMessage":
            require_result_history_checkpoint(frame)
        resident.raw_messages.append(dict(message))
        publishable = resident.handle is not None and (
            resident.after_sequence is None or sequence > resident.after_sequence
        )
        emissions: list[EngineTurnEmission] = []
        terminal: dict[str, Any] | None = None
        for translated in translate_claude_sdk_message(
            message, envelope_seq=sequence, cursor=resident.cursor
        ):
            if translated.get("type") == "result":
                terminal = translated
                continue
            emission = emission_from_translated_frame(translated)
            if isinstance(emission, (InputConsumed, ResponseCompleted, InteractionRequested)):
                raise RuntimeError(
                    "engine-owned response translated a platform-input boundary: "
                    f"session={self._session_id} response={resident.response_id} "
                    f"frame={translated.get('type')!r}"
                )
            emissions.append(emission)
        if message_type == "ResultMessage":
            if terminal is None:
                raise RuntimeError(
                    "Claude Result translated without a terminal: "
                    f"session={self._session_id} response={resident.response_id}"
                )
            if str(terminal.get("terminal_reason") or "") in _ABORTED_TERMINAL_REASONS:
                # The vendor's own verdict that the run was stopped, which the
                # platform reads as cancelled — the same reading the turn
                # consumer gives a stopped platform turn.
                terminal = {**terminal, "finishReason": "cancelled"}
                terminal.pop("error", None)
            background_tasks = build_background_task_manifest(resident.raw_messages)
            if background_tasks is not None:
                emissions.append(
                    emission_from_translated_frame(
                        {"type": "background-tasks-opened", "manifest": background_tasks}
                    )
                )
            emissions.append(
                emission_from_translated_frame(
                    {"type": "data-result", "data": claude_result_data(message)}
                )
            )
        if publishable and emissions:
            sink = self._resident_output_sink
            assert sink is not None and resident.handle is not None
            await sink.publish_resident_output(
                resident.handle,
                emissions,
                engine_sequence_number=sequence,
            )
        if terminal is not None:
            await self._close_resident_response(
                terminal,
                engine_sequence_number=sequence,
            )
        return None

    async def _open_resident_response(
        self,
        frame: dict[str, Any],
        message: dict[str, Any],
    ) -> None:
        sink = self._resident_output_sink
        assert sink is not None
        # The address is the SDK envelope uuid of the first root activity —
        # the `message_start` StreamEvent's own uuid, or the AssistantMessage's
        # — the same value the runner recorded for a stop and the persister
        # used as the boundary event's causation.
        response_id = str(message.get("uuid") or "").strip()
        message_type = str(frame.get("message_type") or "").strip()
        if not response_id:
            raise RuntimeError(
                "engine-owned response boundary carries no SDK envelope uuid: "
                f"session={self._session_id} type={message_type!r}"
            )
        sequence = int(frame.get("seq") or 0)
        handle = await sink.open_resident_response(
            engine_kind="claude_code",
            response_id=response_id,
            engine_session_key=self._resume_session_key,
            causation_id=f"claude_code:{message_type}:{response_id}",
            native_message=dict(message),
            runner_sequence=sequence,
        )
        # The boundary frame is translated by the caller right after this,
        # so it enters ``raw_messages`` there, once.
        self._resident = _ResidentResponse(
            response_id=response_id,
            handle=handle,
            cursor=ClaudeStreamCursor(child_identities=self._child_identities),
            boundary_sequence=sequence,
        )
        if handle is not None:
            self._start_resident_heartbeat()
            logger.info(
                "resident observation opened: session=%s response=%s seq=%s owns_slot=%s",
                self._session_id,
                response_id,
                sequence,
                handle.owns_slot,
            )

    async def _close_resident_response(
        self,
        terminal_frame: dict[str, Any],
        *,
        engine_sequence_number: int,
    ) -> None:
        resident = self._resident
        self._resident = None
        await self._stop_resident_heartbeat()
        if resident is None or resident.handle is None:
            return
        terminal = emission_from_translated_frame(terminal_frame)
        if not isinstance(terminal, TurnTerminal):
            raise RuntimeError("engine-owned response ended without a turn terminal")
        sink = self._resident_output_sink
        assert sink is not None
        await sink.close_resident_response(
            resident.handle,
            terminal=terminal,
            engine_sequence_number=engine_sequence_number,
        )
        logger.info(
            "resident observation closed: session=%s response=%s outcome=%s seq=%s",
            self._session_id,
            resident.response_id,
            terminal.outcome,
            engine_sequence_number,
        )

    def _start_resident_heartbeat(self) -> None:
        resident = self._resident
        if resident is None or resident.handle is None or not resident.handle.owns_slot:
            return
        if self._resident_heartbeat is not None and not self._resident_heartbeat.done():
            return
        self._resident_heartbeat = asyncio.create_task(
            self._resident_heartbeat_loop(resident.handle),
            name=f"claude-resident-heartbeat-{self._session_id}",
        )

    async def _stop_resident_heartbeat(self) -> None:
        task = self._resident_heartbeat
        self._resident_heartbeat = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    async def _resident_heartbeat_loop(self, handle: ResidentResponseHandle) -> None:
        sink = self._resident_output_sink
        assert sink is not None
        while True:
            await asyncio.sleep(_RESIDENT_HEARTBEAT_INTERVAL_S)
            try:
                alive = await sink.heartbeat_resident_response(handle)
            except Exception as exc:  # noqa: BLE001 — a missed stamp is retried next tick
                logger.warning(
                    "resident heartbeat failed session=%s response=%s: %s",
                    self._session_id,
                    handle.response_id,
                    exc,
                )
                continue
            if not alive:
                logger.info(
                    "resident heartbeat stopped: the conversation slot moved on "
                    "session=%s response=%s",
                    self._session_id,
                    handle.response_id,
                )
                return


def _raw(frame: dict[str, Any], *, subtype: str) -> dict[str, Any]:
    return {
        "type": "data-raw-event",
        "data": {"event_type": "claude_code.runner", "subtype": subtype, "raw": dict(frame)},
    }
