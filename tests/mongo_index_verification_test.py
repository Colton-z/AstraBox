"""The default-on index-creation gate + the shared verify-else-fail-loud helper.

Covers, against a REAL mongod (see ``tests/mongo_collection_conformance_test.py``'s
docstring for how to point one), the behaviour
``astrabox/persistence/repository/index_verification.py`` and the
``_RetryingCollectionProxy`` gate in ``astrabox/persistence/repository/mongo``
are supposed to have:

* ``ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED`` unset (the new default, ``true``)
  → :func:`ensure_unique_index` creates the index for real and verifies it.
* Explicitly ``false`` (the managed-Mongo opt-out) with the index already
  present (simulating a DBA-provisioned index) → verify-only, passes silently.
* Explicitly ``false`` with the index absent → raises loud, naming the
  collection, the key spec, and the env var — never a silent "continue
  without uniqueness enforced".
* The plural ``create_indexes`` bypass fix: it now honours the same gate as
  the singular ``create_index``.
* One end-to-end wiring proof on ``SessionEventRepository`` — the one repo
  whose own bespoke verify-else-refuse defense is now replaced by the shared
  helper — confirmed to fail loud (not silently degrade) when misconfigured.

Deselected by default (the ``mongo`` marker); run explicitly with
``pytest -m mongo`` / ``make test-mongo``.
"""

from __future__ import annotations

import pytest

pymongo = pytest.importorskip("pymongo")

pytestmark = pytest.mark.mongo

from astrabox.persistence.repository._compat import DuplicateKeyError  # noqa: E402
from astrabox.persistence.repository.index_verification import (  # noqa: E402
    ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED,
    ensure_unique_index,
)
from astrabox.persistence.repository.mongo import get_async_collection  # noqa: E402


async def _clean_collection(name: str):
    """A collection cleared of both documents AND indexes (hermetic reruns).

    Unlike ``tests/mongo_collection_conformance_test.py``'s ``make_collection``
    (which only clears documents), these tests specifically assert on index
    presence/absence, so a leftover index from a previous run would produce a
    false pass — ``drop_indexes()`` (unaffected by the creation gate; only
    ``create_index``/``create_indexes`` are intercepted) guarantees a clean
    starting state every time.
    """
    collection = await get_async_collection(name)
    await collection.delete_many({})
    await collection.drop_indexes()
    return collection


async def test_default_on_creates_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag unset == the new production default: create + verify, enforced for real."""
    monkeypatch.delenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, raising=False)
    collection = await _clean_collection("gate_default_on")

    await ensure_unique_index(
        collection, "fence_id", name="ux_fence", collection_name="gate_default_on"
    )

    await collection.insert_one({"fence_id": "f1"})
    with pytest.raises(DuplicateKeyError):
        await collection.insert_one({"fence_id": "f1"})


async def test_explicit_off_with_preexisting_index_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off + a DBA already provisioned the index out-of-band: verify-only, passes."""
    collection = await _clean_collection("gate_explicit_off_precreated")

    # Simulate a managed cluster's DBA having pre-created the index out-of-band
    # (i.e. NOT via astrabox, and not via the gated proxy): create it directly
    # while the gate is temporarily on, then flip to the opt-out value before
    # exercising ensure_unique_index.
    monkeypatch.setenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, "true")
    await collection.create_index([("fence_id", 1)], unique=True, name="ux_fence_precreated")

    monkeypatch.setenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, "false")
    await ensure_unique_index(
        collection,
        "fence_id",
        name="ux_fence_precreated",
        collection_name="gate_explicit_off_precreated",
    )

    await collection.insert_one({"fence_id": "f1"})
    with pytest.raises(DuplicateKeyError):
        await collection.insert_one({"fence_id": "f1"})


async def test_explicit_off_without_index_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off + nothing pre-created: raises loud, naming collection/index/env."""
    monkeypatch.setenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, "false")
    collection = await _clean_collection("gate_explicit_off_missing")

    with pytest.raises(RuntimeError) as exc_info:
        await ensure_unique_index(
            collection,
            "fence_id",
            name="ux_fence_missing",
            collection_name="gate_explicit_off_missing",
        )

    message = str(exc_info.value)
    assert "gate_explicit_off_missing" in message, message
    assert "fence_id" in message, message
    assert ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED in message, message

    # And it must be a REAL absence, not a false negative: no index was ever
    # created, so a duplicate is silently admitted (this is exactly the
    # failure mode the 15 repos without a bespoke defense were exposed to).
    await collection.insert_one({"fence_id": "f1"})
    await collection.insert_one({"fence_id": "f1"})  # must NOT raise
    assert await collection.count_documents({"fence_id": "f1"}) == 2


async def test_create_indexes_plural_respects_gate_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plural bypass fix: create_indexes is gated exactly like create_index."""
    collection = await _clean_collection("gate_plural_off")
    monkeypatch.setenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, "false")

    result = await collection.create_indexes(
        [pymongo.IndexModel([("k", 1)], unique=True, name="ux_k_plural")]
    )
    assert result == [], f"expected the no-op skip shape (empty list), got {result!r}"

    # No index was actually created -> a "duplicate" is silently admitted.
    await collection.insert_one({"k": "v1"})
    await collection.insert_one({"k": "v1"})  # must NOT raise while the gate is off

    monkeypatch.setenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, "true")
    await collection.delete_many({})
    await collection.create_indexes(
        [pymongo.IndexModel([("k", 1)], unique=True, name="ux_k_plural")]
    )
    await collection.insert_one({"k": "v1"})
    with pytest.raises(DuplicateKeyError):
        await collection.insert_one({"k": "v1"})  # gate on -> the index is now real


async def test_session_events_ensure_indexes_fails_loud_when_gate_off_and_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring proof: SessionEventRepository's rewritten ensure_indexes().

    This repo goes through the same shared verify-else-fail-loud helper as
    every other unique-index repo, replacing its own bespoke verify-else-
    refuse defense (log CRITICAL + a module flag downstream calls consulted),
    and must actually RAISE — not silently degrade — when misconfigured.
    """
    import astrabox.persistence.repository.session_event_repository as journal_module

    # Redirect this instance of the module-level constants at a private,
    # test-only collection name / freshly-false readiness flag, so this test
    # never touches the real "session_events" collection name and can't leak
    # state into any other test (monkeypatch auto-reverts both at teardown).
    monkeypatch.setattr(journal_module, "COLLECTION_NAME", "gate_test_session_events")
    monkeypatch.setattr(journal_module, "_index_ready", False)
    monkeypatch.setenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, "false")

    await _clean_collection("gate_test_session_events")

    with pytest.raises(RuntimeError, match=ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED):
        await journal_module.SessionEventRepository().ensure_indexes()
