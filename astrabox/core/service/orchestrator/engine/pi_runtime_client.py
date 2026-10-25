"""Pi behind the runtime contract (``engine/runtime.py``).

Pi's headless surface is ``pi --mode rpc``: commands as JSON lines on stdin,
responses and session events as JSON lines on stdout
(``packages/coding-agent/docs/rpc.md`` at the pinned release). AstraBox runs one
such process per conversation as one of the box's long-lived processes (an
execd pipe), and this client is one reader's connection to it.

* **Admission.** Pi persists a user message to its session file in the same
  handler that writes the message's ``message_end`` (``_handleAgentEvent`` in
  ``src/core/agent-session.ts``; ``SessionManager._persist`` starts the file
  at the first user message "so the prompt is on disk if the first turn never
  completes"). From then on a replacement process started with ``--session``
  has it, so ``message_end`` of the user message that takes an input admits
  that input. A prompt pi answers with disposition ``handled`` (an extension
  command or input handler consumed it) is admitted at that receipt: pi has
  finished with it.
* **Which input a user message is.** Pi correlates nothing by id; its receipt
  for a ``prompt`` repeats the command id and says whether the prompt
  ``started`` a run or was ``queued``. Every input is sent with
  ``streamingBehavior: followUp``, which pi drains first in, first out, so the
  user messages pi starts take the accepted inputs in the order their receipts
  appear on stdout. The receipt is written before the run it starts and before
  the user message that drains it, so the order holds on the stream itself.
  A user message that arrives with no accepted input waiting (an extension
  calling ``sendUserMessage``) is not attributed to anyone.
* **Responses.** A response answering inputs opens at the user message that
  takes its first input; inputs whose user messages follow before any answer
  (pi's follow-up mode ``all``) join it. It is announced with its first part.
  Pi starting work on its own (a run with no input waiting, an extension's
  dialog outside a run) opens an ``engine`` response at that output. A
  response ends at ``agent_settled`` — "Pi has no remaining automatic work"
  (``docs/json.md``) — or at the next attributed user message once it has
  answered, which is where pi drains a queued follow-up inside the same run,
  or, when it was opened outside any run, at the ``handled`` receipt that
  says the extension's handler returned. The outcome is read from pi's last
  ``stopReason`` (``pi_events.response_ending``), and pi's word is kept as the
  native reason.
* **Interactions.** Pi's extension dialogs (``select``, ``confirm``,
  ``input``, ``editor``) block until the client answers
  (``docs/rpc-extension-ui.md``). Each becomes a form interaction whose id is
  pi's request id; the message segment closes with ``finish(tool-calls)`` and
  reopens with ``start`` when the response continues. Pi lists no pending
  dialogs and keeps them only in its process.
* **Cursor.** Pi keeps no journal of its stdout; ``get_entries`` returns the
  persisted session entries but none of the events a reader translates
  (deltas, tool executions, run boundaries, dialogs). The pipe's box does
  retain output: execd keeps the last 1 MiB of a process's output and replays
  it from a byte offset. A cursor is the pipe's id and a position: the offset
  where one record starts, the index of the event within that record's
  translation, and the translation state before that record. Resuming
  attaches at that offset, restores the state, translates the record again
  and skips the events through the index, so the events after a cursor are the
  same each time. A cursor from another pipe raises
  :class:`RuntimeCursorExpired`; :meth:`state` reads the new pipe from where
  this conversation began on it. An overwritten or unreadable position on a
  live pipe refuses observation: Pi exposes no input identities with which to
  reconcile the missing records safely.
* **Child runs.** The ``pi-subagents`` status and inspect widgets and its
  foreground tool results become child facts (``pi_child_runs``), sent as
  session-scoped parts. The fold that deduplicates them lives in this client,
  not in the cursor, so a reader that resumes may publish a child's current
  state again.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest, EngineStreamDetached
from astrabox.core.service.orchestrator.engine.input_ids import input_response_message_id
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_FORM,
    validate_interaction_contract,
    validated_question_answer_rows,
)
from astrabox.core.service.orchestrator.engine.pi_child_runs import (
    PiChildResources,
    PiChildRunError,
    inspect_command_line,
    is_async_status_widget,
    is_inspect_widget,
    split_child_reference,
    stop_command_line,
)
from astrabox.core.service.orchestrator.engine.pi_events import (
    PiProtocolError,
    PiResponseParts,
    response_ending,
)
from astrabox.core.service.orchestrator.engine.pi_pipe import PiRpcProcess, PiWireRecord
from astrabox.core.service.orchestrator.engine.runtime import (
    InputAdmitted,
    InputMessageObserved,
    InputRefused,
    InteractionNotOpen,
    InteractionOpened,
    ResponseEnded,
    ResponseOrigin,
    ResponseOutput,
    ResponseStarted,
    RuntimeCursor,
    RuntimeCursorExpired,
    RuntimeDiagnostic,
    RuntimeEvent,
    RuntimeInput,
    RuntimeState,
    StatusChanged,
)
from astrabox.core.service.orchestrator.runtime.process_json_lines import ProcessExited, ProcessReplayUnreadable
from astrabox.core.service.orchestrator.runtime.pty_terminal import resolve_sandbox_endpoint
from astrabox.providers.execd import EXECD_PORT, ExecdFiles, ExecdProcesses
from astrabox.seams.sandbox_data import (
    SandboxFiles,
    SandboxPathNotFound,
    SandboxProcessDetached,
    SandboxProcesses,
    SandboxProcessReplayGap,
)

logger = get_logger(__name__)

ENGINE_KIND = "pi"

#: Pi's extension-UI methods that block until the client answers. The rest
#: (notify, setStatus, setWidget, setTitle, set_editor_text) are
#: fire-and-forget: pi reads no reply to them.
_DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})

#: The labels a confirm dialog offers. Pi's confirm carries no options of its
#: own — it answers with a boolean — so the adapter supplies the two it will
#: translate back.
_CONFIRM_YES = "Yes"
_CONFIRM_NO = "No"

#: Session events that are console controls rather than conversation: pi's
#: view of its input queue (the receipts already say what it took), the
#: session's display name, and the thinking level.
_CONTROL_EVENTS = frozenset({"queue_update", "session_info_changed", "thinking_level_changed"})

#: The file in the conversation's private home naming the pipe its pi runs on
#: and the output offset where the conversation began on it.
PIPE_POINTER_FILE = ".astrabox-pi-rpc.json"

#: How long a submit waits for pi's receipt of a prompt. Pi writes the receipt
#: after the prompt's preflight, which can include an automatic compaction; a
#: receipt later than this arrives on the stream like any other record.
_RECEIPT_WAIT_SECONDS = 30.0
_REPLAY_UNAVAILABLE = (
    "PI_REPLAY_UNAVAILABLE: Pi's retained output cannot be resumed; "
    "native input and response identities cannot be recovered safely. "
    "The live runtime and its inputs have not been replaced or replayed."
)

#: How long ``get_state`` may take on a process that has just started: pi
#: announces no readiness, and its first answer comes after its startup.
_STATE_WAIT_SECONDS = 120.0

#: Code of an input pi refused before accepting it; the message is pi's reason.
INPUT_REFUSED = "ENGINE_INPUT_REFUSED"

#: Namespace of the ids of responses pi starts on its own: the pipe and the
#: output offset of the record that opened the response name it.
_ENGINE_RESPONSES = uuid.UUID("b8a7d1c4-4f0e-5d36-9b52-7e1c3a90f6d2")


def _is_input_id(value: str) -> bool:
    """Whether a command id is a platform input id (a canonical UUID) rather than this client's own."""

    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _internal_id(kind: str) -> str:
    return f"astrabox-{kind}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------


