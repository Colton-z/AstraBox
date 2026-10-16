"""SQLAlchemy DDL differences for the shared relational schema."""

from __future__ import annotations

from copy import copy
from typing import Any

from sqlalchemy import CheckConstraint, Index, Text, column, func
from sqlalchemy.exc import CompileError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ColumnCollectionConstraint, CreateColumn, ForeignKeyConstraint
from sqlalchemy.sql.elements import ColumnElement, TextClause
from sqlalchemy.sql.functions import FunctionElement

MYSQL_TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_row_format": "DYNAMIC",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_bin",
}


@compiles(ForeignKeyConstraint, "mysql")
def _mysql_foreign_key(element: ForeignKeyConstraint, compiler: Any, **kw: Any) -> str:
    if any(reference.info.get("mysql_restrict_environment") for reference in element.elements):
        # Shared deletion clears tombstones; CHECK forbids clearing live references.
        # Copy so compiling MySQL cannot change PostgreSQL's SET NULL metadata.
        element = copy(element)
        element.ondelete = "RESTRICT"
    return compiler.visit_foreign_key_constraint(element, **kw)


class GeneratedOrderIndex(ColumnCollectionConstraint):
    """MySQL's required inline index for a non-primary generated sequence."""


@compiles(GeneratedOrderIndex, "mysql")
def _generated_order_index(element: GeneratedOrderIndex, compiler: Any, **kw: Any) -> str:
    name = compiler.preparer.format_constraint(element)
    columns = ", ".join(compiler.preparer.quote(col.name) for col in element.columns)
    return f"INDEX {name} ({columns})"


@compiles(CreateColumn, "mysql")
def _mysql_column(element: CreateColumn, compiler: Any, **kw: Any) -> str | None:
    col = element.element
    result = compiler.visit_create_column(element, **kw)
    if col.identity is not None and not col.primary_key:
        if not any(
            isinstance(constraint, GeneratedOrderIndex) and col.name in constraint.columns
            for constraint in col.table.constraints
        ):
            raise CompileError(f"{col.table.name}.{col.name} requires an inline generated-order index")
        if result is not None:
            result += " AUTO_INCREMENT"
    return result


class EmptyListDefault(FunctionElement[Any]):
    """An empty native list, retaining the existing PG array/JSON literal."""

    inherit_cache = False

    def __init__(self, *, text_array: bool = False) -> None:
        self.text_array = text_array
        super().__init__()


@compiles(EmptyListDefault, "postgresql")
def _postgres_empty_list(element: EmptyListDefault, compiler: Any, **kw: Any) -> str:
    return "'{}'" if element.text_array else "'[]'"


@compiles(EmptyListDefault, "mysql")
def _mysql_empty_list(element: EmptyListDefault, compiler: Any, **kw: Any) -> str:
    return "(JSON_ARRAY())"


def string_list_check(name: str) -> CheckConstraint:
    """Enforce the native text-array element domain in MySQL's JSON storage."""
    schema = '{"type":"array","items":{"type":["string","null"]}}'
    return CheckConstraint(
        func.json_schema_valid(schema, column(name)), name=f"{name}_strings",
    ).ddl_if(dialect="mysql")


class NullableUnique(ColumnCollectionConstraint):
    """MySQL predicate uniqueness, including its physical marker in CREATE TABLE.

    The marker is absent from the shared Table's projections. The generated
    column and unique key are created together, without a later ALTER step.
    """

    def __init__(
        self, *columns: str, predicate: ColumnElement[bool] | TextClause, marker: str, name: str,
    ) -> None:
        self.predicate = predicate
        self.marker = marker
        super().__init__(*columns, name=name)


@compiles(NullableUnique, "mysql")
def _nullable_unique(element: NullableUnique, compiler: Any, **kw: Any) -> str:
    marker = compiler.preparer.quote(element.marker)
    name = compiler.preparer.format_constraint(element)
    predicate = compiler.sql_compiler.process(element.predicate, literal_binds=True)
    keys = ", ".join(compiler.preparer.quote(col.name) for col in element.columns)
    return (
        f"{marker} SMALLINT GENERATED ALWAYS AS (CASE WHEN {predicate} THEN 1 ELSE NULL END) STORED, "
        f"CONSTRAINT {name} UNIQUE ({keys}, {marker})"
    )


def conditional_unique(
    name: str, *columns: str, predicate: ColumnElement[bool] | TextClause, marker: str,
) -> tuple[Index, NullableUnique]:
    """One logical conditional key with each dialect's atomic enforcement."""
    return (
        Index(name, *columns, unique=True, postgresql_where=predicate).ddl_if(dialect="postgresql"),
        NullableUnique(*columns, predicate=predicate, marker=marker, name=name).ddl_if(dialect="mysql"),
    )


def boolean_checks(*names: str) -> tuple[CheckConstraint, ...]:
    """Constrain MySQL's integer-backed booleans to the native boolean domain."""
    return tuple(
        CheckConstraint(column(name).in_((0, 1)), name=f"{name}_boolean").ddl_if(dialect="mysql")
        for name in names
    )


class EmptyObjectDefault(FunctionElement[Any]):
    """A native empty JSON object, retaining its declared PostgreSQL literal."""

    inherit_cache = False

    def __init__(self, *, postgres_sql: str = "'{}'") -> None:
        self.postgres_sql = postgres_sql
        super().__init__()


@compiles(EmptyObjectDefault, "postgresql")
def _postgres_empty_object(element: EmptyObjectDefault, compiler: Any, **kw: Any) -> str:
    return element.postgres_sql


@compiles(EmptyObjectDefault, "mysql")
def _mysql_empty_object(element: EmptyObjectDefault, compiler: Any, **kw: Any) -> str:
    return "(JSON_OBJECT())"


class TextDefault(FunctionElement[str]):
    """A constant text default, parenthesized for MySQL's large-text columns."""

    inherit_cache = False

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__()


@compiles(TextDefault, "postgresql")
def _postgres_text_default(element: TextDefault, compiler: Any, **kw: Any) -> str:
    return compiler.render_literal_value(element.value, Text())


@compiles(TextDefault, "mysql")
def _mysql_text_default(element: TextDefault, compiler: Any, **kw: Any) -> str:
    return f"({compiler.render_literal_value(element.value, Text())})"


class NullCount(FunctionElement[int]):
    """Count null or non-null fields in a row's completeness constraint."""

    inherit_cache = False

    def __init__(self, *names: str, nonnull: bool = False) -> None:
        self.nonnull = nonnull
        super().__init__(*(column(name) for name in names))


@compiles(NullCount, "postgresql")
def _postgres_null_count(element: NullCount, compiler: Any, **kw: Any) -> str:
    name = "num_nonnulls" if element.nonnull else "num_nulls"
    return f"{name}({compiler.process(element.clauses, **kw)})"


@compiles(NullCount, "mysql")
def _mysql_null_count(element: NullCount, compiler: Any, **kw: Any) -> str:
    condition = "IS NOT NULL" if element.nonnull else "IS NULL"
    return "(" + " + ".join(
        f"({compiler.process(field, **kw)} {condition})" for field in element.clauses
    ) + ")"
