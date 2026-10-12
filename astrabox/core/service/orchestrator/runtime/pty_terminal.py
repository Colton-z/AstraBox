"""Provide one long-lived shell per session, as a process in the session's box.

The shell outlives individual requests, so ``cd`` and ``export`` affect later
commands. A command sentinel reports each command's exit code and final working
directory, because the process's own exit describes the shell, not a command.

The shell is one of the box's long-lived processes
(:class:`astrabox.seams.sandbox_data.SandboxProcesses`): any replica attaches
to it by id and takes it over, which lets a command run on whichever replica
serves the request. The host keeps only the process id.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import shlex
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import websockets

from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox_data import (
    ProcessExit,
    ProcessOutput,
    SandboxEndpoint,
    SandboxProcessDetached,
    SandboxProcesses,
)

#: How long to wait for a command's sentinel before giving the caller back what
#: arrived. A terminal must not hang on a command that never returns — the user
#: can still see the output that did arrive, and the shell stays alive.
_COMMAND_TIMEOUT_SECONDS = 300.0


def websocket_header_kwargs(headers: Mapping[str, str]) -> dict[str, Any]:
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


def _shell_command(envs: Mapping[str, str]) -> str:
    """Start the persistent Bash with only the supplied process-env overlay.

    ``exec`` makes the Bash the process the box started rather than its
    child, so a signal or a close aimed at the process reaches the shell.
    """

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
        return "exec bash --norc --noprofile"
    return "exec env -- " + " ".join(assignments) + " bash --norc --noprofile"


async def resolve_sandbox_endpoint(underlying: Any, port: int) -> SandboxEndpoint:
    """Resolve one in-box port through the sandbox handle's ``get_endpoint``.

    The result keeps the routing or access headers a deployment returns with
    the address: dropping them works against an open local Docker server and
    fails behind a hardened Kubernetes ingress. A failure is the 502
    ``AGENT_RUNTIME_ERROR`` naming the port.
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
    if not isinstance(resolved, SandboxEndpoint):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"in-box port {port} resolved to {type(resolved).__name__}, "
                "not a SandboxEndpoint"
            ),
            status_code=502,
        )
    return resolved


class PtyTerminal:
    """A persistent shell in one box, kept as one of the box's long-lived processes."""

    def __init__(self, processes: SandboxProcesses) -> None:
        self._processes = processes

    async def open_session(
        self,
        *,
        cwd: str | None,
        envs: Mapping[str, str] | None = None,
    ) -> str:
        # The process face carries no environment of its own, so the overlay
        # is part of the shell's command line and belongs to the shell process
        # rather than to the box.
        return await self._processes.start(_shell_command(envs or {}), cwd=cwd or None)

    async def session_exists(self, pty_session_id: str) -> bool:
        return await self._processes.exists(pty_session_id)

    async def run(self, pty_session_id: str, command: str) -> AsyncIterator[dict[str, Any]]:
        """Run one command in the session's shell, yielding terminal events.

        Yields ``{"type": "stdout"|"stderr", "text": ...}`` as output arrives and
        finally ``{"type": "__done__", "exit_code": int, "cwd": str}``. Framing
        the command is the sentinel's job (see :func:`_sentinel_command`); the
        process's own exit would mean the shell died, which is reported as the
        command's status with whatever output it left.
        """
        marker = f"__ASTRABOX_PTY_{uuid.uuid4().hex[:12]}__"
        # Requiring digits, so the sentinel cannot be matched by anything the
        # command itself happened to print that merely began with the marker.
        sentinel = re.compile(rf"{re.escape(marker)}:(-?\d+):([^\r\n]*)")
        # Attaching from where the output ends now is what makes this call see
        # only its own output rather than every earlier command's as well.
        since = await self._processes.output_offset(pty_session_id)
        attachment = await self._processes.attach(pty_session_id, since=since, takeover=True)
        frames = attachment.frames()
        pending = ""
        exit_code: int | None = None
        cwd = ""
        shell_exited = False
        try:
            await attachment.send(_sentinel_command(command, marker).encode())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _COMMAND_TIMEOUT_SECONDS
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    frame = await asyncio.wait_for(anext(frames), timeout=remaining)
                except (TimeoutError, StopAsyncIteration, SandboxProcessDetached):
                    # The deadline passed, or the attachment ended without the
                    # shell's exit: an interrupt took the shell over, or the
                    # connection dropped. What arrived is still output.
                    break

                if isinstance(frame, ProcessExit):
                    # The shell itself left (`exit 7`, or anything that kills
                    # bash), so the sentinel never printed: this is the
                    # command's exit status, and whatever is buffered is the
                    # output nobody else will flush. Output the exit overtook
                    # has already arrived before it.
                    exit_code = frame.exit_code if frame.exit_code is not None else 0
                    shell_exited = True
                    break

                text = frame.data.decode("utf-8", "replace")
                if isinstance(frame, ProcessOutput) and frame.stream == "stderr":
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
                    # Trailing newlines only: the sentinel's own ends the
                    # output, and a leading one separates this from the line
                    # already sent.
                    emit = pending[: found.start()].rstrip("\n")
                    if emit:
                        yield {"type": "stdout", "text": emit}
                    break

                # The sentinel is printed on a line of its own, so only the line
                # still being written can turn out to be it. Everything before
                # that line goes out as it arrives, while the command runs. The
                # line is held back, with the newline before it, which is the
                # sentinel's own when the sentinel follows, only while it could
                # still be the start of a sentinel split across frames.
                head = pending.rfind("\n")
                tail = pending[head + 1:]
                if marker.startswith(tail) or tail.startswith(marker):
                    cut = head
                else:
                    cut = len(pending)
                if cut > 0:
                    emit, pending = pending[:cut], pending[cut:]
                    yield {"type": "stdout", "text": emit}
        finally:
            await frames.aclose()
            await attachment.detach()

        if shell_exited or exit_code is None:
            # Either the shell died mid-command, or the deadline passed before
            # the sentinel arrived. Both leave real output in the buffer, and
            # dropping it would report a command as silent when it was not.
            trailing = pending.rstrip("\n")
            if trailing:
                yield {"type": "stdout", "text": trailing}

        yield {"type": "__done__", "exit_code": exit_code if exit_code is not None else 0, "cwd": cwd}


__all__ = [
    "PtyTerminal",
    "resolve_sandbox_endpoint",
    "websocket_header_kwargs",
]
