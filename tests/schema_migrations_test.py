"""Schema-migration mechanism — fresh stamp, apply-once, downgrade, failure, CAS lock.

Pins the invariants :mod:`astrabox.persistence.migrations` stands on, against
the REAL sqlite collection backend (a per-test tmp file via the autouse
fixture below — mirrors ``tests/sqlite_cas_concurrency_test.py``'s idiom of
proving CAS properties over real cross-connection concurrency, not a hand-rolled
fake) reached through the same ``get_async_collection`` ingress production code
uses:

* a fresh store stamps straight to the latest version with no historical
  ``apply()`` calls;
* a pending migration applies exactly once, even across repeated boots;
* a stored version ahead of the running code's latest (a downgrade) refuses to
  boot, naming both versions;
* a migration that raises halts boot, leaves the version un-bumped, and still
  releases the lock so a retry is not wedged;
* under concurrency, exactly one caller runs the migration body and every other
  caller converges (or would time out loudly, per the same CAS primitive
  ``tests/sqlite_cas_concurrency_test.py`` pins for the lease/lock repos).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import astrabox.persistence.migrations as migrations
from astrabox.config.settings import get_settings
from astrabox.persistence.repository.backend import get_async_collection


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the DAL at a per-test tmp SQLite file (mirrors ``session_ownership_org_test.py``)."""
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _read_schema_doc() -> dict[str, Any] | None:
    collection = await get_async_collection(migrations.SCHEMA_META_COLLECTION)
    return await collection.find_one({"_id": "schema"})


async def _noop(get_collection: migrations.GetCollection) -> None:
    _ = get_collection


def _migration(version: int, apply: Any, description: str = "") -> migrations.Migration:
    return migrations.Migration(version, description or f"v{version}", apply)


# --------------------------------------------------------------------------- #
# The real, in-tree MIGRATIONS list (smoke: it boots clean end to end)          #
# --------------------------------------------------------------------------- #
async def test_default_migrations_list_boots_clean() -> None:
    await migrations.run_pending_migrations()

    doc = await _read_schema_doc()
    assert doc is not None
    assert doc["version"] == migrations.latest_known_version()
    assert doc["locked_by"] is None

    # A second boot against the now-current store is a true no-op.
    await migrations.run_pending_migrations()
    doc_again = await _read_schema_doc()
    assert doc_again == doc


async def test_default_migration_rewrites_only_the_removed_environment_backend() -> None:
    environments = await get_async_collection("environment")
    await environments.insert_one(
        {
            "_id": "legacy-env",
            "name": "legacy",
            "sandbox_backend": " Direct_Docker ",
            "description": "keep me",
        }
    )
    await environments.insert_one(
        {"_id": "current-env", "name": "current", "sandbox_backend": "open_sandbox"}
    )
    await environments.insert_one(
        {"_id": "plugin-env", "name": "plugin", "sandbox_backend": "vendor_cloud"}
    )

    await migrations.run_pending_migrations()

    legacy = await environments.find_one({"_id": "legacy-env"})
    current = await environments.find_one({"_id": "current-env"})
    plugin = await environments.find_one({"_id": "plugin-env"})
    assert legacy is not None
    assert legacy["sandbox_backend"] == "open_sandbox"
    assert legacy["description"] == "keep me"
    assert current is not None and current["sandbox_backend"] == "open_sandbox"
    assert plugin is not None and plugin["sandbox_backend"] == "vendor_cloud"

    schema = await _read_schema_doc()
    assert schema is not None
    assert schema["version"] == migrations.latest_known_version()

    # The runner records the migration once; a second boot leaves the rows alone.
    await migrations.run_pending_migrations()
    assert await environments.find_one({"_id": "legacy-env"}) == legacy


