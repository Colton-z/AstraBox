"""Startup data migrations exercised against the production PostgreSQL backend."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import astrabox.persistence.migrations as migrations
from astrabox.config.settings import get_settings
from astrabox.persistence.repository.backend import create_all, get_async_collection
from astrabox.persistence.repository.postgresql import AsyncCollection, dispose_engines
from tests._postgresql_support import resolve_postgresql_test_url

pytestmark = pytest.mark.postgresql

_MIGRATION_COLLECTIONS = {
    migrations.SCHEMA_META_COLLECTION,
    "environment",
    "sessions",
    "session_snapshots",
    "session_events",
    "template_whitelist",
}


async def _clean_migration_documents(postgres_url: str) -> None:
    for name in _MIGRATION_COLLECTIONS:
        await AsyncCollection(name, postgres_url).delete_many({})


@pytest.fixture(autouse=True)
async def _postgresql_migration_store(monkeypatch: pytest.MonkeyPatch):
    postgres_url = resolve_postgresql_test_url()
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "postgresql")
    monkeypatch.setenv("ASTRABOX_DB_URL", postgres_url)
    get_settings.cache_clear()
    await create_all()
    await _clean_migration_documents(postgres_url)
    yield
    await _clean_migration_documents(postgres_url)
    await dispose_engines(postgres_url)
    get_settings.cache_clear()


async def _schema_doc() -> dict[str, Any] | None:
    collection = await get_async_collection(migrations.SCHEMA_META_COLLECTION)
    return await collection.find_one({"_id": "schema"})


async def _noop(get_collection: migrations.GetCollection) -> None:
    _ = get_collection


async def test_default_migrations_backfill_legacy_postgresql_documents() -> None:
    environments = await get_async_collection("environment")
    await environments.insert_one(
        {
            "_id": "postgresql-legacy-environment",
            "name": "legacy",
            "sandbox_backend": " Direct_Docker ",
        }
    )

    await migrations.run_pending_migrations()

    environment = await environments.find_one({"_id": "postgresql-legacy-environment"})
    assert environment is not None
    assert environment["sandbox_backend"] == "open_sandbox"
    assert environment["idle_action"] == "terminate"
    schema = await _schema_doc()
    assert schema is not None
    assert schema["version"] == migrations.latest_known_version()
    assert schema["locked_by"] is None

    await migrations.run_pending_migrations()
    assert await _schema_doc() == schema


async def test_failed_postgresql_migration_releases_the_startup_lock() -> None:
    await migrations.run_pending_migrations(
        migrations=[migrations.Migration(1, "baseline", _noop)]
    )

    async def _boom(get_collection: migrations.GetCollection) -> None:
        _ = get_collection
        raise RuntimeError("postgresql backfill failed")

    failing = [
        migrations.Migration(1, "baseline", _noop),
        migrations.Migration(2, "fails", _boom),
    ]
    with pytest.raises(RuntimeError, match="postgresql backfill failed"):
        await migrations.run_pending_migrations(migrations=failing)

    schema = await _schema_doc()
    assert schema is not None
    assert schema["version"] == 1
    assert schema["locked_by"] is None


async def test_concurrent_postgresql_boots_have_one_migration_winner() -> None:
    calls = 0
    await migrations.run_pending_migrations(
        migrations=[migrations.Migration(1, "baseline", _noop)]
    )

    async def _slow_apply(get_collection: migrations.GetCollection) -> None:
        nonlocal calls
        _ = get_collection
        calls += 1
        await asyncio.sleep(0.1)

    pending = [
        migrations.Migration(1, "baseline", _noop),
        migrations.Migration(2, "concurrent", _slow_apply),
    ]
    results = await asyncio.gather(
        *(
            migrations.run_pending_migrations(
                migrations=pending,
                lock_poll_interval_seconds=0.01,
                lock_timeout_seconds=5.0,
            )
            for _ in range(8)
        ),
        return_exceptions=True,
    )

    assert [result for result in results if isinstance(result, BaseException)] == []
    assert calls == 1
    schema = await _schema_doc()
    assert schema is not None
    assert schema["version"] == 2
    assert schema["locked_by"] is None
