"""The session web terminal: one command at a time in the session's box, recorded as it runs.

:meth:`TerminalService.run` starts a command and returns its events as they
happen. The command runs in a task of this process (its runner), not in the
request that started it: closing the page leaves the command running, and
:meth:`TerminalService.follow` reads the command back from its record and
follows it to its end, from any process. :meth:`TerminalService.interrupt` asks
the runner, wherever it runs, to stop the command, and returns once the command
has ended.

The starting request sees each event as the runner reads it from the box. The
runner records the same events behind it, in order
(:class:`~astrabox.persistence.repository.terminal_repository.TerminalRepository`),
and commits a command's end (its last events and its status) before anyone is
shown it, so a person who sees a command end can start the next one at once.

Events are ``ack`` (the command, its ``command_id`` and its directory),
``stdout``, ``stderr``, ``cwd`` (the directory the shell ended in) and a final
``exit``. An ``exit`` with ``exit_code`` ``None`` means the platform could not
observe the command's status. Events read back from the record also carry their
``seq``.

The terminal is the session's box shell (:mod:`.terminal_box`). It does not go
through the session's agent runtime or its reader.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.session_runtime.feed import (
    Notice,
    Resync,
    SessionFeed,
    Subscription,
)
from astrabox.core.service.orchestrator.terminal_box import TerminalBox, TerminalTarget
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.terminal_repository import TerminalBusy, TerminalRepository

logger = get_logger(__name__)

#: How long a runner's lease on its command lasts. The runner renews it every
#: third of that, as a session reader renews its own lease
#: (:data:`astrabox.core.service.orchestrator.session_runtime.reader.LEASE_TTL_S`).
LEASE_TTL_S = 30.0

#: How long an interrupt waits for the command to end before it fails.
INTERRUPT_SETTLE_S = 10.0

_FOLLOW_PAGE = 500
_WRITE_BATCH = 500

_LOST_TEXT = "astrabox: the process running this command stopped; its output ends here\n"
_STOPPED_TEXT = "astrabox: the platform stopped while this command ran; the command was interrupted\n"
_UNRECORDED_TEXT = "astrabox: part of this command's output could not be recorded\n"
_NO_EXIT_TEXT = "astrabox: the command ended without reporting an exit status\n"

Spawn = Callable[..., asyncio.Task[Any]]


def _stderr(text: str) -> dict[str, Any]:
    return {"type": "stderr", "text": text}


def _exit(exit_code: int | None) -> dict[str, Any]:
    return {"type": "exit", "exit_code": exit_code}


def _lost_end() -> list[dict[str, Any]]:
    """The last events of a command whose runner stopped."""
    return [_stderr(_LOST_TEXT), _exit(None)]


def _storable(event: dict[str, Any]) -> dict[str, Any]:
    """``event`` with NUL characters replaced, which PostgreSQL text cannot hold.

    Output is decoded from bytes with replacement, so binary output reaches
    here as text that can contain NUL; the live event and the record carry the
    same replacement.
    """
    text = event.get("text")
    if isinstance(text, str) and "\x00" in text:
        return {**event, "text": text.replace("\x00", "�")}
    return event


def _require_ready(session: dict[str, Any]) -> None:
    state = str(session.get("state") or "")
    if state == SessionState.CREATING.value:
        raise APIError(code="SESSION_BUSY", message="session is still creating, please retry", status_code=409)
    if state in (SessionState.TERMINATED.value, SessionState.DELETED.value):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"terminal is unavailable because the session is {state.lower()}",
            status_code=409,
        )


@dataclass(frozen=True, slots=True)
class _End:
    """How a command ended: its recorded status, exit code and closing stderr lines."""

    status: str
    exit_code: int | None
    notes: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class _Run:
    """A command this process runs: the people watching it live and the events not yet recorded."""

    session_id: str
    command_id: str
    command: str
    cwd: str | None
    shell_id: str | None
    started_at: str | None
    target: TerminalTarget
    subscription: Subscription
    pending: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    watchers: list[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=list)
    box: asyncio.Task[dict[str, Any] | None] | None = None
    next_cwd: str | None = None
    #: Why the box task was cancelled from inside the runner: ``interrupt`` or ``lost``.
    stop: str | None = None
    unrecorded: bool = False

    def watch(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.watchers.append(queue)
        return queue

    def unwatch(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        with contextlib.suppress(ValueError):
            self.watchers.remove(queue)

    def show(self, event: dict[str, Any]) -> None:
        for queue in tuple(self.watchers):
            queue.put_nowait(event)

    def stop_box(self, reason: str) -> None:
        """Stop the command in the box for ``reason``; the first reason given is the one kept."""
        if self.box is not None and not self.box.done():
            if self.stop is None:
                self.stop = reason
            self.box.cancel()


class TerminalService:
    def __init__(
        self,
        *,
        repo: TerminalRepository,
        sessions: SessionRepository,
        feed: SessionFeed,
        box: TerminalBox,
        spawn: Spawn,
    ) -> None:
        self._repo = repo
        self._sessions = sessions
        self._feed = feed
        self._box = box
        self._spawn = spawn

    # -- requests ----------------------------------------------------------

    async def run(
        self, user: UserContext, session_id: str, command: str, cwd: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Start ``command`` in the session's terminal; its events as they happen.

        ``cwd`` is where it runs; without one it runs where the previous
        command ended. Refuses with ``409 SESSION_BUSY`` while another command
        runs, and with ``409`` when the session's terminal cannot run now.
        Closing the returned iterator leaves the command running.
        """
        requested_cwd = str(cwd or "").strip() or None
        if "\x00" in command or "\x00" in (requested_cwd or ""):
            raise APIError(code="INVALID_REQUEST", message="command and directory must not contain NUL", status_code=400)
        session = await self._owned(user, session_id)
        _require_ready(session)
        target = await self._box.prepare(session)
        subscription = self._feed.subscribe(session_id)
        command_id = f"tcmd_{uuid.uuid4().hex}"
        try:
            try:
                row = await self._repo.start_command(
                    session_id=session_id,
                    command_id=command_id,
                    command=command,
                    cwd=requested_cwd,
                    default_cwd=target.default_cwd,
                    lease_s=LEASE_TTL_S,
                )
            except TerminalBusy:
                raise APIError(code="SESSION_BUSY", message="terminal is already running", status_code=409) from None
            run = _Run(
                session_id=session_id,
                command_id=command_id,
                command=command,
                cwd=row["cwd"],
                shell_id=row["execution_id"],
                started_at=row["started_at"],
                target=target,
                subscription=subscription,
            )
            live = run.watch()
            await self._start_runner(run)
        except BaseException:
            subscription.close()
            await target.close()
            raise
        return self._live(run, live)

    async def follow(
        self,
        user: UserContext,
        session_id: str,
        *,
        command_id: str | None = None,
        after_seq: int = -1,
    ) -> AsyncIterator[dict[str, Any]] | None:
        """A command's recorded events after ``after_seq``, then live, through its ``exit``.

        Without ``command_id``, the command the session's terminal runs now, or
        ``None`` when none runs. A command whose runner stopped is ended first,
        so following it ends too.
        """
        await self._owned(user, session_id)
        await self._repo.end_lost(session_id, payloads=_lost_end())
        if command_id:
            command = await self._repo.get_command(session_id, command_id)
            if command is None:
                raise APIError(
                    code="TERMINAL_COMMAND_NOT_FOUND",
                    message="terminal command not found",
                    status_code=404,
                )
        else:
            command = await self._repo.running_command(session_id)
            if command is None:
                return None
        return self._replay(session_id, str(command["command_id"]), after_seq)

    async def interrupt(self, user: UserContext, session_id: str) -> dict[str, Any]:
        """Stop the command the session's terminal runs; returns once it has ended.

        ``status`` is ``completed`` when a command was stopped and ``idle`` when
        none ran. The command's runner carries the request out in whichever
        process runs it; a command whose runner stopped is ended here. Fails
        with ``502 AGENT_RUNTIME_ERROR`` when the command is still running after
        :data:`INTERRUPT_SETTLE_S`.
        """
        await self._owned(user, session_id)
        with self._feed.subscribe(session_id) as subscription:
            requested = await self._repo.request_interrupt(session_id)
            if requested is None:
                return {"session_id": session_id, "status": "idle"}
            command_id = str(requested["command_id"])
            loop = asyncio.get_running_loop()
            deadline = loop.time() + INTERRUPT_SETTLE_S
            while True:
                await self._repo.end_lost(session_id, payloads=_lost_end())
                command = await self._repo.get_command(session_id, command_id)
                if command is None or command["status"] != "running":
                    return {"session_id": session_id, "status": "completed"}
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise APIError(
                        code="AGENT_RUNTIME_ERROR",
                        message="interrupt failed: terminal did not settle",
                        status_code=502,
                    )
                await _terminal_changed(subscription, timeout=remaining)

    async def directory(self, session_id: str) -> str | None:
        """Where the session's terminal is: where its latest command ended, or runs.

        ``None`` before the session's first command.
        """
        return await self._repo.directory(session_id)

    async def _owned(self, user: UserContext, session_id: str) -> dict[str, Any]:
        session = await self._sessions.get_owned_session(session_id, str(user.user_id))
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        return session

    # -- streams -----------------------------------------------------------

    async def _live(
        self, run: _Run, queue: asyncio.Queue[dict[str, Any] | None]
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            run.unwatch(queue)

    async def _replay(self, session_id: str, command_id: str, after_seq: int) -> AsyncIterator[dict[str, Any]]:
        # Subscribed before the first read, so output recorded after that read
        # is announced to this subscription.
        with self._feed.subscribe(session_id) as subscription:
            last = after_seq
            while True:
                # An ended row precedes the read of all its committed final output.
                command = await self._repo.get_command(session_id, command_id)
                rows = await self._repo.read_output(command_id, after_seq=last, limit=_FOLLOW_PAGE)
                for row in rows:
                    last = int(row["seq"])
                    event = {**row["payload"], "seq": last}
                    yield event
                    if event.get("type") == "exit":
                        return
                if len(rows) == _FOLLOW_PAGE:
                    continue
                if command is None or command["status"] != "running":
                    return
                if not await self._await_output(subscription, session_id, command_id, last):
                    return

    async def _await_output(
        self, subscription: Subscription, session_id: str, command_id: str, last: int
    ) -> bool:
        """Wait until the command may have more recorded; ``False`` once it is gone.

        Output after ``last`` is announced by a ``terminal`` notice. A notice
        with ``seq`` 0 means a command started, which removes the ended
        command before it. After a quiet :data:`LEASE_TTL_S` the command is
        ended if its runner stopped.
        """
        try:
            async with asyncio.timeout(LEASE_TTL_S):
                while True:
                    item = await subscription.get()
                    if isinstance(item, Resync):
                        return True
                    if isinstance(item, Notice) and item.kind == "terminal":
                        if item.seq > last:
                            return True
                        if item.seq == 0:
                            break
        except TimeoutError:
            await self._repo.end_lost(session_id, payloads=_lost_end())
        return await self._repo.get_command(session_id, command_id) is not None

    # -- the runner --------------------------------------------------------

    async def _start_runner(self, run: _Run) -> None:
        try:
            self._spawn(self._serve(run), name=f"terminal-command-{run.command_id}")
        except BaseException:
            # The command is recorded as running and nothing will run it.
            with contextlib.suppress(Exception):
                await self._repo.end_command(
                    session_id=run.session_id,
                    command_id=run.command_id,
                    status="failed",
                    exit_code=None,
                    next_cwd=None,
                    payloads=[_exit(None)],
                )
            raise

    async def _serve(self, run: _Run) -> None:
        """Run one command to its end and record it; the body of the runner task."""
        writer = asyncio.create_task(self._write(run), name=f"terminal-writer-{run.command_id}")
        self._emit(
            run,
            {
                "type": "ack",
                "command_id": run.command_id,
                "session_id": run.session_id,
                "command": run.command,
                "working_directory": run.cwd,
                "at": run.started_at,
            },
        )
        run.box = asyncio.create_task(self._drain(run), name=f"terminal-box-{run.command_id}")
        watcher = asyncio.create_task(self._watch(run), name=f"terminal-watch-{run.command_id}")
        try:
            end = await self._outcome(run)
        except asyncio.CancelledError:
            # The platform is stopping. Cancelling the box task interrupted the
            # command in the box; record that before the task ends.
            await asyncio.gather(run.box, return_exceptions=True)
            await self._finish(run, _End("interrupted", 130, (_stderr(_STOPPED_TEXT),)), writer, watcher)
            raise
        await self._finish(run, end, writer, watcher)

    async def _outcome(self, run: _Run) -> _End:
        assert run.box is not None
        try:
            exit_event = await run.box
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            if run.stop == "interrupt":
                return _End("interrupted", 130)
            return _End("failed", None, (_stderr(_LOST_TEXT),))
        except Exception as exc:
            logger.exception("terminal command failed session=%s command=%s", run.session_id, run.command_id)
            return _End("failed", 1, (_stderr(f"{exc}\n"),))
        if exit_event is None:
            return _End("failed", None, (_stderr(_NO_EXIT_TEXT),))
        raw_code = exit_event.get("exit_code")
        return _End("exited", int(raw_code) if raw_code is not None else None)

    async def _drain(self, run: _Run) -> dict[str, Any] | None:
        """Read the command's events from the box; its ``exit`` event, or ``None`` if none came."""
        events = self._box.run(
            run.target,
            run.command,
            cwd=run.cwd,
            shell_id=run.shell_id,
            on_shell=partial(self._repo.bind_shell, run.command_id),
        )
        async with contextlib.aclosing(events):
            async for event in events:
                if event.get("type") == "exit":
                    return event
                if event.get("type") == "cwd":
                    run.next_cwd = str(event.get("path") or "") or None
                self._emit(run, event)
        return None

    def _emit(self, run: _Run, event: dict[str, Any]) -> None:
        event = _storable(event)
        run.show(event)
        if not run.unrecorded:
            run.pending.put_nowait(event)

    async def _write(self, run: _Run) -> None:
        """Record the command's events in order, each statement taking all that wait."""
        while True:
            first = await run.pending.get()
            if first is None:
                return
            batch = [first]
            ended = False
            while len(batch) < _WRITE_BATCH and not run.pending.empty():
                event = run.pending.get_nowait()
                if event is None:
                    ended = True
                    break
                batch.append(event)
            try:
                written = await self._repo.append_output(
                    session_id=run.session_id, command_id=run.command_id, payloads=batch
                )
            except Exception:
                logger.exception(
                    "terminal output was not recorded session=%s command=%s", run.session_id, run.command_id
                )
                run.unrecorded = True
                return
            if written is None:
                # Someone ended the command for a runner they took for gone.
                run.stop_box("lost")
                return
            if ended:
                return

    async def _watch(self, run: _Run) -> None:
        """Renew the command's lease and carry out an interrupt request until the runner finishes.

        The lease is renewed after an interrupt too: the box may take a while
        to stop the command (an isolation session is replaced), and the
        command still runs until it has.
        """
        loop = asyncio.get_running_loop()
        renew_every = LEASE_TTL_S / 3
        renew_at = loop.time() + renew_every
        while True:
            timeout = renew_at - loop.time()
            if timeout <= 0:
                renew_at = loop.time() + renew_every
                try:
                    held = await self._repo.renew(run.command_id, lease_s=LEASE_TTL_S)
                except Exception:
                    logger.warning(
                        "terminal command lease renewal failed session=%s command=%s",
                        run.session_id,
                        run.command_id,
                        exc_info=True,
                    )
                    continue
                if not held:
                    run.stop_box("lost")
                    return
                continue
            try:
                item = await asyncio.wait_for(run.subscription.get(), timeout=timeout)
            except TimeoutError:
                continue
            if run.stop is not None:
                continue
            if not (isinstance(item, Resync) or (isinstance(item, Notice) and item.kind == "terminal_interrupt")):
                continue
            try:
                command = await self._repo.get_command(run.session_id, run.command_id)
            except Exception:
                logger.warning(
                    "terminal command read failed session=%s command=%s",
                    run.session_id,
                    run.command_id,
                    exc_info=True,
                )
                continue
            if command is None or command["status"] != "running":
                run.stop_box("lost")
                return
            if command.get("interrupt_requested_at"):
                run.stop_box("interrupt")

    async def _finish(self, run: _Run, end: _End, writer: asyncio.Task[None], watcher: asyncio.Task[None]) -> None:
        """Record the command's end after its output, then show it and let go of the command.

        The people watching live are released however this ends; a command
        whose end was not recorded is ended as lost once its lease passes.
        """
        watcher.cancel()
        run.pending.put_nowait(None)
        final = [_exit(end.exit_code)]
        try:
            await asyncio.gather(writer, watcher, return_exceptions=True)
            notes = [_stderr(_UNRECORDED_TEXT)] if run.unrecorded else []
            final = [_storable(event) for event in (*notes, *end.notes)] + final
            await self._repo.end_command(
                session_id=run.session_id,
                command_id=run.command_id,
                status=end.status,
                exit_code=end.exit_code,
                next_cwd=run.next_cwd,
                payloads=final,
            )
        except Exception:
            logger.exception(
                "terminal command end was not recorded session=%s command=%s", run.session_id, run.command_id
            )
        finally:
            for event in final:
                run.show(event)
            for queue in tuple(run.watchers):
                queue.put_nowait(None)
            run.subscription.close()
            await run.target.close()


async def _terminal_changed(subscription: Subscription, *, timeout: float) -> None:
    """Wait for a ``terminal`` notice or a resync on ``subscription``, at most ``timeout`` seconds."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(timeout):
            while True:
                item = await subscription.get()
                if isinstance(item, Resync) or (isinstance(item, Notice) and item.kind == "terminal"):
                    return
