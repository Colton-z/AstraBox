"""Claude Code behind the runtime contract (``engine/runtime.py``).

The in-box runner (``sandbox_runner.py``) holds Claude's SDK session and
journals everything it does as numbered frames. This client reads that
journal through one :class:`RunnerLink` and reports it as runtime events:

* A response starts where Claude takes a platform input (the root
  ``UserMessage`` the runner journals at consumption) or where the runner marks
  an ``engine_boundary`` (Claude answering on its own, e.g. a finished
  background task). It ends at Claude's ``ResultMessage``, or at the runner's
  ``turn_interrupted`` when an interrupt raced the transcript.
* An input is admitted when Claude takes it: it is then in Claude's own
  transcript, which a replacement runtime resumes.
* A cursor is the runner process's ``boot_id`` and a position ``(seq, index)``:
  the ``index``-th event translated from journal frame ``seq``. Translation
  depends only on the frames since the open response began, and the runner
  keeps every frame since then (it compacts only after a ``ResultMessage``), so
  reading the retained journal again and skipping through the position yields
  the same events after it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, get_args

from claude_agent_sdk import PermissionMode

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.engine.claude_child_runs import ClaudeChildIdentities
from astrabox.core.service.orchestrator.engine.claude_interaction_codec import (
    build_claude_interaction_contract,
    claude_answer_fields,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    InputConsumed,
    PrivateDiagnostic,
    TurnTerminal,
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    claude_result_data,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.engine.input_ids import input_response_message_id
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_tool_approval_request_frame,
    validate_interaction_contract,
)
from astrabox.core.service.orchestrator.engine.runner_link import DeliveryCommand, RunnerLink
from astrabox.core.service.orchestrator.engine.runtime import (
    InputAdmitted,
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
    RuntimeState,
    RuntimeStatus,
    StatusChanged,
)

logger = get_logger(__name__)

_ABORTED_TERMINAL_REASONS = frozenset({"aborted_streaming", "aborted_tools"})
#: A frame index past every event one frame can produce: "after this whole frame".
_WHOLE_FRAME = 1 << 30


def interaction_record(
    *, interaction_id: str, response_id: str, tool_use_id: str, tool_name: str, tool_input: Any
) -> dict[str, Any]:
    """The platform interaction record for one runner gate.

    Built only from the gate's own frame, so reading the frame again builds
    the same record.
    """
    contract = build_claude_interaction_contract(
        tool_name=tool_name, input_payload=dict(tool_input) if isinstance(tool_input, dict) else {}
    )
    validate_interaction_contract(contract)
    record: dict[str, Any] = {"interaction_id": interaction_id, "response_id": response_id, **contract}
    if tool_use_id:
        record["tool_call_id"] = tool_use_id
    return record


class _Translation:
    """Runner frames to runtime events, holding only what the open response needs."""

    def __init__(self) -> None:
        self.children = ClaudeChildIdentities()
        self.stream = ClaudeStreamCursor(child_identities=self.children)
        #: The open response and how to interrupt it (the runner's command or response id).
        self.response_id: str | None = None
        self.interrupt_target: str | None = None
        #: The open response's browser message was closed at an interaction and
        #: reopens with its next part.
        self.closed_for_interaction = False
        self.interactions: dict[str, dict[str, Any]] = {}
        self.engine_session_key: str | None = None

    def translate(self, frame: dict[str, Any]) -> list[RuntimeEvent]:
        op = frame.get("op")
        if op == "event":
            return self._event(frame)
        if op == "interaction":
            return self._interaction(frame)
        if op == "interaction_closed":
            interaction_id = str(frame.get("interaction_id") or "").strip()
            reason = frame.get("reason")
            if not interaction_id or reason not in ("answered", "expired"):
                raise ValueError(f"runner interaction closure is invalid: {frame!r}")
            self.interactions.pop(interaction_id, None)
            return [InteractionClosed(None, interaction_id, reason)]
        if op == "status":
            state = frame.get("state")
            if state == "busy":
                return [StatusChanged(None, "working")]
            if state == "idle":
                return [StatusChanged(None, "idle")]
            raise ValueError(f"runner status has an unknown state {state!r}")
        if op == "turn_interrupted":
            if self.response_id is None or frame.get("command_id") != self.interrupt_target:
                return []
            return self._end("cancelled", "interrupted", usage=None, error=None, parts=[_finish("other")])
        if op == "error":
            error = {"code": str(frame.get("error_class") or "RUNNER_ERROR"), "message": str(frame.get("detail") or "")}
            events: list[RuntimeEvent] = [RuntimeDiagnostic(None, "claude_code.runner_error", dict(frame))]
            if self.response_id is not None:
                events += self._end(
                    "failed", error["code"], usage=None, error=error,
                    parts=[{"type": "error", "errorText": error["message"] or error["code"]}, _finish("error")],
                )
            return events
        if op == "input_ack":
            return []
        return [RuntimeDiagnostic(None, f"claude_code.unknown_op.{op}", dict(frame))]

    # -- frames ------------------------------------------------------------

    def _event(self, frame: dict[str, Any]) -> list[RuntimeEvent]:
        message = frame.get("message")
        if not isinstance(message, dict):
            return [RuntimeDiagnostic(None, "claude_code.unserialized_event", dict(frame))]
        self._observe_session_key(message)
        greeting = _session_start_message(message)
        if greeting is not None:
            return self._greeting(*greeting)
        events: list[RuntimeEvent] = []
        if frame.get("engine_boundary") is True:
            response_id = str(message.get("uuid") or "").strip()
            events += self._start(response_id, "engine", (), interrupt_target=response_id)
        for translated in translate_claude_sdk_message(
            message, envelope_seq=int(frame["seq"]), cursor=self.stream
        ):
            emission = emission_from_translated_frame(translated)
            if isinstance(emission, InputConsumed):
                events += [InputAdmitted(None, emission.input_id)]
                events += self._start(
                    emission.response_message_id, "input", (emission.input_id,),
                    interrupt_target=emission.input_id,
                )
                events.append(ResponseOutput(None, self.response_id, emission.as_frame()))
            elif isinstance(emission, PrivateDiagnostic):
                events.append(RuntimeDiagnostic(None, f"{emission.event_type}.{emission.subtype}", emission.raw))
            elif isinstance(emission, TurnTerminal):
                events += self._result(message, translated)
            else:
                events += self._output(emission.as_frame())
        return events

    def _interaction(self, frame: dict[str, Any]) -> list[RuntimeEvent]:
        interaction_id = str(frame.get("interaction_id") or "").strip()
        events: list[RuntimeEvent] = []
        boundary = frame.get("engine_boundary") is True
        if boundary:
            resumed_id = str(frame.get("response_id") or "").strip()
            if not resumed_id or self.response_id is not None:
                raise ValueError(f"resumed tool boundary conflicts with the open response: {frame!r}")
            events += self._start(resumed_id, "engine", (), interrupt_target=resumed_id)
        response_id = self.response_id
        if not interaction_id or response_id is None:
            raise ValueError(f"runner interaction outside a response: {frame!r}")
        record = interaction_record(
            interaction_id=interaction_id,
            response_id=response_id,
            tool_use_id=str(frame.get("tool_use_id") or "").strip(),
            tool_name=str(frame.get("tool_name") or "").strip(),
            tool_input=frame.get("tool_input"),
        )
        self.interactions[interaction_id] = record
        parts = []
        if boundary:
            # The SDK can reissue this intent before assistant output on resume.
            parts.append({
                "type": "tool-input-available",
                "toolCallId": str(frame.get("tool_use_id") or ""),
                "toolName": str(frame.get("tool_name") or ""),
                "input": frame.get("tool_input"),
            })
        approval = build_tool_approval_request_frame(record)
        if approval is not None:
            parts.append(approval)
        parts += [{"type": "data-interaction", "data": dict(record)}, _finish("tool-calls")]
        # A second request while the first still waits arrives in a message
        # the first one closed.
        events += self._reopen()
        events.append(InteractionOpened(None, response_id, interaction_id, record))
        events += [ResponseOutput(None, response_id, part) for part in parts]
        events.append(StatusChanged(None, "waiting"))
        self.closed_for_interaction = True
        return events

    def _result(self, message: dict[str, Any], terminal: dict[str, Any]) -> list[RuntimeEvent]:
        if self.response_id is None:
            return [RuntimeDiagnostic(None, "claude_code.result_outside_response", dict(message))]
        terminal_reason = str(message.get("terminal_reason") or "").strip()
        finish_reason = str(terminal.get("finishReason") or "")
        outcome: ResponseOutcome
        if terminal_reason in _ABORTED_TERMINAL_REASONS:
            outcome, reason = "cancelled", "other"
        elif finish_reason == "stop":
            outcome, reason = "completed", "stop"
        else:
            outcome, reason = "failed", "error"
        error = terminal.get("error") if outcome == "failed" else None
        parts: list[dict[str, Any]] = [{"type": "data-result", "data": claude_result_data(message)}]
        if error:
            parts.append({"type": "error", "errorText": str(error.get("message") or error.get("code") or "")})
        parts.append(_finish(reason))
        events: list[RuntimeEvent] = []
        if terminal.get("__interaction_closed") is True:
            events += [
                InteractionClosed(None, interaction_id, "expired")
                for interaction_id, record in self.interactions.items()
                if record["response_id"] == self.response_id
            ]
        return events + self._end(
            outcome,
            terminal_reason or str(message.get("subtype") or "") or None,
            usage=message.get("usage") if isinstance(message.get("usage"), dict) else None,
            error=error if isinstance(error, dict) else None,
            parts=parts,
        )

    def _greeting(self, event_id: str, text: str) -> list[RuntimeEvent]:
        """A SessionStart hook's message for the person, as the engine's own response."""
        block = f"{event_id}:text"
        parts = [
            {"type": "text-start", "id": block},
            {"type": "text-delta", "id": block, "delta": text},
            {"type": "text-end", "id": block},
        ]
        if self.response_id is not None:
            return self._reopen() + [ResponseOutput(None, self.response_id, part) for part in parts]
        events = self._start(event_id, "engine", (), interrupt_target=event_id, channel_delivery=False)
        return events + self._end("completed", "session_start", usage=None, error=None, parts=[*parts, _finish("stop")])

    # -- response boundaries -----------------------------------------------

    def _start(
        self, response_id: str, origin: Any, input_ids: tuple[str, ...], *, interrupt_target: str,
        channel_delivery: bool = True,
    ) -> list[RuntimeEvent]:
        if not response_id:
            raise ValueError("a Claude response needs an id")
        if self.response_id is not None:
            # An input the engine takes before the open response's Result is
            # answered within that response (the next Result ends both), so it
            # joins the open response instead of replacing it unended.
            return [ResponseStarted(None, self.response_id, origin, input_ids)] if input_ids else []
        self.response_id = response_id
        self.interrupt_target = interrupt_target
        self.closed_for_interaction = False
        self.stream = ClaudeStreamCursor(child_identities=self.children)
        start = {
            "type": "start",
            "messageId": response_id,
            "messageMetadata": {"response_id": response_id, "input_ids": list(input_ids), "origin": origin},
        }
        return [
            ResponseStarted(None, response_id, origin, input_ids),
            ResponseOutput(None, response_id, start, channel_delivery=channel_delivery),
        ]

    def _end(
        self,
        outcome: ResponseOutcome,
        native_reason: str | None,
        *,
        usage: dict[str, Any] | None,
        error: dict[str, Any] | None,
        parts: list[dict[str, Any]],
    ) -> list[RuntimeEvent]:
        response_id = self.response_id
        assert response_id is not None
        events = self._reopen()
        events += [ResponseOutput(None, response_id, part) for part in parts]
        events.append(ResponseEnded(None, response_id, outcome, native_reason, usage, error))
        self.interactions = {k: v for k, v in self.interactions.items() if v["response_id"] != response_id}
        self.response_id = None
        self.interrupt_target = None
        self.closed_for_interaction = False
        return events

    def _output(self, part: dict[str, Any]) -> list[RuntimeEvent]:
        events = self._reopen()
        events.append(ResponseOutput(None, self.response_id, part))
        return events

    def _reopen(self) -> list[RuntimeEvent]:
        """Reopen the browser message an interaction closed, once the response continues."""
        if not self.closed_for_interaction or self.response_id is None:
            return []
        self.closed_for_interaction = False
        return [
            StatusChanged(None, "working"),
            ResponseOutput(None, self.response_id, {"type": "start", "messageId": self.response_id}),
        ]

    def _observe_session_key(self, message: dict[str, Any]) -> None:
        key = str(message.get("session_id") or "").strip()
        if not key and isinstance(message.get("data"), dict):
            key = str(message["data"].get("session_id") or "").strip()
        if key:
            self.engine_session_key = key


def _session_start_message(message: dict[str, Any]) -> tuple[str, str] | None:
    """``(event uuid, text)`` of a SessionStart hook's ``systemMessage``, if this is one.

    A hook's JSON output may carry ``systemMessage``, which the SDK reports in
    a ``HookEventMessage`` of subtype ``hook_response``; plain stdout carries
    no message for the person.
    """
    if (
        message.get("__sdk_type") != "HookEventMessage"
        or message.get("subtype") != "hook_response"
        or message.get("hook_event_name") != "SessionStart"
    ):
        return None
    data = message.get("data")
    output = data.get("output") if isinstance(data, dict) else None
    if not isinstance(output, str) or not output.strip():
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    content = payload.get("systemMessage") if isinstance(payload, dict) else None
    event_id = str(message.get("uuid") or "").strip()
    if not isinstance(content, str) or not content.strip() or not event_id:
        return None
    return f"session-start:{event_id}", content


def _finish(reason: str) -> dict[str, Any]:
    return {"type": "finish", "finishReason": reason}


class ClaudeRuntimeClient:
    """One reader's connection to one Claude session in its box."""

    engine_kind = "claude_code"

    def __init__(
        self,
        link: RunnerLink,
        *,
        session_id: str,
        hello: dict[str, Any],
        resume_session_key: str | None,
    ) -> None:
        boot_id = str(hello.get("boot_id") or "").strip()
        if not boot_id:
            raise ValueError("the runner's hello names no boot id")
        self._link = link
        self._session_id = session_id
        self._boot_id = boot_id
        self._translation = _Translation()
        self._translation.engine_session_key = resume_session_key
        self._next_sequence: int | None = None
        self._submit_lock = asyncio.Lock()
        self._reading = False

    @property
    def is_live(self) -> bool:
        return self._link.is_live

    @property
    def engine_session_key(self) -> str | None:
        return self._translation.engine_session_key

    async def submit(self, item: RuntimeInput) -> None:
        sdk_input = {
            "type": "user",
            "message": {"role": "user", "content": list(item.content_blocks) or item.content},
            "parent_tool_use_id": None,
            "session_id": self._session_id,
            "uuid": str(uuid.UUID(item.input_id)),
        }
        async with self._submit_lock:
            if self._next_sequence is None:
                self._next_sequence = int((await self._link.state()).get("last_delivery_sequence") or 0) + 1
            sequence, self._next_sequence = self._next_sequence, self._next_sequence + 1
            await self._link.deliver(
                DeliveryCommand(
                    command_id=item.input_id, session_id=self._session_id, sequence=sequence, sdk_input=sdk_input
                )
            )

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        if after is not None and after.generation != self._boot_id:
            raise RuntimeCursorExpired(f"cursor from runner boot {after.generation!r}, runner is {self._boot_id!r}")
        # Refused before taking the link, so the reader can reconcile and ask again.
        if self._reading:
            raise RuntimeError("a Claude runtime client serves one event stream")
        self._reading = True
        through = (
            (int(after.position.get("seq", 0)), int(after.position.get("index", _WHOLE_FRAME)))
            if after is not None
            else (0, _WHOLE_FRAME)
        )
        async for frame in self._link.frames():
            if frame.get("op") == "gap":
                first = frame.get("first_retained_sequence")
                if not isinstance(first, int) or first > through[0] + 1:
                    raise RuntimeCursorExpired(f"the runner no longer retains frames after {through[0]}")
                continue
            seq = int(frame["seq"])
            for index, event in enumerate(self._translation.translate(frame)):
                if (seq, index) <= through:
                    continue
                cursor = RuntimeCursor(self._boot_id, {"seq": seq, "index": index})
                yield dataclasses.replace(event, cursor=cursor)

    async def state(self) -> RuntimeState:
        snapshot = await self._link.state()
        if snapshot.get("boot_id") != self._boot_id:
            raise RuntimeCursorExpired("the runner restarted while this client was connected")
        running = [str(i) for i in snapshot.get("running_input_ids") or []]
        engine_response = str(snapshot.get("engine_response_id") or "").strip()
        open_responses: tuple[OpenResponse, ...] = ()
        response_id: str | None = None
        if engine_response:
            response_id = engine_response
            open_responses = (OpenResponse(engine_response, "engine"),)
        elif running:
            response_id = input_response_message_id(running[0])
            open_responses = (OpenResponse(response_id, "input", tuple(running)),)
        pending_items: list[PendingInteraction] = []
        for item in snapshot.get("pending_interactions") or []:
            if response_id is None:
                raise ValueError("runner snapshot has a pending interaction without an open response")
            pending_items.append(
                PendingInteraction(
                    response_id,
                    str(item["interaction_id"]),
                    interaction_record(
                        interaction_id=str(item["interaction_id"]),
                        response_id=response_id,
                        tool_use_id=str(item.get("tool_use_id") or ""),
                        tool_name=str(item.get("tool_name") or ""),
                        tool_input=item.get("tool_input"),
                    ),
                )
            )
        pending = tuple(pending_items)
        status: RuntimeStatus = "waiting" if pending else ("working" if snapshot.get("busy") else "idle")
        run_start = snapshot.get("run_start_seq")
        # Replay the open response and any earlier SDK events still awaiting custody.
        through = int(run_start) - 1 if isinstance(run_start, int) else int(snapshot.get("last_seq") or 0)
        unpersisted = snapshot["first_unpersisted_event_seq"]
        if unpersisted is not None:
            through = min(through, int(unpersisted) - 1)
        return RuntimeState(
            status=status,
            open_responses=open_responses,
            pending_interactions=pending,
            queued_input_ids=tuple(str(i) for i in snapshot.get("queued_input_ids") or []),
            admitted_input_ids=tuple(str(i) for i in snapshot.get("consumed_input_ids") or []),
            cursor=RuntimeCursor(self._boot_id, {"seq": through, "index": _WHOLE_FRAME}),
            closed_interactions=tuple(
                InteractionClosed(None, str(interaction_id), reason)
                for key, reason in (("answered_interaction_ids", "answered"), ("expired_interaction_ids", "expired"))
                for interaction_id in snapshot.get(key) or []
            ),
        )

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        # Answer delivery can precede journal replay on a new reader. The
        # runner's pending gate, not this reader's cache, owns the contract.
        state = await self.state()
        for item in state.closed_interactions:
            if item.interaction_id == interaction_id:
                if item.reason == "answered":
                    return
                raise InteractionNotOpen(interaction_id)
        pending = next(
            (item for item in state.pending_interactions if item.interaction_id == interaction_id),
            None,
        )
        if pending is None:
            raise InteractionNotOpen(interaction_id)
        choice, updated_input, message = claude_answer_fields(pending.contract, response)
        if choice == "allow":
            accepted = await self._link.answer(interaction_id, "allow", updated_input=updated_input)
        else:
            accepted = await self._link.answer(interaction_id, "deny", message=message or "user denied")
        if not accepted:
            raise InteractionNotOpen(interaction_id)

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        if cancel_queued:
            raise NotImplementedError("Claude's runner interrupts only the response in progress")
        # The runner owns the active target even before this reader replays it.
        state = await self.state()
        if not state.open_responses:
            return
        response = state.open_responses[0]
        target = response.input_ids[0] if response.origin == "input" else response.response_id
        await self._link.interrupt(target)

    async def committed(self, cursor: RuntimeCursor) -> None:
        # Only frames read whole: the platform may hold just some of the
        # events of the frame the cursor points into.
        if cursor.generation == self._boot_id:
            await self._link.acknowledge_persisted(int(cursor.position.get("seq", 0)) - 1)

    async def capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind="claude_code",
            # The pinned Claude input protocol accepts Anthropic image blocks.
            input_content_types=["text", "image"],
            # Read from the pinned SDK, never transcribed: the vendor's set grows.
            permission_modes=list(get_args(PermissionMode)),
            supports_interaction=True,
            supports_child_run_control=True,
            supports_server_info=True,
        )

    async def set_permission_mode(self, mode: str) -> None:
        await self._link.set_permission_mode(mode)

    async def stop_child_run(self, control_id: str) -> None:
        await self._link.stop_task(control_id)

    async def get_server_info(self) -> dict[str, Any] | None:
        return await self._link.get_init_info()

    async def close(self) -> None:
        await self._link.close()
