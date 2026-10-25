"""The commands face over execd, the in-box daemon every built-in image carries.

execd (``opensandbox/execd``, the release the images and the lifecycle server
pin) runs a command line with ``bash -c`` (``sh -c`` when the image has no
bash) on its ``/command`` route and streams the command's output back as
server-sent events. :class:`ExecdCommands` implements
:class:`astrabox.seams.sandbox_data.SandboxCommands` on that route through the
OpenSandbox SDK's execd client, and keeps execd's wire rules out of core:

* output arrives as one event per line (:data:`EXECD_LINE_EVENT_MODEL`), and
  :func:`join_execd_lines` reassembles a stream from them;
* a non-zero exit arrives as an error event whose value is the status, so the
  status is read from the first numeric error value (:class:`ReportedExitStatus`)
  and a success from the completion event;
* a process ended by a signal reports ``-1``;
* execd stops a command when the deadline sent with it passes, which is how a
  command whose caller stopped waiting is not left running in the box.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from opensandbox.adapters.factory import AdapterFactory
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxConnectionException,
    SandboxTimeoutException,
)
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.sandboxes import SandboxEndpoint
from opensandbox.services.command import Commands
from opensandbox.transport.retry import RetryPolicy

from astrabox.common.logger.logger_factory import get_logger
from astrabox.seams.sandbox import SandboxUnreachable
from astrabox.seams.sandbox_data import (
    SandboxCommandOutcomeUnknown,
    SandboxCommandRefused,
    SandboxCommandResult,
)

#: The execd release whose output events :data:`EXECD_LINE_EVENT_MODEL` records.
EXECD_LINE_EVENT_MODEL_VERSION = "1.1.0"

#: execd's stdout and stderr events, as ``(in-box command, events, reassembled text)``.
#:
#: execd publishes no normative contract for these events, so this table is the
#: measurement :func:`join_execd_lines` is built on:
#:
#: * one event carries one output line with its terminator removed, and a
#:   carriage return ends a line as a newline does;
#: * an event whose text is exactly ``"\n"`` carries an empty line.
#:
#: Reassembly therefore reads the empty-line event as ``""`` and joins on
#: ``"\n"``. Interior line boundaries survive; a trailing newline, and a
#: carriage return before a newline, do not.
EXECD_LINE_EVENT_MODEL: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (r"printf 'hello\n200'", ("hello", "200"), "hello\n200"),
    (r"printf 'a\n\n404'", ("a", "\n", "404"), "a\n\n404"),
    (r"printf 'only\n'", ("only",), "only"),
    (r"printf 'no-newline'", ("no-newline",), "no-newline"),
    (r"printf 'a\r\nb\n200'", ("a", "b", "200"), "a\nb\n200"),
)

#: The event text that stands for an empty line (see the model above).
_EMPTY_LINE_EVENT = "\n"

#: How long past the caller's deadline execd lets a command run before stopping
#: it. The caller's side gives up first, so a command that ends just in time is
#: never reported as stopped by the box.
_BOX_STOP_GRACE_SECONDS = 5.0

logger = get_logger(__name__)


def join_execd_lines(lines: Iterable[str]) -> str:
    """Reassemble one output stream from execd's line events (:data:`EXECD_LINE_EVENT_MODEL`)."""

    return "\n".join("" if line == _EMPTY_LINE_EVENT else line for line in lines)


class ReportedExitStatus:
    """The exit status execd named, kept apart from the events after it.

    execd reports a process's own exit status as an error event's value
    (``"9"`` for ``exit 9``) and reports transport trouble as error events
    too, with a message. A process exits once, so the first numeric value is
    its status and anything after it describes the plumbing.
    """

    __slots__ = ("code",)

    def __init__(self) -> None:
        self.code: int | None = None

    def record(self, error: Any) -> None:
        if self.code is not None:
            return
        try:
            self.code = int(str(getattr(error, "value", "")))
        except (TypeError, ValueError):
            return