async def test_default_migration_settles_only_the_idle_action_left_unstated() -> None:
    environments = await get_async_collection("environment")
    await environments.insert_one(
        {"_id": "unstated-env", "name": "unstated", "description": "keep me"}
    )
    await environments.insert_one(
        {"_id": "blank-env", "name": "blank", "idle_action": "  "}
    )
    await environments.insert_one(
        {"_id": "stated-env", "name": "stated", "idle_action": "pause"}
    )

    await migrations.run_pending_migrations()

    unstated = await environments.find_one({"_id": "unstated-env"})
    blank = await environments.find_one({"_id": "blank-env"})
    stated = await environments.find_one({"_id": "stated-env"})
    # Missing and blank values both receive the declared default.
    assert unstated is not None
    assert unstated["idle_action"] == "terminate"
    assert unstated["description"] == "keep me"
    assert blank is not None and blank["idle_action"] == "terminate"
    # An environment that already stated one is not touched.
    assert stated is not None and stated["idle_action"] == "pause"

    # Settling is recorded, so a second boot rewrites nothing.
    await migrations.run_pending_migrations()
    assert await environments.find_one({"_id": "stated-env"}) == stated


# --------------------------------------------------------------------------- #
# Fresh store: stamp latest directly, no historical apply() calls               #
# --------------------------------------------------------------------------- #
async def test_fresh_store_stamps_latest_without_running_historical_migrations() -> None:
    calls: list[int] = []

    async def _v2_apply(get_collection: migrations.GetCollection) -> None:
        calls.append(2)

    custom = [_migration(1, _noop, "baseline"), _migration(2, _v2_apply, "adds a thing")]

    await migrations.run_pending_migrations(migrations=custom)

    assert calls == [], "a fresh store must never run a historical migration's apply()"
    doc = await _read_schema_doc()
    assert doc is not None
    assert doc["version"] == 2
    assert doc["locked_by"] is None
    assert [entry["version"] for entry in doc["applied"]] == [1, 2]


# --------------------------------------------------------------------------- #
# Legacy pre-schema_meta store WITH DATA: stamp baseline, run pending           #
# --------------------------------------------------------------------------- #
async def test_legacy_store_with_data_stamps_baseline_and_runs_pending() -> None:
    """A store carrying data but no ``schema_meta`` doc is a pre-mechanism
    0.1.0 store, NOT a fresh one. It must be stamped at the v1 baseline so the
    v2 migration actually runs against its data — stamping it at latest would
    silently skip every migration authored after the store was created."""
    sessions = await get_async_collection("sessions")
    await sessions.insert_one({"_id": "legacy-session", "user_id": "u1"})

    ran: list[int] = []

    async def _v2_apply(get_collection: migrations.GetCollection) -> None:
        ran.append(2)

    custom = [_migration(1, _noop, "baseline"), _migration(2, _v2_apply, "renames a field")]
    await migrations.run_pending_migrations(migrations=custom)

    assert ran == [2], "the v2 migration must RUN against a legacy data-bearing store"
    doc = await _read_schema_doc()
    assert doc is not None
    assert doc["version"] == 2
    assert [entry["version"] for entry in doc["applied"]] == [1, 2]


async def test_legacy_detection_probes_beyond_sessions() -> None:
    """The legacy probe is a disjunction over core collections — data present
    only in a fixed-name config collection (no sessions at all) still marks
    the store legacy."""
    whitelist = await get_async_collection("template_whitelist")
    await whitelist.insert_one({"_id": "tpl-1", "name": "seeded"})

    ran: list[int] = []

    async def _v2_apply(get_collection: migrations.GetCollection) -> None:
        ran.append(2)

    custom = [_migration(1, _noop, "baseline"), _migration(2, _v2_apply, "backfill")]
    await migrations.run_pending_migrations(migrations=custom)

    assert ran == [2]
    assert (await _read_schema_doc())["version"] == 2


# --------------------------------------------------------------------------- #
# Pending migration applies exactly once (idempotent double-run)                #
# --------------------------------------------------------------------------- #
async def test_pending_migration_applies_exactly_once() -> None:
    calls = 0

    async def _v2_apply(get_collection: migrations.GetCollection) -> None:
        nonlocal calls
        calls += 1

    v1_only = [_migration(1, _noop, "baseline")]
    full = [_migration(1, _noop, "baseline"), _migration(2, _v2_apply, "adds a thing")]

    # The existing store records only migration v1 when the v2 runner arrives.
    await migrations.run_pending_migrations(migrations=v1_only)
    assert (await _read_schema_doc())["version"] == 1

    await migrations.run_pending_migrations(migrations=full)
    assert calls == 1
    doc = await _read_schema_doc()
    assert doc is not None
    assert doc["version"] == 2
    assert [entry["version"] for entry in doc["applied"]] == [1, 2]

    # Re-running against an already-current store must not re-apply v2.
    await migrations.run_pending_migrations(migrations=full)
    assert calls == 1
    assert (await _read_schema_doc())["version"] == 2


