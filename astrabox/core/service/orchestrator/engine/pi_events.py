"""Pi's vocabulary → the platform's contracts.

One-way translation at the seam, reading the vendor's own words. Sources of
truth: ``AgentEvent`` in ``@earendil-works/pi-agent-core``
(``packages/agent/src/types.ts``), ``AgentSessionEvent`` in the coding agent's
``src/core/agent-session.ts``, and ``AssistantMessageEvent`` in
``@earendil-works/pi-ai`` — the three types the RPC mode serializes onto
stdout, unchanged, one JSON object per line.

Vocabulary boundaries this translator enforces:

* **A turn is a step; only ``agent_settled`` settles the platform turn.** Pi
  names three nested lifetimes and AstraBox needs the outermost one. Its
  ``turn`` is one assistant response plus the tools that response called, so
  ``turn_start``/``turn_end`` are step boundaries. Its ``agent_end`` carries
  ``willRetry``: when the loop failed and the auto-retry will re-run it, more
  content follows, so ``agent_end`` is not a terminal either. Pi emits
  ``agent_settled`` exactly when the loop is genuinely at rest, and that is
  the only event that produces the platform ``result`` frame.
* **Committed messages are not re-emitted.** ``message_end`` carries the
  finished assistant message whose text already streamed as deltas. Emitting
  it would write the reply body twice. It is read for its stop reason and
  usage, and dropped as content. ``message_start``/``message_end`` for the
  ``user`` and ``toolResult`` roles are dropped for the same reason: the
  platform already holds the prompt, and tool results arrive through the
  dedicated tool-execution events.
* **Unknown event types become private diagnostics** rather than
  disappearing or crossing the browser protocol, so a vendor field added in a
  release stays visible without this translator inventing a name for it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from astrabox.core.service.orchestrator.engine.emissions import public_ui_frame

from astrabox.core.service.orchestrator.engine.file_changes import (
    FileChange, UnifiedDiff, file_changes_frame,
)


class PiProtocolError(RuntimeError):
    """The pinned pi release emitted an event this translator cannot honor."""


#: Session-level facts AstraBox already holds, or renders as a control rather
#: than as transcript. An event belongs here only when keeping it would write
#: something the platform stores elsewhere a second time.
_DROPPED_EVENT_TYPES = frozenset(
    {
        # The platform owns the input queue and its ordering; pi's view of it
        # is bookkeeping, not conversation.
        "queue_update",
        # Session naming and the thinking control are console controls.
        "session_info_changed",
        "thinking_level_changed",
    }
)

#: ``AssistantMessage.stopReason`` → the platform's finish reason. Pi's
#: ``toolUse`` ends a step, never the turn, so it is absent here on purpose:
#: reaching the terminal with it means the loop settled mid-tool-call, which
#: this translator reports rather than smooths over.
_FINISH_REASONS = {
    "stop": "stop",
    "length": "length",
    "aborted": "cancelled",
    "error": "error",
}

#: The platform outcome each finish reason settles to.
_OUTCOMES = {
    "stop": "completed",
    "length": "completed",
    "cancelled": "cancelled",
    "error": "failed",
}

#: ``AssistantMessageEvent`` streaming kinds → the platform's stream parts.
#: Pi streams a tool call's arguments as ``toolcall_*``; the complete call
#: arrives on ``tool_execution_start``, which is what the console renders, so
#: the argument deltas are diagnostics rather than a fourth stream part.
_STREAM_PARTS = {
    "text": "text",
    "thinking": "reasoning",
}


def raw_event_frame(subtype: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "data-raw-event",
        "data": {
            "event_type": "pi.rpc",
            "subtype": subtype or "unknown",
            "raw": dict(raw),
        },
    }


def _usage_frame(usage: Any) -> dict[str, Any] | None:
    """Pi's ``Usage`` as the platform records it, under pi's own field names."""

    if not isinstance(usage, dict):
        return None
    return dict(usage)


class PiTurnTranslator:
    """Reduce one driven turn's pi event stream to AI SDK frames.

    One instance per driven turn. ``terminal_seen`` flips when
    ``agent_settled`` produced the ``result`` frame and nothing may follow it.
    """

    def __init__(self, *, session_id: str) -> None:
        self._session_id = session_id
        #: assistant message id → {contentIndex: {"kind", "id"}} for the parts
        #: currently open. Pi indexes content within a message, so the message
        #: is part of the identity: two messages in one turn both start at 0.
        self._open_parts: dict[int, dict[str, str]] = {}
        #: Which assistant message the open parts belong to. Pi's wire events
        #: carry ``contentIndex`` but not a message id, so the translator
        #: tracks the message the deltas are landing in.
        self._message_ordinal = 0
        self._last_stop_reason: str | None = None
        self._last_usage: dict[str, Any] | None = None
        self._last_error_message: str | None = None
        self.terminal_seen = False
        self._file_tools: dict[str, tuple[str, str]] = {}

    # ── stream-part identity ─────────────────────────────────────────────
    def _part_id(self, kind: str, content_index: int) -> str:
        return f"pi-{kind}:{self._session_id}:{self._message_ordinal}:{content_index}"

    # ── entry point ──────────────────────────────────────────────────────
    def translate(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if self.terminal_seen:
            raise PiProtocolError(
                "pi event arrived after agent_settled settled the turn"
            )
        event_type = str(event.get("type") or "").strip()
        if not event_type:
            raise PiProtocolError("pi event carries no type")

        if event_type == "agent_start":
            # The loop opening is not a step; pi opens a step with turn_start.
            return
        if event_type == "turn_start":
            yield {"type": "start-step"}
            return
        if event_type == "turn_end":
            yield {"type": "finish-step"}
            return
        if event_type == "message_start":
            self._note_message_start(event)
            return
        if event_type == "message_update":
            yield from self._translate_message_update(event)
            return
        if event_type == "message_end":
            yield from self._translate_message_end(event)
            return
        if event_type == "tool_execution_start":
            yield from self._translate_tool_start(event)
            return
        if event_type == "tool_execution_update":
            # Partial tool output. Pi re-sends the complete result on
            # tool_execution_end, and the platform renders the settled result.
            yield raw_event_frame(event_type, event)
            return
        if event_type == "tool_execution_end":
            yield from self._translate_tool_end(event)
            return
        if event_type == "agent_end":
            yield from self._translate_agent_end(event)
            return
        if event_type == "agent_settled":
            yield self._translate_settled()
            return
        if event_type in _DROPPED_EVENT_TYPES:
            return
        # compaction, auto-retry, summarization retry, bash streaming and any
        # event a later pi release adds: kept as diagnostics. The retry
        # families matter — they are what makes agent_end non-terminal — and
        # they are recorded rather than interpreted.
        yield raw_event_frame(event_type, event)

    # ── assistant message lifecycle ──────────────────────────────────────
    def _note_message_start(self, event: dict[str, Any]) -> None:
        """Open a new assistant message's part-index space. Emits nothing.

        The platform stores the prompt, and tool results reach the console
        through the tool-execution events, so only the assistant role starts
        anything here.
        """

        message = event.get("message")
        role = str(message.get("role") or "") if isinstance(message, dict) else ""
        if role != "assistant":
            return
        self._message_ordinal += 1
        self._open_parts = {}

    def _translate_message_update(
        self, event: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        stream_event = event.get("assistantMessageEvent")
        if not isinstance(stream_event, dict):
            raise PiProtocolError("message_update carries no assistantMessageEvent")
        usage = _usage_frame(event.get("usage"))
        if usage is not None:
            self._last_usage = usage

        kind = str(stream_event.get("type") or "").strip()
        if kind == "start":
            return
        if kind in {"done", "error"}:
            # The step's own terminal. The turn continues until pi settles.
            return
        if kind.startswith("toolcall_"):
            # Argument streaming; tool_execution_start carries the whole call.
            return

        for prefix, part in _STREAM_PARTS.items():
            if kind == f"{prefix}_start":
                index = _content_index(stream_event, kind)
                part_id = self._part_id(part, index)
                self._open_parts[index] = {"kind": part, "id": part_id}
                yield {"type": f"{part}-start", "id": part_id}
                return
            if kind == f"{prefix}_delta":
                index = _content_index(stream_event, kind)
                open_part = self._open_parts.get(index)
                if open_part is None or open_part["kind"] != part:
                    raise PiProtocolError(
                        f"{kind} at contentIndex {index} has no matching open part"
                    )
                yield {
                    "type": f"{part}-delta",
                    "id": open_part["id"],
                    "delta": str(stream_event.get("delta") or ""),
                }
                return
            if kind == f"{prefix}_end":
                index = _content_index(stream_event, kind)
                open_part = self._open_parts.pop(index, None)
                if open_part is None or open_part["kind"] != part:
                    raise PiProtocolError(
                        f"{kind} at contentIndex {index} has no matching open part"
                    )
                yield {"type": f"{part}-end", "id": open_part["id"]}
                return

        raise PiProtocolError(f"unknown assistantMessageEvent type {kind!r}")

    def _translate_message_end(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        message = event.get("message")
        if not isinstance(message, dict):
            raise PiProtocolError("message_end carries no message")
        if str(message.get("role") or "") != "assistant":
            return
        stop_reason = str(message.get("stopReason") or "").strip()
        if stop_reason:
            self._last_stop_reason = stop_reason
        usage = _usage_frame(message.get("usage"))
        if usage is not None:
            self._last_usage = usage
        error_message = message.get("errorMessage")
        self._last_error_message = (
            str(error_message) if isinstance(error_message, str) and error_message else None
        )
        # The body already streamed as deltas. Any part pi left open (an
        # aborted stream ends without its *_end event) is closed here so the
        # console does not hold an open block forever.
        for index in sorted(self._open_parts):
            open_part = self._open_parts[index]
            yield {"type": f"{open_part['kind']}-end", "id": open_part["id"]}
        self._open_parts = {}

    # ── tools ────────────────────────────────────────────────────────────
    def _translate_tool_start(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        call_id = str(event.get("toolCallId") or "").strip()
        name = str(event.get("toolName") or "").strip()
        if not call_id or not name:
            raise PiProtocolError("tool_execution_start lacks toolCallId or toolName")
        arguments = event.get("args")
        if not isinstance(arguments, dict):
            raise PiProtocolError(
                f"tool_execution_start {call_id!r} args are not an object"
            )
        if name in {"write", "edit"}:
            self._file_tools[call_id] = (name, str(arguments["path"]))
        yield public_ui_frame({
            "type": "tool-input-start",
            "toolCallId": call_id,
            "toolName": name,
            "dynamic": True,
        })
        yield public_ui_frame({
            "type": "tool-input-available",
            "toolCallId": call_id,
            "toolName": name,
            "input": dict(arguments),
            "dynamic": True,
        })

    def _translate_tool_end(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        call_id = str(event.get("toolCallId") or "").strip()
        if not call_id:
            raise PiProtocolError("tool_execution_end lacks toolCallId")
        if "isError" not in event:
            # Pi always sets it. Inferring the flag from the result's shape is
            # how a failed tool gets rendered as a success.
            raise PiProtocolError(
                f"tool_execution_end {call_id!r} carries no isError flag"
            )
        yield {
            "type": "tool-output-available",
            "toolCallId": call_id,
            "output": {
                "content": event.get("result"),
                "isError": bool(event.get("isError")),
            },
        }
        file_tool = self._file_tools.pop(call_id, None)
        if file_tool and not event["isError"]:
            name, path = file_tool
            result = event.get("result")
            details = result.get("details") if isinstance(result, dict) else None
            patch = details.get("patch") if isinstance(details, dict) else None
            if name == "edit" and not isinstance(patch, str):
                raise PiProtocolError("successful edit has no native patch")
            yield file_changes_frame(call_id, name, [FileChange(
                path=path, diff=UnifiedDiff(patch=patch) if isinstance(patch, str) else None,
            )])

    # ── turn terminal ────────────────────────────────────────────────────
    @staticmethod
    def _translate_agent_end(event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """The loop stopped, which is never by itself the end of the turn.

        With ``willRetry`` an auto-retry is about to re-run the loop and more
        content follows on this same platform turn. Without it pi still emits
        ``agent_settled`` next, and only that event means the session is at
        rest. Either way this is recorded, not settled.
        """

        yield raw_event_frame("agent_end", event)

    def _translate_settled(self) -> dict[str, Any]:
        stop_reason = self._last_stop_reason
        if stop_reason is None:
            raise PiProtocolError(
                "pi settled without a stop reason; no assistant message ended"
            )
        finish_reason = _FINISH_REASONS.get(stop_reason)
        if finish_reason is None:
            raise PiProtocolError(
                f"pi settled on stop reason {stop_reason!r}, which does not end a turn"
            )
        self.terminal_seen = True
        frame: dict[str, Any] = {
            "type": "result",
            "finishReason": finish_reason,
            "__engine_terminal_reason": stop_reason,
        }
        if self._last_usage is not None:
            frame["usage"] = dict(self._last_usage)
        if finish_reason == "error":
            frame["error"] = {
                "code": "ENGINE_TURN_FAILED",
                "message": self._last_error_message or "pi ended the turn with an error",
            }
        return frame

    @property
    def outcome(self) -> str | None:
        """The platform outcome of the settled turn, or None before it settles."""

        if not self.terminal_seen or self._last_stop_reason is None:
            return None
        finish_reason = _FINISH_REASONS.get(self._last_stop_reason)
        return _OUTCOMES.get(finish_reason or "")


def _content_index(stream_event: dict[str, Any], kind: str) -> int:
    index = stream_event.get("contentIndex")
    if not isinstance(index, int) or isinstance(index, bool):
        raise PiProtocolError(f"{kind} has no integer contentIndex")
    return index