class ExecdCommands:
    """:class:`astrabox.seams.sandbox_data.SandboxCommands` over one execd.

    Build it with :meth:`over` from an SDK execd command client that the caller
    already holds and closes, or with :meth:`at_endpoint` from the URL and
    request headers a provider's ``get_endpoint`` resolves. The second opens a
    connection for each command and closes it after, so the object holds
    nothing that needs closing.
    """

    def __init__(
        self,
        open_commands: Callable[[], AbstractAsyncContextManager[Commands]],
        *,
        sandbox_id: str,
    ) -> None:
        self._open = open_commands
        self._sandbox_id = str(sandbox_id)

    @classmethod
    def over(cls, commands: Commands, *, sandbox_id: str) -> ExecdCommands:
        """Use an execd command client the caller owns and closes."""

        @asynccontextmanager
        async def borrowed() -> AsyncIterator[Commands]:
            yield commands

        return cls(borrowed, sandbox_id=sandbox_id)

    @classmethod
    def at_endpoint(
        cls, url: str, headers: Mapping[str, str], *, sandbox_id: str
    ) -> ExecdCommands:
        """Reach the execd at ``url``, sending ``headers`` on every request."""

        address = urlsplit(url)
        if address.scheme not in {"http", "https"} or not address.netloc:
            raise ValueError(f"an execd endpoint needs an http(s) URL, not {url!r}")
        endpoint = SandboxEndpoint(
            endpoint=address.netloc + address.path.rstrip("/"), headers=dict(headers)
        )

        @asynccontextmanager
        async def connection() -> AsyncIterator[Commands]:
            config = ConnectionConfig(protocol=address.scheme).with_transport_if_missing()
            try:
                yield AdapterFactory(config).create_command_service(endpoint)
            finally:
                await config.close_transport_if_owned()

        return cls(connection, sandbox_id=sandbox_id)

    async def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = 60.0,
        idempotent: bool = False,
    ) -> SandboxCommandResult:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("a sandbox command is a non-empty shell command line")
        if timeout is not None and timeout <= 0:
            raise ValueError(f"a command deadline is a positive number of seconds, not {timeout!r}")
        opts = RunCommandOpts(
            envs=_checked_env(env),
            timeout=(
                timedelta(seconds=timeout + _BOX_STOP_GRACE_SECONDS)
                if timeout is not None
                else None
            ),
        )
        # The SDK's own retry policy, which re-sends only an idempotent request
        # whose connection failed after it was sent, and how often.
        policy = RetryPolicy()
        retries = policy.max_retries if idempotent else 0
        backoff = policy.initial_backoff.total_seconds()
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                attempt = 0
                while True:
                    try:
                        return await self._run_once(command, opts)
                    except (
                        SandboxCommandOutcomeUnknown,
                        SandboxConnectionException,
                        httpx.TransportError,
                    ) as exc:
                        if attempt == retries:
                            if isinstance(exc, SandboxCommandOutcomeUnknown):
                                raise
                            raise SandboxUnreachable(
                                self._sandbox_id, f"execd could not run {command[:200]!r}: {exc}"
                            ) from exc
                        logger.warning(
                            "execd connection failed before %r reported an exit status; "
                            "running it again in %.1fs: %s",
                            command[:200],
                            backoff,
                            exc,
                        )
                    attempt += 1
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * policy.backoff_multiplier, policy.max_backoff.total_seconds())
        except TimeoutError:
            if timeout is None or not deadline.expired():
                raise
            raise TimeoutError(
                f"execd command did not complete within {timeout:g}s: {command[:200]!r}"
            ) from None

    async def _run_once(self, command: str, opts: RunCommandOpts) -> SandboxCommandResult:
        stdout: list[str] = []
        stderr: list[str] = []
        status = ReportedExitStatus()
        completed = False
        errors: list[str] = []

        async def on_stdout(message: Any) -> None:
            stdout.append(str(message.text))

        async def on_stderr(message: Any) -> None:
            stderr.append(str(message.text))

        async def on_error(error: Any) -> None:
            status.record(error)
            errors.append(f"{error.name}: {error.value}")

        async def on_complete(_complete: Any) -> None:
            nonlocal completed
            completed = True

        handlers = ExecutionHandlers(
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            on_error=on_error,
            on_execution_complete=on_complete,
            skip_accumulation=True,
        )
        try:
            async with self._open() as commands:
                await commands.run(command, opts=opts, handlers=handlers)
        except SandboxApiException as exc:
            raise SandboxCommandRefused(
                f"execd refused to run {command[:200]!r}: {exc}"
            ) from exc
        except SandboxTimeoutException as exc:
            raise SandboxUnreachable(
                self._sandbox_id, f"execd could not run {command[:200]!r}: {exc}"
            ) from exc
        if status.code is not None:
            code = status.code
        elif completed:
            code = 0
        else:
            detail = "; ".join(errors) or "the report ended without one"
            raise SandboxCommandOutcomeUnknown(
                f"execd reported no exit status for {command[:200]!r}: {detail}"
            )
        return SandboxCommandResult(
            exit_code=code, stdout=join_execd_lines(stdout), stderr=join_execd_lines(stderr)
        )


def _checked_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if not env:
        return None
    checked: dict[str, str] = {}
    for name, value in env.items():
        if not isinstance(name, str) or not name or "=" in name or "\0" in name:
            raise ValueError(f"invalid environment variable name for a sandbox command: {name!r}")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"environment variable {name!r} must be text without NUL")
        checked[name] = value
    return checked


__all__ = [
    "EXECD_LINE_EVENT_MODEL",
    "EXECD_LINE_EVENT_MODEL_VERSION",
    "ExecdCommands",
    "ReportedExitStatus",
    "join_execd_lines",
]
