"""Provide one long-lived execd shell per session.

The shell outlives individual requests, so ``cd`` and ``export`` affect later
commands. A command sentinel reports each command's exit code and final working
directory because execd's ``exit`` frame describes the shell itself.

Protocol (components/execd/PTY.md):

* ``POST /pty`` with ``{"cwd": ...}`` creates a session and returns its id;
* ``GET  /pty/{id}`` reports ``output_offset`` — where a reconnect should resume;
* ``WS   /pty/{id}/ws`` carries it. ``?since=<offset>`` replays what was missed,
  ``?takeover=1`` evicts an existing connection (a second one is otherwise a
  409, and the evicted one closes with 4001);
* frames are binary with a one-byte kind — ``0x01`` stdout, ``0x02`` stderr in
  pipe mode, ``0x03`` replay — and input is ``0x00`` followed by raw bytes;
* ``?pty=0`` selects pipe mode, avoiding input echo and shell prompts while
  preserving separate stdout and stderr frames;
* control is JSON text: ``resize``, ``signal``, ``ping``.

``takeover=1`` lets any replica attach to the shell owned by the box. The host
only persists the execd session id.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import shlex
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import websockets

from astrabox.common.utils.errors import APIError
from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

#: execd's port inside the sandbox. Fixed by the daemon, not configurable here.
EXECD_PORT = 44772

#: How long to wait for a command's sentinel before giving the caller back what
#: arrived. A terminal must not hang on a command that never returns — the user
#: can still see the output that did arrive, and the shell stays alive.
_COMMAND_TIMEOUT_SECONDS = 300.0

_CONNECT_TIMEOUT_SECONDS = 15.0

#: How long to keep reading after the shell's exit notice. execd's exit control
#: frame can overtake stdout frames still queued behind it — a fast
#: ``echo X && exit 7`` can report exit 7 with X lost. The socket closing ends
#: the drain early; this only bounds a server that neither sends more nor
#: closes.
_EXIT_DRAIN_SECONDS = 0.5

_STDOUT, _STDERR, _REPLAY, _INPUT = 0x01, 0x02, 0x03, 0x00


def websocket_header_kwargs(headers: dict[str, str]) -> dict[str, Any]:
    """Header keyword across websockets' legacy and asyncio client APIs."""

    if not headers:
        return {}
    parameter = (
        "additional_headers"
        if "additional_headers" in inspect.signature(websockets.connect).parameters
        else "extra_headers"
    )
    return {parameter: headers}


def _sentinel_command(command: str, marker: str) -> str:
    """Wrap one command so the shell reports where it ended and what it returned.

    ``$?`` is captured immediately after the command and before anything else,
    and ``$PWD`` with it, so a command that changes directory reports the
    directory the next one will start in — which it will, because the shell is
    the same one.
    """
    return f"{command}\nprintf '\\n{marker}:%s:%s\\n' \"$?\" \"$PWD\"\n"


def _shell_command_with_env(envs: Mapping[str, str]) -> str:
    """Start the persistent Bash with only the supplied process-env overlay."""

    assignments: list[str] = []
    for raw_name, raw_value in sorted(envs.items()):
        name = str(raw_name)
        value = str(raw_value)
        if not name or "=" in name or "\0" in name:
            raise ValueError(f"invalid PTY environment variable name: {name!r}")
        if "\0" in value:
            raise ValueError(f"PTY environment variable {name!r} contains NUL")
        assignments.append(shlex.quote(f"{name}={value}"))
    if not assignments:
        return ""
    return "exec env -- " + " ".join(assignments) + " bash --norc --noprofile"


@dataclass(frozen=True)
class ResolvedExecdEndpoint:
    """Host-reachable execd URL and the routing headers required to use it."""

    origin: str
    headers: dict[str, str]


async def resolve_execd_endpoint(underlying: Any) -> ResolvedExecdEndpoint:
    """Resolve execd through the sandbox backend's official endpoint face."""

    return await resolve_sandbox_endpoint(underlying, EXECD_PORT)


