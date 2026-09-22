"""The platform's execd pipe for a process that speaks JSON lines.

OpenSandbox's supported long-lived process surface is ``POST /pty`` plus the
PTY WebSocket in pipe mode. An engine whose embedding surface is
newline-delimited JSON on stdin/stdout rides that surface directly: the
sandbox opens no TCP port and no platform credential is copied into the box.

This channel knows execd and nothing else — the PTY resource, the WebSocket
frame tags, the byte cursor and its replay gap, LF record framing, "a record
is one JSON object", the stderr tail an exit needs to be diagnosable, and a
single terminal failure. **It reads no vendor vocabulary.** Which record is a
response, what a request envelope looks like, when the process is ready to
answer, and what an unmatched response means are all the adapter's, and the
two installed adapters genuinely disagree about the last one: Hermes drops an
unmatched response, pi reports it.

So the channel is composed, not inherited, and its request support is
mechanism only: :meth:`register_request` hands back a future,
:meth:`complete_request` settles one the adapter has recognized. The channel
never decides that a record with an ``id`` is a response.

``PtyTerminal`` in :mod:`.pty_terminal` serves the other shape — an
interactive shell whose output is bytes for a human and whose completion is a
sentinel — and stays separate. The two share endpoint and header resolution,
not a protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import websockets

from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    ResolvedExecdEndpoint,
    websocket_header_kwargs,
)

#: execd pipe-mode frame tags. The first byte of every binary frame.
STDIN = 0x00
STDOUT = 0x01
STDERR = 0x02
REPLAY = 0x03

CONNECT_TIMEOUT_SECONDS = 20.0

#: How much stderr to keep. An exit notice carries no reason of its own, so
#: this tail is the only account of why a process died.
_STDERR_TAIL_LIMIT = 8000

#: One parsed record and the byte offset just past its terminating LF.
RecordSink = Callable[[dict[str, Any], int], Awaitable[None]]
FailureSink = Callable[[BaseException], Awaitable[None]]


class ExecdChannelReplayGap(RuntimeError):
    """The requested execd byte cursor has fallen out of its replay buffer."""


class ExecdChannelDetached(RuntimeError):
    """The pipe carrying this process is gone; nothing more will arrive."""


class ExecdJsonLineChannel:
    """One execd pipe session carrying newline-delimited JSON objects.

    ``on_record`` receives every parsed record the channel did not settle
    itself — which is all of them, since settling is the adapter's call. The
    adapter routes each one and, when it recognizes a response, calls
    :meth:`complete_request`.

    ``on_failure`` is called once, with the terminal error, after every
    waiting request has been failed with it.
    """

    def __init__(
        self,
        *,
        endpoint: ResolvedExecdEndpoint,
        cwd: str,
        command: str,
        label: str,
        on_record: RecordSink,
        on_failure: FailureSink,
        pty_session_id: str | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.cwd = str(cwd)
        self.command = str(command)
        #: Named in every failure message so an operator can tell what died.
        self.label = str(label or "engine")
        self.pty_session_id = str(pty_session_id or "").strip() or None
        self._on_record = on_record
        self._on_failure = on_failure
        self._http_transport = http_transport
        self._ws: Any = None
        self._reader_task: asyncio.Task[Any] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._fatal: BaseException | None = None
        self._stdout_buffer = b""
        self._buffer_start_offset = 0
        self._next_output_offset = 0
        self._requested_since = 0
        self._stderr_tail = ""

    # ── state ────────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._fatal is None

    @property
    def fatal(self) -> BaseException | None:
        return self._fatal

    @property
    def stderr_tail(self) -> str:
        return self._stderr_tail

    # ── PTY resource ─────────────────────────────────────────────────────
    @property
    def _ws_origin(self) -> str:
        if self.endpoint.origin.startswith("https://"):
            return "wss://" + self.endpoint.origin[len("https://") :]
        return "ws://" + self.endpoint.origin[len("http://") :]

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._http_transport,
            timeout=CONNECT_TIMEOUT_SECONDS,
            headers=self.endpoint.headers,
        )

    async def create(self) -> str:
        if self.pty_session_id:
            return self.pty_session_id
        async with self._http_client() as client:
            response = await client.post(
                f"{self.endpoint.origin}/pty",
                json={"cwd": self.cwd, "command": self.command},
            )
            response.raise_for_status()
            pty_session_id = str((response.json() or {}).get("session_id") or "").strip()
        if not pty_session_id:
            raise RuntimeError(f"execd created a {self.label} PTY without an id")
        self.pty_session_id = pty_session_id
        return pty_session_id

    async def status(self) -> dict[str, Any]:
        if not self.pty_session_id:
            raise RuntimeError(f"{self.label} PTY has not been created")
        async with self._http_client() as client:
            response = await client.get(f"{self.endpoint.origin}/pty/{self.pty_session_id}")
            response.raise_for_status()
            payload = response.json() or {}
        return dict(payload) if isinstance(payload, dict) else {}

    async def current_output_offset(self) -> int:
        return int((await self.status()).get("output_offset") or 0)

    async def connect(self, *, since: int) -> None:
        """Open the pipe from ``since`` and start reading.

        Returning proves the transport is up, not that the process can answer.
        Readiness is the engine's own signal — Hermes announces one, pi does
        not — so the adapter establishes it.
        """

        if self.is_connected:
            return
        pty_session_id = await self.create()
        self._requested_since = max(0, int(since))
        self._next_output_offset = self._requested_since
        self._buffer_start_offset = self._requested_since
        url = (
            f"{self._ws_origin}/pty/{pty_session_id}/ws"
            f"?pty=0&takeover=1&since={self._requested_since}"
        )
        try:
            self._ws = await websockets.connect(
                url,
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                max_size=4 * 1024 * 1024,
                **websocket_header_kwargs(self.endpoint.headers),
            )
            self._reader_task = asyncio.create_task(
                self._reader_loop(),
                name=f"execd-json-lines:{pty_session_id}",
            )
            await asyncio.wait_for(self._connected.wait(), timeout=CONNECT_TIMEOUT_SECONDS)
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
        """Write one JSON object to the process's stdin, LF-terminated."""

        if not self.is_connected or self._ws is None:
            raise ExecdChannelDetached(f"{self.label} is not connected")
        message = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        async with self._send_lock:
            await self._ws.send(bytes([STDIN]) + message)

    # ── request mechanism (no vendor policy) ─────────────────────────────
    def register_request(self, request_id: str) -> asyncio.Future[dict[str, Any]]:
        """Reserve a future for a reply the adapter expects under this id."""

        key = str(request_id)
        if key in self._pending:
            raise ValueError(f"{self.label} already has a request registered as {key!r}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[key] = future
        return future

    def complete_request(self, request_id: str, payload: dict[str, Any]) -> bool:
        """Settle a registered request. False when nothing was waiting.

        The caller decides what a False means. The channel does not: an
        unmatched reply is a fact about the engine's protocol, and the two
        installed adapters read it differently.
        """

        future = self._pending.get(str(request_id))
        if future is None or future.done():
            return False
        future.set_result(dict(payload))
        return True

    def release_request(self, request_id: str) -> None:
        """Forget a registered request, settled or not.

        A request failed by :meth:`fail` is usually abandoned rather than
        awaited — the caller is already raising the channel's terminal error —
        so its exception is consumed here. Left unretrieved it would surface
        later as an asyncio warning in some unrelated place, which reads like
        a second, mysterious failure.
        """

        future = self._pending.pop(str(request_id), None)
        if future is not None and future.done() and not future.cancelled():
            future.exception()

    # ── reading ──────────────────────────────────────────────────────────
    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for frame in self._ws:
                if isinstance(frame, str):
                    await self._handle_control_frame(frame)
                    continue
                await self._handle_binary_frame(bytes(frame))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self.fail(exc)
        finally:
            if self._fatal is None:
                await self.fail(ExecdChannelDetached(f"{self.label} connection closed"))

    async def _handle_control_frame(self, frame: str) -> None:
        try:
            payload = json.loads(frame)
        except json.JSONDecodeError:
            return
        frame_type = str(payload.get("type") or "") if isinstance(payload, dict) else ""
        if frame_type == "connected":
            self._connected.set()
            return
        if frame_type == "exit":
            code = payload.get("exit_code") if isinstance(payload, dict) else None
            raise RuntimeError(
                f"{self.label} process exited with code {code}; "
                f"stderr={self._stderr_tail[-1000:]!r}"
            )
        if frame_type == "error":
            raise RuntimeError(str(payload.get("error") or "execd PTY error"))

    async def _handle_binary_frame(self, frame: bytes) -> None:
        if not frame:
            return
        kind = frame[0]
        if kind == STDERR:
            self._stderr_tail = (
                self._stderr_tail + frame[1:].decode("utf-8", "replace")
            )[-_STDERR_TAIL_LIMIT:]
            return
        if kind == REPLAY:
            if len(frame) < 9:
                raise RuntimeError("execd returned a truncated PTY replay frame")
            actual_offset = int.from_bytes(frame[1:9], "big", signed=False)
            if actual_offset > self._requested_since:
                # The bytes between the requested cursor and what execd
                # still holds are gone. Reporting the gap is the only honest
                # move: resuming here would silently drop records.
                raise ExecdChannelReplayGap(
                    f"{self.label} output replay was truncated: "
                    f"requested={self._requested_since} available={actual_offset}"
                )
            self._next_output_offset = actual_offset
            self._buffer_start_offset = actual_offset
            self._stdout_buffer = b""
            await self._feed_stdout(frame[9:])
            return
        if kind == STDOUT:
            await self._feed_stdout(frame[1:])

    async def _feed_stdout(self, data: bytes) -> None:
        """Split stdout on LF only and hand each record to the adapter.

        LF is the sole delimiter, and the split happens on bytes: a JSON
        string may legally contain U+2028 / U+2029, which a text-mode or
        Unicode-aware line reader would treat as a record boundary and thereby
        corrupt the record. A record may also arrive across several frames, so
        the tail stays buffered until its LF shows up.
        """

        if not data:
            return
        if not self._stdout_buffer:
            self._buffer_start_offset = self._next_output_offset
        self._stdout_buffer += data
        self._next_output_offset += len(data)
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = self._stdout_buffer[:newline]
            consumed = newline + 1
            line_end_offset = self._buffer_start_offset + consumed
            self._stdout_buffer = self._stdout_buffer[consumed:]
            self._buffer_start_offset = line_end_offset
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"{self.label} wrote non-JSON stdout at offset "
                    f"{line_end_offset}: {raw_line[:300]!r}"
                ) from exc
            if not isinstance(record, dict):
                raise RuntimeError(f"{self.label} JSON record is not an object")
            await self._on_record(record, line_end_offset)

    # ── teardown ─────────────────────────────────────────────────────────
    async def fail(self, exc: BaseException) -> None:
        """Make ``exc`` this channel's one terminal failure.

        Idempotent: the first failure is the cause and later ones are its
        consequences, so only the first is kept and reported.
        """

        if self._fatal is not None:
            return
        if not isinstance(
            exc, (ExecdChannelReplayGap, ExecdChannelDetached, RuntimeError)
        ):
            exc = ExecdChannelDetached(f"{self.label} transport detached: {exc}")
        self._fatal = exc
        self._connected.set()
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        await self._on_failure(exc)

    async def detach(self) -> None:
        task = self._reader_task
        self._reader_task = None
        ws = self._ws
        self._ws = None
        if task is not None:
            task.cancel()
        if ws is not None:
            with contextlib.suppress(BaseException):
                await ws.close()
        if task is not None:
            with contextlib.suppress(BaseException):
                await task

    async def delete(self) -> bool:
        """Detach and destroy the execd PTY session behind this channel."""

        await self.detach()
        if not self.pty_session_id:
            return False
        pty_session_id = self.pty_session_id
        async with self._http_client() as client:
            response = await client.delete(f"{self.endpoint.origin}/pty/{pty_session_id}")
            if response.status_code != 404:
                response.raise_for_status()
        self.pty_session_id = None
        return True
