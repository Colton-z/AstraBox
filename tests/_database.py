"""PostgreSQL databases for tests that read or write AstraBox's store.

``ASTRABOX_TEST_POSTGRES_URL`` names a PostgreSQL server and a role allowed to
create databases. Each pytest process (each xdist worker) creates one database
of its own, brings it to this build's schema with the server's own upgrade, and
drops it at the end; a server a test starts therefore finds the schema current.
The ``database`` fixture points the store at it for one test and empties every
table afterwards, so tests see an empty store and never each other's rows.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
from alembic import command
from sqlalchemy.engine import make_url

import astrabox.persistence.sql.models  # noqa: F401 - registers every table
from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.migrate import alembic_config

_ADMIN_URL_ENV = "ASTRABOX_TEST_POSTGRES_URL"


def _admin_url() -> str:
    url = os.environ.get(_ADMIN_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{_ADMIN_URL_ENV} is required for tests that use the store: a postgresql:// "
            "URL for a role that may create databases, for example a throwaway "
            "postgres:17 container"
        )
    return url


def _url_for(admin_url: str, database: str, driver: str) -> str:
    return (
        make_url(admin_url)
        .set(drivername=driver, database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def postgres_database() -> Iterator[str]:
    """A database of this process's own, at this build's schema; its URL."""
    admin_url = _admin_url()
    name = f"astrabox_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin_conninfo = _url_for(admin_url, make_url(admin_url).database or "postgres", "postgresql")
    with psycopg.connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = _url_for(admin_url, name, "postgresql")
    try:
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def database(postgres_database: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """Point the store at this process's test database for one test; empty it afterwards."""
    from astrabox.config.settings import get_settings
    from astrabox.persistence.sql.engine import dispose_engine

    monkeypatch.setenv("ASTRABOX_DB_URL", postgres_database)
    get_settings.cache_clear()
    try:
        yield postgres_database
    finally:
        await dispose_engine()
        tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        # Off the event loop: a statement still running on it (the runtime
        # error recorder writes each ERROR log from a background task) holds
        # locks the TRUNCATE waits for, and needs the loop to finish.
        await asyncio.to_thread(_truncate, postgres_database, tables)
        get_settings.cache_clear()


def _truncate(url: str, tables: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        if tables:
            conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
