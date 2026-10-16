"""Pi's half of its process pipe: which record answers which command.

``pi --mode rpc`` speaks newline-delimited JSON on stdin/stdout, which is
exactly what :class:`ProcessJsonLineChannel` carries. The channel owns the
pipe — the box process, the byte cursor, LF record framing, the stderr tail,
one terminal failure. This module owns what a record *means* to pi, and
nothing else.

Two readings are pi's own and are stated here rather than assumed:

* **A response is matched by ``id``, and there is exactly one per command.**
  Pi documents that a command carrying an ``id`` gets one response with the
  same id, and that failures *after* acceptance arrive as events rather than
  as a second response. So the match is the vendor's correlation, not a guess
  about ordering.
* **Every record is also part of the stream, in the order pi wrote it.** A
  response settles the command waiting on it and still reaches the reader of
  the stream, because some responses are facts about the conversation: a
  ``prompt`` response reports what pi did with the prompt (started, queued or
  handled), and where it sits among the events is how a reader knows which
  user message is that prompt. A reader replaying the pipe sees the same
  records whether or not a command was waiting when they were first written.

Transport failures cross the engine seam as :class:`EngineStreamDetached`.
Translating them belongs here: the platform channel must not depend on the
engine contract to describe its own pipe dying.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrabox.core.service.orchestrator.engine.base import (
    EngineOutputAlreadyObserved,
    EngineStreamDetached,
)
from astrabox.core.service.orchestrator.runtime.process_json_lines import (
    ProcessExited,
    ProcessJsonLineChannel,
)
from astrabox.seams.sandbox_data import (
    SandboxProcessAlreadyAttached,
    SandboxProcessDetached,
    SandboxProcesses,
)

_RPC_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class PiWireRecord:
    """One record pi wrote, and where it starts in the process's output."""

    record: dict[str, Any]
    offset: int


class PiRpcProcess:
    """One attachment to a ``pi --mode rpc`` process, one of its box's long-lived processes.

    An attachment that ends cannot be reopened; attaching again is a new
    instance naming the same ``pty_session_id``.
    """

    def __init__(
        self,
        *,
        processes: SandboxProcesses,
        cwd: str,
        command: str,
        pty_session_id: str | None = None,
    ) -> None:
        self._channel = ProcessJsonLineChannel(
            processes=processes,
            cwd=cwd,
            command=command,
            label="pi RPC",
            on_record=self._on_record,
            on_failure=self._on_failure,
            process_id=pty_session_id,
        )
        self._records: asyncio.Queue[PiWireRecord | BaseException] = asyncio.Queue()
        self._fatal: BaseException | None = None

    # ── delegated pipe state ─────────────────────────────────────────────
    @property
    def pty_session_id(self) -> str | None:
        return self._channel.process_id

    @property
    def is_connected(self) -> bool:
        return self._channel.is_connected

    @property
    def fatal(self) -> BaseException | None:
        """This attachment's terminal failure, in the engine seam's vocabulary."""

        return self._fatal

    @property
    def exited(self) -> bool:
        """Whether the box reported that the process itself ended."""

        return isinstance(self._channel.fatal, ProcessExited)

    @property
    def stderr_tail(self) -> str:
        return self._channel.stderr_tail

    async def create(self) -> str:
        return await self._channel.create()

    async def current_output_offset(self) -> int:
        return await self._channel.current_output_offset()

    async def detach(self) -> None:
        await self._channel.detach()

    async def delete(self) -> bool:
        return await self._channel.delete()

    # ── connect ──────────────────────────────────────────────────────────
    async def connect(self, *, since: int, takeover: bool = True) -> None:
        """Open the pipe and start reading from output offset ``since``.

        Pi announces no readiness record of its own, so a connected pipe is
        all this proves. A caller that needs to know the agent can answer asks
        it — one ``get_state`` round trip — rather than waiting for a signal
        pi never sends. The box's own errors (a replay gap, an unknown
        process) are raised as the box raised them.
        """

        try:
            await self._channel.connect(since=since, takeover=takeover)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)
            raise
        if self._fatal is not None:
            raise self._fatal

    # ── commands ─────────────────────────────────────────────────────────
    async def request(self, request_id: str, payload: dict[str, Any]) -> asyncio.Future[dict[str, Any]]:
        """Send one command carrying ``request_id``; return the future of its response.

        The caller awaits the future as long as it is prepared to and then
        calls :meth:`release`. The response is pi's verbatim, including
        ``success: false``: refusing a rejected command is the caller's
        decision, because "rejected" is an answer about the command and not a
        failure of the pipe.
        """

        if not self.is_connected:
            raise EngineStreamDetached("pi RPC is not connected")
        future = self._channel.register_request(request_id)
        try:
            await self._channel.send({**payload, "id": request_id})
        except asyncio.CancelledError:
            self._channel.release_request(request_id)
            raise
        except BaseException as exc:
            self._channel.release_request(request_id)
            await self._fail(exc)
            assert self._fatal is not None
            raise self._fatal
        return future

    def release(self, request_id: str) -> None:
        self._channel.release_request(request_id)

    async def command(
        self,
        request_id: str,
        payload: dict[str, Any],
        *,
        timeout: float = _RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Send one command and await its response for at most ``timeout`` seconds.

        A response that is late is not a broken pipe: :class:`TimeoutError`
        reaches the caller and the pipe stays open, and the response, when pi
        writes it, still reaches the stream.
        """

        future = await self.request(request_id, payload)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=float(timeout))
        finally:
            self.release(request_id)

    async def send_untracked(self, payload: dict[str, Any]) -> None:
        """Write without installing a response waiter.

        UI answers carry Pi's request id and get no response of their own.
        Commands whose responses only the stream needs (an abort, a stop, an
        extension command whose handler may wait on a person) are written this
        way too; their responses arrive with the other records.
        """

        if not self.is_connected:
            raise EngineStreamDetached("pi RPC is not connected")
        try:
            await self._channel.send(payload)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)
            assert self._fatal is not None
            raise self._fatal

    async def next_record(self) -> PiWireRecord:
        """The next record in pipe order; raises the attachment's terminal failure."""

        item = await self._records.get()
        if isinstance(item, BaseException):
            raise item
        return item

    # ── pi's reading of one record ───────────────────────────────────────
    async def _on_record(self, record: dict[str, Any], offset: int) -> None:
        if str(record.get("type") or "") == "response":
            request_id = str(record.get("id") or "").strip()
            if request_id:
                self._channel.complete_request(request_id, record)
        await self._records.put(PiWireRecord(record=record, offset=offset))

    async def _on_failure(self, exc: BaseException) -> None:
        if self._fatal is not None:
            return
        if isinstance(exc, SandboxProcessAlreadyAttached):
            self._fatal = EngineOutputAlreadyObserved(str(exc))
        elif isinstance(exc, SandboxProcessDetached):
            self._fatal = EngineStreamDetached(str(exc))
        else:
            self._fatal = exc
        await self._records.put(self._fatal)

    async def _fail(self, exc: BaseException) -> None:
        await self._channel.fail(exc)
        if self._fatal is None:
            # The channel already held a failure, so this one never reached
            # _on_failure; adopt the one that actually settled it.
            await self._on_failure(self._channel.fatal or exc)
