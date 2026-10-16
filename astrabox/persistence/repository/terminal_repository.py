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
through the selected persistence coordination. Rows are returned as dicts with
ISO-8601 times.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ColumnElement, column, delete, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Select

from astrabox.persistence.sql import UniqueViolation, execute, fetch_all, fetch_one, fetch_value, to_iso, transaction
from astrabox.persistence.sql.models.sessions import TerminalCommand, TerminalOutput
from astrabox.persistence.sql.runtime_expressions import json_array_items, json_literal
from astrabox.persistence.sql.statements import TimestampAfter
from astrabox.persistence.provider import get_persistence
from astrabox.seams.persistence_coordination import SessionNotice

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
    return TimestampAfter(func.now(), lease_s)


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
        """Record a running command and remove previous commands atomically.

        Inherit the latest eligible directory and exited shell. A lost runner
        does not establish an idle shell. TerminalBusy rolls back all cleanup.
        """
        async def write(conn: AsyncConnection) -> dict[str, Any]:
            previous = (await conn.execute(
                select(_COMMANDS)
                .where(
                    _COMMANDS.c.session_id == session_id,
                    or_(_COMMANDS.c.status != "running", _COMMANDS.c.expires_at < func.now()),
                )
                .order_by(_COMMANDS.c.started_at.desc())
                .with_for_update()
            )).mappings().all()
            shell = next((row["execution_id"] for row in previous
                          if row["execution_id"] is not None and row["status"] == "exited"), None)
            directory = None
            if previous:
                latest = previous[0]
                directory = latest["next_cwd"] if latest["next_cwd"] is not None else latest["cwd"]
                await conn.execute(delete(_COMMANDS).where(
                    _COMMANDS.c.command_id.in_([row["command_id"] for row in previous])
                ))
            inherited = directory if directory is not None else default_cwd
            await conn.execute(insert(_COMMANDS).values(
                command_id=command_id, session_id=session_id, command=command,
                cwd=cwd if cwd is not None else inherited, status="running",
                execution_id=shell, expires_at=_lease(lease_s),
            ))
            row = (await conn.execute(
                select(_COMMANDS).where(_COMMANDS.c.command_id == command_id)
            )).mappings().one()
            await get_persistence().coordination.notify(
                conn, SessionNotice(session_id=session_id, kind="terminal", seq=0),
            )
            return dict(row)

        try:
            row = await transaction(write)
        except UniqueViolation as exc:
            if (exc.constraint or "").rsplit(".", 1)[-1] == _RUNNING_INDEX:
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
        transaction, so an end written concurrently comes after this output.
        """
        async def write(conn: AsyncConnection) -> int | None:
            running = (await conn.execute(
                select(_COMMANDS.c.command_id)
                .where(_COMMANDS.c.command_id == command_id, _COMMANDS.c.status == "running")
                .with_for_update(read=True)
            )).first()
            if running is None:
                return None
            return await _record(conn, session_id, command_id, payloads)

        return await transaction(write)

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
        """End a running command and record its last events atomically.

        Return the last written seq, or None if already ended or no events.
        """
        async def write(conn: AsyncConnection) -> int | None:
            ended = await conn.execute(
                update(_COMMANDS)
                .where(_COMMANDS.c.command_id == command_id, _COMMANDS.c.status == "running")
                .values(status=status, exit_code=exit_code, next_cwd=next_cwd, ended_at=func.now())
            )
            if ended.rowcount == 0:
                return None
            return await _record(conn, session_id, command_id, payloads)

        return await transaction(write)

    async def end_lost(self, session_id: str, *, payloads: Sequence[Mapping[str, Any]]) -> int | None:
        """End the session's running command if its runner's lease has passed.

        Records ``payloads`` as the command's last events and marks it
        ``failed``; returns the last ``seq`` written, or ``None`` when no
        command was lost.
        """
        async def write(conn: AsyncConnection) -> int | None:
            conditions = [
                _COMMANDS.c.session_id == session_id,
                _COMMANDS.c.status == "running",
                _COMMANDS.c.expires_at < func.now(),
            ]
            command_id = (await conn.execute(
                select(_COMMANDS.c.command_id).where(*conditions).with_for_update()
            )).scalar_one_or_none()
            if command_id is None:
                return None
            ended = await conn.execute(
                update(_COMMANDS).where(*conditions, _COMMANDS.c.command_id == command_id)
                .values(status="failed", ended_at=func.now())
            )
            if ended.rowcount == 0:
                return None
            return await _record(conn, session_id, str(command_id), payloads)

        return await transaction(write)

    async def request_interrupt(self, session_id: str) -> dict[str, Any] | None:
        """Ask the session's running command to stop; that command, or ``None``.

        The request stays on the row until the command ends, so a runner that
        missed the ``terminal_interrupt`` notice finds it when it reads its row.
        """
        statement = (
            update(_COMMANDS)
            .where(_COMMANDS.c.session_id == session_id, _COMMANDS.c.status == "running")
            .values(
                interrupt_requested_at=func.coalesce(_COMMANDS.c.interrupt_requested_at, func.now())
            )
        )

        async def write(conn: AsyncConnection) -> dict[str, Any] | None:
            if (await conn.execute(statement)).rowcount == 0:
                return None
            row = (await conn.execute(
                _command().where(_COMMANDS.c.session_id == session_id, _COMMANDS.c.status == "running")
            )).mappings().one()
            await get_persistence().coordination.notify(
                conn, SessionNotice(session_id=session_id, kind="terminal_interrupt", seq=0),
            )
            return dict(row)

        return _view(await transaction(write))

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


async def _record(
    conn: AsyncConnection, session_id: str, command_id: str, payloads: Sequence[Mapping[str, Any]],
) -> int | None:
    """Write ordered output under the caller's command lock and transaction.

    The singleton tail yields its own generated key. No other writer's output
    can be mistaken for this batch by a max(seq) read or identity arithmetic.
    """
    if not payloads:
        return None
    if len(payloads) > 1:
        events = json_array_items(json_literal([dict(payload) for payload in payloads[:-1]])).table_valued(
            column("value", _OUTPUT.c.payload.type), column("ordinality", BigInteger),
        )
        await conn.execute(insert(_OUTPUT).from_select(
            ["command_id", "payload"],
            select(literal(command_id, type_=_OUTPUT.c.command_id.type), events.c.value)
            .select_from(events).order_by(events.c.ordinality),
        ))
    written = await conn.execute(insert(_OUTPUT).values(
        command_id=command_id, payload=dict(payloads[-1]),
    ))
    identity = written.inserted_primary_key[0]
    if identity is None:
        raise RuntimeError("persistence driver did not return the terminal output identity")
    seq = int(identity)
    await get_persistence().coordination.notify(
        conn, SessionNotice(session_id=session_id, kind="terminal", seq=seq),
    )
    return seq
