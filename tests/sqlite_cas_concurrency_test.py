"""SQLite backend CAS atomicity — the exactly-one-winner invariant.

Every coordination primitive in the kernel (turn locks, attach leases, epoch
fencing, snapshot CAS) reduces to ``find_one_and_update`` / guarded
``update_one`` on the default SQLite backend. Under real cross-connection
concurrency, a CAS claim must have exactly one winner and ``$inc`` must preserve
every update. ``BEGIN IMMEDIATE`` keeps the predicate read and write in one
transaction.

The suite also requires dotted-path ``$set`` to persist changes to existing
nested objects.
"""

from __future__ import annotations

import asyncio

import pytest

from astrabox.persistence.repository._compat import ReturnDocument
from astrabox.persistence.repository.sqlite.collection import AsyncCollection


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/cas.sqlite"


async def test_concurrent_cas_claim_has_exactly_one_winner(db_url: str) -> None:
    coll = AsyncCollection("locks", db_url)
    await coll.insert_one({"_id": "turn-lock", "owner": None})

    async def claim(worker: str):
        return await coll.find_one_and_update(
            {"_id": "turn-lock", "owner": None},
            {"$set": {"owner": worker}},
            return_document=ReturnDocument.AFTER,
        )

    results = await asyncio.gather(*(claim(f"w{i}") for i in range(8)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, (
        f"CAS claim must have exactly one winner, got {len(winners)}: "
        f"{[w['owner'] for w in winners]}"
    )

    stored = await coll.find_one({"_id": "turn-lock"})
    assert stored is not None
    assert stored["owner"] == winners[0]["owner"]


async def test_concurrent_inc_never_collapses(db_url: str) -> None:
    coll = AsyncCollection("epochs", db_url)
    await coll.insert_one({"_id": "chk", "lease_epoch": 0})

    async def bump():
        doc = await coll.find_one_and_update(
            {"_id": "chk"},
            {"$inc": {"lease_epoch": 1}},
            return_document=ReturnDocument.AFTER,
        )
        assert doc is not None
        return int(doc["lease_epoch"])

    epochs = await asyncio.gather(*(bump() for _ in range(8)))
    stored = await coll.find_one({"_id": "chk"})
    assert stored is not None
    assert stored["lease_epoch"] == 8, f"$inc collapsed: {stored['lease_epoch']} != 8"
    # every bump observed a distinct epoch — the fencing property lease renewal
    # depends on (two claimants must never share an epoch)
    assert sorted(epochs) == list(range(1, 9)), f"epochs not distinct: {sorted(epochs)}"


async def test_dotted_set_on_nested_parent_persists(db_url: str) -> None:
    coll = AsyncCollection("snapshots", db_url)
    await coll.insert_one({"_id": "s1", "meta": {"phase": "a", "keep": 1}})

    result = await coll.update_one({"_id": "s1"}, {"$set": {"meta.phase": "b"}})
    assert result.matched_count == 1
    assert result.modified_count == 1, "dotted $set reported no-op — write was lost"

    stored = await coll.find_one({"_id": "s1"})
    assert stored is not None
    assert stored["meta"] == {"phase": "b", "keep": 1}


async def test_find_one_and_update_before_returns_pre_update_values(
    db_url: str,
) -> None:
    coll = AsyncCollection("snapshots2", db_url)
    await coll.insert_one({"_id": "s2", "meta": {"phase": "a"}})

    before = await coll.find_one_and_update(
        {"_id": "s2"},
        {"$set": {"meta.phase": "b"}},
        return_document=ReturnDocument.BEFORE,
    )
    assert before is not None
    assert before["meta"]["phase"] == "a", "BEFORE doc leaked post-update values"

    stored = await coll.find_one({"_id": "s2"})
    assert stored is not None
    assert stored["meta"]["phase"] == "b"


async def test_guarded_update_one_is_atomic_under_concurrency(db_url: str) -> None:
    coll = AsyncCollection("guards", db_url)
    await coll.insert_one({"_id": "g", "state": "idle", "holder": None})

    async def transition(worker: str) -> bool:
        result = await coll.update_one(
            {"_id": "g", "state": "idle"},
            {"$set": {"state": "busy", "holder": worker}},
        )
        return result.modified_count == 1

    outcomes = await asyncio.gather(*(transition(f"w{i}") for i in range(8)))
    assert sum(outcomes) == 1, f"guarded update won {sum(outcomes)} times, want 1"
