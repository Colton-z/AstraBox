"""Hermes engine client over an OpenSandbox execd pipe session.

Hermes' supported embedding surface for a custom web host is newline-delimited
JSON-RPC on ``python -m tui_gateway.entry``.  OpenSandbox's supported long-lived
process surface is ``POST /pty`` plus the PTY WebSocket in pipe mode.  This
module composes those two public protocols; Hermes opens no TCP port and no
platform credential is copied into the sandbox.

One gateway process serves every conversation of one (user, assistant)
profile — the vendor's own multi-session design (``session.create`` /
``session.resume`` against a process-wide registry). Process residency lives
in :mod:`.hermes_gateway`; this module owns the wire pieces
(:class:`HermesTuiProcess`, the event translation) and the per-conversation
:class:`HermesTuiEngineClient` that consumes one session of the shared
gateway.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import shlex
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    EngineConversationBinding,
    EngineEventSink,
    EngineInputCommand,
    EngineOutputCheckpoint,
    EngineStreamDetached,
    EngineTurnReceipt,
    ResidentOutputCheckpoint,
    ResidentOutputSink,
)
from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
    canonical_child_run_data,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    EngineTurnEmission,
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_TOOL_APPROVAL,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
)
from astrabox.core.service.orchestrator.engine.hermes_cron import HermesCronDelivery
from astrabox.core.service.orchestrator.engine.resident_relay import ResidentRelay
from astrabox.persistence.repository.hermes_cron_repository import (
    HermesCronJobRepository,
)
from astrabox.core.service.orchestrator.runtime.hermes_backend_channel import (
    BackendDetached,
    BackendNotListening,
    HermesBackendChannel,
)

_CONNECT_TIMEOUT_SECONDS = 20.0
_RPC_TIMEOUT_SECONDS = 120.0

#: Context occupancy, in percent of the model's window, at which an idle
#: conversation is compressed ahead of need. Hermes compresses inside a turn
#: at 50% of the window, or 75% for windows under 512K tokens
#: (``agent/context_compressor.py`` at the pinned commit), and the user waits
#: for that. Compressing between turns, well below it, leaves every turn the
#: headroom it needs to run without compressing itself.
_IDLE_COMPACT_AT_PERCENT = 40
#: How long a conversation must stay quiet after a reply before it is compressed.
_IDLE_COMPACT_QUIET_SECONDS = 15.0
_COMPRESS_TIMEOUT_SECONDS = 900.0
_ANCHOR_PREFIX = "hermes-tui-v1."

logger = get_logger(__name__)


_INFORMATIONAL_EVENTS = frozenset(
    {
        "agent.ready",
        "background.complete",
        "browser.progress",
        "delegation.status",
        "gateway.ready",
        "gateway.stderr",
        "memory.committed",
        "message.start",
        "review.summary",
        "session.info",
        "session.title",
        "session.usage",
        "skin.changed",
        "status.update",
        "tool.generating",
        "tool.progress",
        "voice.status",
        "voice.transcript",
    }
)

_HERMES_CHILD_RUN_OPEN_EVENTS = frozenset(
    {"subagent.spawn_requested", "subagent.start"}
)
_HERMES_CHILD_RUN_UPDATE_EVENTS = frozenset(
    {"subagent.thinking", "subagent.tool", "subagent.progress"}
)


class HermesTuiRpcError(RuntimeError):
    """A JSON-RPC error returned by the pinned Hermes TUI Gateway."""

    def __init__(self, *, code: Any, message: str) -> None:
        super().__init__(f"Hermes TUI RPC {code}: {message}")
        self.code = code
        self.message = message


class HermesTuiProtocolError(RuntimeError):
    """The pinned gateway emitted a terminal shape the adapter cannot classify."""


@dataclass(frozen=True)
class HermesTuiWireEvent:
    event: dict[str, Any]
    output_offset: int
    input_command_id: str | None = None


def encode_turn_anchor(
    *,
    tui_session_id: str,
    turn_id: str,
) -> str:
    """Name one turn durably, by the two things that outlive an attachment.

    Deliberately NOT the gateway process this turn was established on. The
    engine is a supervised service and nothing names an instance of it: the
    vendor's `gateway.ready` carries only a UI skin and `/api/status` is
    byte-identical across a restart, both checked on a real box.

    Nothing is lost by leaving it out, measured rather than assumed: resuming a
    session id on a restarted backend is refused outright (`4007 session not
    found`), so a caller cannot silently continue against an engine that has
    forgotten the conversation. Both counterparts that embed this same gateway
    — Centaur and lotsoftick/hermes_client — check only whether the process is
    still there and carry continuity on the durable session id, which is what
    this anchor holds.

    An anchor this decoder rejects belongs to a turn that is rebuilt rather
    than resumed, which is the safe direction for a field that names one.
    """

    payload = {
        "tui_session_id": str(tui_session_id or "").strip(),
        "turn_id": str(turn_id or "").strip(),
    }
    if any(not value for value in payload.values()):
        raise ValueError("Hermes turn anchor requires TUI session and turn ids")
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return _ANCHOR_PREFIX + token


def decode_turn_anchor(value: str) -> dict[str, str]:
    text = str(value or "").strip()
    if not text.startswith(_ANCHOR_PREFIX):
        raise ValueError("not a Hermes TUI turn anchor")
    token = text[len(_ANCHOR_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid Hermes TUI turn anchor") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid Hermes TUI turn anchor payload")
    result = {
        key: str(decoded.get(key) or "").strip()
        for key in ("tui_session_id", "turn_id")
    }
    if any(not value for value in result.values()):
        raise ValueError("incomplete Hermes TUI turn anchor")
    return result


def _interaction_id(turn_id: str, payload: dict[str, Any]) -> str:
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{turn_id}\0{material}".encode("utf-8")).hexdigest()[:24]
    return f"hermes-approval-{digest}"


def _hermes_child_run_frame_id(
    event_type: str,
    child_run_id: str,
    payload: dict[str, Any],
    *,
    suffix: str,
) -> str:
    material = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"child-run:hermes:{child_run_id}:{event_type}:{digest}:{suffix}"


def _hermes_child_run_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    usage: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "reasoning_tokens", "api_calls"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            usage[key] = value
    tool_count = payload.get("tool_count")
    if isinstance(tool_count, int) and not isinstance(tool_count, bool):
        usage["tool_uses"] = tool_count
    duration_seconds = payload.get("duration_seconds")
    if isinstance(duration_seconds, (int, float)) and not isinstance(duration_seconds, bool):
        usage["duration_ms"] = round(float(duration_seconds) * 1000)
    cost_usd = payload.get("cost_usd")
    if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool):
        usage["cost_usd"] = float(cost_usd)
    return usage or None


def _hermes_child_run_message_content(
    event_type: str,
    child_run_id: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    if event_type == "subagent.thinking":
        text = str(payload.get("text") or "").strip()
        return [{"type": "thinking", "thinking": text}] if text else []
    if event_type == "subagent.tool":
        tool_name = str(payload.get("tool_name") or "").strip()
        if not tool_name:
            return []
        preview = str(payload.get("tool_preview") or payload.get("text") or "").strip()
        return [
            {
                "type": "tool_use",
                "id": _hermes_child_run_frame_id(
                    event_type,
                    child_run_id,
                    payload,
                    suffix="tool",
                ),
                "name": tool_name,
                "input": {"preview": preview} if preview else {},
            }
        ]
    if event_type != "subagent.complete":
        return []

    content: list[dict[str, Any]] = []
    output_tail = payload.get("output_tail")
    if isinstance(output_tail, list):
        for index, raw_item in enumerate(output_tail):
            if not isinstance(raw_item, dict):
                raise ChildRunProjectionError(
                    "Hermes subagent.complete output_tail must contain objects"
                )
            tool_use_id = f"hermes:{child_run_id}:tail:{index}"
            tool_name = str(raw_item.get("tool") or "tool").strip() or "tool"
            content.extend(
                [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": tool_name,
                        "input": {},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": str(raw_item.get("preview") or ""),
                        "is_error": raw_item.get("is_error") is True,
                    },
                ]
            )
    summary = str(payload.get("summary") or "").strip()
    if summary:
        content.append({"type": "text", "text": summary})
    return content


def _translate_hermes_child_run_event(
    event_type: str,
    payload: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    engine_ref = str(payload.get("subagent_id") or "").strip()
    if not engine_ref:
        raise ChildRunProjectionError(f"Hermes {event_type} event lacks subagent_id")
    if event_type == "subagent.complete":
        engine_status = str(payload.get("status") or "").strip()
        if not engine_status:
            raise ChildRunProjectionError("Hermes subagent.complete lacks status")
        event = "closed"
    elif event_type in _HERMES_CHILD_RUN_OPEN_EVENTS:
        engine_status = str(payload.get("status") or "").strip()
        event = "opened"
    elif event_type in _HERMES_CHILD_RUN_UPDATE_EVENTS:
        engine_status = str(payload.get("status") or "").strip()
        event = "updated"
    else:
        raise ChildRunProjectionError(
            f"Hermes child-run event is unsupported event_type={event_type!r}"
        )

    lifecycle: dict[str, Any] = {
        "kind": "lifecycle",
        "engineRef": str(payload.get("child_session_id") or engine_ref),
        "controlRef": engine_ref,
        "event": event,
        "engineEvent": event_type,
        "operations": [] if event == "closed" else ["stop"],
        "taskType": "delegate_task",
        "nativeState": {"subagent_id": engine_ref},
    }
    if payload.get("child_session_id"):
        lifecycle["nativeState"]["child_session_id"] = str(payload["child_session_id"])
    if engine_status:
        lifecycle["engineStatus"] = engine_status
    engine_reason = str(payload.get("reason") or payload.get("stop_reason") or "").strip()
    if engine_reason:
        lifecycle["engineReason"] = engine_reason
    parent_engine_ref = str(payload.get("parent_id") or "").strip()
    if parent_engine_ref:
        lifecycle["parentEngineRef"] = parent_engine_ref
    for source, target in (
        ("goal", "description"),
        ("tool_name", "lastToolName"),
        ("summary", "summary"),
        ("model", "model"),
    ):
        value = str(payload.get(source) or "").strip()
        if value:
            lifecycle[target] = value
    usage = _hermes_child_run_usage(payload)
    if usage is not None:
        lifecycle["usage"] = usage
    canonical_lifecycle = canonical_child_run_data(
        lifecycle,
        engine_kind="assistant",
    )
    yield session_scoped_engine_frame(
        {
            "type": "data-subagent",
            "id": _hermes_child_run_frame_id(
                event_type,
                engine_ref,
                payload,
                suffix="lifecycle",
            ),
            "data": canonical_lifecycle,
        }
    )

    content = _hermes_child_run_message_content(event_type, engine_ref, payload)
    if not content:
        return
    message_id = _hermes_child_run_frame_id(
        event_type,
        engine_ref,
        payload,
        suffix="message",
    )
    message: dict[str, Any] = {
        "kind": "message",
        "engineRef": lifecycle["engineRef"],
        "role": "assistant",
        "content": content,
        "messageId": message_id,
    }
    if parent_engine_ref:
        message["parentEngineRef"] = parent_engine_ref
    yield session_scoped_engine_frame(
        {
            "type": "data-subagent",
            "id": message_id,
            "data": canonical_child_run_data(message, engine_kind="assistant"),
        }
    )


def translate_tui_event(
    event: dict[str, Any],
    *,
    turn_id: str,
    active_tool_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Translate one official Hermes TUI event into AI SDK stream frames."""

    event_type = str(event.get("type") or "").strip()
    payload_raw = event.get("payload")
    payload = payload_raw if isinstance(payload_raw, dict) else {}

    if event_type == "message.delta":
        text = str(payload.get("text") or "")
        if text:
            yield {
                "type": "text-delta",
                "id": f"hermes-text:{turn_id}",
                "delta": text,
            }
        return

    if event_type == "message.complete":
        status = str(payload.get("status") or "complete").strip().lower()
        finish_reasons = {
            "complete": "stop",
            "completed": "stop",
            "interrupted": "cancelled",
            "cancelled": "cancelled",
            "error": "error",
            "failed": "error",
        }
        finish_reason = finish_reasons.get(status)
        if finish_reason is None:
            raise HermesTuiProtocolError(
                f"message.complete has unsupported status={status!r}"
            )
        frame: dict[str, Any] = {
            "type": "result",
            "finishReason": finish_reason,
            "__engine_terminal_reason": status,
        }
        usage = payload.get("usage")
        if isinstance(usage, dict):
            frame["usage"] = dict(usage)
        if finish_reason == "error":
            frame["error"] = {
                "code": "HERMES_TURN_FAILED",
                "message": str(payload.get("text") or payload.get("error") or ""),
            }
        yield frame
        return

    if event_type == "tool.start":
        tool_id = str(payload.get("tool_id") or "").strip()
        tool_name = str(payload.get("name") or "tool").strip() or "tool"
        arguments = payload.get("args")
        if isinstance(arguments, dict):
            tool_input = dict(arguments)
        else:
            # Hermes separates native arguments from the display preview.
            context = payload.get("context")
            tool_input = dict(context) if isinstance(context, dict) else {}
            if isinstance(context, str) and context.strip():
                tool_input = {"preview": context}
        # `dynamic: True` is what makes the AI SDK build a `dynamic-tool` part
        # rather than a typed `tool-<name>` one, and every console reader of a
        # tool part accepts only the former.
        yield {
            "type": "tool-input-start",
            "toolCallId": tool_id,
            "toolName": tool_name,
            "dynamic": True,
        }
        yield {
            "type": "tool-input-available",
            "toolCallId": tool_id,
            "toolName": tool_name,
            "input": tool_input,
            "dynamic": True,
        }
        return

    if event_type == "tool.complete":
        tool_id = str(payload.get("tool_id") or active_tool_id or "").strip()
        output = {key: value for key, value in payload.items() if key not in {"tool_id", "name"}}
        yield {
            "type": "tool-output-available",
            "toolCallId": tool_id,
            "output": output or str(payload.get("summary") or ""),
        }
        return

    if event_type == "reasoning.delta":
        text = str(payload.get("text") or "")
        if text:
            yield {
                "type": "reasoning-delta",
                "id": f"hermes-reasoning:{turn_id}",
                "delta": text,
            }
        return

    # Hermes maps reasoning_callback to ``reasoning.delta`` and
    # thinking_callback to ``thinking.delta`` in tui_gateway/server.py. The
    # latter carries a spinner label or its empty-string reset, not model
    # reasoning. Spinner labels have also been observed on the resident
    # WebSocket transport without a terminal, so suppress them here.

    # ``reasoning.available`` carries _on_tool_progress's preview snapshot,
    # not a reasoning increment; Hermes' acp_adapter/events.py also ignores it.
    # Treating it as a delta would append the preview as another reasoning
    # segment, potentially repeating the finished answer. The informational
    # path below preserves the event without rendering it.

    if event_type == "approval.request":
        tool_id = str(payload.get("tool_id") or active_tool_id or "").strip()
        command = str(payload.get("command") or "")
        tool_name = str(payload.get("tool_name") or "terminal")
        yield {
            "type": "interaction.request",
            "interactionId": _interaction_id(turn_id, payload),
            "payload": {
                "tool_name": tool_name,
                "presentation": PRESENTATION_TOOL_APPROVAL,
                "prompt": f"Allow {tool_name} to continue?",
                # The command the user is approving, verbatim. Hermes's
                # pattern vocabulary (the rule an "always" grant would
                # persist) is not carried: the platform exposes no "always"
                # choice yet, so a copied pattern would be an inert field.
                "raw_input": {"command": command},
                "tool_use_id": tool_id,
            },
        }
        return

    if event_type == "error":
        message = str(payload.get("message") or event.get("message") or "Hermes failed")
        yield {
            "type": "result",
            "finishReason": "error",
            "__engine_terminal_reason": "error",
            "error": {"code": "HERMES_TUI_ERROR", "message": message},
        }
        return

    if event_type.startswith("subagent."):
        yield from _translate_hermes_child_run_event(event_type, payload)
        return

    if event_type in _INFORMATIONAL_EVENTS:
        return

    # These interactive TUI events are retained as private diagnostics rather
    # than misrepresented as dangerous-command approval controls.
    if event_type in {"clarify.request", "secret.request", "sudo.request"}:
        yield {
            "type": "data-raw-event",
            "data": {
                "event_type": "hermes.tui_gateway",
                "subtype": event_type,
                "raw": dict(event),
            },
        }
        return

    # Hermes adds non-blocking telemetry events over time. Its own pinned Ink
    # client ignores event types it does not recognize; preserve that same
    # forward-compatible behavior while retaining the raw event privately for
    # support diagnostics. Known blocking requests are handled above and must
    # never fall through here.
    yield {
        "type": "data-raw-event",
        "data": {
            "event_type": "hermes.tui_gateway",
            "subtype": event_type or "unknown",
            "raw": dict(event),
        },
    }


