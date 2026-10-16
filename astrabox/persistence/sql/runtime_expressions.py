"""Native SQL expressions shared by Session transaction recipes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import ARRAY, JSON, Boolean, Text, cast, literal
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.functions import FunctionElement


_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def json_literal(value: Any) -> ColumnElement[Any]:
    """A bound JSON document, parsed as JSON for structural SQL comparisons."""
    return cast(literal(value, type_=_DOCUMENT), _DOCUMENT)


class ordered_values(FunctionElement[list[str]]):
    """Ordered values for a scalar aggregate subquery limited to one row."""

    type = JSON().with_variant(ARRAY(Text), "postgresql")
    inherit_cache = True


@compiles(ordered_values, "postgresql")
def _pg_values(element: Any, compiler: Any, **kw: Any) -> str:
    value, position = element.clauses
    return f"array_agg({compiler.process(value, **kw)} ORDER BY {compiler.process(position, **kw)})"


@compiles(ordered_values, "mysql")
def _mysql_values(element: Any, compiler: Any, **kw: Any) -> str:
    value, position = element.clauses
    return (
        f"JSON_ARRAYAGG(CAST({compiler.process(value, **kw)} AS CHAR CHARACTER SET utf8mb4)) OVER "
        f"(ORDER BY {compiler.process(position, **kw)} "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"
    )


class json_array_matches(FunctionElement[list[bool]]):
    """Native full-value comparisons of a nonempty array, in element order."""

    type = JSON().with_variant(ARRAY(Boolean), "postgresql")
    inherit_cache = True


@compiles(json_array_matches, "postgresql")
def _pg_matches(element: Any, compiler: Any, **kw: Any) -> str:
    values, record = element.clauses
    return (
        f"(SELECT array_agg((_ab_items.value = {compiler.process(record, **kw)}) "
        "ORDER BY _ab_items.item_ordinal) "
        f"FROM jsonb_array_elements({compiler.process(values, **kw)}) WITH ORDINALITY "
        "AS _ab_items(value, item_ordinal))"
    )


@compiles(json_array_matches, "mysql")
def _mysql_matches(element: Any, compiler: Any, **kw: Any) -> str:
    values, record = element.clauses
    array = compiler.process(values, **kw)
    expected = compiler.process(record, **kw)
    return (
        f"(SELECT JSON_ARRAYAGG((JSON_EXTRACT({array}, "
        f"CONCAT('$[', _ab_items.item_ordinal - 1, ']')) = {expected})) "
        "OVER (ORDER BY _ab_items.item_ordinal "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) "
        f"FROM JSON_TABLE({compiler.process(values, **kw)}, '$[*]' "
        "COLUMNS (item_ordinal FOR ORDINALITY)) "
        "AS _ab_items LIMIT 1)"
    )


class json_without_key(FunctionElement[Any]):
    """A JSON object with only the named top-level key removed."""

    type = _DOCUMENT
    inherit_cache = True


@compiles(json_without_key, "postgresql")
def _pg_without_key(element: Any, compiler: Any, **kw: Any) -> str:
    document, key = element.clauses
    return f"({compiler.process(document, **kw)} - {compiler.process(key, **kw)})"


@compiles(json_without_key, "mysql")
def _mysql_without_key(element: Any, compiler: Any, **kw: Any) -> str:
    document, key = element.clauses
    return (
        f"JSON_REMOVE({compiler.process(document, **kw)}, "
        f"CONCAT('$.', JSON_QUOTE({compiler.process(key, **kw)})))"
    )


class json_array_items(FunctionElement[Any]):
    """JSON array elements as value/ordinality columns, without coercing objects."""

    inherit_cache = True


@compiles(json_array_items, "postgresql")
def _pg_array_items(element: Any, compiler: Any, **kw: Any) -> str:
    array, = element.clauses
    return f"jsonb_array_elements({compiler.process(array, **kw)}) WITH ORDINALITY"


@compiles(json_array_items, "mysql")
def _mysql_array_items(element: Any, compiler: Any, **kw: Any) -> str:
    array, = element.clauses
    return (
        f"JSON_TABLE({compiler.process(array, **kw)}, '$[*]' "
        "COLUMNS (value JSON PATH '$' ERROR ON ERROR, ordinality FOR ORDINALITY))"
    )
