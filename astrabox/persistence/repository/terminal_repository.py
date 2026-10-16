"""Persistence for the session web terminal: its commands and their output.

The process running a command (its runner) holds the command row under a lease
(``terminal_commands.expires_at``) and is the only writer of the command's
output while the command runs. The database numbers output events
(``terminal_output.seq``), so a command's events keep increasing numbers however
long its output grows, and a command's end, written by its runner or by
whoever finds the runner gone, follows everything recorded before it.

Statements that record output or end a command send a ``terminal`` notice with
the last ``seq`` they wrote; starting a command sends one with 0, and an
interrupt request sends ``terminal_interrupt``
(:mod:`astrabox.persistence.sql.notify`). Rows are returned as dicts with
ISO-8601 times.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import CTE, ColumnElement, Text, delete, func, insert, literal, or_, select, true, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import Select

from astrabox.persistence.sql import UniqueViolation, execute, fetch_all, fetch_one, fetch_value, to_iso
from astrabox.persistence.sql.models.sessions import TerminalCommand, TerminalOutput
from astrabox.persistence.sql.notify import notice

_COMMANDS = TerminalCommand.__table__
_OUTPUT = TerminalOutput.__table__

_RUNNING_INDEX = "ix_terminal_commands_running"


class TerminalBusy(Exception):
    """The session's terminal is already running a command."""


def _view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        key: (to_iso(value) if isinstance(value, datetime) else value)
        for key, value in row.items()
        if key != "notified"
    }


def _lease(lease_s: float) -> ColumnElement[Any]:
    return func.now() + func.make_interval(0, 0, 0, 0, 0, 0, lease_s)


def _command() -> Select[Any]:
    """A command row with whether its runner's lease has passed."""
    return select(_COMMANDS, (_COMMANDS.c.expires_at < func.now()).label("lease_expired"))