class HermesTurnTranslator:
    """Frames for one Hermes run: the open text and reasoning blocks it carries.

    Hermes streams text and reasoning as bare deltas; the AI SDK stream needs
    each contiguous run of them wrapped in a start/end pair, closed when the
    other kind, a tool, an approval or the terminal arrives. A platform turn
    and an engine-started run translate the same way.

    ``block_prefix`` names the blocks; ``turn_id`` names approvals and the
    vendor translation's own ids. When ``offset`` is given, block ids carry it,
    which keeps a recovered stream's ids distinct from the committed ones.
    """

    def __init__(
        self,
        *,
        block_prefix: str,
        turn_id: str,
        recovered: bool = False,
        text_open_id: str | None = None,
        reasoning_open_id: str | None = None,
    ) -> None:
        self._block_prefix = block_prefix
        self.turn_id = turn_id
        self._recovered = recovered
        self._active_tool_id: str | None = None
        self._text_id = text_open_id
        self._text_open = bool(text_open_id)
        self._reasoning_id = reasoning_open_id
        self._reasoning_open = bool(reasoning_open_id)
        self._saw_delta = False
        self._segment = 0
        self._reasoning_segment = 0

    def restart(self, turn_id: str) -> None:
        """Begin the next run on the same stream, with no block open."""

        self.turn_id = turn_id
        self._active_tool_id = None
        self._text_id = None
        self._text_open = False
        self._reasoning_id = None
        self._reasoning_open = False
        self._saw_delta = False
        self._segment = 0
        self._reasoning_segment = 0

    def _close_text(self) -> list[dict[str, Any]]:
        if not self._text_open:
            return []
        frame = {"type": "text-end", "id": self._text_id}
        self._text_open = False
        self._text_id = None
        return [frame]

    def _close_reasoning(self) -> list[dict[str, Any]]:
        if not self._reasoning_open:
            return []
        frame = {"type": "reasoning-end", "id": self._reasoning_id}
        self._reasoning_open = False
        self._reasoning_id = None
        return [frame]

    def frames(self, event: dict[str, Any], *, offset: int | None) -> list[dict[str, Any]]:
        event_type = str(event.get("type") or "").strip()
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        out: list[dict[str, Any]] = []
        if event_type == "tool.start":
            self._active_tool_id = str(payload.get("tool_id") or "").strip() or None
        position = "" if offset is None else f":{offset}"

        # message.complete carries the full final string as a fallback. On a
        # recovered suffix that would duplicate deltas already committed
        # before the cursor, so only use it on an uninterrupted stream that
        # emitted no deltas.
        if (
            event_type == "message.complete"
            and not self._recovered
            and not self._saw_delta
            and str(payload.get("text") or "")
        ):
            self._text_id = f"hermes-text:{self._block_prefix}{position}"
            out.append({"type": "text-start", "id": self._text_id})
            out.append(
                {"type": "text-delta", "id": self._text_id, "delta": str(payload.get("text") or "")}
            )
            self._text_open = True

        for frame in translate_tui_event(
            event, turn_id=self.turn_id, active_tool_id=self._active_tool_id
        ):
            frame_type = str(frame.get("type") or "")
            if frame_type == "text-delta":
                out.extend(self._close_reasoning())
                if not self._text_open:
                    self._segment += 1
                    self._text_id = f"hermes-text:{self._block_prefix}{position}:{self._segment}"
                    out.append({"type": "text-start", "id": self._text_id})
                    self._text_open = True
                frame["id"] = self._text_id
                self._saw_delta = True
            elif frame_type == "reasoning-delta":
                out.extend(self._close_text())
                if not self._reasoning_open:
                    self._reasoning_segment += 1
                    self._reasoning_id = (
                        f"hermes-reasoning:{self._block_prefix}{position}:"
                        f"{self._reasoning_segment}"
                    )
                    out.append({"type": "reasoning-start", "id": self._reasoning_id})
                    self._reasoning_open = True
                frame["id"] = self._reasoning_id
            elif frame_type in {"tool-input-start", "interaction.request", "result"}:
                out.extend(self._close_text())
                out.extend(self._close_reasoning())
            out.append(frame)
        return out


