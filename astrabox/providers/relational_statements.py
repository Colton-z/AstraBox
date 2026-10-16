"""Native conflict operations reusable by relational persistence providers.

These physical components construct or execute SQLAlchemy statements only.
Platform repositories retain eligibility, state transitions and transactions.
The MySQL component is not a complete persistence provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement, Table, insert, literal, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import CompileError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.dml import Insert

from astrabox.persistence.errors import UniqueViolation
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.sql.statements import _require_primary_key_only, _unique_targets


class PostgresStatements:
    """PostgreSQL's native targeted conflict handling and RETURNING."""

    def upsert_primary_key(
        self, connection: AsyncConnection, table: Table,
        rows: dict[str, Any] | list[dict[str, Any]], *,
        update_columns: Sequence[str], update_values: Mapping[str, Any] | None = None,
    ) -> Insert:
        statement = postgres_insert(table).values(rows)
        updates = {name: statement.excluded[name] for name in update_columns}
        updates.update(update_values or {})
        return statement.on_conflict_do_update(
            index_elements=list(table.primary_key.columns), set_=updates
        )

    def insert_if_absent_primary_key(
        self, connection: AsyncConnection, table: Table,
        rows: dict[str, Any] | list[dict[str, Any]],
    ) -> Insert:
        return postgres_insert(table).values(rows).on_conflict_do_nothing(
            index_elements=list(table.primary_key.columns)
        )

    async def insert_if_absent(
        self, connection: AsyncConnection, table: Table, values: dict[str, Any], *,
        index_columns: Sequence[str], where: ColumnElement[bool] | None = None,
    ) -> bool:
        candidate = postgres_insert(table)
        if where is None:
            candidate = candidate.values(values)
        else:
            candidate = candidate.from_select(
                list(values),
                select(*(
                    literal(value, type_=table.c[name].type) for name, value in values.items()
                )).where(where),
            )
        result = await connection.execute(
            candidate.on_conflict_do_nothing(index_elements=[table.c[name] for name in index_columns])
            .returning(literal(True))
        )
        return result.scalar_one_or_none() is True


class MySQLStatements:
    """MySQL conflict syntax and exact constraint-specific duplicate handling.

    Duplicate INSERT confirmation uses a shared current read; it is not an
    exclusive mutation guard. PK-only ensure instead uses native no-op upsert.
    Unknown/different integrity violations always propagate.
    """

    def upsert_primary_key(
        self, connection: AsyncConnection, table: Table,
        rows: dict[str, Any] | list[dict[str, Any]], *,
        update_columns: Sequence[str], update_values: Mapping[str, Any] | None = None,
    ) -> Insert:
        _require_primary_key_only(table)
        proposed = mysql_insert(table).values(rows)
        updates = {name: proposed.inserted[name] for name in update_columns}
        updates.update(update_values or {})
        return proposed.on_duplicate_key_update(updates)

    def insert_if_absent_primary_key(
        self, connection: AsyncConnection, table: Table,
        rows: dict[str, Any] | list[dict[str, Any]],
    ) -> Insert:
        _require_primary_key_only(table)
        first = next(iter(table.primary_key.columns))
        return mysql_insert(table).values(rows).on_duplicate_key_update({first.name: first})

    async def insert_if_absent(
        self, connection: AsyncConnection, table: Table, values: dict[str, Any], *,
        index_columns: Sequence[str], where: ColumnElement[bool] | None = None,
    ) -> bool:
        expected_names = _mysql_conflict_names(connection, table, index_columns)
        try:
            async with connection.begin_nested():
                insertion = insert(table)
                if where is None:
                    insertion = insertion.values(values)
                else:
                    insertion = insertion.from_select(
                        list(values),
                        select(*(
                            literal(value, type_=table.c[name].type) for name, value in values.items()
                        )).where(where),
                    )
                result = await connection.execute(insertion)
                if result.rowcount not in {0, 1} or (where is None and result.rowcount != 1):
                    raise RuntimeError("insert-if-absent requires a known single-row insertion count")
                if result.rowcount == 0:
                    return False
        except IntegrityError as error:
            classified = get_persistence().translate_error(error)
            if not isinstance(classified, UniqueViolation) or classified.constraint not in expected_names:
                raise
            if any(values[name] is None for name in index_columns):
                raise
            existing = await connection.execute(
                select(literal(True)).select_from(table)
                .where(*(table.c[name] == values[name] for name in index_columns))
                .with_for_update(read=True)
            )
            if existing.scalar_one_or_none() is None:
                raise
            return False
        return True


def _mysql_conflict_names(
    connection: AsyncConnection, table: Table, index_columns: Sequence[str]
) -> set[str]:
    names: set[str] = set()
    for key in _unique_targets(table, index_columns):
        if key is table.primary_key:
            native = "PRIMARY"
        else:
            rendered = connection.dialect.identifier_preparer.format_constraint(key)
            if rendered is None:
                continue
            native_parts = connection.dialect.identifier_preparer.unformat_identifiers(rendered)
            if len(native_parts) != 1:
                continue
            native = native_parts[0]
        names.update({native, f"{table.name}.{native}"})
        schema = table.schema or connection.dialect.default_schema_name
        if schema:
            names.add(f"{schema}.{table.name}.{native}")
    if not names:
        raise CompileError("insert-if-absent requires a named unique target")
    return names