def pi_dialog_record(request: dict[str, Any], *, response_id: str) -> dict[str, Any]:
    """One pi extension dialog as the platform's interaction record.

    Every dialog becomes a form with a single question. Pi's four dialogs
    differ in how the answer is encoded, not in what the console renders, and
    the method rides on the question so the reply is encoded pi's way.
    """

    method = str(request.get("method") or "").strip()
    request_id = str(request.get("id") or "").strip()
    if not request_id:
        raise PiProtocolError(f"pi {method} dialog carries no id")
    title = str(request.get("title") or "").strip()
    if method == "confirm":
        message = str(request.get("message") or "").strip()
        prompt = " ".join(part for part in (title, message) if part) or "Confirm"
        options = [{"label": _CONFIRM_YES}, {"label": _CONFIRM_NO}]
    elif method == "select":
        prompt = title or "Select an option"
        raw_options = request.get("options")
        options = [
            {"label": str(option)}
            for option in (raw_options if isinstance(raw_options, list) else [])
            if str(option).strip()
        ]
        if not options:
            raise PiProtocolError(f"pi select {request_id!r} offered no options")
    else:
        # input and editor take free text; an empty option list is how the
        # console is told to ask for it.
        prompt = title or ("Edit" if method == "editor" else "Enter a value")
        options = []
    contract = {
        "tool_name": f"pi.extension.{method}",
        "presentation": PRESENTATION_FORM,
        "prompt": prompt,
        "raw_input": dict(request),
        "questions": [
            {
                "id": request_id,
                "header": title or method,
                "question": prompt,
                "native_answer_key": method,
                "multi_select": False,
                "allow_free_text": method in {"input", "editor"},
                "allow_empty_text": method in {"input", "editor"},
                "options": options,
            }
        ],
    }
    validate_interaction_contract(contract)
    return {"interaction_id": request_id, "response_id": response_id, **contract}