async def resolve_sandbox_endpoint(
    underlying: Any, port: int
) -> ResolvedExecdEndpoint:
    """Resolve one in-box port through the sandbox backend's endpoint face.

    Mirrors how the file panel reaches the in-box file server: ``get_endpoint``
    is the backend's contract for turning an in-box port into an address the
    host can dial. Some deployments also return routing or access headers;
    dropping those makes the same code work on an open local Docker server and
    fail on a hardened Kubernetes ingress, so they are part of the result.
    """
    get_endpoint = getattr(underlying, "get_endpoint", None)
    if underlying is None or not callable(get_endpoint):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "sandbox does not expose an endpoint face for in-box port "
                f"{port}"
            ),
            status_code=502,
        )
    try:
        resolved = await get_endpoint(int(port))
    except APIError:
        raise
    except Exception as exc:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"in-box port {port} endpoint resolution failed: {exc}",
            status_code=502,
        ) from exc
    if isinstance(resolved, dict):
        raw_origin = resolved.get("endpoint") or resolved.get("url")
        raw_headers = resolved.get("headers")
    else:
        raw_origin = getattr(resolved, "endpoint", None) or getattr(
            resolved, "url", None
        )
        raw_headers = getattr(resolved, "headers", None)
    origin = str(raw_origin or "").strip().rstrip("/")
    if not origin.startswith(("http://", "https://")):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"in-box port {port} endpoint is not an absolute URL: "
                f"{origin!r}"
            ),
            status_code=502,
        )
    headers = {
        str(key): str(value)
        for key, value in dict(raw_headers or {}).items()
        if str(key).strip() and value is not None
    }
    return ResolvedExecdEndpoint(origin=origin, headers=headers)


async def resolve_execd_origin(underlying: Any) -> str:
    """URL-only view of :func:`resolve_execd_endpoint`, without its headers."""

    return (await resolve_execd_endpoint(underlying)).origin