class TerminalRepository:
    # -- a command's lifetime ----------------------------------------------

    async def start_command(
        self,
        *,
        session_id: str,
        command_id: str,
        command: str,
        cwd: str | None,
        default_cwd: str | None,
        lease_s: float,
    ) -> dict[str, Any]:
        """Record a running command for the session, in one statement.

        The statement deletes the session's earlier commands that ended or
        whose runner's lease passed, with their output, and starts the new one
        in ``cwd``, else where the latest of them ended, else ``default_cwd``.
        The new row keeps a shell only after its command exited. A lost or
        interrupted runner does not establish that its shell is idle. Raises
        :class:`TerminalBusy` when another command is running, and then
        deletes nothing.
        """
        previous = (
            delete(_COMMANDS)
            .where(
                _COMMANDS.c.session_id == session_id,
                or_(_COMMANDS.c.status != "running", _COMMANDS.c.expires_at < func.now()),
            )
            .returning(
                _COMMANDS.c.execution_id,
                _COMMANDS.c.cwd,
                _COMMANDS.c.next_cwd,
                _COMMANDS.c.started_at,
                _COMMANDS.c.status,
            )
            .cte("previous")
        )
        shell = (
            select(previous.c.execution_id)
            .where(previous.c.execution_id.isnot(None), previous.c.status == "exited")
            .order_by(previous.c.started_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        last_directory = (
            select(func.coalesce(previous.c.next_cwd, previous.c.cwd))
            .order_by(previous.c.started_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        started = (
            insert(_COMMANDS)
            .from_select(
                ["command_id", "session_id", "command", "cwd", "status", "execution_id", "expires_at"],
                select(
                    literal(command_id),
                    literal(session_id),
                    literal(command),
                    func.coalesce(literal(cwd, Text), last_directory, literal(default_cwd, Text)),
                    literal("running"),
                    shell,
                    _lease(lease_s),
                ),
            )
            .returning(*_COMMANDS.c)
            .cte("started")
        )
        try:
            row = await fetch_one(select(started, notice(started.c.session_id, "terminal").label("notified")))
        except UniqueViolation as exc:
            if exc.constraint == _RUNNING_INDEX:
                raise TerminalBusy(session_id) from exc
            raise
        stored = _view(row)
        assert stored is not None
        return stored

    async def bind_shell(self, command_id: str, execution_id: str | None) -> None:
        """Name the box process the session's terminal shell runs as, on its command."""
        await execute(
            update(_COMMANDS).where(_COMMANDS.c.command_id == command_id).values(execution_id=execution_id)
        )

    async def append_output(
        self, *, session_id: str, command_id: str, payloads: Sequence[Mapping[str, Any]]
    ) -> int | None:
        """Record output events of a running command, in order; the last ``seq`` written.

        ``None`` when the command has ended: whoever ended it recorded the end,
        and nothing follows it. The command row is share-locked for the
        statement, so an end written concurrently comes after this output.
        """
        running = (
            select(_COMMANDS.c.command_id)
            .where(_COMMANDS.c.command_id == command_id, _COMMANDS.c.status == "running")
            .with_for_update(read=True)
            .cte("running")
        )
        return await _record(session_id, running, payloads)

    async def renew(self, command_id: str, *, lease_s: float) -> bool:
        """Extend a running command's lease; ``False`` once the command has ended."""
        return (
            await execute(
                update(_COMMANDS)
                .where(_COMMANDS.c.command_id == command_id, _COMMANDS.c.status == "running")
                .values(expires_at=_lease(lease_s))
            )
            > 0
        )

    async def end_command(
        self,
        *,
        session_id: str,
        command_id: str,
        status: str,
        exit_code: int | None,
        next_cwd: str | None,
        payloads: Sequence[Mapping[str, Any]],
    ) -> int | None:
        """End a running command and record its last events, in one statement.

        Returns the last ``seq`` written, or ``None`` when the command had
        already ended and its end is recorded.
        """
        ended = (
            update(_COMMANDS)
            .where(_COMMANDS.c.command_id == command_id, _COMMANDS.c.status == "running")
            .values(status=status, exit_code=exit_code, next_cwd=next_cwd, ended_at=func.now())
            .returning(_COMMANDS.c.command_id)
            .cte("ended")
        )
        return await _record(session_id, ended, payloads)

    async def end_lost(self, session_id: str, *, payloads: Sequence[Mapping[str, Any]]) -> int | None:
        """End the session's running command if its runner's lease has passed.

        Records ``payloads`` as the command's last events and marks it
        ``failed``; returns the last ``seq`` written, or ``None`` when no
        command was lost.
        """
        ended = (
            update(_COMMANDS)
            .where(
                _COMMANDS.c.session_id == session_id,
                _COMMANDS.c.status == "running",
                _COMMANDS.c.expires_at < func.now(),
            )
            .values(status="failed", ended_at=func.now())
            .returning(_COMMANDS.c.command_id)
            .cte("ended")
        )
        return await _record(session_id, ended, payloads)

    async def request_interrupt(self, session_id: str) -> dict[str, Any] | None:
        """Ask the session's running command to stop; that command, or ``None``.

        The request stays on the row until the command ends, so a runner that
        missed the ``terminal_interrupt`` notice finds it when it reads its row.
        """
        return _view(
            await fetch_one(
                update(_COMMANDS)
                .where(_COMMANDS.c.session_id == session_id, _COMMANDS.c.status == "running")
                .values(
                    interrupt_requested_at=func.coalesce(_COMMANDS.c.interrupt_requested_at, func.now())
                )
                .returning(
                    *_COMMANDS.c,
                    (_COMMANDS.c.expires_at < func.now()).label("lease_expired"),
                    notice(_COMMANDS.c.session_id, "terminal_interrupt").label("notified"),
                )
            )
        )

    # -- reads -------------------------------------------------------------

    async def get_command(self, session_id: str, command_id: str) -> dict[str, Any] | None:
        return _view(
            await fetch_one(
                _command().where(_COMMANDS.c.session_id == session_id, _COMMANDS.c.command_id == command_id)
            )
        )

    async def running_command(self, session_id: str) -> dict[str, Any] | None:
        return _view(
            await fetch_one(
                _command().where(_COMMANDS.c.session_id == session_id, _COMMANDS.c.status == "running")
            )
        )

    async def read_output(self, command_id: str, *, after_seq: int, limit: int = 500) -> list[dict[str, Any]]:
        """A command's output events after ``after_seq``, in order."""
        rows = await fetch_all(
            select(_OUTPUT.c.seq, _OUTPUT.c.payload)
            .where(_OUTPUT.c.command_id == command_id, _OUTPUT.c.seq > after_seq)
            .order_by(_OUTPUT.c.seq)
            .limit(limit)
        )
        return [dict(row) for row in rows]

    async def directory(self, session_id: str) -> str | None:
        """Where the session's terminal is: where its latest command ended, or runs."""
        value = await fetch_value(
            select(func.coalesce(_COMMANDS.c.next_cwd, _COMMANDS.c.cwd))
            .where(_COMMANDS.c.session_id == session_id)
            .order_by(_COMMANDS.c.started_at.desc())
            .limit(1)
        )
        return str(value) if value else None

    async def shell(self, session_id: str) -> str | None:
        """The box process the session's terminal shell runs as, if it has one."""
        value = await fetch_value(
            select(_COMMANDS.c.execution_id)
            .where(_COMMANDS.c.session_id == session_id, _COMMANDS.c.execution_id.isnot(None))
            .order_by(_COMMANDS.c.started_at.desc())
            .limit(1)
        )
        return str(value) if value else None


async def _record(session_id: str, commands: CTE, payloads: Sequence[Mapping[str, Any]]) -> int | None:
    """Append ``payloads`` to the output of each command ``commands`` yields.

    Events are numbered in ``payloads`` order. Returns the last ``seq`` and
    sends one ``terminal`` notice with it; ``None`` and no notice when
    ``commands`` yields nothing.
    """
    events = (
        func.jsonb_array_elements(literal([dict(payload) for payload in payloads], JSONB))
        .table_valued("value", with_ordinality="position")
        .render_derived()
        .alias("events")
    )
    written = (
        insert(_OUTPUT)
        .from_select(
            ["command_id", "payload"],
            select(commands.c.command_id, events.c.value)
            .select_from(commands.join(events, true()))
            .order_by(events.c.position),
        )
        .returning(_OUTPUT.c.seq)
        .cte("written")
    )
    last = func.max(written.c.seq)
    row = await fetch_one(
        select(last.label("seq"), notice(session_id, "terminal", last).label("notified")).having(
            func.count() > 0
        )
    )
    return int(row["seq"]) if row is not None else None