# --------------------------------------------------------------------------- #
# Downgrade fails loud, naming both versions                                    #
# --------------------------------------------------------------------------- #
async def test_downgrade_refuses_to_boot_naming_both_versions() -> None:
    ahead = [_migration(1, _noop), _migration(2, _noop), _migration(3, _noop)]
    await migrations.run_pending_migrations(migrations=ahead)
    assert (await _read_schema_doc())["version"] == 3

    older_code = [_migration(1, _noop)]
    with pytest.raises(RuntimeError) as exc_info:
        await migrations.run_pending_migrations(migrations=older_code)

    message = str(exc_info.value).lower()
    assert "downgrade" in message
    assert "version=3" in str(exc_info.value)
    assert "version=1" in str(exc_info.value)

    # The refusal must not have mutated the stored version.
    assert (await _read_schema_doc())["version"] == 3


# --------------------------------------------------------------------------- #
# Mid-migration failure halts boot, does not stamp, and releases the lock       #
# --------------------------------------------------------------------------- #
async def test_migration_failure_halts_and_does_not_stamp() -> None:
    async def _boom(get_collection: migrations.GetCollection) -> None:
        raise RuntimeError("backfill exploded")

    v1_only = [_migration(1, _noop, "baseline")]
    await migrations.run_pending_migrations(migrations=v1_only)

    failing = [_migration(1, _noop, "baseline"), _migration(2, _boom, "boom")]
    with pytest.raises(RuntimeError, match="backfill exploded"):
        await migrations.run_pending_migrations(migrations=failing)

    doc = await _read_schema_doc()
    assert doc is not None
    assert doc["version"] == 1, "a failed migration must not bump the stored version"
    assert [entry["version"] for entry in doc["applied"]] == [1]
    assert doc["locked_by"] is None, "the lock must be released even on failure"

    # No partial silent state: the same boot attempt fails again identically...
    with pytest.raises(RuntimeError, match="backfill exploded"):
        await migrations.run_pending_migrations(migrations=failing)

    # A corrected migration uses the next version; replacing a published body
    # at the same version is not valid authoring. The retry also proves that the
    # failed attempt released its lock.
    fixed = [_migration(1, _noop, "baseline"), _migration(2, _noop, "boom-fixed")]
    await migrations.run_pending_migrations(migrations=fixed)
    assert (await _read_schema_doc())["version"] == 2


# --------------------------------------------------------------------------- #
# CAS lock admits exactly one winner under concurrency                          #
# --------------------------------------------------------------------------- #
async def test_concurrent_boots_have_exactly_one_migration_winner() -> None:
    calls = 0

    async def _slow_v2(get_collection: migrations.GetCollection) -> None:
        nonlocal calls
        calls += 1
        # Widen the race window so the other 7 concurrent callers reliably
        # observe the lock held (and take the poll-and-converge path) instead
        # of the whole wave finishing before they even attempt the CAS claim.
        await asyncio.sleep(0.1)

    v1_only = [_migration(1, _noop, "baseline")]
    await migrations.run_pending_migrations(migrations=v1_only)

    full = [_migration(1, _noop, "baseline"), _migration(2, _slow_v2, "slow")]

    results = await asyncio.gather(
        *(
            migrations.run_pending_migrations(
                migrations=full,
                lock_poll_interval_seconds=0.01,
                lock_timeout_seconds=5.0,
            )
            for _ in range(8)
        ),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], f"every concurrent boot must converge, got: {failures}"
    assert calls == 1, f"exactly one replica must run the migration body, ran {calls} times"

    doc = await _read_schema_doc()
    assert doc is not None
    assert doc["version"] == 2
    assert doc["locked_by"] is None
