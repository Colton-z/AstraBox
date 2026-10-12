"""The processes face over execd, the in-box daemon every built-in image carries.

execd (``opensandbox/execd``, the release the images and the lifecycle server
pin) keeps long-lived processes it calls PTY sessions
(``components/execd/PTY.md``). :class:`ExecdProcesses` implements
:class:`astrabox.seams.sandbox_data.SandboxProcesses` on them and keeps execd's
wire rules out of core:

* ``POST /pty`` registers a process; execd runs its command line with
  ``bash -c`` when the first WebSocket attaches, and again when one attaches
  after it exited;
* the WebSocket ``/pty/{id}/ws`` in pipe mode (``pty=0``) carries binary
  frames tagged ``0x01`` stdout, ``0x02`` stderr and ``0x03`` replay (an
  8-byte big-endian offset, then the bytes), stdin tagged ``0x00``, and JSON
  text for control; a second holder is refused with 409 unless it takes over;
* a replay asked for from before execd's buffer starts at what the buffer
  still holds and says so only through its offset, so the offset is compared
  with the one asked for and a gap refuses the attachment;
* execd's exit notice can overtake output still queued behind it, so output
  arriving within a short window after the notice is delivered before it;
* execd answers a Close frame but never closes its side of the TCP connection,
  so the client ends the connection itself once both Close frames have passed;
* execd takes a signal only from the connection that holds the process, so
  :meth:`ExecdProcesses.signal` takes the process over to send one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidHandshake, InvalidStatus
from websockets.frames import Frame, Opcode

from astrabox.seams.sandbox import SandboxUnreachable
from astrabox.seams.sandbox_data import (
    ProcessExit,
    ProcessFrame,
    ProcessOutput,
    ProcessReplay,
    SandboxEndpoint,
    SandboxProcessAlreadyAttached,
    SandboxProcessDetached,
    SandboxProcessReplayGap,
)

#: execd's frame tags in pipe mode: the first byte of every binary frame.
STDIN = 0x00
STDOUT = 0x01
STDERR = 0x02
REPLAY = 0x03

#: How long an HTTP request to execd, or an attachment's handshake up to execd's
#: ``connected`` notice, may take before the box counts as unreachable.
CONNECT_TIMEOUT_SECONDS = 20.0

#: How long a Close frame this side sends may go unanswered before the socket
#: is dropped. websockets' own default, named because detaching relies on it
#: as the only wait left once execd's missing TCP close is not waited for.
CLOSE_TIMEOUT_SECONDS = 10.0

#: How long to keep reading after execd's exit notice for output it overtook.
#: The socket closing ends the wait early; this bounds a server that neither
#: sends more nor closes.
EXIT_DRAIN_SECONDS = 0.5

_MAX_FRAME_BYTES = 4 * 1024 * 1024


class ExecdClientConnection(ClientConnection):
    """A client connection that ends TCP itself once the closing handshake is done.

    RFC 6455 §7.1.1 has the server close the TCP connection first once both
    Close frames have been exchanged, and lets the client close it when the
    server has not. execd answers a Close frame but never closes its side, and
    websockets' ``close()`` otherwise waits its whole ``close_timeout`` for
    that FIN on every connection it leaves. The handshake itself is still
    awaited: nothing here closes the socket before the peer's Close frame has
    answered the one this side sent, so every frame sent before it has been
    read by execd.
    """

    def process_event(self, event: Any) -> None:
        super().process_event(event)
        protocol = self.protocol
        if (
            isinstance(event, Frame)
            and event.opcode is Opcode.CLOSE
            and protocol.close_sent is not None
            and protocol.close_rcvd is not None
            and not self.transport.is_closing()
        ):
            # The Close this side sent, or the echo the protocol just
            # queued, is flushed before the transport closes.
            self.transport.close()


def _websocket_url(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    return "ws://" + url[len("http://") :]


def _exit_code(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _control(message: str) -> dict[str, Any]:
    try:
        payload = json.loads(message)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _output(frame: bytes, process_id: str) -> ProcessFrame | None:
    """One binary frame from execd as a process frame; ``None`` for a tag it does not send."""

    kind = frame[0]
    if kind == STDOUT:
        return ProcessOutput(stream="stdout", data=frame[1:])
    if kind == STDERR:
        return ProcessOutput(stream="stderr", data=frame[1:])
    if kind == REPLAY:
        if len(frame) < 9:
            raise RuntimeError(f"execd sent a truncated replay frame for process {process_id!r}")
        return ProcessReplay(offset=int.from_bytes(frame[1:9], "big"), data=frame[9:])
    return None


class ExecdProcessAttachment:
    """:class:`astrabox.seams.sandbox_data.SandboxProcessAttachment` on one execd WebSocket."""

    def __init__(
        self,
        connection: ClientConnection,
        *,
        process_id: str,
        pending: list[ProcessFrame],
    ) -> None:
        self._connection = connection
        self._process_id = process_id
        self._pending = pending
        self._detached = False
        self._send_lock = asyncio.Lock()

    async def frames(self) -> AsyncGenerator[ProcessFrame, None]:
        pending, self._pending = self._pending, []
        for earlier in pending:
            yield earlier
        loop = asyncio.get_running_loop()
        exited: ProcessExit | None = None
        drain_until: float | None = None
        while True:
            try:
                if drain_until is None:
                    message = await self._connection.recv()
                else:
                    remaining = drain_until - loop.time()
                    if remaining <= 0:
                        break
                    message = await asyncio.wait_for(self._connection.recv(), timeout=remaining)
            except TimeoutError:
                break
            except websockets.ConnectionClosed as exc:
                if exited is not None or self._detached:
                    break
                raise SandboxProcessDetached(
                    f"the attachment to process {self._process_id!r} ended without "
                    f"its exit: {exc}"
                ) from exc
            if isinstance(message, str):
                payload = _control(message)
                kind = str(payload.get("type") or "")
                if kind == "exit" and exited is None:
                    exited = ProcessExit(exit_code=_exit_code(payload.get("exit_code")))
                    drain_until = loop.time() + EXIT_DRAIN_SECONDS
                elif kind == "error":
                    raise RuntimeError(
                        f"execd reported an error on process {self._process_id!r}: "
                        f"{payload.get('error') or payload}"
                    )
                continue
            frame = bytes(message)
            if not frame:
                continue
            output = _output(frame, self._process_id)
            if output is not None:
                yield output
        if exited is not None:
            yield exited

    async def send(self, data: bytes) -> None:
        if self._detached:
            raise SandboxProcessDetached(f"the attachment to process {self._process_id!r} is closed")
        try:
            async with self._send_lock:
                await self._connection.send(bytes([STDIN]) + bytes(data))
        except websockets.ConnectionClosed as exc:
            raise SandboxProcessDetached(
                f"the attachment to process {self._process_id!r} is closed: {exc}"
            ) from exc

    async def detach(self) -> None:
        self._detached = True
        await self._connection.close()

    async def _send_control(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self._connection.send(json.dumps(payload))


class ExecdProcesses:
    """:class:`astrabox.seams.sandbox_data.SandboxProcesses` over one execd.

    Build it from an endpoint resolver, which is called once, at the first
    operation, or with :meth:`at_endpoint` from a URL and the headers a
    request needs. It holds no connection between operations, so it needs no
    closing; each attachment is closed by its own ``detach``.
    """

    def __init__(
        self,
        resolve_endpoint: Callable[[], Awaitable[SandboxEndpoint]],
        *,
        sandbox_id: str,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolve_endpoint = resolve_endpoint
        self._endpoint: SandboxEndpoint | None = None
        self._sandbox_id = str(sandbox_id)
        self._http_transport = http_transport

    @classmethod
    def at_endpoint(
        cls,
        url: str,
        headers: Mapping[str, str],
        *,
        sandbox_id: str,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> ExecdProcesses:
        """Reach the execd at ``url``, sending ``headers`` on every request."""

        endpoint = SandboxEndpoint(url=url, headers=headers)

        async def resolved() -> SandboxEndpoint:
            return endpoint

        return cls(resolved, sandbox_id=sandbox_id, http_transport=http_transport)

    # ── the contract ─────────────────────────────────────────────────────

    async def start(self, command: str, *, cwd: str | None) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("a process command is a non-empty shell command line")
        body: dict[str, Any] = {"command": command}
        if cwd:
            body["cwd"] = cwd
        response = await self._request("POST", "/pty", body=body)
        if response.status_code not in (200, 201):
            raise self._refused(response, "start a process")
        process_id = str((response.json() or {}).get("session_id") or "").strip()
        if not process_id:
            raise RuntimeError("execd started a process without returning its id")
        return process_id

    async def exists(self, process_id: str) -> bool:
        response = await self._request("GET", self._path(process_id))
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise self._refused(response, f"report process {process_id!r}")
        return True

    async def output_offset(self, process_id: str) -> int:
        return int((await self._status(process_id)).get("output_offset") or 0)

    async def attach(
        self, process_id: str, *, since: int, takeover: bool
    ) -> ExecdProcessAttachment:
        endpoint = await self._resolved()
        requested = max(0, int(since))
        url = (
            f"{_websocket_url(endpoint.url)}{self._path(process_id)}/ws"
            f"?pty=0&takeover={int(takeover)}&since={requested}"
        )
        try:
            connection = await websockets.connect(
                url,
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                close_timeout=CLOSE_TIMEOUT_SECONDS,
                max_size=_MAX_FRAME_BYTES,
                create_connection=ExecdClientConnection,
                additional_headers=dict(endpoint.headers) or None,
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status == 404:
                raise SandboxProcessDetached(f"execd does not know process {process_id!r}") from exc
            if status == 409 and not takeover:
                raise SandboxProcessAlreadyAttached(
                    f"another attachment holds process {process_id!r}"
                ) from exc
            raise RuntimeError(
                f"execd refused to attach to process {process_id!r}: HTTP {status}"
            ) from exc
        except (OSError, TimeoutError, InvalidHandshake) as exc:
            raise SandboxUnreachable(
                self._sandbox_id, f"execd could not attach to process {process_id!r}: {exc}"
            ) from exc
        try:
            pending = await asyncio.wait_for(
                self._until_connected(connection, process_id, requested),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except BaseException as exc:
            await connection.close()
            if isinstance(exc, TimeoutError):
                raise SandboxUnreachable(
                    self._sandbox_id,
                    f"execd did not finish attaching to process {process_id!r} "
                    f"within {CONNECT_TIMEOUT_SECONDS:g}s",
                ) from exc
            raise
        return ExecdProcessAttachment(connection, process_id=process_id, pending=pending)

    async def signal(self, process_id: str, name: str) -> None:
        status = await self._status(process_id)
        if not status.get("running"):
            return
        attachment = await self.attach(
            process_id, since=int(status.get("output_offset") or 0), takeover=True
        )
        try:
            await attachment._send_control({"type": "signal", "signal": str(name)})
        finally:
            await attachment.detach()

    async def close(self, process_id: str) -> None:
        response = await self._request("DELETE", self._path(process_id))
        if response.status_code not in (200, 204, 404):
            raise self._refused(response, f"close process {process_id!r}")

    # ── wire ─────────────────────────────────────────────────────────────

    @staticmethod
    def _path(process_id: str) -> str:
        target = str(process_id or "").strip()
        if not target:
            raise ValueError("a process id is required")
        return f"/pty/{quote(target, safe='')}"

    async def _resolved(self) -> SandboxEndpoint:
        if self._endpoint is None:
            self._endpoint = await self._resolve_endpoint()
        return self._endpoint

    async def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        endpoint = await self._resolved()
        try:
            async with httpx.AsyncClient(
                transport=self._http_transport,
                timeout=CONNECT_TIMEOUT_SECONDS,
                headers=dict(endpoint.headers),
            ) as client:
                return await client.request(method, f"{endpoint.url}{path}", json=body)
        except httpx.TransportError as exc:
            raise SandboxUnreachable(
                self._sandbox_id, f"execd {method} {path} failed: {exc}"
            ) from exc

    async def _status(self, process_id: str) -> dict[str, Any]:
        response = await self._request("GET", self._path(process_id))
        if response.status_code == 404:
            raise SandboxProcessDetached(f"execd does not know process {process_id!r}")
        if response.status_code != 200:
            raise self._refused(response, f"report process {process_id!r}")
        payload = response.json()
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _refused(response: httpx.Response, operation: str) -> RuntimeError:
        return RuntimeError(
            f"execd refused to {operation}: HTTP {response.status_code} {response.text[:300]}"
        )

    @staticmethod
    async def _until_connected(
        connection: ClientConnection, process_id: str, requested: int
    ) -> list[ProcessFrame]:
        """Read up to execd's ``connected`` notice; return the frames sent before it.

        Only a replay of earlier output precedes the notice, and its offset is
        the one place a lost stretch of output shows.
        """

        pending: list[ProcessFrame] = []
        while True:
            try:
                message = await connection.recv()
            except websockets.ConnectionClosed as exc:
                raise SandboxProcessDetached(
                    f"execd closed the attachment to process {process_id!r} before "
                    f"it was established: {exc}"
                ) from exc
            if isinstance(message, str):
                payload = _control(message)
                kind = str(payload.get("type") or "")
                if kind == "connected":
                    return pending
                if kind == "error":
                    raise RuntimeError(
                        f"execd refused to attach to process {process_id!r}: "
                        f"{payload.get('error') or payload}"
                    )
                continue
            frame = bytes(message)
            if not frame:
                continue
            output = _output(frame, process_id)
            if isinstance(output, ProcessReplay) and output.offset > requested:
                raise SandboxProcessReplayGap(
                    process_id, requested=requested, available=output.offset
                )
            if output is not None:
                pending.append(output)


__all__ = [
    "CLOSE_TIMEOUT_SECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "EXIT_DRAIN_SECONDS",
    "ExecdClientConnection",
    "ExecdProcessAttachment",
    "ExecdProcesses",
    "REPLAY",
    "STDERR",
    "STDIN",
    "STDOUT",
]
