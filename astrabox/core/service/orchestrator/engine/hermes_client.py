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
    EngineInputCommand,
    EngineOutputCheckpoint,
    EngineStreamDetached,
    EngineTurnReceipt,
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
from astrabox.core.service.orchestrator.runtime.execd_json_lines import (
    ExecdChannelDetached,
)
from astrabox.core.service.orchestrator.runtime.hermes_backend_channel import (
    HermesBackendChannel,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    ResolvedExecdEndpoint,
)

_CONNECT_TIMEOUT_SECONDS = 20.0
_RPC_TIMEOUT_SECONDS = 120.0
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
            raise ChildRunProjectionError(
                "Hermes subagent.complete lacks status"
            )
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
        "engineRef": engine_ref,
        "controlRef": engine_ref,
        "event": event,
        "engineEvent": event_type,
        "operations": [] if event == "closed" else ["stop"],
        "taskType": "delegate_task",
    }
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
        "engineRef": engine_ref,
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
        context = payload.get("context")
        tool_input = dict(context) if isinstance(context, dict) else {}
        if isinstance(context, str) and context.strip():
            # Hermes 0.14.0 sends ``build_tool_preview(...)`` here, not the
            # original argument object. Preserve the official information
            # without pretending the preview is a parsed command payload.
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
        dial: tuple[str, int] | None = None,
    ) -> None:
        self._channel = HermesBackendChannel(
            url=url,
            headers=headers,
            dial=dial,
            label="Hermes backend",
            on_record=self._on_record,
            on_failure=self._on_failure,
        )
        self._events: asyncio.Queue[HermesTuiWireEvent | BaseException] = asyncio.Queue()
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
    ) -> dict[str, Any]:
        if not self.is_connected:
            raise EngineStreamDetached("Hermes TUI Gateway is not connected")
        request_id = uuid.uuid4().hex
        future = self._channel.register_request(request_id)
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
            if isinstance(exc, ExecdChannelDetached)
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
    ) -> dict[str, Any]: ...

    def subscribe(
        self, tui_session_id: str, *, after_offset: int = 0
    ) -> HermesGatewayEventStream: ...

    def unsubscribe(self, subscription: Any) -> None: ...



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
    ) -> None:
        self._gateway = gateway
        self._platform_session_id = str(platform_session_id)
        self._resume_session_key = str(resume_session_key or "").strip() or None
        self._engine_session_key = self._resume_session_key
        self._tui_session_id: str | None = None
        self._subscription: HermesGatewayEventStream | None = None
        self._session_lock = asyncio.Lock()
        self._input_lock = asyncio.Lock()
        self._active_receipt: EngineTurnReceipt | None = None
        self._active_command_id: str | None = None
        self._commands: dict[str, EngineInputCommand] = {}
        self._pending_command_ids: deque[str] = deque()
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
                result = await self._gateway.request(
                    "session.resume",
                    {"session_id": self._resume_session_key, "cols": 120},
                )
                engine_session_key = str(
                    result.get("resumed") or self._resume_session_key
                ).strip()
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

    def _require_anchor_gateway(self, anchor: dict[str, str]) -> None:
        """Adopt a turn anchor, or fail loud when its gateway epoch is gone.

        A turn anchor names the PTY of the gateway that ran it. When the
        resident gateway was replaced (park, crash, config restart), the
        events of that turn died with the old process; pretending the new
        gateway can serve them would silently cross epochs, so settling then
        belongs to the durable recovery lane.
        """

        if not self._gateway.is_live:
            raise EngineStreamDetached(
                "Hermes resident backend does not carry this turn: the "
                "attachment it was established on is gone"
            )
        self._tui_session_id = anchor["tui_session_id"]

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
        """Accept a platform command into the per-conversation FIFO."""

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
            self._delivery_sequence = command.sequence
            self._commands[command.command_id] = command
            self._pending_command_ids.append(command.command_id)

    async def _submit_fifo_head(
        self,
        *,
        expected_command_id: str | None = None,
    ) -> EngineInputCommand | None:
        async with self._input_lock:
            if not self._pending_command_ids:
                return None
            command_id = self._pending_command_ids[0]
            if expected_command_id is not None and command_id != expected_command_id:
                raise RuntimeError(
                    "Hermes input consumption is not the FIFO head: "
                    f"head={command_id!r} requested={expected_command_id!r}"
                )
            command = self._commands[command_id]
            try:
                await self._ensure_session()
                assert self._tui_session_id is not None
                await self._gateway.request(
                    "prompt.submit",
                    {"session_id": self._tui_session_id, "text": command.content},
                )
            except BaseException:
                # A submit that raised never proved consumption, and consumption
                # is proven by the engine's own boundary frame rather than by
                # this call returning. Leaving the command queued makes it the
                # head forever: the next turn arrives with its own command,
                # finds someone else at the front, and is refused — so one lost
                # sandbox silences the conversation for good. The durable FIFO
                # is the authority on what is owed and redelivers it.
                self._pending_command_ids.popleft()
                raise
            self._pending_command_ids.popleft()
            self._active_command_id = command.command_id
            return command

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        """Start or reattach the stream for the FIFO head."""

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
                await self.deliver(command)
        # Queued above, and everything from here to the submit can fail against
        # a sandbox that is gone. A command left in the queue becomes its head
        # forever: the next turn arrives with its own command, is told it is not
        # the head, and the conversation never speaks again — so one lost box
        # would be permanent. Nothing here proves consumption, which only the
        # engine's own boundary frame does, so the durable FIFO still owes this
        # input and redelivers it.
        try:
            await self._ensure_session()
            assert self._tui_session_id is not None
            assert self._engine_session_key is not None
            if consumption_confirmed:
                with contextlib.suppress(ValueError):
                    self._pending_command_ids.remove(command.command_id)
                self._active_command_id = command.command_id
                self._consumed_command_ids.add(command.command_id)
            else:
                started = await self._submit_fifo_head(
                    expected_command_id=command.command_id
                )
                if started is None:
                    raise RuntimeError("Hermes FIFO head disappeared before dispatch")
        except BaseException:
            async with self._input_lock:
                with contextlib.suppress(ValueError):
                    self._pending_command_ids.remove(command.command_id)
            raise
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
            starting_after_sequence=None,
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
            starting_after_sequence=output_checkpoint.after_sequence,
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
        starting_after_sequence: int | None,
        text_block_open_id: str | None,
        reasoning_block_open_id: str | None,
        recovered: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        anchor = decode_turn_anchor(receipt.engine_turn_id)
        self._require_anchor_gateway(anchor)
        if (
            recovered
            or self._subscription is None
            or self._subscription.tui_session_id != anchor["tui_session_id"]
        ):
            # A fresh subscription starts after the durable cursor to avoid
            # duplicate delivery. This transport subscribes to live events; it
            # does not request vendor replay of events missed during a host
            # disconnect. The durable recovery lane settles the recovered turn.
            # See docs/maintainers/hermes-transport.md.
            if self._subscription is not None:
                self._gateway.unsubscribe(self._subscription)
            self._subscription = self._gateway.subscribe(
                anchor["tui_session_id"],
                after_offset=int(starting_after_sequence or 0),
            )
        subscription = self._subscription
        assert subscription is not None
        if receipt.engine_session_key:
            self._engine_session_key = receipt.engine_session_key
        active_command = self._commands.get(self._active_command_id or "")
        if not recovered and active_command is None and not receipt.input_consumed:
            raise RuntimeError("Hermes active receipt has no matching FIFO command")
        if (
            active_command is not None
            and not recovered
            and active_command.command_id not in self._consumed_command_ids
        ):
            self._consumed_command_ids.add(active_command.command_id)
            yield self._input_consumed_frame(active_command)

        active_tool_id: str | None = None
        text_id = text_block_open_id
        text_open = bool(text_id)
        reasoning_id = reasoning_block_open_id
        reasoning_open = bool(reasoning_id)
        saw_delta = False
        segment = 0
        reasoning_segment = 0

        while True:
            # Session routing and offset dedup live in the gateway pump and
            # the subscription; what arrives here is this session's events
            # plus unscoped process-level notices, exactly once each.
            wire = await subscription.next_event()
            event = wire.event
            event_type = str(event.get("type") or "").strip()
            payload = event.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if event_type == "tool.start":
                active_tool_id = str(payload.get("tool_id") or "").strip() or None

            # message.complete carries the full final string as a fallback. On
            # a recovered suffix that would duplicate deltas already committed
            # before the cursor, so only use it on an uninterrupted stream that
            # genuinely emitted no deltas.
            if (
                event_type == "message.complete"
                and not recovered
                and not saw_delta
                and str(payload.get("text") or "")
            ):
                text_id = f"hermes-text:{anchor['turn_id']}:{wire.output_offset}"
                yield {
                    "type": "text-start",
                    "id": text_id,
                    "__engine_sequence_number": wire.output_offset,
                }
                yield {
                    "type": "text-delta",
                    "id": text_id,
                    "delta": str(payload.get("text") or ""),
                    "__engine_sequence_number": wire.output_offset,
                }
                text_open = True

            frames = list(
                translate_tui_event(
                    event,
                    turn_id=(
                        active_command.command_id
                        if active_command is not None
                        else anchor["turn_id"]
                    ),
                    active_tool_id=active_tool_id,
                )
            )
            for frame in frames:
                frame_type = str(frame.get("type") or "")
                if frame_type == "text-delta":
                    if reasoning_open:
                        yield {
                            "type": "reasoning-end",
                            "id": reasoning_id,
                            "__engine_sequence_number": wire.output_offset,
                        }
                        reasoning_open = False
                        reasoning_id = None
                    if not text_open:
                        segment += 1
                        text_id = f"hermes-text:{anchor['turn_id']}:{wire.output_offset}:{segment}"
                        yield {
                            "type": "text-start",
                            "id": text_id,
                            "__engine_sequence_number": wire.output_offset,
                        }
                        text_open = True
                    frame["id"] = text_id
                    saw_delta = True
                elif frame_type == "reasoning-delta":
                    if text_open:
                        yield {
                            "type": "text-end",
                            "id": text_id,
                            "__engine_sequence_number": wire.output_offset,
                        }
                        text_open = False
                        text_id = None
                    if not reasoning_open:
                        reasoning_segment += 1
                        reasoning_id = (
                            f"hermes-reasoning:{anchor['turn_id']}:"
                            f"{wire.output_offset}:{reasoning_segment}"
                        )
                        yield {
                            "type": "reasoning-start",
                            "id": reasoning_id,
                            "__engine_sequence_number": wire.output_offset,
                        }
                        reasoning_open = True
                    frame["id"] = reasoning_id
                elif frame_type in {
                    "tool-input-start",
                    "interaction.request",
                    "result",
                }:
                    if text_open:
                        yield {
                            "type": "text-end",
                            "id": text_id,
                            "__engine_sequence_number": wire.output_offset,
                        }
                        text_open = False
                        text_id = None
                    if reasoning_open:
                        yield {
                            "type": "reasoning-end",
                            "id": reasoning_id,
                            "__engine_sequence_number": wire.output_offset,
                        }
                        reasoning_open = False
                        reasoning_id = None
                if frame_type == "result":
                    completed_command = active_command
                    next_command = await self._submit_fifo_head()
                    if next_command is not None:
                        if completed_command is not None:
                            self._commands.pop(completed_command.command_id, None)
                            self._consumed_command_ids.discard(completed_command.command_id)
                        yield {
                            "type": "response-result",
                            "data": {key: value for key, value in frame.items() if key != "type"},
                            "__engine_sequence_number": wire.output_offset,
                        }
                        active_command = next_command
                        active_tool_id = None
                        text_id = None
                        text_open = False
                        reasoning_id = None
                        reasoning_open = False
                        saw_delta = False
                        segment = 0
                        reasoning_segment = 0
                        self._consumed_command_ids.add(next_command.command_id)
                        yield self._input_consumed_frame(next_command)
                        continue
                out = dict(frame)
                out["__engine_sequence_number"] = wire.output_offset
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
                    if active_command is not None:
                        self._commands.pop(active_command.command_id, None)
                        self._consumed_command_ids.discard(active_command.command_id)
                    self._active_receipt = None
                    self._active_command_id = None
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
                "transport": "hermes_tui_json_rpc_over_opensandbox_execd_pty",
            },
        )

    async def close(self) -> None:
        self._closed = True
        self._consumed_command_ids.clear()
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
            subscription, self._subscription = self._subscription, None
            if subscription is not None:
                self._gateway.unsubscribe(subscription)
            self._tui_session_id = None
            self._active_receipt = None
            self._active_command_id = None
            self._pending_command_ids.clear()
            self._commands.clear()
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
