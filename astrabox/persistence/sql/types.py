"""Relational value types with unchanged PostgreSQL representations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, LargeBinary, Table, Text
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.type_api import TypeEngine
from sqlalchemy.types import TypeDecorator


class KeyText(TypeDecorator[str]):
    """Text on PostgreSQL; exact UTF-8 bytes with a physical MySQL key limit.

    The limit is in bytes, not characters. PostgreSQL values are neither
    limited nor transformed. MySQL values are checked before binding, never
    truncated, padded, case-folded or substituted with a digest.
    """

    impl = Text
    cache_ok = True

    @property
    def python_type(self) -> type[str]:
        return str

    def __init__(self, max_bytes: int, *, postgres_collation: str | None = None) -> None:
        if max_bytes < 1:
            raise ValueError("key byte limit must be positive")
        self.max_bytes = max_bytes
        self.postgres_collation = postgres_collation
        super().__init__(collation=postgres_collation)

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.VARBINARY(self.max_bytes))
        return dialect.type_descriptor(self.impl)

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | bytes | None:
        if value is None or dialect.name != "mysql":
            return value
        if not isinstance(value, str):
            raise TypeError("relational keys must be strings")
        encoded = value.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError(
                f"MySQL key exceeds {self.max_bytes} UTF-8 bytes "
                f"({len(encoded)} bytes); refusing without truncation"
            )
        return encoded

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if value is None or dialect.name != "mysql":
            return value
        return bytes(value).decode("utf-8")


def validate_backend_keys(
    table: Table, rows: Iterable[Mapping[str, Any]], dialect: Dialect,
) -> None:
    """Validate a batch's concrete key bindings before its first write.

    Rows contain concrete values, not SQL expressions. Typed SQLAlchemy binds
    enforce the same check individually; INSERT FROM SELECT literals must use
    the destination type or validate their concrete values here first. The
    selected dialect's adapted bind processor owns the validation, including
    any provider-specific representation of these shared key types.
    """
    keys = [(column.key, column.type) for column in table.c if isinstance(column.type, KeyText)]
    for row in rows:
        for key, value_type in keys:
            if key in row:
                try:
                    processor = value_type.dialect_impl(dialect).bind_processor(dialect)
                    if processor is not None:
                        processor(row[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{table.name}.{key}: {exc}") from exc


class UTCDateTime(TypeDecorator[datetime]):
    """PG timestamptz, or microsecond MySQL DATETIME containing UTC."""

    impl = postgresql.TIMESTAMP(timezone=True)
    cache_ok = True

    @property
    def python_type(self) -> type[datetime]:
        return datetime

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(self.impl)

    def coerce_compared_value(self, op: Any, value: Any) -> TypeEngine[Any]:
        if isinstance(value, datetime) or value is None:
            return self
        return self.impl.coerce_compared_value(op, value)

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None or dialect.name != "mysql":
            return value
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None or dialect.name != "mysql":
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class StringList(TypeDecorator[list[str | None]]):
    """Ordered PG text array, or MySQL JSON array; SQL NULL remains absent.

    NULL elements in existing arrays round-trip as JSON null elements. The
    service's stricter string-list input validation remains its own boundary.
    """

    impl = postgresql.ARRAY(Text)
    cache_ok = True

    @property
    def python_type(self) -> type[list[str | None]]:
        return list

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(JSON(none_as_null=True))
        return dialect.type_descriptor(self.impl)

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is not None and dialect.name == "mysql":
            if not isinstance(value, list) or any(item is not None and not isinstance(item, str) for item in value):
                raise TypeError("string-list storage requires an ordered list of strings or NULL elements")
        return value


def payload_json() -> postgresql.JSONB:
    """Native JSON, with Python None stored as SQL NULL on both dialects."""
    return postgresql.JSONB(none_as_null=True).with_variant(JSON(none_as_null=True), "mysql")


def payload_text() -> Text:
    """Unindexed text without MySQL TEXT's 64 KiB payload ceiling."""
    return Text().with_variant(mysql.LONGTEXT(), "mysql")


def binary_payload() -> LargeBinary:
    """Opaque bytes, including native archive chunks larger than MySQL BLOB."""
    return LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
