"""The default SQLite backend, held to the collection contract.

Binds :class:`astrabox.testing.collection_conformance.CollectionContractSuite`
(the reusable suite a persistence plugin runs against ITS implementation) to the
in-tree ``AsyncCollection`` — so the reference implementation is proven against
the same invariants the seam promises plugin authors
(see ``astrabox/seams/repository.py``, Tier A).
"""

from __future__ import annotations

import pytest

from astrabox.persistence.repository.sqlite.collection import AsyncCollection
from astrabox.testing.collection_conformance import CollectionContractSuite


class TestSqliteCollectionContract(CollectionContractSuite):
    @pytest.fixture(autouse=True)
    def _bind_tmp_db(self, tmp_path) -> None:
        self._db_url = f"sqlite+aiosqlite:///{tmp_path}/conformance.sqlite"

    async def make_collection(self, name: str) -> AsyncCollection:
        return AsyncCollection(name, self._db_url)