class HermesTuiProcess:
    """One attachment to a box's resident Hermes backend.

    The socket is the platform's (:class:`HermesBackendChannel`); this class is
    Hermes' half of it — the JSON-RPC envelope, which record answers which
    request, the gateway's own readiness signal, and the event queue. The
    channel deliberately settles nothing on its own, because "a record with an
    ``id`` is a response" is Hermes' protocol and not every engine's.

    Transport failures cross the engine seam as :class:`EngineStreamDetached`.
    Translating them is this adapter's job: the platform channel must not
    depend on the engine contract to describe its own pipe dying.
    """

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._channel = HermesBackendChannel(
            url=url,
            headers=headers,
            label="Hermes backend",
            on_record=self._on_record,
            on_failure=self._on_failure,
        )
        self._events: asyncio.Queue[HermesTuiWireEvent | BaseException] = asyncio.Queue()
        self._input_requests: dict[str, tuple[str, str]] = {}
        self._gateway_ready = asyncio.Event()
        self._fatal: BaseException | None = None

    # ── delegated pipe state ─────────────────────────────────────────────
    @property
    def url(self) -> str:
        return self._channel.url

    @property
    def is_connected(self) -> bool:
        return self._channel.is_connected

    @property
    def fatal(self) -> BaseException | None:
        """This process's terminal failure, in the engine seam's vocabulary."""

        return self._fatal

    @property
    def backend_not_listening(self) -> bool:
        """Whether this attachment failed because nothing accepted it yet.

        Read from the channel, which classified the failure where it happened;
        the seam's vocabulary above it has no word for "try again shortly".
        """

        return isinstance(self._channel.fatal, BackendNotListening)

    async def current_output_offset(self) -> int:
        return await self._channel.current_output_offset()

    async def detach(self) -> None:
        await self._channel.detach()

    # ── connect ──────────────────────────────────────────────────────────
    async def connect(self, *, require_gateway_ready: bool) -> None:
        """Open the socket, and optionally wait for Hermes to announce itself.

        A connected socket only proves the backend is listening.
        ``gateway.ready`` is Hermes' own statement that it can answer, which a
        fresh attachment needs and a reconnect to a live one does not.
        """

        try:
            await self._channel.connect()
            if require_gateway_ready:
                await asyncio.wait_for(
                    self._gateway_ready.wait(), timeout=_CONNECT_TIMEOUT_SECONDS
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)
            assert self._fatal is not None
            raise self._fatal
        if self._fatal is not None:
            raise self._fatal

    # ── JSON-RPC ─────────────────────────────────────────────────────────
    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = _RPC_TIMEOUT_SECONDS,
        input_command_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.is_connected:
            raise EngineStreamDetached("Hermes TUI Gateway is not connected")
        request_id = uuid.uuid4().hex
        future = self._channel.register_request(request_id)
        if input_command_id is not None:
            if method != "prompt.submit":
                raise ValueError("input identity belongs to prompt.submit only")
            self._input_requests[request_id] = (
                str((params or {}).get("session_id") or ""), input_command_id
            )
        try:
            await self._channel.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": str(method),
                    "params": dict(params or {}),
                }
            )
            response = await asyncio.wait_for(future, timeout=float(timeout))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)
            assert self._fatal is not None
            raise self._fatal
        finally:
            self._channel.release_request(request_id)
            self._input_requests.pop(request_id, None)
        error = response.get("error")
        if isinstance(error, dict):
            raise HermesTuiRpcError(
                code=error.get("code"),
                message=str(error.get("message") or "request failed"),
            )
        result = response.get("result")
        return dict(result) if isinstance(result, dict) else {}

    async def next_event(self) -> HermesTuiWireEvent:
        item = await self._events.get()
        if isinstance(item, BaseException):
            raise item
        return item

    # ── Hermes' reading of one record ────────────────────────────────────
    async def _on_record(self, record: dict[str, Any], output_offset: int) -> None:
        response_id = str(record.get("id") or "").strip()
        if response_id:
            # A reply nobody is waiting for is dropped: Hermes answers every
            # request it was given, so an unmatched id belongs to a request
            # this process already gave up on.
            input_request = self._input_requests.get(response_id)
            if input_request is not None:
                session_id, command_id = input_request
                # Preserve the actual RPC receipt in the same wire order as
                # native events; this is not a vendor message.start event.
                await self._events.put(HermesTuiWireEvent(
                    event={"type": "prompt.submit", "session_id": session_id,
                           "payload": dict(record)},
                    output_offset=output_offset, input_command_id=command_id,
                ))
            self._channel.complete_request(response_id, record)
            return
        if record.get("method") != "event":
            return
        params = record.get("params")
        if not isinstance(params, dict):
            return
        if str(params.get("type") or "") == "gateway.ready":
            self._gateway_ready.set()
        await self._events.put(
            HermesTuiWireEvent(event=dict(params), output_offset=output_offset)
        )

    async def _on_failure(self, exc: BaseException) -> None:
        if self._fatal is not None:
            return
        self._fatal = (
            EngineStreamDetached(str(exc))
            if isinstance(exc, BackendDetached)
            else exc
        )
        self._gateway_ready.set()
        await self._events.put(self._fatal)

    async def _fail(self, exc: BaseException) -> None:
        """Make ``exc`` terminal for this process, through the channel."""

        await self._channel.fail(exc)
        if self._fatal is None:
            # The channel already held a failure, so this one never reached
            # _on_failure; adopt the one that actually settled it.
            await self._on_failure(self._channel.fatal or exc)


