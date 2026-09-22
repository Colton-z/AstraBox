"""The app-server link to one in-box Codex.

`codex app-server` is the interface every OpenAI-built Codex surface runs on —
the VS Code extension, the desktop app, the CLI's own TUI — and it is
JSON-RPC 2.0 in both directions, with the ``"jsonrpc": "2.0"`` header omitted
on the wire. One socket carries all four message kinds: the adapter's
requests, the server's responses, its notifications, and the requests it makes
of the client (an approval, a question). The link keeps them apart and hands
the last two on as one ordered stream, so a conversation and its interruptions
cannot reorder.

Which listener this dials is a decision, not a default. The vendor offers four
transports and marks the TCP one experimental in the same sentence that offers
it: "Websocket transport is currently experimental and unsupported. Do not
rely on it for production workloads." The unix-socket listener carries the
identical protocol — the same HTTP Upgrade handshake, the same frames — and is
the one the vendor's own remote path uses (`codex app-server proxy` pipes that
socket to stdio for an SSH client). So the image binds the supported listener
and a stock TCP forwarder publishes it, which is also what keeps a caller
check off the wire: the listener demands authentication only for non-loopback
binds, and this one never leaves loopback.

Two details are measured against a running server rather than read off the
schema, because each one fails as silence:

* **Compression must be off.** With ``permessage-deflate`` offered, the server
  closes the connection during the handshake and sends nothing at all — not a
  status line, not a close frame.
* **The handshake is mandatory and ordered.** ``initialize`` must be the first
  request on a connection and ``initialized`` must follow it before any other
  method; anything else earns "Not initialized", and a second ``initialize``
  earns "Already initialized".
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator
from typing import Any

import websockets

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    ResolvedExecdEndpoint,
    resolve_sandbox_endpoint,
    websocket_header_kwargs,
)

logger = get_logger(__name__)

#: The in-box port the image's forwarder publishes the app-server on. The
#: other half of the contract is `containers/sandbox-codex/supervisord.codex
#: .conf`, where the server binds a unix socket and the forwarder waits for it
#: to answer before opening this port.
CODEX_APP_SERVER_PORT = 44790

#: What AstraBox tells the server it is. The server echoes this back inside
#: the user agent it presents upstream, so it is a real identifier and not
#: decoration.
CODEX_CLIENT_NAME = "astrabox"
CODEX_CLIENT_TITLE = "AstraBox"

_CONNECT_TIMEOUT_SECONDS = 30.0
_RPC_TIMEOUT_SECONDS = 120.0
_MAX_FRAME_BYTES = 8 * 1024 * 1024


class CodexRpcError(APIError):
    """A JSON-RPC error object returned in place of a result."""

    def __init__(self, *, code: Any, message: str, data: Any = None) -> None:
        super().__init__(
            code="AGENT_RUNTIME_ERROR",
            message=f"codex app-server error {code}: {message}",
            status_code=502,
        )
        self.rpc_code = code
        self.rpc_message = message
        self.rpc_data = data


class CodexAppServerLink:
    """One WebSocket to one box's app-server, initialized and multiplexed."""

    def __init__(
        self,
        *,
        endpoint: ResolvedExecdEndpoint,
        connector: Any = None,
    ) -> None:
        self.endpoint = endpoint
        #: Injection point for tests; production uses `websockets.connect`.
        self._connector = connector
        self._ws: Any = None
        self._reader: asyncio.Task[Any] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._inbound: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self._send_lock = asyncio.Lock()
        self._detached: BaseException | None = None
        self._server_info: dict[str, Any] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    @classmethod
    async def connect(
        cls,
        sandbox: Any,
        *,
        connector: Any = None,
        port: int = CODEX_APP_SERVER_PORT,
    ) -> "CodexAppServerLink":
        endpoint = await resolve_sandbox_endpoint(sandbox, port=port)
        link = cls(endpoint=endpoint, connector=connector)
        await link._attach()
        return link

    @property
    def is_live(self) -> bool:
        return self._ws is not None and self._detached is None

    @property
    def server_info(self) -> dict[str, Any] | None:
        """What `initialize` answered: user agent, codex home, platform."""

        return dict(self._server_info) if self._server_info else None

    async def _attach(self) -> None:
        origin = self.endpoint.origin
        url = ("wss://" + origin[len("https://"):]) if origin.startswith("https://") else (
            "ws://" + origin[len("http://"):]
        )
        connect = self._connector or websockets.connect
        try:
            self._ws = await connect(
                url,
                open_timeout=_CONNECT_TIMEOUT_SECONDS,
                max_size=_MAX_FRAME_BYTES,
                # Measured, not chosen: offering permessage-deflate makes the
                # server close the connection mid-handshake with no response.
                compression=None,
                **websocket_header_kwargs(self.endpoint.headers),
            )
        except BaseException as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"codex app-server at {origin} refused a websocket: {exc}"
                ),
                status_code=502,
            ) from exc
        self._reader = asyncio.create_task(self._read_loop(), name="codex-app-server")
        self._server_info = await self._initialize()

    async def _initialize(self) -> dict[str, Any]:
        result = await self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": CODEX_CLIENT_NAME,
                    "title": CODEX_CLIENT_TITLE,
                    "version": "0.0.0",
                },
                # collaborationMode/list and turn/start.collaborationMode are
                # experimental in the pinned app-server protocol. The server
                # refuses either unless the client opts in during initialize;
                # that capability belongs to this connection, not to an Agent
                # option or a per-turn fallback.
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                },
            },
        )
        await self.notify("initialized")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(BaseException):
                await ws.close()
        self._fail(EngineStreamDetached("codex app-server link closed"))

    # ── the four message kinds ───────────────────────────────────────────
    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = _RPC_TIMEOUT_SECONDS,
    ) -> Any:
        """One request, awaited by id. Raises on a JSON-RPC error object."""

        request_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params or {}})
            message = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"codex app-server did not answer {method!r} in {timeout:g}s",
                status_code=504,
            ) from exc
        finally:
            self._pending.pop(request_id, None)
        error = message.get("error")
        if error is not None:
            detail = error if isinstance(error, dict) else {"message": str(error)}
            raise CodexRpcError(
                code=detail.get("code"),
                message=str(detail.get("message") or "unknown"),
                data=detail.get("data"),
            )
        return message.get("result")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params:
            payload["params"] = params
        await self._send(payload)

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        """Answer a request the SERVER made — an approval, a question."""

        await self._send({"id": request_id, "result": result})

    def iter_inbound(self) -> AsyncIterator[dict[str, Any]]:
        """Notifications and server requests, in the order they arrived.

        A response to a call made from here is routed to its waiter instead
        and never appears on this stream. The iterator raises
        `EngineStreamDetached` when the socket ends, which is the signal the
        turn stream is built on.
        """

        async def _iter() -> AsyncIterator[dict[str, Any]]:
            while True:
                item = await self._inbound.get()
                if isinstance(item, BaseException):
                    raise item
                yield item

        return _iter()

    # ── plumbing ─────────────────────────────────────────────────────────
    async def _send(self, payload: dict[str, Any]) -> None:
        if self._detached is not None:
            raise self._detached
        if self._ws is None:
            raise EngineStreamDetached("codex app-server link is not connected")
        async with self._send_lock:
            await self._ws.send(json.dumps(payload))

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    logger.warning("codex app-server sent non-JSON: %.200r", raw)
                    continue
                if not isinstance(message, dict):
                    continue
                self._route(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail(EngineStreamDetached(f"codex app-server detached: {exc}"))
            return
        self._fail(EngineStreamDetached("codex app-server closed the socket"))

    def _route(self, message: dict[str, Any]) -> None:
        # A response carries an id and no method; a server request carries
        # both; a notification carries a method and no id. Deciding on the
        # pair is what keeps an approval from being mistaken for an answer.
        has_method = "method" in message
        raw_id = message.get("id")
        if not has_method and raw_id is not None:
            future = self._pending.get(raw_id if isinstance(raw_id, int) else -1)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if has_method:
            self._inbound.put_nowait(message)

    def _fail(self, exc: BaseException) -> None:
        if self._detached is not None:
            return
        self._detached = exc
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        self._inbound.put_nowait(exc)
