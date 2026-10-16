"""Declarative base, naming conventions and value conversions for every table.

Tables are declared with SQLAlchemy's typed ORM mapping and queried with Core
statements. Columns are typed: identifiers are text, times are
``timestamptz``, and only payloads the platform stores without interpreting are
JSONB.

Repositories hand services ISO-8601 strings for times, the representation the
services compare and serialize. :func:`to_iso` and :func:`from_iso` are the only
conversions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase

#: Constraint names are derived from the table and columns, so a migration can
#: name every constraint it alters.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base of every AstraBox table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # ``None`` is stored as SQL NULL, not the JSON ``null`` a JSONB column
    # stores by default: both read back as ``None``, but only SQL NULL matches
    # ``IS NULL``, so a compare-and-set expecting "no value" fails on JSON null.
    type_annotation_map = {
        datetime: TIMESTAMP(timezone=True),
        dict[str, Any]: JSONB(none_as_null=True),
        list[Any]: JSONB(none_as_null=True),
    }


def to_iso(value: datetime | None) -> str | None:
    """A stored time as the ISO-8601 UTC string services use."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | datetime | None) -> datetime | None:
    """A service time (ISO-8601 string or datetime) as a timezone-aware datetime.

    A naive value is taken as UTC, the zone every service time is written in.
    """
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
