"""The platform's channel to a box process that speaks JSON lines.

An engine whose embedding surface is newline-delimited JSON on stdin and
stdout runs as one of the box's long-lived processes
(:class:`astrabox.seams.sandbox_data.SandboxProcesses`): the sandbox opens no
TCP port for it and no platform credential is copied into the box.

This channel knows that process face and nothing else — the byte cursor a
reattach resumes from, LF record framing, "a record is one JSON object", the
stderr tail an exit needs to be diagnosable, and a single terminal failure.
**It reads no vendor vocabulary.** Which record is a response, what a request
envelope looks like, when the process is ready to answer, and what an
unmatched response means are all the adapter's, and the installed adapters
genuinely disagree about the last one.

So the channel is composed, not inherited, and its request support is
mechanism only: :meth:`register_request` hands back a future,
:meth:`complete_request` settles one the adapter has recognized. The channel
never decides that a record with an ``id`` is a response.

``PtyTerminal`` in :mod:`.pty_terminal` serves the other shape — an
interactive shell whose output is bytes for a human and whose completion is a
sentinel — and stays separate. The two share the process face, not a protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from astrabox.seams.sandbox_data import (
    ProcessExit,
    ProcessFrame,
    ProcessReplay,
    SandboxProcessAttachment,
    SandboxProcessDetached,
    SandboxProcesses,
)

#: How much stderr to keep. An exit notice carries no reason of its own, so
#: this tail is the only account of why a process died.
_STDERR_TAIL_LIMIT = 8000

#: One parsed record and the byte offset just past its terminating LF.
RecordSink = Callable[[dict[str, Any], int], Awaitable[None]]
FailureSink = Callable[[BaseException], Awaitable[None]]


class ProcessJsonLineChannel:
    """One box process carrying newline-delimited JSON objects.

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
        processes: SandboxProcesses,
        cwd: str,
        command: str,
        label: str,
        on_record: RecordSink,
        on_failure: FailureSink,
        process_id: str | None = None,
    ) -> None:
        self.processes = processes
        self.cwd = str(cwd)
        self.command = str(command)
        #: Named in every failure message so an operator can tell what died.
        self.label = str(label or "engine")
        self.process_id = str(process_id or "").strip() or None
        self._on_record = on_record
        self._on_failure = on_failure
        self._attachment: SandboxProcessAttachment | None = None
        self._reader_task: asyncio.Task[Any] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._fatal: BaseException | None = None
        self._stdout_buffer = b""
        self._buffer_start_offset = 0
        self._next_output_offset = 0
        self._stderr_tail = ""

    # ── state ────────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._attachment is not None and self._fatal is None

    @property
    def fatal(self) -> BaseException | None:
        return self._fatal

    @property
    def stderr_tail(self) -> str:
        return self._stderr_tail

    # ── the process ──────────────────────────────────────────────────────
    async def create(self) -> str:
        if self.process_id:
            return self.process_id
        self.process_id = await self.processes.start(self.command, cwd=self.cwd)
        return self.process_id

    async def current_output_offset(self) -> int:
        if not self.process_id:
            raise RuntimeError(f"{self.label} process has not been created")
        return await self.processes.output_offset(self.process_id)

    async def connect(self, *, since: int, takeover: bool = True) -> None:
        """Attach to the process from ``since`` and start reading.

        Returning proves the transport is up, not that the process can answer.
        Readiness is the engine's own signal, so the adapter establishes it.
        """

        if self.is_connected:
            return
        process_id = await self.create()
        requested = max(0, int(since))
        self._next_output_offset = requested
        self._buffer_start_offset = requested
        try:
            self._attachment = await self.processes.attach(
                process_id, since=requested, takeover=takeover
            )
            self._reader_task = asyncio.create_task(
                self._reader_loop(self._attachment),
                name=f"process-json-lines:{process_id}",
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self.fail(exc)
            assert self._fatal is not None
            if self._fatal is exc:
                raise
            raise self._fatal from exc

    # ── sending ──────────────────────────────────────────────────────────
    async def send(self, payload: dict[str, Any]) -> None:
        """Write one JSON object to the process's stdin, LF-terminated."""

        attachment = self._attachment
        if not self.is_connected or attachment is None:
            raise SandboxProcessDetached(f"{self.label} is not connected")
        message = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        async with self._send_lock:
            await attachment.send(message)

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
        unmatched reply is a fact about the engine's protocol, and the
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
    async def _reader_loop(self, attachment: SandboxProcessAttachment) -> None:
        frames = attachment.frames()
        try:
            async for frame in frames:
                await self._handle_frame(frame)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self.fail(exc)
        finally:
            await frames.aclose()
            if self._fatal is None:
                await self.fail(SandboxProcessDetached(f"{self.label} connection closed"))

    async def _handle_frame(self, frame: ProcessFrame) -> None:
        if isinstance(frame, ProcessExit):
            raise RuntimeError(
                f"{self.label} process exited with code {frame.exit_code}; "
                f"stderr={self._stderr_tail[-1000:]!r}"
            )
        if isinstance(frame, ProcessReplay):
            # The replay starts at what the box still holds, and the attach
            # already refused one that starts past the cursor asked for.
            self._next_output_offset = frame.offset
            self._buffer_start_offset = frame.offset
            self._stdout_buffer = b""
            await self._feed_stdout(frame.data)
            return
        if frame.stream == "stderr":
            self._stderr_tail = (
                self._stderr_tail + frame.data.decode("utf-8", "replace")
            )[-_STDERR_TAIL_LIMIT:]
            return
        await self._feed_stdout(frame.data)

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
        consequences, so only the first is kept and reported. A failure that
        is not one of the process face's own, nor a ``RuntimeError`` the
        channel or the box raised, is a transport the attachment lost.
        """

        if self._fatal is not None:
            return
        if not isinstance(exc, RuntimeError):
            exc = SandboxProcessDetached(f"{self.label} transport detached: {exc}")
        self._fatal = exc
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        await self._on_failure(exc)

    async def detach(self) -> None:
        task = self._reader_task
        self._reader_task = None
        attachment = self._attachment
        self._attachment = None
        if task is not None:
            task.cancel()
        if attachment is not None:
            with contextlib.suppress(BaseException):
                await attachment.detach()
        if task is not None:
            with contextlib.suppress(BaseException):
                await task

    async def delete(self) -> bool:
        """Detach and stop the process behind this channel."""

        await self.detach()
        if not self.process_id:
            return False
        await self.processes.close(self.process_id)
        self.process_id = None
        return True
