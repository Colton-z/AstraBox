"""Alembic environment: run revisions against the configured PostgreSQL database.

Revisions run in one transaction that first takes a transaction-level advisory
lock, so replicas starting together apply each revision once.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import astrabox.persistence.sql.models  # noqa: F401 - registers every table on the metadata
from astrabox.persistence.sql.base import Base

#: Advisory-lock key for schema upgrades: hashtext('astrabox.schema') is not
#: used so the key is a fixed literal any operator can recognise in pg_locks.
SCHEMA_LOCK_KEY = 0x41535452  # "ASTR"

target_metadata = Base.metadata
environment_name = context.config.get_main_option("astrabox.environment_name")
if not environment_name:
    raise RuntimeError("schema initialization requires the configured Environment name")


def _run_migrations(connection: Connection) -> None:
    # The lock is released when this transaction commits or rolls back, not
    # before: a replica waiting on it then reads the revision this one
    # committed, and a replica that dies mid-upgrade releases it with its
    # connection.
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
    connection.execute(text("SET LOCAL search_path TO public"))
    bound = connection.scalar(text(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'environments' "
        "AND column_name = 'singleton')"
    ))
    if bound:
        owner = connection.scalar(text("SELECT name FROM public.environments WHERE singleton"))
        if owner is not None and owner != environment_name:
            raise RuntimeError(
                f"database belongs to Environment {owner!r}, not {environment_name!r}; "
                "use this Environment's separate database and credentials"
            )
    context.configure(
        connection=connection, target_metadata=target_metadata, version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    engine = create_async_engine(str(url))
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=context.config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