def pi_dialog_reply(record: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """The ``extension_ui_response`` that answers one dialog, encoded the way its method expects."""

    raw_input = record.get("raw_input") if isinstance(record.get("raw_input"), dict) else {}
    request_id = str(raw_input.get("id") or "").strip()
    method = str(raw_input.get("method") or "").strip()
    rows = validated_question_answer_rows(record, response)
    answer = str(rows[0]["response_text"])
    reply: dict[str, Any] = {"type": "extension_ui_response", "id": request_id}
    if method == "confirm":
        # Pi's confirm answers with a boolean, not with the label the console showed.
        if answer not in {_CONFIRM_YES, _CONFIRM_NO}:
            raise PiProtocolError("pi confirm takes one of its two choices")
        reply["confirmed"] = answer == _CONFIRM_YES
        return reply
    if method == "select" and answer not in (raw_input.get("options") or []):
        raise PiProtocolError("pi select takes one of its declared choices")
    reply["value"] = answer
    return reply


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------


class _Translation:
    """Pi's records to runtime events.

    Everything the translation of the next record depends on is in
    :meth:`snapshot`, except the child-run fold, which the client keeps.
    """

    def __init__(self, *, pipe_id: str, state: dict[str, Any] | None, children: PiChildResources) -> None:
        restored = deepcopy(state) if state else {}
        self._pipe_id = pipe_id
        self._children = children
        #: Inputs pi accepted (``started`` or ``queued``) whose user message has not begun.
        self.pending: list[str] = list(restored.get("pending") or [])
        #: The subset of ``pending`` pi holds in its follow-up queue.
        self.queued: list[str] = list(restored.get("queued") or [])
        #: The input whose user message pi is writing; admitted at its ``message_end``.
        self.taking: str | None = restored.get("taking")
        #: Between ``agent_start`` and ``agent_settled``.
        self.run = bool(restored.get("run"))
        #: Skipping the rest of a run whose start this reader did not read.
        self.resync = bool(restored.get("resync"))
        #: ``none``, ``pending`` (pi started a turn that has no part yet) or ``open``.
        self.step = str(restored.get("step") or "none")
        #: Dialogs relayed and not yet answered, ended or withdrawn, by pi's request id.
        self.dialogs: dict[str, dict[str, Any]] = dict(restored.get("dialogs") or {})
        response = restored.get("response")
        self.response: dict[str, Any] | None = None
        self.parts: PiResponseParts | None = None
        if isinstance(response, dict):
            self.response = {key: value for key, value in response.items() if key != "parts"}
            self.parts = PiResponseParts(str(response["id"]), response.get("parts"))

    def snapshot(self) -> dict[str, Any]:
        response = None
        if self.response is not None and self.parts is not None:
            response = {**deepcopy(self.response), "parts": self.parts.snapshot()}
        return {
            "pending": list(self.pending),
            "queued": list(self.queued),
            "taking": self.taking,
            "run": self.run,
            "resync": self.resync,
            "step": self.step,
            "dialogs": deepcopy(self.dialogs),
            "response": response,
        }

    # -- records -------------------------------------------------------------

    def translate(self, wire: PiWireRecord) -> list[RuntimeEvent]:
        record = wire.record
        kind = str(record.get("type") or "").strip()
        events: list[RuntimeEvent]
        if kind == "response":
            events = self._receipt(record)
        elif kind == "agent_start":
            self.run = True
            self.resync = False
            if self.response is not None:
                self.response["outside_run"] = False
            events = [StatusChanged(None, "working")]
        elif kind == "agent_settled":
            events = self._end(without_stop="agent_settled") if self.response is not None else []
            events += self._forget_untaken()
            self.run = False
            self.resync = False
            events.append(StatusChanged(None, "idle"))
        elif kind == "message_start":
            events = self._message_started(wire)
        elif kind == "message_end":
            events = self._message_ended(wire)
        elif kind in {"turn_start", "turn_end", "message_update", "tool_execution_start", "tool_execution_end"}:
            events = self._content(wire, kind)
        elif kind == "extension_ui_request":
            method = str(record.get("method") or "").strip()
            if method in _DIALOG_METHODS:
                events = self._dialog(wire)
            elif is_async_status_widget(record) or is_inspect_widget(record):
                events = []
            else:
                events = [_diagnostic(kind, record)]
        elif kind in _CONTROL_EVENTS:
            events = []
        else:
            # agent_end (a retry or a queued follow-up may still run), the
            # retry and compaction families, tool_execution_update,
            # entry_appended, extension_error, and anything a later pi adds.
            events = [_diagnostic(kind, record)]
        return events + self._child_facts(record)

    def _receipt(self, record: dict[str, Any]) -> list[RuntimeEvent]:
        request_id = str(record.get("id") or "").strip()
        command = str(record.get("command") or "")
        if command == "prompt" and _is_input_id(request_id):
            if record.get("success") is not True:
                return [InputRefused(None, request_id, {
                    "code": INPUT_REFUSED,
                    "message": str(record.get("error") or "pi refused the prompt"),
                    "status_code": 409,
                })]
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            disposition = data.get("disposition")
            if disposition in {"started", "queued"}:
                self.pending.append(request_id)
                if disposition == "queued":
                    self.queued.append(request_id)
                return []
            if disposition == "handled":
                events: list[RuntimeEvent] = [InputAdmitted(None, request_id)]
                if self.response is not None and self.response["outside_run"]:
                    events += self._end(without_stop="handled")
                    events.append(StatusChanged(None, "idle"))
                return events
            logger.warning("pi answered prompt %s with an unknown disposition %r", request_id, disposition)
            return [_diagnostic("prompt_disposition_unknown", record)]
        if command == "clear_queue" and record.get("success") is True:
            # Pi dropped its queued messages; the inputs among them will never start.
            self.pending = [input_id for input_id in self.pending if input_id not in self.queued]
            self.queued = []
        return [_diagnostic("response", record)]

    def _forget_untaken(self) -> list[RuntimeEvent]:
        """Drop inputs pi started a run for that settled without taking them.

        Pi writes a ``started`` receipt before the run it starts, and the
        run's first user message is that input. One still waiting when the run
        settles was never taken; keeping it would attribute every later user
        message to the input before it. Queued inputs stay: pi continues them
        after the run, even after an abort.
        """

        untaken = [input_id for input_id in self.pending if input_id not in self.queued]
        if not untaken:
            return []
        logger.warning("pi settled a run without taking inputs %s", untaken)
        self.pending = [input_id for input_id in self.pending if input_id in self.queued]
        return [RuntimeDiagnostic(None, "pi.inputs_not_taken", {"input_ids": untaken})]

    def _message_started(self, wire: PiWireRecord) -> list[RuntimeEvent]:
        message = wire.record.get("message")
        role = str(message.get("role") or "") if isinstance(message, dict) else ""
        if role == "user":
            return self._user_started(wire.record)
        if role != "assistant":
            # The system prompt, tool results (which arrive through the tool
            # execution events) and extension messages are not shown.
            return []
        if self.resync:
            return [_diagnostic("message_start", wire.record)]
        events = self._answering(wire.offset)
        assert self.parts is not None
        self.parts.assistant_started()
        return events

    def _user_started(self, record: dict[str, Any]) -> list[RuntimeEvent]:
        if not self.pending:
            return [_diagnostic("user_message_unattributed", record)]
        input_id = self.pending.pop(0)
        if input_id in self.queued:
            self.queued.remove(input_id)
        self.taking = input_id
        self.resync = False
        events: list[RuntimeEvent] = [InputMessageObserved(None, input_id)]
        if self.response is not None and self.response["answered"]:
            events += self._end(without_stop="message_start")
        if self.response is None:
            self._open(input_response_message_id(input_id), "input", [input_id])
        else:
            self.response["inputs"].append(input_id)
        return events

    def _message_ended(self, wire: PiWireRecord) -> list[RuntimeEvent]:
        message = wire.record.get("message")
        role = str(message.get("role") or "") if isinstance(message, dict) else ""
        if role == "user":
            if self.taking is None:
                return []
            input_id, self.taking = self.taking, None
            return [InputAdmitted(None, input_id)]
        if role != "assistant":
            return []
        if self.resync:
            return [_diagnostic("message_end", wire.record)]
        events = self._answering(wire.offset)
        assert self.parts is not None
        return events + self._parts(self.parts.assistant_ended(wire.record))

    def _content(self, wire: PiWireRecord, kind: str) -> list[RuntimeEvent]:
        if self.resync:
            return [_diagnostic(kind, wire.record)]
        if kind == "turn_start":
            events = self._close_step()
            self.step = "pending"
            return events
        if kind == "turn_end":
            return self._close_step()
        events = self._answering(wire.offset)
        assert self.parts is not None
        if kind == "message_update":
            parts = self.parts.message_update(wire.record)
        elif kind == "tool_execution_start":
            parts = self.parts.tool_started(wire.record)
        else:
            parts = self.parts.tool_ended(wire.record)
        return events + self._parts(parts)

    def _dialog(self, wire: PiWireRecord) -> list[RuntimeEvent]:
        events = self._answering(wire.offset)
        assert self.response is not None
        response_id = str(self.response["id"])
        record = pi_dialog_record(wire.record, response_id=response_id)
        interaction_id = str(record["interaction_id"])
        self.dialogs[interaction_id] = record
        events.append(InteractionOpened(None, response_id, interaction_id, record))
        events += [
            ResponseOutput(None, response_id, {"type": "data-interaction", "data": dict(record)}),
            ResponseOutput(None, response_id, {"type": "finish", "finishReason": "tool-calls"}),
            StatusChanged(None, "waiting"),
        ]
        self.response["closed"] = True
        return events

    def _child_facts(self, record: dict[str, Any]) -> list[RuntimeEvent]:
        kind = str(record.get("type") or "")
        try:
            if kind == "extension_ui_request" and is_async_status_widget(record):
                frames = self._children.observe_ui_request(record)
            elif kind == "extension_ui_request" and is_inspect_widget(record):
                frames = self._children.observe_persisted_inspect_reply(record)
            elif kind in {"tool_execution_update", "tool_execution_end"}:
                frames = self._children.observe_tool_event(record)
            else:
                return []
        except PiChildRunError as error:
            logger.warning("pi child-run record is unreadable: %s", error)
            return [RuntimeDiagnostic(None, "pi.child_run_unreadable", {"error": str(error), "record": dict(record)})]
        target = self.response["id"] if self.response is not None and self.response["announced"] else None
        events: list[RuntimeEvent] = []
        for frame in frames:
            if frame.get("type") == "data-raw-event":
                events.append(RuntimeDiagnostic(None, "pi.child_run", frame.get("data")))
            else:
                events.append(ResponseOutput(None, target, frame))
        return events

    # -- response boundaries -------------------------------------------------

    def _open(self, response_id: str, origin: ResponseOrigin, inputs: list[str]) -> None:
        self.response = {
            "id": response_id,
            "origin": origin,
            "inputs": inputs,
            "announced": False,
            "answered": False,
            "closed": False,
            "outside_run": not self.run,
        }
        self.parts = PiResponseParts(response_id)

    def _answering(self, offset: int) -> list[RuntimeEvent]:
        """The open response, announced and reopened, about to show something.

        With no response open, pi is working on its own: an ``engine``
        response opens, named by the pipe and the offset of this record.
        """

        if self.response is None:
            self._open(str(uuid.uuid5(_ENGINE_RESPONSES, f"{self._pipe_id}:{offset}")), "engine", [])
        assert self.response is not None
        events = [] if self.response["announced"] else self._announce()
        events += self._reopen()
        self.response["answered"] = True
        return events

    def _announce(self) -> list[RuntimeEvent]:
        response = self.response
        assert response is not None
        response["announced"] = True
        response_id = str(response["id"])
        start = {
            "type": "start",
            "messageId": response_id,
            "messageMetadata": {
                "response_id": response_id,
                "input_ids": list(response["inputs"]),
                "origin": response["origin"],
            },
        }
        return [
            ResponseStarted(None, response_id, response["origin"], tuple(response["inputs"])),
            ResponseOutput(None, response_id, start),
        ]

    def _reopen(self, *, working: bool = True) -> list[RuntimeEvent]:
        """Reopen the browser message a dialog closed, once the response continues.

        ``working`` is false when the response only reopens to end.
        """

        response = self.response
        if response is None or not response["closed"]:
            return []
        response["closed"] = False
        start = ResponseOutput(None, response["id"], {"type": "start", "messageId": response["id"]})
        return [StatusChanged(None, "working"), start] if working else [start]

    def _parts(self, parts: list[dict[str, Any]]) -> list[RuntimeEvent]:
        assert self.response is not None
        if not parts:
            return []
        if self.step == "pending":
            parts = [{"type": "start-step"}, *parts]
            self.step = "open"
        return [ResponseOutput(None, self.response["id"], part) for part in parts]

    def _close_step(self) -> list[RuntimeEvent]:
        was_open = self.step == "open"
        self.step = "none"
        if not was_open or self.response is None:
            return []
        return [ResponseOutput(None, self.response["id"], {"type": "finish-step"})]

    def _end(self, *, without_stop: str) -> list[RuntimeEvent]:
        """End the open response the way pi ended its last assistant message.

        ``without_stop`` is pi's word for what ended a response that has no
        assistant message: the event or receipt that ended it.
        """

        response, parts = self.response, self.parts
        assert response is not None and parts is not None
        events = [] if response["announced"] else self._announce()
        events += self._reopen(working=False)
        if self.step == "open":
            events += self._close_step()
        if parts.stop_reason is None:
            # Pi's word stays the reason; a response the person stopped ended
            # because of the stop, whatever pi says handled it.
            outcome = "cancelled" if response.get("stop_requested") else "completed"
            finish_reason, error = "other", None
            native_reason = without_stop
        elif response.get("stop_requested") and parts.stop_reason == "error":
            # Pi can end a run the person stopped with `error` and the abort's
            # own message ("This operation was aborted") rather than `aborted`.
            # The response ended because of the stop; pi's word stays the reason.
            outcome, finish_reason, error = "cancelled", "other", None
            native_reason = parts.stop_reason
        else:
            outcome, finish_reason, error = response_ending(parts.stop_reason, parts.error_message)
            native_reason = parts.stop_reason
        response_id = str(response["id"])
        if error is not None:
            events.append(ResponseOutput(None, response_id, {"type": "error", "errorText": str(error["message"])}))
        events.append(ResponseOutput(None, response_id, {"type": "finish", "finishReason": finish_reason}))
        events.append(ResponseEnded(None, response_id, outcome, native_reason, parts.usage, error))
        self.dialogs = {key: value for key, value in self.dialogs.items() if value.get("response_id") != response_id}
        self.response = None
        self.parts = None
        return events


def _diagnostic(kind: str, record: dict[str, Any]) -> RuntimeDiagnostic:
    return RuntimeDiagnostic(None, f"pi.{kind or 'unknown'}", dict(record))


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class PiRuntimeClient:
    """One reader's connection to the pi process of one conversation."""

    engine_kind = ENGINE_KIND

    def __init__(
        self,
        *,
        processes: SandboxProcesses,
        files: SandboxFiles,
        pipe_id: str,
        origin: int,
        cwd: str,
        command: str,
        engine_session_key: str,
        pointer_path: str,
        session_root: str,
        subagent_temp_root: str,
    ) -> None:
        self._processes = processes
        self._files = files
        self._pipe_id = pipe_id
        self._origin = origin
        self._cwd = cwd
        self._command = command
        self._engine_session_key = engine_session_key
        self._pointer_path = pointer_path
        self._children = PiChildResources(session_root=session_root)
        self._async_root = PurePosixPath(subagent_temp_root) / "async-subagent-runs"
        self._owner_session: str | None = None
        self._process: PiRpcProcess | None = None
        self._translation: _Translation | None = None
        self._attached = asyncio.Event()
        #: Why the last cursor could not be resumed: ``pipe`` (another pipe's
        #: cursor) or ``position`` (execd has overwritten that part of this pipe's output).
        self._expired: str | None = None
        self._submitted: set[str] = set()
        self._streaming = False
        self._closed = False

    @property
    def is_live(self) -> bool:
        return not self._closed and (self._process is None or self._process.fatal is None)

    @property
    def engine_session_key(self) -> str | None:
        return self._engine_session_key

    @property
    def pipe_id(self) -> str:
        return self._pipe_id

    # -- events --------------------------------------------------------------

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        if self._streaming:
            raise RuntimeError("a pi runtime client serves one event stream at a time")
        self._streaming = True
        try:
            async for event in self._read(after):
                yield event
        finally:
            self._streaming = False

    async def _read(self, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        if after is not None and after.generation != self._pipe_id:
            self._expired = "pipe"
            raise RuntimeCursorExpired(f"cursor from pi pipe {after.generation!r}; this runtime runs on {self._pipe_id!r}")
        position = after.position if after is not None else {}
        since = int(position.get("offset", self._origin))
        through = int(position.get("index", -1))
        process = PiRpcProcess(
            processes=self._processes, cwd=self._cwd, command=self._command, pty_session_id=self._pipe_id
        )
        try:
            await process.connect(since=since, takeover=True)
        except SandboxProcessReplayGap as gap:
            self._expired = "position"
            raise PiProtocolError(_REPLAY_UNAVAILABLE) from gap
        except (SandboxProcessDetached, EngineStreamDetached) as detached:
            # The box does not know this pipe; the reader reconnects and the
            # activation finds or starts the conversation's pi.
            logger.info("pi pipe %s is not reachable: %s", self._pipe_id, detached)
            self._process = process
            return
        translation = _Translation(pipe_id=self._pipe_id, state=position.get("state"), children=self._children)
        self._process = process
        self._translation = translation
        self._attached.set()
        try:
            while True:
                try:
                    wire = await process.next_record()
                except ProcessReplayUnreadable as unreadable:
                    self._expired = "position"
                    raise PiProtocolError(_REPLAY_UNAVAILABLE) from unreadable
                except (EngineStreamDetached, ProcessExited):
                    if process.exited:
                        await self._forget_pipe()
                    return
                if wire.offset < since:
                    continue
                before = translation.snapshot()
                events = translation.translate(wire)
                events.extend(await self._read_children(process, source_id=f"native-status:{wire.offset}"))
                for index, event in enumerate(events):
                    if wire.offset == since and index <= through:
                        continue
                    if isinstance(event, ResponseOutput):
                        event = dataclasses.replace(event, frame=self._children.canonical_child_frame(event.frame))
                    cursor = RuntimeCursor(self._pipe_id, {"offset": wire.offset, "index": index, "state": before})
                    yield dataclasses.replace(event, cursor=cursor)
        finally:
            self._attached.clear()
            with contextlib.suppress(Exception):
                await process.detach()

    async def _read_children(self, process: PiRpcProcess, *, source_id: str) -> list[RuntimeEvent]:
        """Ask the sub-agent package for the words of each child whose state changed.

        Its inspect command is an extension command, which pi "executes
        immediately even during streaming" (``docs/rpc-commands.md``), and the
        reply arrives on its own widget in the stream. The same activity
        boundary retains exact session links from the supplier's status files.
        """

        events: list[RuntimeEvent] = []
        requests = self._children.native_session_requests()
        if requests:
            try:
                if self._owner_session is None:
                    owner = await _get_state(process)
                    if owner.get("sessionId") != self._engine_session_key:
                        raise PiChildRunError("Pi native child status owner changed session")
                    self._owner_session = str(owner.get("sessionFile") or owner["sessionId"])
                statuses: dict[str, dict[str, Any]] = {}
                for reference, inspect_reference in requests:
                    run_id, _ = split_child_reference(inspect_reference)
                    if PurePosixPath(run_id).name != run_id or run_id in {".", ".."}:
                        raise PiChildRunError("Pi snapshot run id is not a native path segment")
                    if run_id not in statuses:
                        path = self._async_root / run_id / "status.json"
                        status = json.loads(await self._files.read_bytes(str(path)))
                        if (not isinstance(status, dict) or status.get("runId") != run_id
                                or status.get("sessionId") != self._owner_session):
                            raise PiChildRunError("Pi child status does not belong to the native run and owner")
                        statuses[run_id] = status
                    frames = self._children.observe_native_status(
                        reference, inspect_reference, statuses[run_id], source_id=source_id,
                    )
                    events.extend(ResponseOutput(None, None, frame) for frame in frames)
            except Exception as error:
                # Artifact observation cannot declare the parent engine dead.
                logger.warning("pi child native session linkage failed: %s", error)
                events.append(RuntimeDiagnostic(None, "pi.child_transcript_read_failed", {"error": str(error)}))
        for request_id, reference in self._children.inspect_requests():
            await process.send_untracked(
                {"id": _internal_id("inspect"), "type": "prompt", "message": inspect_command_line(reference, request_id)}
            )
        return events

    async def _connected(self) -> PiRpcProcess:
        await self._attached.wait()
        process = self._process
        if process is None or not process.is_connected:
            raise EngineStreamDetached("pi RPC is not connected")
        return process

    # -- state ---------------------------------------------------------------

    async def state(self) -> RuntimeState:
        """Pi's own status, and where the events continue.

        Pi lists no pending dialogs and no queued input ids, so a reader that
        lost the stream learns only whether pi is running. An overwritten
        position cannot be reconciled from that account. After a cursor from
        another pipe the events start where this conversation began on it.
        """

        if self._expired == "position":
            raise PiProtocolError(_REPLAY_UNAVAILABLE)
        snapshot = await self._get_state()
        running = bool(snapshot.get("isStreaming"))
        if self._expired == "pipe":
            cursor = RuntimeCursor(self._pipe_id, {"offset": self._origin, "index": -1, "state": {}})
        else:
            end = await self._processes.output_offset(self._pipe_id)
            cursor = RuntimeCursor(
                self._pipe_id, {"offset": end, "index": -1, "state": {"run": running, "resync": running}}
            )
        return RuntimeState(status="working" if running else "idle", cursor=cursor)

    async def _get_state(self) -> dict[str, Any]:
        process = self._process if self._attached.is_set() else None
        if process is not None and process.is_connected:
            return await _get_state(process)
        temporary = PiRpcProcess(
            processes=self._processes, cwd=self._cwd, command=self._command, pty_session_id=self._pipe_id
        )
        await temporary.connect(since=await self._processes.output_offset(self._pipe_id), takeover=True)
        try:
            return await _get_state(temporary)
        finally:
            await temporary.detach()

    # -- what people ask of pi -------------------------------------------------

    async def submit(self, item: RuntimeInput) -> None:
        """Hand pi one prompt and wait for its receipt.

        Sent as a follow-up, which pi applies only while it is running, and
        under the input's id, which pi repeats on its receipt. A prompt naming
        one of pi's extension commands is not waited on: pi answers it only
        after the command's handler returns, and the handler may wait on a
        person whose answer arrives through this same reader. Once the prompt
        is written, a receipt that is late or lost with the pipe is not waited
        for: pi writes it to the stream a resumed reader reads.
        """

        if item.input_id in self._submitted:
            return
        process = await self._connected()
        command = {"type": "prompt", "message": item.content, "streamingBehavior": "followUp"}
        if await _is_extension_command(process, item.content):
            await process.send_untracked({**command, "id": item.input_id})
            self._submitted.add(item.input_id)
            return
        future = await process.request(item.input_id, command)
        self._submitted.add(item.input_id)
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=_RECEIPT_WAIT_SECONDS)
        except (TimeoutError, RuntimeError) as unanswered:
            logger.info("pi prompt %s written without a receipt yet: %s", item.input_id, unanswered or "timeout")
            return
        finally:
            process.release(item.input_id)
        # Every receipt also reaches the event stream. Both timely and late
        # refusals settle there once, with the cursor of the native refusal.

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        translation = self._translation
        record = translation.dialogs.get(interaction_id) if translation is not None else None
        if translation is None or record is None:
            raise InteractionNotOpen(interaction_id)
        reply = pi_dialog_reply(record, response)
        process = await self._connected()
        await process.send_untracked(reply)
        translation.dialogs.pop(interaction_id, None)

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        """Stop pi's run; with ``cancel_queued`` also drop the inputs pi queued.

        Pi's ``abort`` waits for the session to go idle, and a dialog an
        extension did not tie to the abort signal holds the run open, so open
        dialogs are cancelled first. Queued messages survive an abort unless
        ``clear_queue`` comes before it (``docs/rpc-commands.md``).
        """

        process = await self._connected()
        if cancel_queued:
            await process.send_untracked({"id": _internal_id("clear"), "type": "clear_queue"})
        translation = self._translation
        if translation is not None and translation.response is not None:
            translation.response["stop_requested"] = True
        for dialog_id in list(translation.dialogs if translation is not None else ()):
            await process.send_untracked({"type": "extension_ui_response", "id": dialog_id, "cancelled": True})
            if translation is not None:
                translation.dialogs.pop(dialog_id, None)
        await process.send_untracked({"id": _internal_id("abort"), "type": "abort"})

    async def stop_child_run(self, control_id: str) -> None:
        """Stop one child through the sub-agent package's own command.

        Pi's ``abort`` ends the whole run, which would take the parent down with
        the child; the package's stop is an extension command, which pi runs
        even while streaming.
        """

        process = await self._connected()
        await process.send_untracked(
            {"id": _internal_id("stop"), "type": "prompt", "message": stop_command_line(control_id)}
        )

    async def committed(self, cursor: RuntimeCursor) -> None:
        # Pi keeps no history for its reader to release: the box's replay
        # window is bounded and overwrites itself.
        return None

    async def capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind=ENGINE_KIND,
            tools=[],
            # Pi ships no permission system; it says so itself and recommends
            # containment, which the sandbox is (docs/security.md).
            permission_modes=[],
            supports_interaction=True,
            # The image pins a sub-agent package whose stop is a command.
            supports_child_run_control=True,
            supports_server_info=False,
        )

    async def close(self) -> None:
        """Detach from the pipe; pi keeps running."""

        self._closed = True
        process = self._process
        if process is not None:
            with contextlib.suppress(Exception):
                await process.detach()

    async def dispose(self) -> bool:
        """Stop the pi process behind this conversation for good."""

        await self.close()
        await self._processes.close(self._pipe_id)
        await self._forget_pipe()
        return True

    async def _forget_pipe(self) -> None:
        """Remove the pointer to this pipe, so the next activation starts pi again with ``--session``."""

        with contextlib.suppress(Exception):
            pointer = await read_pipe_pointer(self._files, self._pointer_path)
            if pointer is not None and pointer[0] == self._pipe_id:
                await self._files.remove([self._pointer_path])


