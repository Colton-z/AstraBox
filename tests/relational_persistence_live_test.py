"""The external-entry-point journey on an owned PostgreSQL database, not a candidate DB.

Install tests/fixtures/relational_postgres first. ASTRABOX_TEST_POSTGRES_URL must
name a disposable PostgreSQL server's administrative role. This binding proves
public PostgreSQL only; downstream providers supply their own native observer
and faults to the same inherited journey.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from astrabox.config.settings import get_settings
from astrabox.persistence.provider import get_persistence
from astrabox.testing.relational_persistence_conformance import (
    RelationalContractStore,
    RelationalPersistenceContractSuite,
)

pytestmark = pytest.mark.e2e


class _PostgresObservation:
    def __init__(self, connection: Any, audit: Any, database: str, roles: list[str]) -> None:
        self.connection, self.audit = connection, audit
        self.database, self.roles = database, roles
        self.notices: set[str] = set()
        self.notice_seen = asyncio.Event()

    async def watch(self) -> None:
        async for notice in self.audit.notifies():
            self.notices.add(notice.payload.split(":", 2)[2])
            self.notice_seen.set()

    async def rows(self, statement: Any, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = await self.connection.execute(statement, parameters)
        return list(await cursor.fetchall())

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        async with self.connection.transaction():
            await self.connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            result: dict[str, Any] = {}
            for key, table, order in (
                ("session", "sessions", "session_id"), ("inputs", "session_inputs", "seq"),
                ("output", "session_output", "seq"), ("reader", "session_readers", "session_id"),
                ("responses", "session_responses", "response_id"),
            ):
                result[key] = await self.rows(sql.SQL(
                    "SELECT * FROM {} WHERE session_id=%s ORDER BY {}"
                ).format(sql.Identifier(table), sql.Identifier(order)), (session_id,))
            result["session"] = result["session"][0]
            return result

    async def notice_sessions(self) -> set[str]:
        await asyncio.wait_for(self.notice_seen.wait(), 30)
        return self.notices

    async def _wait(self, predicate: str) -> None:
        async with asyncio.timeout(30):
            while not (await self.rows(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity a "
                "WHERE datname=%s AND usename=%s AND " + predicate + ") AS present",
                (self.database, self.roles[1]),
            ))[0]["present"]:
                await asyncio.sleep(0.05)

    async def subscriber_waiting(self) -> None:
        # The follower has finished its initial log read and is awaiting notices.
        await self._wait("state='idle' AND position('session_output' in query)>0")

    async def runtime_contender_waiting(self) -> None:
        await self._wait("EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid=a.pid "
                         "AND l.locktype='advisory' AND NOT l.granted)")

    async def disconnect_runtime_holder(self) -> None:
        rows = await self.rows(
            "SELECT DISTINCT a.pid FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid "
            "WHERE a.datname=%s AND a.usename=%s AND l.locktype='advisory' AND l.granted",
            (self.database, self.roles[0]),
        )
        assert len(rows) == 1, rows
        assert (await self.rows("SELECT pg_terminate_backend(%s, 5000) AS stopped", (rows[0]["pid"],)))[0]["stopped"]

    @contextlib.asynccontextmanager
    async def listener_gap(self) -> AsyncIterator[None]:
        role = sql.Identifier(self.roles[1])
        try:
            await self.connection.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(role))
            rows = await self.rows(
                "SELECT pid FROM pg_stat_activity WHERE datname=%s AND usename=%s "
                "AND application_name='astrabox-feed'",
                (self.database, self.roles[1]),
            )
            assert len(rows) == 1, rows
            assert (await self.rows("SELECT pg_terminate_backend(%s, 5000) AS stopped", (rows[0]["pid"],)))[0]["stopped"]
            assert (await self.rows("SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", (self.roles[1],)))[0]["rolcanlogin"] is False
            # NOLOGIN prevents reconnect, but keeps the replica's existing SQL connection.
            assert await self.rows(
                "SELECT pid FROM pg_stat_activity WHERE datname=%s AND usename=%s",
                (self.database, self.roles[1]),
            )
            yield
        finally:
            await self.connection.execute(sql.SQL("ALTER ROLE {} LOGIN").format(role))


@pytest.fixture
async def relational_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RelationalContractStore]:
    configured = os.environ.get("ASTRABOX_TEST_POSTGRES_URL", "").strip()
    if not configured:
        raise RuntimeError("ASTRABOX_TEST_POSTGRES_URL must supply a disposable PostgreSQL admin server")
    selected = entry_points(group="astrabox.providers.persistence", name="contract-postgres")
    assert len(selected) == 1, "install tests/fixtures/relational_postgres; no built-in fallback"
    entry_point, = selected
    print(f"external entry point: {entry_point.value} distribution={entry_point.dist}")
    admin_url = make_url(configured).set(drivername="postgresql", database="postgres")
    name = "astrabox_contract_" + uuid.uuid4().hex[:16]
    roles, passwords = [name + "_a", name + "_b"], [secrets.token_urlsafe(24), secrets.token_urlsafe(24)]
    environments = [{
        "ASTRABOX_PERSISTENCE_PROVIDER": "contract-postgres",
        "ASTRABOX_DB_URL": admin_url.set(database=name, username=role, password=password).render_as_string(hide_password=False),
    } for role, password in zip(roles, passwords, strict=True)]
    admin = await psycopg.AsyncConnection.connect(admin_url.render_as_string(hide_password=False), autocommit=True)
    created: list[str] = []
    database_created = False
    observer = audit = watch = store = provider = None
    try:
        for role, password in zip(roles, passwords, strict=True):
            await admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION").format(
                sql.Identifier(role), sql.Literal(password),
            ))
            created.append(role)
        await admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(roles[0])))
        database_created = True
        await admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
        await admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(name), sql.Identifier(roles[1])))
        for key, value in environments[0].items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        get_persistence.cache_clear()
        provider = get_persistence()
        assert type(provider) is entry_point.load(), "the selected external entry point was not used"
        await provider.initialize_schema()
        db_url = admin_url.set(database=name).render_as_string(hide_password=False)
        observer = await psycopg.AsyncConnection.connect(db_url, autocommit=True, row_factory=dict_row)
        await observer.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(roles[1])))
        await observer.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(roles[1])))
        await observer.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(sql.Identifier(roles[1])))
        audit = await psycopg.AsyncConnection.connect(db_url, autocommit=True)
        await audit.execute("LISTEN astrabox_session")
        native = _PostgresObservation(observer, audit, name, roles)
        watch = asyncio.create_task(native.watch())
        store = RelationalContractStore(environments[1], native)
        print(f"owned PostgreSQL database={name} roles={roles}; not the candidate database")
        yield store
        if watch.done():
            store.passed = False
            await watch
            raise AssertionError("the independent native notice observer stopped")
    finally:
        if watch is not None:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        if audit is not None:
            await audit.close()
        if observer is not None:
            await observer.close()
        if provider is not None:
            await provider.dispose()
        get_persistence.cache_clear()
        get_settings.cache_clear()
        if database_created and (store is None or not store.passed):
            receipt = tmp_path / "retained-postgres.private.json"
            receipt.write_text(json.dumps({"database": name, "environments": environments}, indent=2))
            receipt.chmod(0o600)
            print(f"KEPT own PostgreSQL database={name}; private receipt={receipt}")
        else:
            if database_created:
                await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in reversed(created):
                await admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        await admin.close()


class TestSuppliedRelationalPersistence(RelationalPersistenceContractSuite):
    pass
