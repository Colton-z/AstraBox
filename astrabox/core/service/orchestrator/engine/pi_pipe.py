"""Pi's half of the execd pipe: which record answers which command.

``pi --mode rpc`` speaks newline-delimited JSON on stdin/stdout, which is
exactly what :class:`ExecdJsonLineChannel` carries. The channel owns the pipe
— the PTY resource, the byte cursor and its replay gap, LF record framing, the
stderr tail, one terminal failure. This module owns what a record *means* to
pi, and nothing else.

Two readings are pi's own and are stated here rather than assumed:

* **A response is matched by ``id``, and there is exactly one per command.**
  Pi documents that a command carrying an ``id`` gets one response with the
  same id, and that failures *after* acceptance arrive as events rather than
  as a second response. So the match is the vendor's correlation, not a guess
  about ordering.
* **An unmatched response is reported, not dropped.** Pi emits one response
  per command it accepted, so a response nobody is waiting for is evidence
  about a command this process has stopped tracking. The client
  decides what to do with it; swallowing it here would hide the case.

Transport failures cross the engine seam as :class:`EngineStreamDetached`.
Translating them belongs here: the platform channel must not depend on the
engine contract to describe its own pipe dying.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.runtime.execd_json_lines import (
    ExecdChannelDetached,
    ExecdJsonLineChannel,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    ResolvedExecdEndpoint,
)

_RPC_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class PiWireRecord:
    """One record pi wrote that no command was waiting for.

    Session events, extension UI requests, extension errors — and an
    response handled by the turn consumer, which is why this is not called an
    event.
    """

    record: dict[str, Any]
    output_offset: int


class PiRpcProcess:
    """One ``pi --mode rpc`` process behind one execd pipe session."""

    def __init__(
        self,
        *,
        endpoint: ResolvedExecdEndpoint,
        cwd: str,
        command: str,
        pty_session_id: str | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._channel = ExecdJsonLineChannel(
            endpoint=endpoint,
            cwd=cwd,
            command=command,
            label="pi RPC",
            on_record=self._on_record,
            on_failure=self._on_failure,
            pty_session_id=pty_session_id,
            http_transport=http_transport,
        )
        self._records: asyncio.Queue[PiWireRecord | BaseException] = asyncio.Queue()
        self._fatal: BaseException | None = None

    # ── delegated pipe state ─────────────────────────────────────────────
    @property
    def endpoint(self) -> ResolvedExecdEndpoint:
        return self._channel.endpoint

    @property
    def cwd(self) -> str:
        return self._channel.cwd

    @property
    def pty_session_id(self) -> str | None:
        return self._channel.pty_session_id

    @property
    def is_connected(self) -> bool:
        return self._channel.is_connected

    @property
    def fatal(self) -> BaseException | None:
        """This process's terminal failure, in the engine seam's vocabulary."""

        return self._fatal

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
    async def connect(self, *, since: int) -> None:
        """Open the pipe and start reading.

        Pi announces no readiness record of its own, so a connected pipe is
        all this proves. A caller that needs to know the agent can answer asks
        it — one ``get_state`` round trip — rather than waiting for a signal
        pi never sends.
        """

        try:
            await self._channel.connect(since=since)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)
            assert self._fatal is not None
            raise self._fatal
        if self._fatal is not None:
            raise self._fatal

    # ── commands ─────────────────────────────────────────────────────────
    async def command(
        self,
        request_id: str,
        payload: dict[str, Any],
        *,
        timeout: float = _RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Send one command carrying ``request_id`` and await its response.

        The returned record is pi's response verbatim, including
        ``success: false``. Refusing a rejected command is the caller's
        decision, because "rejected" is an answer about the command and not a
        failure of the pipe.
        """

        if not self.is_connected:
            raise EngineStreamDetached("pi RPC is not connected")
        future = self._channel.register_request(request_id)
        try:
            await self._channel.send({**payload, "id": request_id})
            return await asyncio.wait_for(future, timeout=float(timeout))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._fail(exc)
            assert self._fatal is not None
            raise self._fatal
        finally:
            self._channel.release_request(request_id)

    async def send_untracked(self, payload: dict[str, Any]) -> None:
        """Write without installing a separate response waiter.

        UI answers carry Pi's request id and get no response of their own.
        Interactive extension commands carry a command id whose response the
        turn consumer handles alongside the dialogs that precede it.
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
        item = await self._records.get()
        if isinstance(item, BaseException):
            raise item
        return item

    # ── pi's reading of one record ───────────────────────────────────────
    async def _on_record(self, record: dict[str, Any], output_offset: int) -> None:
        if str(record.get("type") or "") == "response":
            request_id = str(record.get("id") or "").strip()
            if request_id and self._channel.complete_request(request_id, record):
                return
        await self._records.put(
            PiWireRecord(record=record, output_offset=output_offset)
        )

    async def _on_failure(self, exc: BaseException) -> None:
        if self._fatal is not None:
            return
        self._fatal = (
            EngineStreamDetached(str(exc))
            if isinstance(exc, ExecdChannelDetached)
            else exc
        )
        await self._records.put(self._fatal)

    async def _fail(self, exc: BaseException) -> None:
        await self._channel.fail(exc)
        if self._fatal is None:
            # The channel already held a failure, so this one never reached
            # _on_failure; adopt the one that actually settled it.
            await self._on_failure(self._channel.fatal or exc)
