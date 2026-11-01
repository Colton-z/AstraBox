"""One WebSocket to a sandbox's Hermes gateway, carrying JSON-RPC.

The sibling of :class:`ProcessJsonLineChannel`, and deliberately much smaller.
That one drives a PROCESS: it starts one of the box's long-lived processes,
reads its stdout as a byte stream, and cuts newline-delimited JSON out of it.
This one talks to a SERVICE the image already runs — Hermes' gateway daemon,
reached through the image's bridge (``hermes_gateway_bridge.py`` in
``containers/sandbox-hermes``) — where every text frame is one JSON-RPC
message of the gateway's canonical protocol and there is nothing to
demultiplex. The bridge relays each frame unchanged.

The offset handed to ``on_record`` counts frames on this attachment and starts
again on the next one; it is not a resumable cursor. Resuming is Hermes' own:
every event of a session carries the ``seq`` its replay ring stamped, and
``session.events.since`` returns the events after one
(``engine/hermes_runtime_client.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable
from typing import Any, Callable
from urllib.parse import urlsplit

import websockets
from websockets.exceptions import InvalidMessage, InvalidStatus

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    websocket_header_kwargs,
)

logger = get_logger(__name__)

#: Long enough for a backend that is starting under supervisord and short
#: enough that a wedged one is reported rather than waited on forever.
CONNECT_TIMEOUT_SECONDS = 30.0

#: The largest frame read. One ``session.events.since`` reply carries a
#: session's whole replay ring, which Hermes bounds at 4 MiB of event JSON
#: (``_REPLAY_BUFFER_BYTES_MAX`` in ``tui_gateway/event_replay.py``); the rest
#: is the reply's own envelope. Hermes' own client and the image's bridge read
#: with the same bound. A frame over the limit closes the socket every
#: conversation of the profile shares.
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: ``(record, offset)`` — the same shape :class:`ProcessJsonLineChannel` calls,
#: so one protocol reader serves both transports. The offset here is a frame
#: counter for THIS attachment rather than a resumable cursor.
RecordSink = Callable[[dict[str, Any], int], Awaitable[None]]
FailureSink = Callable[[BaseException], Awaitable[None]]

#: The statuses with which something in front of the gateway reports that it
#: could not reach it: the bridge's port not yet published behind the sandbox
#: endpoint, or the gateway not ready behind the bridge (which answers 503).
#: Any other status is an answer, and a status that refuses the upgrade stays a
#: refusal.
_UPSTREAM_UNREACHABLE_STATUSES = frozenset({502, 503, 504})

#: How much of a refusal's response body is carried into the error.
_REFUSAL_BODY_EXCERPT_BYTES = 300


class BackendDetached(RuntimeError):
    """The connection to the backend is gone; nothing more will arrive on it."""


class BackendNotListening(BackendDetached):
    """Nothing is accepting connections at the backend's address yet.

    The one connect failure that time can cure: the gateway is starting under
    supervisord, or restarting for a changed profile, and the bridge cannot
    reach it yet. A caller with a startup budget may wait on this and on
    nothing else.
    """


class BackendRefusedUpgrade(RuntimeError):
    """The bridge, or the endpoint in front of it, answered and refused.

    A rejected credential reaches the client as an HTTP status on the upgrade.
    Waiting cannot change the answer, so it is reported at once with the status
    and what the response said.
    """

    def __init__(self, *, label: str, host: str, status: int, body: bytes) -> None:
        excerpt = body[:_REFUSAL_BODY_EXCERPT_BYTES].decode("utf-8", "replace").strip()
        super().__init__(
            f"{label} refused the WebSocket upgrade for Host {host} with HTTP "
            f"{status}" + (f": {excerpt}" if excerpt else " and no response body")
        )
        self.status = status


def _classify_connect_failure(
    exc: BaseException, *, label: str, url: str
) -> BaseException:
    """Name why an upgrade failed: not listening, refused, or neither.

    Not listening is a connection nobody accepted or that closed before any
    HTTP reply, a connect that timed out, or an unreachable-upstream status. A
    status the server chose is a refusal. Anything else is returned as it came.
    """

    if isinstance(exc, InvalidStatus):
        status = int(exc.response.status_code)
        if status in _UPSTREAM_UNREACHABLE_STATUSES:
            return BackendNotListening(
                f"{label} is not reachable yet: the endpoint answered HTTP {status}"
            )
        return BackendRefusedUpgrade(
            label=label,
            # The authority the upgrade presented; the rest of the URL is the
            # endpoint's route, which this error does not need to repeat.
            host=urlsplit(url).netloc,
            status=status,
            body=bytes(exc.response.body or b""),
        )
    if isinstance(exc, (OSError, TimeoutError, InvalidMessage)):
        return BackendNotListening(f"{label} is not listening yet: {exc}")
    return exc


class HermesBackendChannel:
    """A JSON-RPC WebSocket to one box's Hermes gateway.

    ``on_record`` receives every parsed frame; settling is the adapter's call,
    exactly as with the execd channel. ``on_failure`` is called once with the
    terminal error after every waiting request has been failed with it.
    """

    def __init__(
        self,
        *,
        url: str,
        label: str,
        on_record: RecordSink,
        on_failure: FailureSink,
        headers: dict[str, str] | None = None,
    ) -> None:
        #: Carries no credential: every proxy on the way logs the request line.
        #: The credential is one of :attr:`headers`.
        self.url = str(url)
        #: Named in every failure message so an operator can tell what died.
        self.label = str(label or "engine")
        #: The sandbox's endpoint face may hand back routing or access headers
        #: — dropping them works against an open local Docker server and fails
        #: against a hardened Kubernetes ingress, which is the shape of bug
        #: that only appears on the deployment that matters.
        self.headers = dict(headers or {})
        self._on_record = on_record
        self._on_failure = on_failure
        self._ws: Any = None
        self._reader_task: asyncio.Task[Any] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._fatal: BaseException | None = None
        #: Monotonic within one attachment. It orders frames for subscribers
        #: that filter on it; it is NOT a cursor another attachment could
        #: resume from.
        self._frame_offset = 0

    # ── state ────────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and self._fatal is None

    @property
    def fatal(self) -> BaseException | None:
        return self._fatal

    # ── connect ──────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Open the socket and start reading.

        Returning proves the transport is up, not that Hermes can answer.
        Readiness is the engine's own signal — `gateway.ready`, which the
        gateway sends a connection as soon as it accepts it — and establishing
        it is the adapter's call, the same division the execd channel keeps.

        :attr:`url` is the sandbox endpoint itself, so the connection goes
        where the endpoint routes and presents the `Host` that route expects.
        """

        if self.is_connected:
            return
        try:
            self._ws = await websockets.connect(
                self.url,
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                max_size=MAX_FRAME_BYTES,
                **websocket_header_kwargs(self.headers),
            )
            self._connected.set()
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name=f"hermes-backend:{self.label}"
            )
            if self._fatal is not None:
                raise self._fatal
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self.fail(
                _classify_connect_failure(exc, label=self.label, url=self.url)
            )
            assert self._fatal is not None
            raise self._fatal

    # ── sending ──────────────────────────────────────────────────────────
    async def send(self, payload: dict[str, Any]) -> None:
        """Write one JSON object to the backend."""

        if not self.is_connected or self._ws is None:
            raise BackendDetached(f"{self.label} is not connected")
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            await self._ws.send(message)

    # ── request mechanism (no vendor policy) ─────────────────────────────
    def register_request(self, request_id: str) -> asyncio.Future[dict[str, Any]]:
        key = str(request_id)
        if key in self._pending:
            raise ValueError(f"{self.label} already has a request registered as {key!r}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[key] = future
        return future

    def complete_request(self, request_id: str, payload: dict[str, Any]) -> bool:
        future = self._pending.pop(str(request_id), None)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True

    def release_request(self, request_id: str) -> None:
        future = self._pending.pop(str(request_id), None)
        if future is not None and not future.done():
            future.cancel()

    # ── reading ──────────────────────────────────────────────────────────
    async def _reader_loop(self) -> None:
        try:
            assert self._ws is not None
            async for frame in self._ws:
                text = frame if isinstance(frame, str) else frame.decode("utf-8", "replace")
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        # A frame this transport cannot parse is the backend's
                        # to explain; dropping it silently would turn a wire
                        # break into a hang somewhere upstream.
                        logger.warning(
                            "%s: unparseable frame (%d bytes)", self.label, len(line)
                        )
                        continue
                    if isinstance(record, dict):
                        self._frame_offset += 1
                        await self._on_record(record, self._frame_offset)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self.fail(exc)
            return
        # The socket closed without an error, which for a resident service is
        # still the end of this attachment.
        await self.fail(BackendDetached(f"{self.label} closed its socket"))

    # ── failure ──────────────────────────────────────────────────────────
    async def fail(self, exc: BaseException) -> None:
        """Settle this channel's terminal failure exactly once."""

        if self._fatal is not None:
            return
        if not isinstance(exc, RuntimeError):
            # Transport errors reach callers in the engine seam's vocabulary,
            # the way ProcessJsonLineChannel translates its own: an OSError from
            # a socket is not something an engine adapter should have to
            # recognise, and one that escaped untranslated would surface as an
            # unhandled error rather than a detached stream.
            exc = BackendDetached(f"{self.label} transport detached: {exc}")
        self._fatal = exc
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        with contextlib.suppress(BaseException):
            if self._ws is not None:
                await self._ws.close()
        await self._on_failure(exc)

    async def detach(self) -> None:
        """Settle pending requests and close this attachment; keep the gateway running."""

        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await self.fail(BackendDetached(f"{self.label} attachment detached"))
        with contextlib.suppress(BaseException):
            if self._ws is not None:
                await self._ws.close()
        self._connected.clear()
        self._ws = None
