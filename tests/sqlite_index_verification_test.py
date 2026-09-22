"""``ensure_unique_index`` against the default SQLite backend — unaffected by the gate.

``ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED`` defaults to ``true`` and routes
every unique-index repo through the shared ``ensure_unique_index`` helper (see
``astrabox/persistence/repository/index_verification.py``). SQLite's own
``AsyncCollection.create_index`` has never read this flag at all — its indexes
are always built for real (see ``sqlite/collection.py``) — so this file pins
the one invariant the design explicitly requires: the new default-on flag and
the new shared helper must not change SQLite's behaviour one bit, regardless
of which way the flag is set.
"""

from __future__ import annotations

import pytest

from astrabox.persistence.repository._compat import DuplicateKeyError
from astrabox.persistence.repository.index_verification import (
    ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED,
    ensure_unique_index,
)
from astrabox.persistence.repository.sqlite.collection import AsyncCollection


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/index_verification.sqlite"


async def test_ensure_unique_index_creates_and_enforces_with_flag_unset(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset flag leaves runtime index creation enabled."""
    monkeypatch.delenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, raising=False)
    coll = AsyncCollection("sqlite_gate_default", db_url)

    await ensure_unique_index(
        coll, "fence_id", name="ux_fence", collection_name="sqlite_gate_default"
    )

    await coll.insert_one({"fence_id": "f1"})
    with pytest.raises(DuplicateKeyError):
        await coll.insert_one({"fence_id": "f1"})


async def test_ensure_unique_index_unaffected_by_explicit_opt_out(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag explicitly OFF (the managed-Mongo opt-out) must not leak into SQLite.

    SQLite has no create_index gate to begin with, so ``ensure_unique_index``
    must still create and enforce the index — never raise "index missing" —
    even when an operator has set the opt-out value in their environment for
    an unrelated Mongo deployment elsewhere.
    """
    monkeypatch.setenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, "false")
    coll = AsyncCollection("sqlite_gate_explicit_off", db_url)

    await ensure_unique_index(
        coll,
        "fence_id",
        name="ux_fence_off",
        collection_name="sqlite_gate_explicit_off",
    )

    await coll.insert_one({"fence_id": "f1"})
    with pytest.raises(DuplicateKeyError):
        await coll.insert_one({"fence_id": "f1"})


async def test_ensure_unique_index_matches_by_key_spec_not_name(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit ``name`` (several repos rely on this): verification still passes.

    SQLite auto-generates a name of its own shape when none is given; a
    caller-agnostic verification (by key spec, not name) must still recognise
    it as present.
    """
    monkeypatch.delenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, raising=False)
    coll = AsyncCollection("sqlite_gate_unnamed", db_url)

    await ensure_unique_index(coll, "assistant_id", collection_name="sqlite_gate_unnamed")

    specs = await coll.list_indexes()
    assert any(
        spec.get("unique") and spec.get("key") == {"assistant_id": 1} for spec in specs
    ), f"expected an auto-named unique index over assistant_id, got {specs!r}"


async def test_swallowed_ddl_failure_is_detected_by_verify(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``CREATE INDEX`` failure must fail loud, not be masked by the registry.

    ``create_index`` registers the spec unconditionally and the DDL swallows any
    error (a locked db, full disk, unsupported expression). ``list_indexes``
    reads the REAL sqlite schema, so a swallowed failure leaves the index absent
    there and ``ensure_unique_index``'s verify raises — the SQL index is
    the sole enforcer, so a silent gap would ship uniqueness that isn't enforced.
    """
    monkeypatch.delenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, raising=False)
    coll = AsyncCollection("sqlite_ddl_fails", db_url)

    async def _boom(*_args, **_kwargs) -> None:
        return None  # swallow, exactly as the real except-block does on failure

    # Simulate the DDL being swallowed (no real index created) while the spec is
    # still registered by create_index.
    monkeypatch.setattr(coll, "_create_sqlite_expression_index", _boom)

    with pytest.raises(RuntimeError, match="required unique index is missing"):
        await ensure_unique_index(
            coll, "fence_id", name="ux_never_built", collection_name="sqlite_ddl_fails"
        )