def _open_hermes_output_blocks(
    committed_frames: tuple[dict[str, Any], ...],
) -> tuple[str | None, str | None]:
    """Rebuild Hermes translator state from the turn's committed frames."""

    open_text_id: str | None = None
    open_reasoning_id: str | None = None
    for frame in committed_frames:
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            continue
        frame_type = str(payload.get("type") or "").strip()
        frame_id = payload.get("id")
        if not isinstance(frame_id, str) or not frame_id:
            continue
        if frame_type == "text-start":
            open_text_id = frame_id
        elif frame_type == "text-end" and open_text_id == frame_id:
            open_text_id = None
        elif frame_type == "reasoning-start":
            open_reasoning_id = frame_id
        elif frame_type == "reasoning-end" and open_reasoning_id == frame_id:
            open_reasoning_id = None
    return open_text_id, open_reasoning_id


class HermesGatewayEventStream(Protocol):
    """One session's ordered view of the shared gateway event stream."""

    tui_session_id: str

    @property
    def last_offset(self) -> int: ...

    async def next_event(self) -> HermesTuiWireEvent: ...


class HermesGatewayPort(Protocol):
    """What this client needs from the profile's resident gateway handle.

    The concrete implementation is
    :class:`~.hermes_gateway.HermesGatewayHandle`; the port exists so the
    client depends on the narrow consumer surface rather than on process
    ownership, which belongs to the handle and its registry.
    """

    @property
    def is_live(self) -> bool: ...

    @property

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = _RPC_TIMEOUT_SECONDS,
        input_command_id: str | None = None,
    ) -> dict[str, Any]: ...

    def subscribe(
        self, tui_session_id: str, *, after_offset: int = 0
    ) -> HermesGatewayEventStream: ...

    def unsubscribe(self, subscription: Any) -> None: ...