# --------------------------------------------------------------------------
# Activation
# --------------------------------------------------------------------------


async def _get_state(process: PiRpcProcess) -> dict[str, Any]:
    response = await process.command(_internal_id("state"), {"type": "get_state"}, timeout=_STATE_WAIT_SECONDS)
    data = response.get("data")
    if response.get("success") is not True or not isinstance(data, dict):
        raise PiProtocolError(f"pi refused get_state: {response.get('error') or 'no state returned'}")
    return data


async def _is_extension_command(process: PiRpcProcess, content: str) -> bool:
    """Whether ``content`` invokes one of pi's extension commands, by pi's own catalog."""

    if not content.startswith("/"):
        return False
    name = content[1:].split(" ", 1)[0]
    response = await process.command(_internal_id("commands"), {"type": "get_commands"}, timeout=_STATE_WAIT_SECONDS)
    data = response.get("data")
    commands = data.get("commands") if isinstance(data, dict) else None
    if response.get("success") is not True or not isinstance(commands, list):
        raise PiProtocolError("pi get_commands returned no command catalog")
    return any(
        isinstance(command, dict) and command.get("name") == name and command.get("source") == "extension"
        for command in commands
    )


async def box_process_faces(
    sandbox: Any, *, port: int, service_credential: str | None
) -> tuple[SandboxProcesses, SandboxFiles]:
    """The processes and files of the execd that runs this conversation's pi.

    The box's own execd on the conversation tenancy; the conversation's own
    execd, from the image, under the shared tenancy.
    """

    if port == EXECD_PORT:
        return sandbox.processes, sandbox.files
    if not service_credential:
        raise RuntimeError("isolated pi service has no platform control credential")
    endpoint = await resolve_sandbox_endpoint(sandbox, port)
    headers = {**endpoint.headers, "X-EXECD-ACCESS-TOKEN": service_credential}
    return (
        ExecdProcesses.at_endpoint(endpoint.url, headers, sandbox_id=sandbox.sandbox_id),
        ExecdFiles.at_endpoint(endpoint.url, headers, sandbox_id=sandbox.sandbox_id),
    )


