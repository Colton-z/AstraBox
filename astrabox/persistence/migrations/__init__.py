"""Sequential data migrations run during application startup.

``backend.create_all()`` creates the PostgreSQL document table when it is missing,
but it cannot update documents written by an older release. This module runs
those updates after ``create_all()`` and before the API starts serving traffic.

``MIGRATIONS`` is an ordered list of version, description, and async apply
function. Each function receives the same collection lookup used by normal
repositories, so it works with every supported persistence backend. Version 1
is the no-op baseline; actual data changes start at version 2.

The ``schema_meta`` collection records completed versions. Startup stops when
a migration fails or when the stored version is newer than the running code.
A compare-and-set lock ensures that only one replica applies an upgrade while
other replicas wait for it to finish. See ``docs/migrations.md`` for the
authoring and recovery guide.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable, NamedTuple, Sequence

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository._compat import DuplicateKeyError, ReturnDocument
from astrabox.persistence.repository.backend import get_async_collection

logger = get_logger(__name__)

__all__ = [
    "SCHEMA_META_COLLECTION",
    "GetCollection",
    "Migration",
    "MIGRATIONS",
    "latest_known_version",
    "run_pending_migrations",
]

#: The single bookkeeping collection, reached through the normal DAL ingress
#: (:func:`astrabox.persistence.repository.backend.get_async_collection`) —
#: never a bespoke connection. One document lives here: ``_id == "schema"``.
SCHEMA_META_COLLECTION = "schema_meta"

_SCHEMA_DOC_ID = "schema"

#: Default lock-wait tuning for the multi-replica CAS lock. Both are
#: overridable per :func:`run_pending_migrations` call, so a caller that
#: cannot afford a 30s wait can shorten them.
_DEFAULT_LOCK_POLL_INTERVAL_SECONDS = 0.25
_DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0

#: A migration's ``apply`` body receives the same collection ingress every
#: repository uses — ``get_collection(name) -> awaitable collection`` — so it
#: is exactly as store-agnostic as a repository method, never a raw DB handle.
GetCollection = Callable[[str], Awaitable[Any]]


class Migration(NamedTuple):
    """One schema migration: a monotonic version, a human description, a body.

    ``apply(get_collection)`` runs at most once per store (recorded in
    ``schema_meta.applied`` only after it returns without raising) and must be
    safe to run against a store that already carries real data — see the
    additive-first policy in ``docs/migrations.md``.
    """

    version: int
    description: str
    apply: Callable[[GetCollection], Awaitable[None]]


async def _baseline_noop(get_collection: GetCollection) -> None:
    """Record the version-1 ``create_all`` schema without transforming data."""
    _ = get_collection


async def _v2_replace_removed_environment_backend(
    get_collection: GetCollection,
) -> None:
    """Move saved Environment presets to the built-in backend that replaced it."""
    from astrabox.common.utils.settings import load_astrabox_settings

    collection_name = load_astrabox_settings().environment_collection
    collection = await get_collection(collection_name)
    cursor = collection.find({})
    changed = 0
    async for document in cursor:
        raw_backend = document.get("sandbox_backend")
        if str(raw_backend or "").strip().lower() != "direct_docker":
            continue
        name = str(document.get("name") or "").strip()
        identity = {"name": name} if name else {"_id": document.get("_id")}
        await collection.update_one(
            {**identity, "sandbox_backend": raw_backend},
            {"$set": {"sandbox_backend": "open_sandbox"}},
        )
        changed += 1
    logger.info(
        "schema migration updated %d Environment preset(s) to sandbox backend %s",
        changed,
        "open_sandbox",
    )


async def _v3_settle_environment_idle_action(
    get_collection: GetCollection,
) -> None:
    """Fill empty Environment idle actions from the installation setting.

    Sandbox cleanup requires every stored Environment to name its idle action.
    This migration copies ``ASTRABOX_SANDBOX_IDLE_ACTION`` into records where
    the field is empty.
    """
    from astrabox.common.utils.settings import load_astrabox_settings

    settings = load_astrabox_settings()
    action = str(settings.sandbox_idle_action or "").strip().lower()
    collection = await get_collection(settings.environment_collection)
    cursor = collection.find({})
    changed = 0
    async for document in cursor:
        if str(document.get("idle_action") or "").strip().lower():
            continue
        name = str(document.get("name") or "").strip()
        identity = {"name": name} if name else {"_id": document.get("_id")}
        await collection.update_one(identity, {"$set": {"idle_action": action}})
        changed += 1
    logger.info(
        "schema migration settled idle_action=%s on %d Environment preset(s)",
        action,
        changed,
    )


async def _v4_retire_transcript_entry_uuid_index(
    get_collection: GetCollection,
) -> None:
    """Drop the transcript index that made an entry ``uuid`` unique per scope.

    A transcript batch is identified by its ``append_id``, so the same entries
    sent under a new id are a second append and must be stored. A unique index
    on the entry ``uuid`` contradicts that: it rejects the second append instead,
    and because the index object survives in the schema after the repository
    stops declaring it, the contradiction would only appear on a database that
    already carries it.
    """
    from astrabox.persistence.repository.transcript_entry_repository import (
        COLLECTION_NAME,
    )

    collection = await get_collection(COLLECTION_NAME)
    drop_index = getattr(collection, "drop_index", None)
    if not callable(drop_index):
        raise RuntimeError(
            f"collection backend for {COLLECTION_NAME!r} cannot drop an index; "
            "ux_transcript_scope_uuid must be dropped before batch-identified "
            "appends are correct"
        )
    await drop_index("ux_transcript_scope_uuid")
    logger.info("schema migration dropped index ux_transcript_scope_uuid")


MIGRATIONS: list[Migration] = [
    Migration(
        1,
        "baseline: current schema as of the migration mechanism's introduction",
        _baseline_noop,
    ),
    Migration(
        2,
        "replace the removed local sandbox backend in Environment presets",
        _v2_replace_removed_environment_backend,
    ),
    Migration(
        3,
        "settle idle_action on Environment presets that deferred to the installation",
        _v3_settle_environment_idle_action,
    ),
    Migration(
        4,
        "retire the transcript entry-uuid unique index superseded by batch identity",
        _v4_retire_transcript_entry_uuid_index,
    ),
]


def latest_known_version(migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """The highest version the running code knows how to apply."""
    return max(m.version for m in migrations)


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    """Fail loud on a malformed ``MIGRATIONS`` list (author error)."""
    if not migrations:
        raise RuntimeError(
            "run_pending_migrations: MIGRATIONS is empty; at least the version-1 "
            "baseline must be registered"
        )
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise RuntimeError(
            f"run_pending_migrations: MIGRATIONS has duplicate version numbers: {versions!r}"
        )
    if 1 not in versions:
        raise RuntimeError(
            "run_pending_migrations: MIGRATIONS has no version-1 baseline entry "
            f"(got versions={sorted(versions)!r})"
        )


#: Collections probed to tell a genuinely fresh store from one written before
#: ``schema_meta`` existed, when the schema doc is absent. Any document in any
#: of these means the store predates this mechanism (or has real data), so it
#: must be stamped at the v1 baseline — not at ``latest`` — or every migration
#: authored after the store was created would be silently skipped.
#: The set is a disjunction of fixed core names: even a deployment that
#: renamed the configurable ``sessions`` collection cannot have meaningful
#: data without tripping one of the fixed-name session/config collections.
_LEGACY_DATA_PROBE_COLLECTIONS: tuple[str, ...] = (
    "sessions",
    "session_snapshots",
    "session_events",
    "template_whitelist",
    "environment",
)


async def _store_has_pre_mechanism_data() -> bool:
    """True if any core collection already holds a document (legacy store).

    Fail-loud on probe errors: this runs right after ``create_all()`` in the
    boot path, so a collection that cannot even answer ``find_one`` is a real
    store fault, not a condition to paper over.
    """
    for name in _LEGACY_DATA_PROBE_COLLECTIONS:
        collection = await get_async_collection(name)
        if collection is None:
            continue
        if await collection.find_one({}) is not None:
            return True
    return False


async def _ensure_schema_doc(
    collection: Any, migrations: Sequence[Migration], latest: int
) -> dict[str, Any]:
    """Return the schema doc, stamping it if absent — at ``latest`` only when
    the store is verifiably empty.

    Absence means one of two very different stores:

    * a genuinely fresh store — ``create_all`` just built the current-shape
      empty schema, so there is no data for any migration to transform: stamp
      ``latest`` directly, with no historical ``apply`` calls; or
    * a store with data but no schema document. Its shape is the v1
      ``create_all`` baseline, so it is stamped at version 1 with only the
      baseline recorded as applied — and
      :func:`run_pending_migrations` then runs every v2+ migration against it
      normally. Stamping such a store at ``latest`` would silently skip them.

    The two are distinguished by :func:`_store_has_pre_mechanism_data`.

    A racing insert (two replicas booting a brand-new store simultaneously) is
    resolved by making the loser catch :class:`DuplicateKeyError` and re-read
    what the winner wrote.
    """
    existing = await collection.find_one({"_id": _SCHEMA_DOC_ID})
    if existing is not None:
        return existing

    now = utcnow_iso()
    if await _store_has_pre_mechanism_data():
        baseline = min(migrations, key=lambda m: m.version)
        stamp_version = baseline.version
        applied = [
            {
                "version": baseline.version,
                "description": baseline.description,
                "applied_at": now,
            }
        ]
        log_msg = (
            "schema_meta absent but the store already holds data — legacy "
            "pre-schema_meta store detected; stamped at baseline version=%d "
            "so pending migrations will run"
        )
    else:
        stamp_version = latest
        applied = [
            {"version": m.version, "description": m.description, "applied_at": now}
            for m in sorted(migrations, key=lambda m: m.version)
        ]
        log_msg = "schema_meta stamped fresh at version=%d (no historical migrations run)"
    fresh_doc = {
        "_id": _SCHEMA_DOC_ID,
        "version": stamp_version,
        "applied": applied,
        "locked_by": None,
        "locked_at": None,
    }
    try:
        await collection.insert_one(fresh_doc)
    except DuplicateKeyError:
        existing = await collection.find_one({"_id": _SCHEMA_DOC_ID})
        if existing is None:  # pragma: no cover - the winner's insert just committed
            raise RuntimeError(
                "schema_meta doc vanished immediately after a duplicate-key race; "
                "this should be unreachable"
            ) from None
        return existing
    else:
        logger.info(log_msg, stamp_version)
        return fresh_doc


async def _record_migration_applied(collection: Any, migration: Migration) -> None:
    """Bump ``version`` and append to ``applied`` (sole-writer safe, no ``$push``).

    Only the CAS-lock winner calls this, and it is the sole writer to this
    document for the duration of the migration wave (see
    :func:`run_pending_migrations`), so a plain read-then-``$set`` is sufficient
    — a second CAS is not needed. ``$push`` is not in the SQLite shim's
    supported update-operator set (:mod:`astrabox.persistence.repository.sqlite.query`
    implements only ``$set``/``$inc``/``$setOnInsert``), so the array is read,
    appended to in Python, and written back whole with ``$set``.
    """
    doc = await collection.find_one({"_id": _SCHEMA_DOC_ID}) or {}
    applied = list(doc.get("applied") or [])
    applied.append(
        {
            "version": migration.version,
            "description": migration.description,
            "applied_at": utcnow_iso(),
        }
    )
    await collection.update_one(
        {"_id": _SCHEMA_DOC_ID},
        {"$set": {"version": migration.version, "applied": applied}},
    )


async def _wait_for_convergence(
    collection: Any,
    *,
    target_version: int,
    poll_interval: float,
    timeout: float,
) -> None:
    """Poll ``schema_meta`` until ``version >= target_version``, or time out loudly.

    The loser side of the CAS lock in :func:`run_pending_migrations`: another
    replica holds the lock and is running the migration wave, so this replica
    does not retry the lock — it only waits for the winner's result. A timeout
    is a loud failure (the owner may have crashed mid-migration) rather than a
    silent proceed on a guess.
    """
    deadline = time.monotonic() + timeout
    last_seen: int | None = None
    while True:
        doc = await collection.find_one({"_id": _SCHEMA_DOC_ID})
        if doc is not None:
            last_seen = int(doc.get("version", 0))
            if last_seen >= target_version:
                return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"timed out after {timeout}s waiting for a concurrent schema "
                f"migration to reach version={target_version} (last observed "
                f"version={last_seen}); the replica holding the migration lock "
                "may have crashed mid-migration - check its logs before retrying boot"
            )
        await asyncio.sleep(poll_interval)


async def run_pending_migrations(
    *,
    migrations: Sequence[Migration] | None = None,
    lock_poll_interval_seconds: float = _DEFAULT_LOCK_POLL_INTERVAL_SECONDS,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Bring ``schema_meta`` up to :data:`MIGRATIONS`'s latest version (fail-loud).

    Called once at boot, immediately after ``backend.create_all()`` (see
    :mod:`astrabox.api.app`).

    1. **Fresh store** (no ``schema_meta`` doc yet) — stamp ``latest`` directly,
       no historical ``apply`` calls (see :func:`_ensure_schema_doc`).
    2. **Downgrade** (stored version > the running code's latest known version)
       — raise immediately, naming both versions: the running code cannot
       know what a future migration did to the document shape.
    3. **Up to date** (stored version == latest) — no-op; no lock taken.
    4. **Pending migrations** (stored version < latest) — take the CAS lock on
       the schema doc (a ``locked_by`` guard via ``find_one_and_update``). The
       winner runs each pending migration in
       ascending version order, recording success after each one (so a crash
       mid-wave resumes from the last completed version, not from scratch); a
       raised exception halts the loop, still releases the lock, and
       propagates (boot must not continue on a half-migrated store). Every
       other concurrent caller loses the lock claim and instead waits for
       ``version`` to reach ``latest`` (:func:`_wait_for_convergence`), timing
       out loudly rather than proceeding on a guess.

    Single-writer-per-upgrade is the operating expectation this rests on: at
    most one replica is ever actually executing migration bodies at a time,
    enforced by the lock rather than by deployment discipline — but operators
    running a fleet of replicas should still expect the first one up after a
    version bump to pay the (usually sub-second) migration cost while its
    siblings wait on it.
    """
    active = list(migrations) if migrations is not None else MIGRATIONS
    _validate_migrations(active)
    latest = latest_known_version(active)

    collection = await get_async_collection(SCHEMA_META_COLLECTION)
    doc = await _ensure_schema_doc(collection, active, latest)

    stored_version = int(doc["version"])
    if stored_version > latest:
        raise RuntimeError(
            f"schema downgrade refused: schema_meta is at version={stored_version} "
            f"but the running code only knows migrations up to version={latest}; "
            "deploy the matching (or newer) release, or restore the previous one, "
            "before booting against this store"
        )
    if stored_version == latest:
        logger.debug("schema_meta already at latest version=%d; nothing to migrate", latest)
        return

    owner = uuid.uuid4().hex
    acquired = await collection.find_one_and_update(
        {"_id": _SCHEMA_DOC_ID, "locked_by": None},
        {"$set": {"locked_by": owner, "locked_at": utcnow_iso()}},
        return_document=ReturnDocument.AFTER,
    )
    if acquired is None:
        logger.info(
            "schema migration already in progress on another replica; waiting "
            "for version to reach %d",
            latest,
        )
        await _wait_for_convergence(
            collection,
            target_version=latest,
            poll_interval=lock_poll_interval_seconds,
            timeout=lock_timeout_seconds,
        )
        return

    try:
        current = int(acquired.get("version", stored_version))
        pending = sorted((m for m in active if m.version > current), key=lambda m: m.version)
        for migration in pending:
            logger.info(
                "schema migration version=%d starting: %s",
                migration.version,
                migration.description,
            )
            await migration.apply(get_async_collection)
            await _record_migration_applied(collection, migration)
            logger.info("schema migration version=%d applied", migration.version)
    finally:
        await collection.update_one(
            {"_id": _SCHEMA_DOC_ID},
            {"$set": {"locked_by": None, "locked_at": None}},
        )