class PtyTerminal:
    """A shell in one sandbox, addressed by the box's execd endpoint."""

    def __init__(
        self,
        origin: str,
        *,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        #: ``http://host:port`` for this box's execd, as the endpoint face resolved it.
        self._origin = origin.rstrip("/")
        self._headers = dict(headers or {})
        self._transport = transport

    @property
    def _ws_origin(self) -> str:
        if self._origin.startswith("https://"):
            return "wss://" + self._origin[len("https://") :]
        return "ws://" + self._origin[len("http://") :]

    async def open_session(
        self,
        *,
        cwd: str | None,
        envs: Mapping[str, str] | None = None,
    ) -> str:
        body: dict[str, Any] = {}
        if cwd:
            body["cwd"] = cwd
        command = _shell_command_with_env(envs or {})
        if command:
            # Execd v1.1.0 has no PTY ``envs`` field. Its documented
            # ``command`` field launches this shell when the WebSocket opens,
            # so the overlay belongs to the PTY process rather than the box.
            body["command"] = command
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=_CONNECT_TIMEOUT_SECONDS,
            headers=self._headers,
        ) as client:
            response = await client.post(f"{self._origin}/pty", json=body)
            response.raise_for_status()
            session_id = str((response.json() or {}).get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError("execd created a pty session without returning an id")
        return session_id

    async def close_session(self, pty_session_id: str) -> bool:
        """Terminate a persistent shell; 404 is already-clean idempotent success."""
        target = str(pty_session_id or "").strip()
        if not target:
            return False
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=_CONNECT_TIMEOUT_SECONDS,
            headers=self._headers,
        ) as client:
            response = await client.delete(f"{self._origin}/pty/{target}")
            if response.status_code != 404:
                response.raise_for_status()
        return True

    async def output_offset(self, pty_session_id: str) -> int:
        """Where this shell's output currently ends.

        A reconnect without this replays the session's whole history, so every
        command would arrive carrying every earlier command's output. Asking
        first, and attaching with ``since=<offset>``, is what makes one call see
        only its own output.
        """
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=_CONNECT_TIMEOUT_SECONDS,
            headers=self._headers,
        ) as client:
            response = await client.get(f"{self._origin}/pty/{pty_session_id}")
            response.raise_for_status()
            return int((response.json() or {}).get("output_offset") or 0)

    async def session_exists(self, pty_session_id: str) -> bool:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_CONNECT_TIMEOUT_SECONDS,
                headers=self._headers,
            ) as client:
                response = await client.get(f"{self._origin}/pty/{pty_session_id}")
            return response.status_code == 200
        except Exception:
            return False

    async def run(self, pty_session_id: str, command: str) -> AsyncIterator[dict[str, Any]]:
        """Run one command in the session's shell, yielding terminal events.

        Yields ``{"type": "stdout"|"stderr", "text": ...}`` as output arrives and
        finally ``{"type": "__done__", "exit_code": int, "cwd": str}``. Framing
        the command is the sentinel's job (see :func:`_sentinel_command`); the
        socket's own ``exit`` frame would mean the shell died, which is reported
        as a failure rather than as the command finishing.
        """
        marker = f"__ASTRABOX_PTY_{uuid.uuid4().hex[:12]}__"
        # Requiring digits, so the sentinel cannot be matched by anything the
        # command itself happened to print that merely began with the marker.
        sentinel = re.compile(rf"{re.escape(marker)}:(-?\d+):([^\r\n]*)")
        since = await self.output_offset(pty_session_id)
        url = f"{self._ws_origin}/pty/{pty_session_id}/ws?pty=0&takeover=1&since={since}"
        pending = ""
        exit_code: int | None = None
        cwd = ""
        shell_exited = False

        async with websockets.connect(
            url,
            open_timeout=_CONNECT_TIMEOUT_SECONDS,
            **websocket_header_kwargs(self._headers),
        ) as ws:
            await ws.send(bytes([_INPUT]) + _sentinel_command(command, marker).encode())
            loop = asyncio.get_event_loop()
            deadline = loop.time() + _COMMAND_TIMEOUT_SECONDS

            while exit_code is None or shell_exited:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                except websockets.ConnectionClosed:
                    # The drain's natural end: execd said everything it had.
                    break

                if isinstance(frame, str):
                    # Control frames. `exit` here is the shell leaving — which a
                    # command can cause (`exit 7`, or anything that kills bash).
                    # The sentinel never printed in that case, so this is the
                    # command's exit code and the only chance to hand over what
                    # it wrote: whatever is buffered is the output nobody else
                    # will flush. Reading continues past it, because the exit
                    # notice can overtake stdout frames still queued behind it:
                    # a fast `echo X && exit 7` can report exit 7 with X lost.
                    # Drain briefly; the socket closing or the window lapsing
                    # ends it.
                    with contextlib.suppress(Exception):
                        payload = json.loads(frame)
                        if str(payload.get("type") or "") == "exit":
                            exit_code = int(payload.get("exit_code") or 0)
                            shell_exited = True
                            deadline = min(deadline, loop.time() + _EXIT_DRAIN_SECONDS)
                    continue

                if not frame:
                    continue
                kind, data = frame[0], frame[1:]
                if kind == _REPLAY:
                    data = data[8:]  # an 8-byte offset header precedes replayed bytes
                elif kind not in (_STDOUT, _STDERR):
                    continue

                text = data.decode("utf-8", "replace")
                if kind == _STDERR:
                    # Straight through. Only stdout can carry the sentinel, so
                    # stderr never needs holding back — buffering the two
                    # together would label stderr as stdout.
                    if text:
                        yield {"type": "stderr", "text": text}
                    continue
                pending += text

                found = sentinel.search(pending)
                if found is not None:
                    exit_code = int(found.group(1))
                    cwd = found.group(2).strip()
                    emit = pending[: found.start()].strip("\n")
                    if emit:
                        yield {"type": "stdout", "text": emit}
                    break

                # Emit only complete lines. The echo is removed line by line, so
                # a stream cut at an arbitrary byte boundary would hand out the
                # first half of a line the filter was going to drop — letting
                # the echoed command reach the user. Whatever follows the last
                # newline stays buffered until the rest of it arrives.
                # Hold back only enough that a sentinel split across frames is
                # not handed out as output before it can be recognised.
                keep = len(marker) + 64
                if len(pending) > keep:
                    emit, pending = pending[:-keep], pending[-keep:]
                    if emit:
                        yield {"type": "stdout", "text": emit}

        if shell_exited or exit_code is None:
            # Either the shell died mid-command, or the deadline passed before
            # the sentinel arrived. Both leave real output in the buffer, and
            # dropping it would report a command as silent when it was not.
            trailing = pending.strip("\n")
            if trailing:
                yield {"type": "stdout", "text": trailing}

        yield {"type": "__done__", "exit_code": exit_code if exit_code is not None else 0, "cwd": cwd}


__all__ = [
    "EXECD_PORT",
    "PtyTerminal",
    "ResolvedExecdEndpoint",
    "resolve_execd_endpoint",
    "resolve_execd_origin",
    "websocket_header_kwargs",
]