def pipe_pointer_path(home: str) -> str:
    return f"{home.rstrip('/')}/{PIPE_POINTER_FILE}"


async def read_pipe_pointer(files: SandboxFiles, path: str) -> tuple[str, int] | None:
    """The pipe and origin offset a previous activation recorded, or ``None``."""

    try:
        raw = await files.read_bytes(path)
    except (SandboxPathNotFound, FileNotFoundError):
        return None
    try:
        pointer = json.loads(raw.decode("utf-8"))
        pipe_id = str(pointer["process_id"]).strip()
        origin = pointer["origin"]
    except (ValueError, KeyError, TypeError):
        logger.warning("pi pipe pointer %s is unreadable; a new pi process will be started", path)
        return None
    if not pipe_id or not isinstance(origin, int) or isinstance(origin, bool):
        return None
    return pipe_id, origin


async def _write_pipe_pointer(files: SandboxFiles, path: str, pipe_id: str, origin: int) -> None:
    payload = json.dumps({"process_id": pipe_id, "origin": origin}).encode("utf-8")
    await files.write_bytes(path, payload, mode=0o600)


async def _prove(
    processes: SandboxProcesses,
    pipe_id: str,
    *,
    cwd: str,
    command: str,
    switch_to: str | None = None,
) -> tuple[str, int]:
    """Ask the pi on ``pipe_id`` which session it runs; return it and the output offset after the answer.

    ``get_state`` is the readiness proof: pi announces nothing when it comes
    up, so its first answer is the evidence that it can answer at all. With
    ``switch_to``, pi first switches to that session file, its native resume
    for a process that is already running.
    """

    process = PiRpcProcess(processes=processes, cwd=cwd, command=command, pty_session_id=pipe_id)
    await process.connect(since=await processes.output_offset(pipe_id), takeover=True)
    try:
        if switch_to is not None:
            switched = await process.command(
                _internal_id("switch"), {"type": "switch_session", "sessionPath": switch_to}, timeout=_STATE_WAIT_SECONDS
            )
            if switched.get("success") is not True or (switched.get("data") or {}).get("cancelled") is not False:
                raise PiProtocolError("pi did not confirm the session switch")
        state = await _get_state(process)
        session_id = str(state.get("sessionId") or "").strip()
        if not session_id:
            raise PiProtocolError("pi get_state returned no sessionId")
        return session_id, await processes.output_offset(pipe_id)
    finally:
        await process.detach()


