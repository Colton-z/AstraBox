"""Shared insert contracts and SQLAlchemy expression compilation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger, Column, ColumnElement, DateTime, Index, PrimaryKeyConstraint,
    Table, UniqueConstraint, literal,
)
from sqlalchemy.exc import CompileError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement, now
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ClauseElement

from astrabox.persistence.provider import get_persistence


@compiles(now, "mysql")
@compiles(now, "mariadb")
def _mysql_timestamp(element: now, compiler: SQLCompiler, **kw: Any) -> str:
    """Keep database-generated timestamps at the schema's microsecond precision."""
    return "CURRENT_TIMESTAMP(6)"


class TimestampAfter(FunctionElement[datetime]):
    """A database timestamp plus seconds, rounded once to microseconds."""

    type = DateTime(timezone=True)
    inherit_cache = True

    def __init__(self, base: ColumnElement[datetime], seconds: float) -> None:
        # timedelta uses the same microsecond resolution as stored timestamps.
        offset = timedelta(seconds=seconds)
        microseconds = (offset.days * 86400 + offset.seconds) * 1_000_000 + offset.microseconds
        super().__init__(base, literal(microseconds, type_=BigInteger()))


@compiles(TimestampAfter, "postgresql")
def _postgres_timestamp_after(element: TimestampAfter, compiler: SQLCompiler, **kw: Any) -> str:
    base, offset = element.clauses
    return (
        f"({compiler.process(base, **kw)} + "
        f"{compiler.process(offset, **kw)} * INTERVAL '1 microsecond')"
    )


@compiles(TimestampAfter, "mysql")

@compiles(TimestampAfter, "mariadb")
def _mysql_timestamp_after(element: TimestampAfter, compiler: SQLCompiler, **kw: Any) -> str:
    base, offset = element.clauses
    return f"TIMESTAMPADD(MICROSECOND, {compiler.process(offset, **kw)}, {compiler.process(base, **kw)})"


def _require_primary_key_only(table: Table) -> None:
    if not len(table.primary_key.columns):
        raise CompileError(f"{table.name!r} has no primary-key conflict target")
    if any(isinstance(constraint, UniqueConstraint) for constraint in table.constraints) or any(
        index.unique for index in table.indexes
    ):
        raise CompileError(
            f"cannot target only the primary key of {table.name!r} "
            "because the table has another unique constraint or index"
        )


def _unique_targets(
    table: Table, index_columns: Sequence[str]
) -> list[PrimaryKeyConstraint | UniqueConstraint | Index]:
    """Plain unconditional unique keys matching the caller's exact target."""
    columns = frozenset(index_columns)
    if not columns or len(columns) != len(index_columns):
        raise CompileError("insert-if-absent requires distinct conflict target columns")
    for name in columns:
        column = table.c[name]
        if column.computed is not None:
            raise CompileError("insert-if-absent does not target generated columns")
    candidates: list[PrimaryKeyConstraint | UniqueConstraint | Index] = [table.primary_key, *(
        constraint for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
    ), *(index for index in table.indexes if index.unique)]
    targets: list[PrimaryKeyConstraint | UniqueConstraint | Index] = []
    for key in candidates:
        if frozenset(column.name for column in key.columns) != columns:
            continue
        if isinstance(key, Index) and not all(isinstance(value, Column) for value in key.expressions):
            continue
        if any(
            (name.endswith("_where") or name.endswith("_nulls_not_distinct")
             or name in {"mysql_length", "mariadb_length"}) and value is not None
            and value is not False
            for name, value in key.dialect_kwargs.items()
        ):
            continue
        targets.append(key)
    if not targets:
        raise CompileError("insert-if-absent requires a plain unconditional unique target")
    return targets


def upsert_primary_key(
    connection: AsyncConnection,
    table: Table,
    rows: dict[str, Any] | list[dict[str, Any]],
    *,
    update_columns: Sequence[str],
    update_values: Mapping[str, Any] | None = None,
) -> Insert:
    """Construct a PK-targeted upsert through the selected physical capability.

    Proposed insert values supply update_columns; update_values are ordinary
    SQLAlchemy values/expressions. The caller executes on its own transaction.
    """
    if not len(table.primary_key.columns):
        raise CompileError(f"{table.name!r} has no primary-key conflict target")
    return get_persistence().statements.upsert_primary_key(
        connection, table, rows, update_columns=update_columns, update_values=update_values
    )


def insert_if_absent_primary_key(
    connection: AsyncConnection,
    table: Table,
    rows: dict[str, Any] | list[dict[str, Any]],
) -> Insert:
    """Ensure PK-only rows exist, without an inserted/existing result contract.

    Execute on the caller's transaction before its locking read. Competing
    unique keys are forbidden; rowcount is not an ownership/admission signal.
    """
    _require_primary_key_only(table)
    return get_persistence().statements.insert_if_absent_primary_key(connection, table, rows)


async def insert_if_absent(
    connection: AsyncConnection,
    table: Table,
    values: dict[str, Any],
    *,
    index_columns: Sequence[str],
    where: ColumnElement[bool] | None = None,
) -> bool:
    """Insert once under the exact target and caller-supplied eligibility.

    False means no eligible row or the exact expected unique conflict. The
    capability preserves all other violations, native transaction ownership,
    NULL-distinct semantics and concrete destination-typed guarded values.
    Duplicate readback does not confer an exclusive mutation guard.
    """
    if not connection.in_transaction():
        raise RuntimeError("insert-if-absent requires the caller's transaction")
    _unique_targets(table, index_columns)
    for name in index_columns:
        if name not in values or isinstance(values[name], ClauseElement):
            raise CompileError("insert-if-absent requires concrete values for its target columns")
    return await get_persistence().statements.insert_if_absent(
        connection, table, values, index_columns=index_columns, where=where
    )
