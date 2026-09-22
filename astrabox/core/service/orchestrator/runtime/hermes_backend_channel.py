"""One WebSocket to a sandbox's resident Hermes backend, carrying JSON-RPC.

The sibling of :class:`ExecdJsonLineChannel`, and deliberately much smaller.
That one drives a PROCESS: it creates an execd PTY session, demultiplexes the
binary frames of a terminal into a stdout byte stream, and cuts newline-
delimited JSON out of the result. This one talks to a SERVICE the image
already runs — `hermes serve`, published by `astrabox-hermes-forward` — where
every text frame is one JSON-RPC line and there is nothing to demultiplex.

The wire above it is unchanged, which is the whole reason this exists.
Hermes' own `tui_gateway/ws.py` reuses `tui_gateway.server.dispatch` verbatim
and states the contract: "Identical to stdio: newline-delimited JSON-RPC in
both directions ... No framing differences." So every method, every approval
flow and every agent event the adapter already translates arrives here
unchanged. See `docs/maintainers/hermes-transport.md`.

What it does NOT carry, and the caller must know:

* **No PTY.** Nothing here has a `pty_session_id` because the backend is a
  supervised service and there is no terminal to allocate. This is a
  simplification, not a fix for the terminal spinner in the reasoning stream:
  that was once attributed to `isatty()` and the attribution was refuted here
  — the label arrives over this transport too, and its real cause was the
  adapter merging two vendor events (see `hermes_client.py`).
* **No offset replay.** execd replays a PTY's bytes from a cursor, which is
  what turn recovery reconnects at. The pinned Hermes has no equivalent — its
  seq-stamped `session.events.since` landed upstream on 2026-08-24 and was
  fixed on 2026-08-25 to deliver events at all, so it is neither released to
  PyPI nor proven. `current_output_offset` therefore answers 0 and a
  reconnect resumes live. Recorded as a gap rather than hidden: turn recovery
  over this transport re-attaches without the suffix it asked for.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable
from typing import Any, Callable

import websockets

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.runtime.execd_json_lines import (
    ExecdChannelDetached,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    websocket_header_kwargs,
)

logger = get_logger(__name__)

#: Long enough for a backend that is starting under supervisord and short
#: enough that a wedged one is reported rather than waited on forever.
CONNECT_TIMEOUT_SECONDS = 30.0

#: ``(record, offset)`` — the same shape :class:`ExecdJsonLineChannel` calls,
#: so one protocol reader serves both transports. The offset here is a frame
#: counter for THIS attachment rather than a resumable cursor; see
#: :meth:`HermesBackendChannel.current_output_offset`.
RecordSink = Callable[[dict[str, Any], int], Awaitable[None]]
FailureSink = Callable[[BaseException], Awaitable[None]]


class HermesBackendChannel:
    """A JSON-RPC WebSocket to one box's resident Hermes backend.

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
        dial: tuple[str, int] | None = None,
    ) -> None:
        #: Already carries its credential. The backend binds loopback inside
        #: the box, where the WS upgrade takes the session token as a `?token=`
        #: query parameter — a header is refused, which is what a first probe
        #: found the hard way.
        self.url = str(url)
        #: Named in every failure message so an operator can tell what died.
        self.label = str(label or "engine")
        #: The backend's endpoint face may hand back routing or access headers
        #: — dropping them works against an open local Docker server and fails
        #: against a hardened Kubernetes ingress, which is the shape of bug
        #: that only appears on the deployment that matters.
        self.headers = dict(headers or {})
        #: ``(host, port)`` of the box's published forwarder — where the TCP
        #: connection actually goes, while :attr:`url` stays the address the
        #: backend bound to. See :meth:`connect` for why the two differ.
        self.dial = (str(dial[0]), int(dial[1])) if dial is not None else None
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
        #: resume from, which is the difference recorded on
        #: :meth:`current_output_offset`.
        self._frame_offset = 0

    # ── state ────────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and self._fatal is None

    @property
    def fatal(self) -> BaseException | None:
        return self._fatal

    async def current_output_offset(self) -> int:
        """This attachment's frame count — not a cursor to resume from.

        Answered rather than raised because the caller's recovery path asks
        every transport the same question. For execd the answer is a byte
        offset a NEW attachment can replay from; here it only orders frames
        within the attachment that produced it, so reconnecting resumes live.
        The module docstring records what that costs.
        """

        return self._frame_offset

    # ── connect ──────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Open the socket and start reading.

        Returning proves the transport is up, not that Hermes can answer.
        Readiness is the engine's own signal — `gateway.ready`, which the
        backend emits immediately on accept — and establishing it is the
        adapter's call, the same division the execd channel keeps.

        The URI and the TCP target are deliberately different addresses, and
        conflating them is what a first attempt did: every upgrade came back
        `HTTP 403`. Hermes binds loopback and then checks that the `Host`
        header names the interface it bound to — its DNS-rebinding defence,
        GHSA-ppp5-vxwm-4cf7 — so a peer that dials the box by address is
        refused on arrival. The vendor's supported answer is to reach a
        loopback bind through a tunnel, and `astrabox-hermes-forward` is that
        tunnel; a tunnel's client addresses the service by the name the
        service bound to and connects where the tunnel opens. So the URI
        carries the loopback address, which is what `websockets` builds the
        `Host` header from, and ``dial`` carries the box's, which it takes as
        the connection target (`kwargs.setdefault("host", ...)` in
        `websockets.asyncio.client`).

        Naming the `Host` header through `additional_headers` instead does not
        work and was measured, not assumed: the header store is multi-valued,
        so the request goes out with two `Host` lines.

        The alternative — binding the backend to all interfaces — is closed by
        the vendor on purpose: since its June 2026 hardening a non-loopback
        bind ALWAYS requires an auth provider, `--insecure` is a no-op, and a
        `?token=` credential is refused in that mode.
        """

        if self.is_connected:
            return
        dial_kwargs: dict[str, Any] = {}
        if self.dial is not None:
            dial_kwargs["host"], dial_kwargs["port"] = self.dial
        try:
            self._ws = await websockets.connect(
                self.url,
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                max_size=4 * 1024 * 1024,
                **dial_kwargs,
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
            await self.fail(exc)
            assert self._fatal is not None
            raise self._fatal

    # ── sending ──────────────────────────────────────────────────────────
    async def send(self, payload: dict[str, Any]) -> None:
        """Write one JSON object to the backend."""

        if not self.is_connected or self._ws is None:
            raise ExecdChannelDetached(f"{self.label} is not connected")
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
        await self.fail(ExecdChannelDetached(f"{self.label} closed its socket"))

    # ── failure ──────────────────────────────────────────────────────────
    async def fail(self, exc: BaseException) -> None:
        """Settle this channel's terminal failure exactly once."""

        if self._fatal is not None:
            return
        if not isinstance(exc, (ExecdChannelDetached, RuntimeError)):
            # Transport errors reach callers in the engine seam's vocabulary,
            # the way ExecdJsonLineChannel translates its own: an OSError from
            # a socket is not something an engine adapter should have to
            # recognise, and one that escaped untranslated would surface as an
            # unhandled error rather than a detached stream.
            exc = ExecdChannelDetached(f"{self.label} transport detached: {exc}")
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
        """Close this attachment, leaving the backend running."""

        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        with contextlib.suppress(BaseException):
            if self._ws is not None:
                await self._ws.close()
        self._connected.clear()
        self._ws = None