class _HermesRelaySeam:
    """Hermes' answers to the resident relay, in its TUI gateway vocabulary.

    Hermes opens every model run with ``message.start`` and ends it with
    ``message.complete`` (or ``error``), whether ``prompt.submit`` started it
    or Hermes did on its own. Native prompt receipts and the native inflight
    user echo identify platform inputs; merely waiting for an input must not
    claim a background run.

    A record's offset counts frames on one WebSocket attachment and starts
    again on the next (``runtime/hermes_backend_channel.py``), so the relay's
    sequence is that offset above ``base``: the highest position the
    Session's journal already holds when the attachment's reader starts.
    """

    engine_kind = "assistant"

    def __init__(self, client: "HermesTuiEngineClient", *, base: int = 0) -> None:
        self._client = client
        self._base = int(base)

    def sequence(self, wire: HermesTuiWireEvent) -> int:
        return self._base + int(wire.output_offset)

    @staticmethod
    def record(wire: HermesTuiWireEvent) -> dict[str, Any]:
        return wire.event

    def starts_run(self, record: dict[str, Any]) -> bool:
        # Hermes refuses some inputs with an ``error`` and no ``message.start``
        # (an ownership refusal, a cancel before the agent was ready). While a
        # submit is in flight that error is the run its input started.
        event_type = str(record.get("type") or "")
        return bool(record.get("__input_command_ids")) or event_type == "message.start" or (
            event_type == "error" and self._client.submitting
        )

    def platform_run(self, record: dict[str, Any], *, pending: bool, active: bool) -> bool | None:
        if "__platform_run" in record:
            return bool(record["__platform_run"])
        if self._client._unattributed_start is not None:
            return None
        return bool(record.get("__input_command_ids")) or active

    @staticmethod
    def settles_run(record: dict[str, Any]) -> bool:
        return str(record.get("type") or "") in {"message.complete", "error"}

    def handoff_to_platform(
        self, translator: HermesTurnTranslator, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not record.get("__input_command_ids"):
            return None
        # Close the resident response, not the native run. Its translator
        # survives so a tool already running still has its native identity.
        self._client._handoff_translator = translator
        return {"type": "result", "finishReason": "stop"}

    def response_id(self, record: dict[str, Any], sequence: int) -> str:
        key = str(self._client.engine_session_key or "").strip()
        if not key:
            raise RuntimeError("Hermes run began before the native session was known")
        return f"{key}:{sequence}"

    @staticmethod
    def new_translator() -> HermesTurnTranslator:
        run = f"hermes-run-{uuid.uuid4().hex[:12]}"
        return HermesTurnTranslator(block_prefix=run, turn_id=run)

    @staticmethod
    def translate(
        translator: HermesTurnTranslator, record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # An approval is opened through ``interaction`` instead.
        return [
            frame
            for frame in translator.frames(record, offset=None)
            if frame.get("type") != "interaction.request"
        ]

    @staticmethod
    async def child_facts(record: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def native_records() -> list[dict[str, Any]]:
        return []

    @staticmethod
    def owed_child_reads() -> list[dict[str, Any]]:
        return []

    @staticmethod
    def carries_child_facts(record: dict[str, Any]) -> bool:
        return False

    @staticmethod
    def interaction(record: dict[str, Any]) -> dict[str, Any] | None:
        if str(record.get("type") or "") != "approval.request":
            return None
        return next(iter(translate_tui_event(record, turn_id="resident")), None)


class HermesTuiEngineClient:
    """EngineClient for one conversation on the profile's resident gateway.

    The gateway process is shared by every conversation of one
    (user, assistant) profile — Hermes' own multi-session design. This client
    owns exactly one vendor session on it: creation or resume, the platform
    FIFO, and the translation of that session's events. It never spawns,
    reattaches, or deletes the gateway process; that residency belongs to
    :mod:`.hermes_gateway`, and a client whose gateway epoch (the PTY it
    pinned at session establishment) is gone reports ``is_live`` False so the
    runtime manager rebuilds it through the ordinary attach path.
    """

    def __init__(
        self,
        *,
        gateway: HermesGatewayPort,
        platform_session_id: str,
        resume_session_key: str | None = None,
        resident_output_sink: ResidentOutputSink | None = None,
        event_sink: EngineEventSink | None = None,
        output_checkpoint: ResidentOutputCheckpoint | None = None,
        read_cron_run_output: Any = None,
        cron_store: HermesCronJobRepository | None = None,
    ) -> None:
        self._gateway = gateway
        self._read_cron_run_output = read_cron_run_output
        self._cron_store = cron_store
        self._cron_delivery: HermesCronDelivery | None = None
        self._resident_output_sink = resident_output_sink
        self._event_sink = event_sink
        self._output_checkpoint = output_checkpoint
        #: The session's one reader for the life of its subscription; a turn
        #: reads what the relay hands it (see ``resident_relay``).
        self._relay: ResidentRelay | None = None
        self._submission_numbers: dict[str, int] = {}
        self._submitting = False
        #: Where this attachment's frame offsets start in the Session journal.
        self._sequence_base = 0
        #: The latest context occupancy Hermes reported, and the background
        #: compression waiting for the conversation to go quiet.
        self._context_percent: int | None = None
        self._compaction: asyncio.Task[None] | None = None
        self._platform_session_id = str(platform_session_id)
        self._resume_session_key = str(resume_session_key or "").strip() or None
        self._engine_session_key = self._resume_session_key
        self._tui_session_id: str | None = None
        self._subscription: HermesGatewayEventStream | None = None
        self._session_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._handoff_translator: HermesTurnTranslator | None = None
        self._active_receipt: EngineTurnReceipt | None = None
        self._active_command_id: str | None = None
        self._commands: dict[str, EngineInputCommand] = {}
        self._pending_command_ids: deque[str] = deque()
        self._native_input_status: dict[str, str] = {}
        self._native_user_row_ids: dict[str, int] = {}
        self._unattributed_start: HermesTuiWireEvent | None = None
        self._native_started_ids: set[str] = set()
        self._consumed_command_ids: set[str] = set()
        self._delivery_sequence = 0
        self._server_info: dict[str, Any] | None = None
        self._closed = False

    @property
    def is_live(self) -> bool:
        """Whether the client is open and its established session is attached.

        Before a vendor session exists, first use establishes one. After that,
        this property checks the gateway attachment, not the backend's process
        identity. Session continuity is checked by the vendor on use: an
        attempted resume after backend restart was observed to return
        ``4007 session not found``.
        """

        if self._closed:
            return False
        if self._tui_session_id is None:
            # No vendor session yet; the first use establishes one and fails
            # loudly there if the gateway cannot carry it.
            return True
        return self._gateway.is_live

    @property
    def engine_session_key(self) -> str | None:
        return self._engine_session_key

    @property
    def submitting(self) -> bool:
        """Whether a ``prompt.submit`` for a platform input is in flight."""

        return self._submitting

    @property
    def active_receipt(self) -> EngineTurnReceipt | None:
        return self._active_receipt

    def _session_established(self) -> bool:
        return (
            self._tui_session_id is not None
            and self._subscription is not None
            and self._gateway.is_live
        )

    async def _discard_half_activated_session(self, tui_session_id: str) -> None:
        """Destroy a vendor session whose activation did not complete.

        Activation is single and destructive on failure: a session that
        returned an id but never yielded its durable key must not linger in
        the shared gateway as an orphan nothing can resume.
        """

        with contextlib.suppress(BaseException):
            await self._gateway.request(
                "session.close", {"session_id": tui_session_id}
            )

    async def _ensure_session(self) -> None:
        if self._session_established():
            return
        async with self._session_lock:
            if self._session_established():
                return
            if self._closed:
                raise EngineStreamDetached("Hermes TUI client is closed")
            if not self._gateway.is_live:
                raise EngineStreamDetached(
                    "Hermes resident gateway is not live for this profile"
                )
            if self._resume_session_key:
                # Compression ends a Hermes session and continues it in a child,
                # and resume answers with the child it followed the lineage to
                # (``tui_gateway/methods_session.py`` ``_resume_follow_tip`` at the
                # pinned commit). The key that was asked for still names the
                # whole conversation: every later resume of it reaches the
                # newest child, so it stays this conversation's durable key.
                result = await self._gateway.request(
                    "session.resume",
                    {"session_id": self._resume_session_key, "cols": 120},
                )
                engine_session_key = self._resume_session_key
            else:
                result = await self._gateway.request(
                    "session.create", {"cols": 120}
                )
                engine_session_key = ""
            tui_session_id = str(result.get("session_id") or "").strip()
            if not tui_session_id:
                raise RuntimeError("Hermes did not return a TUI session id")
            if not engine_session_key:
                try:
                    title = await self._gateway.request(
                        "session.title", {"session_id": tui_session_id}
                    )
                except BaseException:
                    await self._discard_half_activated_session(tui_session_id)
                    raise
                engine_session_key = str(title.get("session_key") or "").strip()
            if not engine_session_key:
                await self._discard_half_activated_session(tui_session_id)
                raise RuntimeError("Hermes did not return its durable session key")
            self._subscription = self._gateway.subscribe(tui_session_id)
            self._tui_session_id = tui_session_id
            self._engine_session_key = engine_session_key
            info = result.get("info")
            if isinstance(info, dict):
                self._server_info = dict(info)
            await self._start_relay(self._subscription)

    async def _start_relay(self, subscription: HermesGatewayEventStream) -> None:
        """Make ``subscription`` the session's one reader, through a new relay."""

        await self._stop_relay()
        checkpoint, self._output_checkpoint = self._output_checkpoint, None
        sink = self._resident_output_sink
        if checkpoint is None and sink is not None:
            checkpoint = await sink.restore_resident_output(
                engine_kind="assistant", engine_session_key=self._engine_session_key
            )
        base = 0
        if checkpoint is not None:
            base = max(
                int(checkpoint.high_water_sequence or 0),
                int(checkpoint.replay_after_sequence or 0),
                int(checkpoint.after_sequence or 0),
            )
        self._sequence_base = base
        cron = self._new_cron_delivery(subscription)

        async def current_sequence() -> int:
            return base + int(subscription.last_offset)

        async def send_command(payload: dict[str, Any]) -> Any:
            raise RuntimeError("the Hermes relay sends no engine commands")

        async def next_record() -> HermesTuiWireEvent:
            wire = await subscription.next_event()
            if cron is not None:
                await cron.observe(wire.event)
            self._observe_context(wire.event)
            return await self._attribute_input(wire)

        relay = ResidentRelay(
            seam=_HermesRelaySeam(self, base=base),
            session_id=self._platform_session_id,
            engine_session_key=self._engine_session_key,
            next_record=next_record,
            send_command=send_command,
            current_sequence=current_sequence,
            resident_output_sink=self._resident_output_sink,
            event_sink=self._event_sink,
            checkpoint=checkpoint,
        )
        self._relay = relay
        relay.start()
        self._cron_delivery = cron
        if cron is not None:
            cron.start()

    def _new_cron_delivery(
        self, subscription: HermesGatewayEventStream
    ) -> HermesCronDelivery | None:
        """Deliver this conversation's own cron runs while its reader is attached."""

        profile_key = str(getattr(self._gateway, "profile_key", "") or "")
        sink = self._resident_output_sink
        if not profile_key or sink is None or self._read_cron_run_output is None:
            return None

        async def list_jobs() -> list[dict[str, Any]]:
            listed = await self._gateway.request(
                "cron.manage", {"action": "list", "include_disabled": True}
            )
            jobs = listed.get("jobs")
            return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []

        return HermesCronDelivery(
            profile_key=profile_key,
            session_id=self._platform_session_id,
            store=self._cron_store or HermesCronJobRepository(),
            sink=sink,
            list_jobs=list_jobs,
            read_run_output=self._read_cron_run_output,
            engine_session_key=lambda: self._engine_session_key,
            sequence=lambda: self._sequence_base + int(subscription.last_offset),
        )

    def _observe_context(self, event: dict[str, Any]) -> None:
        """Track context occupancy and compress after a reply once the conversation is quiet.

        Hermes' manual compression snapshots the history and summarizes it
        without holding the history lock, so a message the user sends while
        it runs is submitted and answered at once on the full history; the
        compression result is then discarded by Hermes' history-version fence
        and the next quiet period compresses again
        (``tui_gateway/session_compression.py`` at the pinned commit).
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
        self._compaction = asyncio.get_running_loop().create_task(
            self._compact_when_quiet(), name=f"hermes-idle-compaction-{self._platform_session_id}"
        )

    def _conversation_quiet(self) -> bool:
        return (
            not self._closed
            and self._active_receipt is None
            and not self._pending_command_ids
            and not self._submitting
        )

    async def _compact_when_quiet(self) -> None:
        await asyncio.sleep(_IDLE_COMPACT_QUIET_SECONDS)
        tui_session_id = self._tui_session_id
        if not tui_session_id or not self._conversation_quiet():
            return
        try:
            result = await self._gateway.request(
                "session.compress",
                {"session_id": tui_session_id},
                timeout=_COMPRESS_TIMEOUT_SECONDS,
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
            logger.info(
                "Hermes idle compression skipped: session=%s error=%s",
                self._platform_session_id,
                exc,
            )

    async def _stop_relay(self) -> None:
        compaction, self._compaction = self._compaction, None
        if compaction is not None and not compaction.done():
            compaction.cancel()
        cron, self._cron_delivery = self._cron_delivery, None
        if cron is not None:
            await cron.stop()
        relay, self._relay = self._relay, None
        if relay is not None:
            await relay.stop()

    async def _next_turn_wire(self) -> HermesTuiWireEvent:
        """The next record of the platform's own turn, as the relay routed it."""

        relay = self._relay
        if relay is None:
            raise EngineStreamDetached("Hermes relay is not running")
        if relay.failure is not None and relay.turn_inbox.empty():
            raise relay.failure
        item = await relay.turn_inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def _require_anchor_gateway(self, anchor: dict[str, str]) -> None:
        """Adopt a turn anchor, or fail loud when this client's attachment is gone.

        A turn anchor names the TUI session that ran the turn. Whether the
        backend still holds that session is asked separately, on recovery
        (:meth:`_backend_holds_session`): a backend that restarted lost it,
        and the turn then ends as a failure instead of waiting for events.
        """

        if not self._gateway.is_live:
            raise EngineStreamDetached(
                "Hermes resident backend does not carry this turn: the "
                "attachment it was established on is gone"
            )
        self._tui_session_id = anchor["tui_session_id"]

    async def _backend_holds_session(self, tui_session_id: str) -> bool:
        """Whether the backend still has this TUI session in memory.

        ``session.active_list`` reports every attachable session of the
        backend process, and Hermes draws a TUI session id at random for each
        session it creates, so an id missing from the list belongs to a
        process that has restarted since, or to a session torn down. Either
        way nothing will deliver the rest of that session's turn.
        """

        live = await self._gateway.request("session.active_list", {})
        return any(
            isinstance(row, dict) and str(row.get("id") or "") == tui_session_id
            for row in live.get("sessions") or []
        )

    async def _forget_lost_session(self) -> None:
        """Drop a TUI session the backend does not hold.

        The conversation itself is not lost: Hermes keeps it in its session
        store under the durable key, so the next message re-enters it with
        ``session.resume`` instead of addressing an id the backend does not
        know.
        """

        if self._subscription is not None:
            self._gateway.unsubscribe(self._subscription)
        self._subscription = None
        self._tui_session_id = None
        self._resume_session_key = self._engine_session_key or self._resume_session_key
        await self._stop_relay()

    @staticmethod
    def _input_consumed_frame(command: EngineInputCommand) -> dict[str, Any]:
        return {
            "type": "data-input-consumed",
            "id": f"input-consumed:{command.input_id}",
            "transient": True,
            "data": {
                "inputId": command.input_id,
                "responseMessageId": input_response_message_id(command.input_id),
                "content": command.content,
            },
        }

    async def deliver(self, command: EngineInputCommand) -> None:
        """Forward each durable FIFO input to Hermes' native busy-input policy."""

        if self._closed:
            raise EngineStreamDetached("Hermes TUI client is closed")
        if command.session_id != self._platform_session_id:
            raise RuntimeError(
                "Hermes input belongs to another conversation: "
                f"client={self._platform_session_id!r} "
                f"command={command.session_id!r}"
            )
        if command.sequence <= 0:
            raise ValueError("Hermes input sequence must be positive")
        input_response_message_id(command.input_id)
        async with self._input_lock:
            existing = self._commands.get(command.command_id)
            if existing is not None:
                if existing != command:
                    raise RuntimeError(
                        "Hermes input command identity collided with another payload"
                    )
                return
            if command.sequence <= self._delivery_sequence:
                raise RuntimeError(
                    "Hermes input commands are not a strict FIFO: "
                    f"last={self._delivery_sequence} next={command.sequence}"
                )
            await self._ensure_session()
            assert self._tui_session_id is not None
            self._delivery_sequence = command.sequence
            self._commands[command.command_id] = command
            self._pending_command_ids.append(command.command_id)
            relay = self._relay
            if relay is not None:
                self._submission_numbers[command.command_id] = relay.platform_input_submitted()
            self._submitting = True
            try:
                result = await self._gateway.request(
                    "prompt.submit",
                    {"session_id": self._tui_session_id, "text": command.content},
                    input_command_id=command.command_id,
                )
                status = str(result.get("status") or "")
                if status not in {"streaming", "queued", "steered", "redirected"}:
                    raise HermesTuiProtocolError(f"unknown prompt.submit status: {status!r}")
                self._native_input_status[command.command_id] = status
                row_id = result.get("user_row_id")
                if isinstance(row_id, int) and not isinstance(row_id, bool):
                    self._native_user_row_ids[command.command_id] = row_id
            except BaseException:
                self._pending_command_ids.remove(command.command_id)
                self._commands.pop(command.command_id, None)
                if relay is not None:
                    relay.platform_input_rejected()
                raise
            finally:
                self._submitting = False

    def _unstarted_inputs(self) -> list[str]:
        return [
            command_id for command_id in self._pending_command_ids
            if command_id not in self._native_started_ids
            and self._native_input_status.get(command_id) in {"streaming", "queued"}
        ]

    def _commands_for_native_user(self, user: str) -> list[str]:
        candidates: list[str] = []
        texts: list[str] = []
        for command_id in self._unstarted_inputs():
            candidates.append(command_id)
            texts.append(self._commands[command_id].content)
            # Hermes merges consecutive text-only queued prompts with this
            # separator. Decode the native echo; do not schedule inputs here.
            if "\n\n".join(texts).strip() == user.strip() and user.strip():
                return candidates
        return []

    async def _attribute_input(self, wire: HermesTuiWireEvent) -> HermesTuiWireEvent:
        """Correlate native receipts and run starts with already-forwarded inputs."""

        event = wire.event
        command_ids: list[str] = []
        async with self._input_lock:
            if wire.input_command_id is not None:
                result = event.get("payload", {}).get("result", {})
                if result.get("status") in {"steered", "redirected"}:
                    command_ids = [wire.input_command_id]
            elif event.get("type") == "message.start" and self._unstarted_inputs():
                live = await self._gateway.request(
                    "session.activate", {"session_id": self._tui_session_id, "omit_messages": True}
                )
                inflight = live.get("inflight")
                user = str(inflight.get("user") or "") if isinstance(inflight, dict) else ""
                command_ids = self._commands_for_native_user(user)
                if not command_ids:
                    # A fast native run can clear inflight before this read.
                    # Keep its original wire prelude until its own durable row
                    # receipt identifies it, rather than claim another run.
                    self._unattributed_start = wire
            elif event.get("type") == "message.complete" and self._unattributed_start is not None:
                payload = event.get("payload") or {}
                persisted = payload.get("persisted_turn") or {}
                row_id = persisted.get("user_row_id")
                for command_id in self._unstarted_inputs():
                    if row_id is not None and self._native_user_row_ids.get(command_id) == row_id:
                        command_ids = [command_id]
                        break
                if not command_ids:
                    if not isinstance(row_id, int):
                        raise HermesTuiProtocolError("native run completed without an attributable user row")
                    live = await self._gateway.request(
                        "session.activate", {"session_id": self._tui_session_id}
                    )
                    for message in live.get("messages") or []:
                        if message.get("role") == "user" and message.get("row_id") == row_id:
                            command_ids = self._commands_for_native_user(str(message.get("text") or ""))
                            break
                    if (not command_ids and self._unstarted_inputs()
                            and not live.get("running") and not live.get("queued")):
                        raise HermesTuiProtocolError("Hermes settled without handling its accepted input")
                start, self._unattributed_start = self._unattributed_start, None
                start.event["__input_command_ids"] = command_ids
                self._native_started_ids.update(command_ids)
                return HermesTuiWireEvent(
                    event={**event, "__platform_run": bool(command_ids)},
                    output_offset=wire.output_offset,
                )
            elif event.get("type") == "error" and self._unattributed_start is not None:
                raise HermesTuiProtocolError("native run failed before its input could be identified")
            self._native_started_ids.update(command_ids)
        if not command_ids:
            return wire
        return HermesTuiWireEvent(
            event={**event, "__input_command_ids": command_ids},
            output_offset=wire.output_offset, input_command_id=wire.input_command_id,
        )

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        """Attach to the native run of an already-forwarded input."""

        if self._closed:
            raise EngineStreamDetached("Hermes TUI client is closed")
        if self._active_receipt is not None:
            if self._active_command_id == command.command_id:
                return self._active_receipt
            raise RuntimeError("Hermes already has an active turn")
        if command.command_id not in self._commands:
            if consumption_confirmed:
                self._commands[command.command_id] = command
                self._delivery_sequence = max(self._delivery_sequence, command.sequence)
            else:
                # Some seam callers attach their first input directly. They
                # still use the same immediate native delivery entry point.
                await self.deliver(command)
        await self._ensure_session()
        assert self._tui_session_id is not None
        assert self._engine_session_key is not None
        self._active_command_id = command.command_id
        if not consumption_confirmed and self._relay is not None:
            self._relay.platform_turn_begins(self._submission_numbers.get(command.command_id))
        if consumption_confirmed:
            with contextlib.suppress(ValueError):
                self._pending_command_ids.remove(command.command_id)
            self._consumed_command_ids.add(command.command_id)
            if self._relay is not None:
                self._relay.platform_turn_reattached()
        receipt = EngineTurnReceipt(
            engine_turn_id=encode_turn_anchor(
                tui_session_id=self._tui_session_id,
                turn_id=command.command_id,
            ),
            engine_session_key=self._engine_session_key,
            started_at_monotonic_ns=time.monotonic_ns(),
            input_id=command.input_id,
            input_consumed=consumption_confirmed,
        )
        self._active_receipt = receipt
        return receipt

    async def bind_conversation(
        self,
        binding: EngineConversationBinding,
    ) -> None:
        """Resume the durable Hermes session before this runtime is usable."""

        if binding.platform_session_id != self._platform_session_id:
            raise RuntimeError(
                "Hermes conversation identity mismatch: "
                f"client={self._platform_session_id!r} "
                f"binding={binding.platform_session_id!r}"
            )
        durable_key = str(binding.engine_session_key or "").strip() or None
        if durable_key != self._resume_session_key:
            raise RuntimeError(
                "Hermes resume key does not match the durable conversation: "
                f"configured={self._resume_session_key!r} durable={durable_key!r}"
            )
        await self._ensure_session()
        if durable_key and self._engine_session_key != durable_key:
            raise RuntimeError(
                "Hermes resumed a different native conversation: "
                f"expected={durable_key!r} actual={self._engine_session_key!r}"
            )

    async def iter_turn_events(
        self,
        receipt: EngineTurnReceipt,
    ) -> AsyncIterator[EngineTurnEmission]:
        async for frame in self._iter_events(
            receipt,
            text_block_open_id=None,
            reasoning_block_open_id=None,
            recovered=False,
        ):
            yield emission_from_translated_frame(frame)

    def iter_reconnected_turn_events(
        self,
        *,
        engine_turn_id: str,
        output_checkpoint: EngineOutputCheckpoint,
    ) -> AsyncIterator[EngineTurnEmission]:
        text_block_open_id, reasoning_block_open_id = _open_hermes_output_blocks(
            output_checkpoint.committed_frames
        )
        if not self._engine_session_key:
            raise RuntimeError("Hermes output reconnect has no durable native session key")
        receipt = EngineTurnReceipt(
            engine_turn_id=engine_turn_id,
            engine_session_key=self._engine_session_key,
            started_at_monotonic_ns=time.monotonic_ns(),
        )
        frames = self._iter_events(
            receipt,
            text_block_open_id=text_block_open_id,
            reasoning_block_open_id=reasoning_block_open_id,
            recovered=True,
        )
        return self._typed_events(frames)

    @staticmethod
    async def _typed_events(
        frames: AsyncIterator[dict[str, Any]],
    ) -> AsyncIterator[EngineTurnEmission]:
        async for frame in frames:
            yield emission_from_translated_frame(frame)

    async def _iter_events(
        self,
        receipt: EngineTurnReceipt,
        *,
        text_block_open_id: str | None,
        reasoning_block_open_id: str | None,
        recovered: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        anchor = decode_turn_anchor(receipt.engine_turn_id)
        self._require_anchor_gateway(anchor)
        if recovered and not await self._backend_holds_session(anchor["tui_session_id"]):
            await self._forget_lost_session()
            yield {
                "type": "result",
                "finishReason": "error",
                "__engine_terminal_reason": "error",
                "error": {
                    "code": "HERMES_SESSION_LOST",
                    "message": (
                        "The Assistant's Hermes backend does not hold the session "
                        "this reply was running in, so the reply ended before it "
                        "finished. This happens when the backend restarts."
                    ),
                },
            }
            return
        if receipt.engine_session_key:
            self._engine_session_key = receipt.engine_session_key
        if (
            recovered
            or self._subscription is None
            or self._subscription.tui_session_id != anchor["tui_session_id"]
        ):
            # This transport subscribes to live events and requests no vendor
            # replay of events missed during a host disconnect; the durable
            # recovery lane settles the recovered turn. A new subscription only
            # receives what the backend sends from now on, and the stored
            # sequence counted frames on an attachment that may be gone, so it
            # filters nothing here. See docs/maintainers/hermes-transport.md.
            if self._subscription is not None:
                self._gateway.unsubscribe(self._subscription)
            self._subscription = self._gateway.subscribe(anchor["tui_session_id"])
            await self._start_relay(self._subscription)
        if recovered and self._relay is not None:
            self._relay.platform_turn_reattached()
        active_command = self._commands.get(self._active_command_id or "")
        if not recovered and active_command is None and not receipt.input_consumed:
            raise RuntimeError("Hermes active receipt has no matching FIFO command")
        translator = HermesTurnTranslator(
            block_prefix=anchor["turn_id"],
            turn_id=(
                active_command.command_id if active_command is not None else anchor["turn_id"]
            ),
            recovered=recovered,
            text_open_id=text_block_open_id,
            reasoning_open_id=reasoning_block_open_id,
        )
        while True:
            # The relay is the subscription's one reader; it hands this turn
            # the records of the run its input started, in wire order.
            wire = await self._next_turn_wire()
            if self._handoff_translator is not None:
                translator, self._handoff_translator = self._handoff_translator, None
            for command_id in wire.event.get("__input_command_ids", []):
                command = self._commands.get(command_id)
                if command is None or command_id in self._consumed_command_ids:
                    continue
                for closing in translator._close_text() + translator._close_reasoning():
                    yield closing
                self._consumed_command_ids.add(command_id)
                with contextlib.suppress(ValueError):
                    self._pending_command_ids.remove(command_id)
                active_command = command
                self._active_command_id = command_id
                # Keep native tool identity and delta history across a steer.
                # A correction does not finish the running tool or native run.
                translator.turn_id = command_id
                yield self._input_consumed_frame(command)
            if wire.event.get("type") == "prompt.submit":
                yield {
                    "type": "data-raw-event",
                    "data": {"event_type": "prompt.submit", "subtype": "receipt",
                             "raw": {"command_id": wire.input_command_id,
                                     "response": wire.event.get("payload")}},
                    "__engine_sequence_number": self._sequence_base + wire.output_offset,
                }
                continue
            for frame in translator.frames(wire.event, offset=wire.output_offset):
                frame_type = str(frame.get("type") or "")
                if frame_type == "result":
                    if self._pending_command_ids:
                        yield {
                            "type": "response-result",
                            "data": {key: value for key, value in frame.items() if key != "type"},
                            "__engine_sequence_number": self._sequence_base + wire.output_offset,
                        }
                        translator.restart(translator.turn_id)
                        continue
                out = dict(frame)
                out["__engine_sequence_number"] = self._sequence_base + wire.output_offset
                # Settle BEFORE the terminal frame is yielded. The consumer
                # stops iterating as soon as it has that frame, which closes
                # this generator — anything after the yield never runs, and
                # the turn slot would stay held. The next message in the same
                # conversation then fails with "Hermes already has an active
                # turn", which is a defect only a SECOND turn can show: every
                # probe and the Assistant e2e send one turn per conversation,
                # so it reached the product. `pi_client` carries the same
                # comment for the same reason, on the same shape of loop.
                if frame_type == "result":
                    for command_id in self._consumed_command_ids:
                        self._commands.pop(command_id, None)
                        self._submission_numbers.pop(command_id, None)
                        self._native_input_status.pop(command_id, None)
                        self._native_user_row_ids.pop(command_id, None)
                        self._native_started_ids.discard(command_id)
                    self._consumed_command_ids.clear()
                    self._active_receipt = None
                    self._active_command_id = None
                    if self._relay is not None:
                        self._relay.platform_turn_finished()
                elif frame_type == "interaction.request":
                    # The turn is NOT over: an approval suspends it, and the
                    # receipt is what the answer reattaches to.
                    self._active_receipt = receipt
                yield out
                if frame_type in {"interaction.request", "result"}:
                    return

    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        anchor = decode_turn_anchor(receipt.engine_turn_id)
        self._require_anchor_gateway(anchor)
        result = await self._gateway.request(
            "session.interrupt", {"session_id": anchor["tui_session_id"]}
        )
        return str(result.get("status") or "").strip() == "interrupted"

    async def interrupt_active_turn(self) -> bool:
        if self._active_receipt is None:
            return False
        return await self.cancel_turn(self._active_receipt)

    async def stop_child_run(self, control_ref: str) -> None:
        await self._ensure_session()
        assert self._tui_session_id is not None
        await self._gateway.request(
            "subagent.interrupt",
            {
                "session_id": self._tui_session_id,
                "subagent_id": str(control_ref),
            },
        )

    async def set_permission_mode(self, mode: str) -> None:
        raise NotImplementedError(f"Hermes TUI Gateway has no AstraBox permission mode {mode!r}")

    async def get_server_info(self) -> dict[str, Any] | None:
        await self._ensure_session()
        return dict(self._server_info) if isinstance(self._server_info, dict) else None

    async def submit_interaction_response(
        self,
        receipt: EngineTurnReceipt,
        *,
        pending: dict[str, Any],
        response: dict[str, Any],
    ) -> bool:
        if not str(pending.get("interaction_id") or "").strip():
            return False
        decision = str(response.get("decision") or "").strip().lower()
        hermes_choice = {"approve": "once", "reject": "deny"}.get(decision)
        if hermes_choice is None:
            raise ValueError(f"unknown Hermes approval decision {decision!r}")
        # A deny comment has no Hermes channel: approval.respond carries only
        # the session and the choice. The rendered reply still reaches the
        # transcript through the platform's answer continuation.
        anchor = decode_turn_anchor(receipt.engine_turn_id)
        self._require_anchor_gateway(anchor)
        result = await self._gateway.request(
            "approval.respond",
            {
                "session_id": anchor["tui_session_id"],
                "choice": hermes_choice,
            },
        )
        self._active_receipt = receipt
        return bool(result.get("resolved"))

    async def get_capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(
            engine_kind="assistant",
            tools=[],
            permission_modes=[],
            supports_interaction=True,
            supports_child_run_control=True,
            supports_server_info=True,
            extra={
                "transport": "hermes_tui_json_rpc_over_hermes_serve_websocket",
            },
        )

    async def close(self) -> None:
        self._closed = True
        self._consumed_command_ids.clear()
        await self._stop_relay()
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            self._gateway.unsubscribe(subscription)

    async def dispose(self) -> bool:
        """Permanently close this conversation's session on the gateway.

        ``close`` remains reconnect-safe. This stronger operation is reserved
        for delete/archive/end. It ends only this conversation's vendor
        session: the gateway process and its PTY are the profile's resident
        runtime, shared with the profile's other conversations, and are torn
        down with the sandbox (or by the gateway registry on a config
        restart), never by one conversation ending.
        """
        self._closed = True
        async with self._session_lock:
            tui_session_id = str(self._tui_session_id or "").strip()
            closed = False
            if tui_session_id and self._gateway.is_live:
                try:
                    await self._gateway.request(
                        "session.close",
                        {"session_id": tui_session_id},
                    )
                    closed = True
                except Exception as exc:
                    logger.warning(
                        "Hermes session.close failed on dispose: "
                        "platform_session=%s tui_session=%s err=%s",
                        self._platform_session_id,
                        tui_session_id,
                        exc,
                    )
            await self._stop_relay()
            subscription, self._subscription = self._subscription, None
            if subscription is not None:
                self._gateway.unsubscribe(subscription)
            self._tui_session_id = None
            self._active_receipt = None
            self._active_command_id = None
            self._pending_command_ids.clear()
            self._commands.clear()
            self._native_input_status.clear()
            self._native_user_row_ids.clear()
            self._native_started_ids.clear()
            self._unattributed_start = None
            return closed


__all__ = [
    "HermesGatewayEventStream",
    "HermesGatewayPort",
    "HermesTuiEngineClient",
    "HermesTuiProcess",
    "HermesTuiRpcError",
    "HermesTuiWireEvent",
    "decode_turn_anchor",
    "encode_turn_anchor",
    "translate_tui_event",
]