async def _session_file(files: SandboxFiles, session_root: str, session_key: str) -> str:
    """The one session file under ``session_root`` whose header names ``session_key``."""

    matches = []
    for path in await files.search(session_root, f"*_{session_key}.jsonl"):
        content = (await files.read_bytes(path)).decode("utf-8")
        first_line = content.splitlines()[0] if content else ""
        if not first_line:
            continue
        header = json.loads(first_line)
        if header.get("type") == "session" and header.get("id") == session_key:
            matches.append(path)
    if len(matches) != 1:
        raise PiProtocolError(f"resuming {session_key!r} requires one session file, found {len(matches)}")
    return matches[0]


async def connect_pi_runtime(
    sandbox: Any,
    *,
    cwd: str,
    command: str,
    home: str,
    session_root: str,
    subagent_temp_root: str,
    resume_session_key: str | None,
    reattach: bool,
    parked_pipe: str | None = None,
    port: int = EXECD_PORT,
    service_credential: str | None = None,
) -> PiRuntimeClient:
    """Connect to the conversation's pi, starting it when the box runs none.

    ``reattach`` rejoins the pi the conversation's private home names, which
    keeps a running pi and its response in progress. A pipe the box does not
    know, or one whose pi now runs another session (execd starts a command
    again when an attachment arrives after it exited), is replaced by a new
    pi resuming ``resume_session_key``. ``parked_pipe`` adopts a prepared
    unit's started pi, switching it to ``resume_session_key`` when there is
    one. Otherwise the pipe is created with ``command``.
    """

    processes, files = await box_process_faces(sandbox, port=port, service_credential=service_credential)
    pointer_path = pipe_pointer_path(home)

    def client(pipe_id: str, origin: int, session_key: str) -> PiRuntimeClient:
        return PiRuntimeClient(
            processes=processes,
            files=files,
            pipe_id=pipe_id,
            origin=origin,
            cwd=cwd,
            command=command,
            engine_session_key=session_key,
            pointer_path=pointer_path,
            session_root=session_root,
            subagent_temp_root=subagent_temp_root,
        )

    if reattach:
        pointer = await read_pipe_pointer(files, pointer_path)
        if pointer is not None and await processes.exists(pointer[0]):
            pipe_id, origin = pointer
            session_key, _ = await _prove(processes, pipe_id, cwd=cwd, command=command)
            if session_key == resume_session_key:
                return client(pipe_id, origin, session_key)
            logger.warning(
                "pi on pipe %s runs session %r, not %r; starting pi again on a new pipe",
                pipe_id,
                session_key,
                resume_session_key,
            )
    switch_to = None
    if parked_pipe is not None and resume_session_key:
        switch_to = await _session_file(files, session_root, resume_session_key)
    pipe_id = parked_pipe or await processes.start(command, cwd=cwd)
    session_key, origin = await _prove(processes, pipe_id, cwd=cwd, command=command, switch_to=switch_to)
    if resume_session_key and session_key != resume_session_key:
        raise PiProtocolError(
            f"pi resumed a different native conversation: expected={resume_session_key!r} actual={session_key!r}"
        )
    await _write_pipe_pointer(files, pointer_path, pipe_id, origin)
    return client(pipe_id, origin, session_key)


async def park_pi_process(
    sandbox: Any, *, cwd: str, command: str, port: int = EXECD_PORT, service_credential: str | None = None
) -> str:
    """Start pi in a prepared box and prove it answers; return the pipe holding it.

    After this returns, the expensive part of a start — pipe creation, the
    child's boot, and the first ``get_state`` it answers — is paid, and a
    later claim adopts the pipe instead of repeating it.
    """

    processes, _files = await box_process_faces(sandbox, port=port, service_credential=service_credential)
    pipe_id = await processes.start(command, cwd=cwd)
    await _prove(processes, pipe_id, cwd=cwd, command=command)
    return pipe_id


__all__ = [
    "ENGINE_KIND",
    "INPUT_REFUSED",
    "PIPE_POINTER_FILE",
    "PiRuntimeClient",
    "box_process_faces",
    "connect_pi_runtime",
    "park_pi_process",
    "pi_dialog_record",
    "pi_dialog_reply",
    "pipe_pointer_path",
    "read_pipe_pointer",
]
