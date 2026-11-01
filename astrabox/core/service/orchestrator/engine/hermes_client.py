"""The wire half of the Hermes adapter: one connection to the box's Hermes gateway.

Hermes' gateway daemon (``hermes gateway run``) serves its canonical protocol,
``hermes-gateway-v1``, as JSON-RPC over a WebSocket (``tui_gateway/ws.py`` and
``gateway/session_controls.py`` at the pinned commit); the image's bridge
publishes it to this host. Three kinds of frame arrive on that socket:

* a reply to a request this host sent (an ``id`` with ``result`` or
  ``error`` and no ``method``);
* an event (``method == "event"``), whose ``params`` carry ``type``,
  ``session_id``, ``payload`` and, for a session's events, the ``seq`` and
  ``replay_epoch`` of that session's replay ring and the execution stamp of
  the turn that produced it (``admission_id``, ``execution_generation``,
  ``authority_epoch``; ``gateway/session_events.py``);
* a request the gateway asks the client (a ``srq-`` id together with a
  ``method``): a password or secret prompt. The client answers it with a
  response frame bearing the same id (``gateway/session_server_requests.py``).

:class:`HermesGatewayConnection` tells them apart and queues events and
requests in wire order. One gateway serves every conversation of one (user,
assistant) profile; :mod:`.hermes_gateway` owns this host's connection to it
and routes each session's frames to its subscribers, and
:mod:`.hermes_runtime_client` reads one session's frames as runtime events.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
    canonical_child_run_data,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.runtime.hermes_backend_channel import (
    BackendDetached,
    BackendNotListening,
    HermesBackendChannel,
)

_CONNECT_TIMEOUT_SECONDS = 20.0
_RPC_TIMEOUT_SECONDS = 120.0

logger = get_logger(__name__)


_HERMES_CHILD_RUN_OPEN_EVENTS = frozenset(
    {"subagent.spawn_requested", "subagent.start"}
)
_HERMES_CHILD_RUN_UPDATE_EVENTS = frozenset(
    {"subagent.thinking", "subagent.tool", "subagent.progress"}
)


class HermesGatewayRpcError(RuntimeError):
    """A JSON-RPC error returned by the Hermes gateway.

    The gateway answers a refused request with code ``4001`` and the refusal's
    bounded reason (``admission_conflict``, ``stale_generation``,
    ``invalid_params``…) as ``message`` and ``data.reason``, and an unknown
    method with ``-32601`` (``_refusal_data`` and ``dispatch`` in
    ``gateway/session_controls.py``). :attr:`reason` is that reason.
    """

    def __init__(self, *, code: Any, message: str, data: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.data = dict(data or {})
        self.reason = str(self.data.get("reason") or message)
        detail = "".join(
            f" {key}={self.data[key]}" for key in ("field", "detail") if self.data.get(key)
        )
        super().__init__(f"Hermes gateway RPC {code}: {message}{detail}")


@dataclass(frozen=True)
class HermesGatewayFrame:
    """One session-scoped frame from the gateway, in wire order.

    ``event`` is an event's ``params`` (``type``, ``session_id``,
    ``payload``, ``seq``, ``replay_epoch`` and the execution stamp), or, when
    ``is_request`` is true, a request the gateway asks the client:
    ``{"id", "method", "params"}``. ``output_offset`` counts frames on the
    connection that received it.
    """

    event: dict[str, Any]
    output_offset: int
    is_request: bool = False

    @property
    def session_id(self) -> str:
        source = self.event.get("params") if self.is_request else self.event
        if not isinstance(source, dict):
            return ""
        return str(source.get("session_id") or "").strip()


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



class HermesGatewayConnection:
    """One connection to a box's Hermes gateway.

    The socket is the platform's (:class:`HermesBackendChannel`); this class is
    Hermes' half of it — the JSON-RPC envelope, which frame answers which
    request, which frame is the gateway asking a question, the gateway's own
    readiness signal, and the queue of events and requests. The channel
    deliberately settles nothing on its own, because how a frame is read is
    Hermes' protocol and not every engine's.

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
            label="Hermes gateway",
            on_record=self._on_record,
            on_failure=self._on_failure,
        )
        self._events: asyncio.Queue[HermesGatewayFrame | BaseException] = asyncio.Queue()
        self._gateway_ready = asyncio.Event()
        self._fatal: BaseException | None = None
        #: The shape of a frame that was neither JSON-RPC nor an event, kept so
        #: a failure that follows it says what the endpoint sent.
        self._foreign_frame: str | None = None

    # ── delegated pipe state ─────────────────────────────────────────────
    @property
    def url(self) -> str:
        return self._channel.url

    @property
    def is_connected(self) -> bool:
        return self._channel.is_connected

    @property
    def fatal(self) -> BaseException | None:
        """This connection's terminal failure, in the engine seam's vocabulary."""

        return self._fatal

    @property
    def backend_not_listening(self) -> bool:
        """Whether this attachment failed because nothing accepted it yet.

        Read from the channel, which classified the failure where it happened;
        the seam's vocabulary above it has no word for "try again shortly".
        """

        return isinstance(self._channel.fatal, BackendNotListening)

    async def detach(self) -> None:
        await self._channel.detach()

    # ── connect ──────────────────────────────────────────────────────────
    async def connect(self, *, require_gateway_ready: bool) -> None:
        """Open the socket, and optionally wait for Hermes to announce itself.

        A connected socket only proves the bridge accepted it.
        ``gateway.ready`` is Hermes' own statement that the gateway can answer,
        which a fresh attachment needs and a reconnect to a live one does not.
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
            raise EngineStreamDetached("Hermes gateway is not connected")
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
            data = error.get("data")
            raise HermesGatewayRpcError(
                code=error.get("code"),
                message=str(error.get("message") or "request failed"),
                data=data if isinstance(data, dict) else None,
            )
        result = response.get("result")
        return dict(result) if isinstance(result, dict) else {}

    async def respond_error(self, request_id: str, *, code: int, message: str) -> None:
        """Answer one of the gateway's requests with a JSON-RPC error.

        Hermes resolves the waiting request with an empty answer at once
        (``resolve`` in ``gateway/session_server_requests.py``), so the agent
        fails fast instead of waiting out the request's deadline.
        """

        await self._channel.send(
            {
                "jsonrpc": "2.0",
                "id": str(request_id),
                "error": {"code": int(code), "message": str(message)},
            }
        )

    async def next_event(self) -> HermesGatewayFrame:
        item = await self._events.get()
        if isinstance(item, BaseException):
            raise item
        return item

    # ── Hermes' reading of one frame ─────────────────────────────────────
    async def _on_record(self, record: dict[str, Any], output_offset: int) -> None:
        method = record.get("method")
        request_id = record.get("id")
        if isinstance(method, str) and method != "event" and isinstance(request_id, str):
            params = record.get("params")
            await self._events.put(
                HermesGatewayFrame(
                    event={
                        "id": request_id,
                        "method": method,
                        "params": dict(params) if isinstance(params, dict) else {},
                    },
                    output_offset=output_offset,
                    is_request=True,
                )
            )
            return
        if request_id is not None and ("result" in record or "error" in record):
            # A reply nobody is waiting for is dropped: Hermes answers every
            # request it was given, so an unmatched id belongs to a request
            # this connection already gave up on.
            self._channel.complete_request(str(request_id), record)
            return
        if method != "event":
            if "jsonrpc" in record:
                return
            # Something in front of the gateway can answer on its behalf, such
            # as a proxy reporting that the gateway refused it. Only the keys
            # and a short code are kept: the rest can carry addresses.
            code = record.get("code")
            self._foreign_frame = f"keys={sorted(record)}" + (
                f" code={code}" if isinstance(code, (str, int)) and len(str(code)) <= 64 else ""
            )
            logger.warning("Hermes gateway channel received a frame that is not JSON-RPC: %s",
                           self._foreign_frame)
            return
        params = record.get("params")
        if not isinstance(params, dict):
            return
        if str(params.get("type") or "") == "gateway.ready":
            self._gateway_ready.set()
        await self._events.put(
            HermesGatewayFrame(event=dict(params), output_offset=output_offset)
        )

    async def _on_failure(self, exc: BaseException) -> None:
        if self._fatal is not None:
            return
        detail = (
            f"{exc} (after a frame that was not JSON-RPC: {self._foreign_frame})"
            if self._foreign_frame and not self._gateway_ready.is_set()
            else str(exc)
        )
        self._fatal = (
            EngineStreamDetached(detail)
            if isinstance(exc, BackendDetached)
            else exc
        )
        self._gateway_ready.set()
        await self._events.put(self._fatal)

    async def _fail(self, exc: BaseException) -> None:
        """Make ``exc`` terminal for this connection, through the channel."""

        await self._channel.fail(exc)
        if self._fatal is None:
            # The channel already held a failure, so this one never reached
            # _on_failure; adopt the one that actually settled it.
            await self._on_failure(self._channel.fatal or exc)


__all__ = [
    "HermesGatewayConnection",
    "HermesGatewayFrame",
    "HermesGatewayRpcError",
]
