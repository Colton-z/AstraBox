"""Pi's message and tool events as AI SDK browser parts, and how a response ends.

One-way translation at the seam, reading the vendor's own words. Sources of
truth: ``AgentEvent`` in ``@earendil-works/pi-agent-core``
(``packages/agent/src/types.ts``), ``AgentSessionEvent`` in the coding agent's
``src/core/agent-session.ts``, ``AssistantMessageEvent`` and ``StopReason`` in
``@earendil-works/pi-ai`` (``packages/ai/src/types.ts``), and the coding
agent's ``docs/json.md`` — the event stream the RPC mode writes to stdout.

Vocabulary boundaries this translator keeps:

* **A turn is a step.** Pi's ``turn`` is one assistant response plus the tools
  that response called, so ``turn_start``/``turn_end`` become
  ``start-step``/``finish-step``. The step opens with its first part rather
  than at ``turn_start``: pi starts the turn that drains a queued follow-up
  before writing the follow-up's user message, and that step belongs to the
  answer to the follow-up, not to the answer before it.
* **Committed messages are not re-emitted.** ``message_end`` carries the
  finished assistant message whose text already streamed as deltas; it is
  read for its stop reason, usage and error, and closes any part pi left open.
  Tool results arrive through the tool-execution events.
* **The stop reason is pi's, read pi's way.** ``AssistantMessageEvent`` puts
  ``stop``, ``length``, ``toolUse`` and ``deferred`` under ``done`` ("the
  provider stream completed successfully") and ``aborted``/``error`` under
  ``error``; ``length`` is "the output was cut off by the token limit"
  (``packages/agent/src/agent-loop.ts``). The response's outcome follows that
  split and pi's word travels beside it unchanged.

The translator's state is a plain JSON object, so a reader that stopped after
any record can resume with the state it had before that record.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from astrabox.core.service.orchestrator.engine.emissions import public_ui_frame
from astrabox.core.service.orchestrator.engine.file_changes import FileChange, UnifiedDiff, file_changes_frame
from astrabox.core.service.orchestrator.engine.runtime import ResponseOutcome


class PiProtocolError(RuntimeError):
    """The pinned pi release emitted something this adapter cannot honor."""


#: ``AssistantMessage.stopReason`` → the response's outcome and the AI SDK
#: finish reason of its last part. ``toolUse`` ends a run only when the tools
#: asked the loop to stop after their batch, and ``deferred`` is a provider
#: deferral; both are ``done`` endings with no AI SDK word of their own.
_ENDINGS: dict[str, tuple[ResponseOutcome, str]] = {
    "stop": ("completed", "stop"),
    "length": ("completed", "length"),
    "toolUse": ("completed", "other"),
    "deferred": ("completed", "other"),
    "aborted": ("cancelled", "other"),
    "error": ("failed", "error"),
}

#: ``AssistantMessageEvent`` streaming kinds → the AI SDK's stream parts.
#: Pi streams a tool call's arguments as ``toolcall_*``; the complete call
#: arrives on ``tool_execution_start``, which is what the console renders.
_STREAM_PARTS = {
    "text": "text",
    "thinking": "reasoning",
}


def raw_event_frame(subtype: str, raw: dict[str, Any]) -> dict[str, Any]:
    """A pi record kept as operator evidence rather than shown."""

    return {
        "type": "data-raw-event",
        "data": {
            "event_type": "pi.rpc",
            "subtype": subtype or "unknown",
            "raw": dict(raw),
        },
    }


def response_ending(
    stop_reason: str | None, error_message: str | None
) -> tuple[ResponseOutcome, str, dict[str, Any] | None]:
    """The outcome, AI SDK finish reason and error of a response pi ended with ``stop_reason``.

    A value outside the pinned vendor's ``StopReason`` cannot be read as a
    success, so it ends the response as failed and names the value.
    """

    reading = _ENDINGS.get(str(stop_reason or ""))
    if reading is None:
        return (
            "failed",
            "error",
            {"code": "PI_STOP_REASON_UNKNOWN", "message": f"pi ended the response with stop reason {stop_reason!r}"},
        )
    outcome, finish_reason = reading
    if outcome != "failed":
        return outcome, finish_reason, None
    return outcome, finish_reason, {"code": str(stop_reason), "message": error_message or "pi ended the response with an error"}


class PiResponseParts:
    """One response's assistant messages and tool calls as AI SDK parts.

    ``state`` is what :meth:`snapshot` returned for the same response; a new
    response starts from none.
    """

    def __init__(self, response_id: str, state: dict[str, Any] | None = None) -> None:
        self._response_id = response_id
        restored = deepcopy(state) if state else {}
        #: The ordinal of the assistant message the deltas land in. Pi's
        #: ``contentIndex`` counts within one message, so two messages of one
        #: response both start at 0.
        self._message = int(restored.get("message") or 0)
        #: ``contentIndex`` → part kind for the parts still open.
        self._open: dict[str, str] = dict(restored.get("open") or {})
        self.stop_reason: str | None = restored.get("stop_reason")
        self.usage: dict[str, Any] | None = restored.get("usage")
        self.error_message: str | None = restored.get("error_message")
        #: ``toolCallId`` → ``[tool name, path]`` for a write or edit still running.
        self._file_tools: dict[str, list[str]] = dict(restored.get("file_tools") or {})

    def snapshot(self) -> dict[str, Any]:
        return {
            "message": self._message,
            "open": dict(self._open),
            "stop_reason": self.stop_reason,
            "usage": deepcopy(self.usage),
            "error_message": self.error_message,
            "file_tools": {key: list(value) for key, value in self._file_tools.items()},
        }

    def _part_id(self, kind: str, content_index: int) -> str:
        return f"pi-{kind}:{self._response_id}:{self._message}:{content_index}"

    # ── assistant messages ───────────────────────────────────────────────
    def assistant_started(self) -> None:
        """Open a new assistant message's part-index space. Emits nothing."""

        self._message += 1
        self._open = {}

    def message_update(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        stream_event = event.get("assistantMessageEvent")
        if not isinstance(stream_event, dict):
            raise PiProtocolError("message_update carries no assistantMessageEvent")
        if isinstance(event.get("usage"), dict):
            self.usage = dict(event["usage"])
        kind = str(stream_event.get("type") or "").strip()
        if kind in {"start", "done", "error"} or kind.startswith("toolcall_"):
            # The stream's own boundaries, and argument streaming that
            # tool_execution_start carries whole.
            return []
        for prefix, part in _STREAM_PARTS.items():
            if kind == f"{prefix}_start":
                index = _content_index(stream_event, kind)
                part_id = self._part_id(part, index)
                self._open[str(index)] = part
                return [{"type": f"{part}-start", "id": part_id}]
            if kind == f"{prefix}_delta":
                index = _content_index(stream_event, kind)
                if self._open.get(str(index)) != part:
                    raise PiProtocolError(f"{kind} at contentIndex {index} has no matching open part")
                return [
                    {"type": f"{part}-delta", "id": self._part_id(part, index), "delta": str(stream_event.get("delta") or "")}
                ]
            if kind == f"{prefix}_end":
                index = _content_index(stream_event, kind)
                if self._open.pop(str(index), None) != part:
                    raise PiProtocolError(f"{kind} at contentIndex {index} has no matching open part")
                return [{"type": f"{part}-end", "id": self._part_id(part, index)}]
        raise PiProtocolError(f"unknown assistantMessageEvent type {kind!r}")

    def assistant_ended(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        message = event.get("message")
        if not isinstance(message, dict):
            raise PiProtocolError("message_end carries no message")
        stop_reason = str(message.get("stopReason") or "").strip()
        if stop_reason:
            self.stop_reason = stop_reason
        if isinstance(message.get("usage"), dict):
            self.usage = dict(message["usage"])
        error_message = message.get("errorMessage")
        self.error_message = error_message if isinstance(error_message, str) and error_message else None
        # The body already streamed as deltas. A part pi left open (an aborted
        # stream ends without its *_end event) is closed here so the console
        # does not hold an open block forever.
        closing = [
            {"type": f"{self._open[index]}-end", "id": self._part_id(self._open[index], int(index))}
            for index in sorted(self._open, key=int)
        ]
        self._open = {}
        return closing

    # ── tools ────────────────────────────────────────────────────────────
    def tool_started(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        call_id = str(event.get("toolCallId") or "").strip()
        name = str(event.get("toolName") or "").strip()
        if not call_id or not name:
            raise PiProtocolError("tool_execution_start lacks toolCallId or toolName")
        arguments = event.get("args")
        if not isinstance(arguments, dict):
            raise PiProtocolError(f"tool_execution_start {call_id!r} args are not an object")
        if name in {"write", "edit"}:
            self._file_tools[call_id] = [name, str(arguments["path"])]
        return [
            public_ui_frame({"type": "tool-input-start", "toolCallId": call_id, "toolName": name, "dynamic": True}),
            public_ui_frame(
                {
                    "type": "tool-input-available",
                    "toolCallId": call_id,
                    "toolName": name,
                    "input": dict(arguments),
                    "dynamic": True,
                }
            ),
        ]

    def tool_ended(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        call_id = str(event.get("toolCallId") or "").strip()
        if not call_id:
            raise PiProtocolError("tool_execution_end lacks toolCallId")
        if "isError" not in event:
            # Pi always sets it. Inferring the flag from the result's shape is
            # how a failed tool gets rendered as a success.
            raise PiProtocolError(f"tool_execution_end {call_id!r} carries no isError flag")
        file_tool = self._file_tools.pop(call_id, None)
        if event["isError"]:
            return [
                {
                    "type": "tool-output-error",
                    "toolCallId": call_id,
                    "errorText": json.dumps(event.get("result"), ensure_ascii=False),
                }
            ]
        parts: list[dict[str, Any]] = [
            {
                "type": "tool-output-available",
                "toolCallId": call_id,
                "output": {"content": event.get("result"), "isError": False},
            }
        ]
        if file_tool:
            name, path = file_tool
            result = event.get("result")
            details = result.get("details") if isinstance(result, dict) else None
            patch = details.get("patch") if isinstance(details, dict) else None
            if name == "edit" and not isinstance(patch, str):
                raise PiProtocolError("successful edit has no native patch")
            parts.append(
                file_changes_frame(
                    call_id,
                    name,
                    [FileChange(path=path, diff=UnifiedDiff(patch=patch) if isinstance(patch, str) else None)],
                )
            )
        return parts


def _content_index(stream_event: dict[str, Any], kind: str) -> int:
    index = stream_event.get("contentIndex")
    if not isinstance(index, int) or isinstance(index, bool):
        raise PiProtocolError(f"{kind} has no integer contentIndex")
    return index
