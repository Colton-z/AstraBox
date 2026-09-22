"""The production PostgreSQL backend, held to the collection contract."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import anyio
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from astrabox.persistence.repository._compat import DuplicateKeyError
from astrabox.persistence.repository.postgresql import (
    AsyncCollection,
    create_all,
    dispose_engines,
    get_engine,
)
from astrabox.testing.collection_conformance import CollectionContractSuite
from tests._postgresql_support import resolve_postgresql_test_url

pytestmark = pytest.mark.postgresql


class TestPostgresqlCollectionContract(CollectionContractSuite):
    postgres_url = ""

    @pytest.fixture(autouse=True)
    async def _fresh_pool_per_test(self):
        self.postgres_url = resolve_postgresql_test_url()
        await create_all(self.postgres_url)
        yield
        await dispose_engines(self.postgres_url)

    async def make_collection(self, name: str) -> AsyncCollection:
        collection = AsyncCollection(name, self.postgres_url)
        await collection.delete_many({})
        return collection

    async def test_documents_are_stored_as_jsonb(self) -> None:
        engine = get_engine(self.postgres_url)
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'astrabox_documents' "
                    "AND column_name IN ('doc', 'seq')"
                )
            )
            column_types = {row.column_name: row.data_type for row in result}
        assert column_types == {"doc": "jsonb", "seq": "bigint"}

    async def test_partial_string_unique_index_is_enforced_by_postgresql(self) -> None:
        collection = await self.make_collection("conf_partial_unique")
        await collection.create_index(
            [("scope", 1), ("uuid", 1)],
            unique=True,
            name="ux_conf_partial_unique",
            partialFilterExpression={"uuid": {"$type": "string"}},
        )
        await collection.insert_one({"_id": "number-a", "scope": "s", "uuid": 7})
        await collection.insert_one({"_id": "number-b", "scope": "s", "uuid": 7})
        await collection.insert_one({"_id": "string-a", "scope": "s", "uuid": "u"})
        with pytest.raises(DuplicateKeyError):
            await collection.insert_one({"_id": "string-b", "scope": "s", "uuid": "u"})

    async def test_anyio_cancelled_read_returns_its_connection(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        collection = await self.make_collection("conf_cancelled_read")
        await collection.insert_one({"_id": "cancel-safe-read", "value": 1})
        engine = get_engine(self.postgres_url)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_execute = AsyncSession.execute

        async def gated_execute(session, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            result = await original_execute(session, *args, **kwargs)
            entered.set()
            await release.wait()
            return result

        caplog.set_level(logging.ERROR, logger="sqlalchemy.pool")
        with patch.object(AsyncSession, "execute", gated_execute):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(
                    collection.find_one, {"_id": "cancel-safe-read"}
                )
                await entered.wait()
                assert engine.pool.checkedout() == 1
                task_group.cancel_scope.cancel()

            # The request task is gone, but the read still owns its connection
            # until the real query/session boundary has completed.
            assert engine.pool.checkedout() == 1
            release.set()
            for _ in range(100):
                if engine.pool.checkedout() == 0:
                    break
                await asyncio.sleep(0.01)

        assert engine.pool.checkedout() == 0
        assert "Exception terminating connection" not in caplog.text
        assert "garbage collector is trying to clean up" not in caplog.text
        assert await collection.find_one({"_id": "cancel-safe-read"}) == {
            "_id": "cancel-safe-read",
            "value": 1,
        }
