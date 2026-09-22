"""The optional MongoDB backend, held to the same collection contract as SQLite.

Binds :class:`astrabox.testing.collection_conformance.CollectionContractSuite`
(the reusable suite a persistence plugin runs against ITS implementation) to
the REAL production Mongo ingress —
:func:`astrabox.persistence.repository.mongo.get_async_collection` — the same
object the 22 store-agnostic repository bodies receive once
``ASTRABOX_DB_BACKEND=mongo`` (or a ``mongodb://`` ``ASTRABOX_DB_URL``) is
configured. See ``tests/sqlite_collection_conformance_test.py`` for the
default backend's twin binding.

Needs a reachable ``mongod`` + the ``[mongo]`` extra installed
(``pip install -e '.[mongo]'``), pointed at via ``ASTRABOX_DB_URL=mongodb://…``
(or ``ASTRABOX_MONGODB_URI`` / ``MONGODB_URI`` — see
``astrabox/persistence/repository/mongo/__init__.py::_read_uri``). Run it with
``make test-mongo`` or directly via ``pytest -m mongo``. Deselected by default
(mirrors the existing ``e2e`` marker; see ``pyproject.toml``'s ``addopts``),
and ``pytest.importorskip("pymongo")`` below means the whole module is
skipped — not errored — at collection time when the ``[mongo]`` extra is not
installed, so the default unit lane needs neither Docker nor pymongo.

The autouse fixture below pins ``ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED=true``
explicitly for THIS test process only, via ``monkeypatch`` (auto-reverted at
fixture teardown). ``true`` is also the production default (see
``runtime_index_creation_enabled()`` in
``astrabox/persistence/repository/index_verification.py``), so this fixture
keeps the suite hermetic against an ambient shell that exports the opt-out value
(``=false``, meant for a managed-Mongo operator whose DBAs pre-create indexes
out-of-band), which would otherwise make
``test_unique_index_violation_raises_duplicate_key_error`` depend on a
pre-existing index instead of proving ``create_index`` itself reaches Mongo.
See ``tests/mongo_index_verification_test.py`` for the dedicated coverage of
both the default-on and explicit-off behaviours of the gate itself.
"""

from __future__ import annotations

import pytest

pymongo = pytest.importorskip("pymongo")

pytestmark = pytest.mark.mongo

from astrabox.persistence.repository.mongo import get_async_collection  # noqa: E402
from astrabox.testing.collection_conformance import CollectionContractSuite  # noqa: E402


class TestMongoCollectionContract(CollectionContractSuite):
    @pytest.fixture(autouse=True)
    def _enable_runtime_index_creation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED", "true")

    async def make_collection(self, name: str):
        """Return ``name``'s Mongo collection, cleared of any leftover docs.

        Unlike the SQLite binding (a fresh ``tmp_path`` file per test), the
        target ``mongod`` is a persistent instance possibly reused across
        runs — each collection is explicitly cleared here (rather than relying
        on a fresh database per test) so reruns against the same mongod stay
        hermetic.
        """
        collection = await get_async_collection(name)
        await collection.delete_many({})
        return collection
