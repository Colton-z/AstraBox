"""Optional MongoDB backend ([mongo] extra) — reached through the same ingress.

This subpackage is the Mongo collection path, quarantined so the **core never
imports pymongo unconditionally**: every ``pymongo`` import lives lazily *inside*
the functions here, and this module is imported by ``..backend`` only when the
active backend is ``mongo`` (selected by ``ASTRABOX_DB_BACKEND=mongo`` or a
``mongodb://`` ``ASTRABOX_DB_URL``). With the default PostgreSQL backend this file is
never imported, so a default install needs no ``pymongo``.

It exposes the same ``get_async_collection(name)`` contract as the SQLite shim:
an async collection object the store-agnostic repository bodies call. Here the
object is the real (async) pymongo collection wrapped in the transient-retry proxy
(so managed Mongo-proxy reconnect behaviour is preserved for the ``[mongo]`` user).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import weakref
from typing import Any

from astrabox.common.logger.logger_factory import get_logger

from ..index_verification import runtime_index_creation_enabled

logger = get_logger(__name__)

__all__ = [
    "get_async_collection",
    "is_transient_error",
    "close_for_current_loop",
    "wrap_collection_with_retry",
    "runtime_index_creation_enabled",
]


def _is_uvloop_fd_transport_error(exc: Exception) -> bool:
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc)
    return (
        "File descriptor " in message
        and " is used by transport <" in message
        and "Transport" in message
    )


def is_transient_error(exc: Exception) -> bool:
    """Classify ``exc`` as a retryable transient error — the mongo classifier.

    The mongo half of the uniform ``is_transient_error(exc)`` surface (sqlite's
    counterpart is unconditionally ``False``): real pymongo connection/timeout
    errors (``TRANSIENT_CONNECTION_ERRORS`` — real classes when ``[mongo]`` is
    installed, name-compatible stand-ins otherwise, from :mod:`.._compat`), the
    uvloop fd-transport error, and a proxied transient-dial ``OperationFailure``
    (a Mongo mesh/proxy may wrap a transient next-hop dial-refused as
    ``OperationFailure`` passed through verbatim).
    """
    from .._compat import OperationFailure, TRANSIENT_CONNECTION_ERRORS

    if TRANSIENT_CONNECTION_ERRORS and isinstance(exc, TRANSIENT_CONNECTION_ERRORS):
        return True
    if _is_uvloop_fd_transport_error(exc):
        return True
    # A Mongo mesh/proxy may wrap a transient next-hop dial-refused as OperationFailure
    # passed through verbatim; detect by message (code==1 InternalError at top).
    if isinstance(exc, OperationFailure):
        message = str(exc)
        return "dial tcp" in message and "connection refused" in message
    return False


# --------------------------------------------------------------------------- #
# Per-event-loop client state (one AsyncMongoClient per loop)                   #
# --------------------------------------------------------------------------- #
class _LoopState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.client: Any = None
        self.db: Any = None
        self.uri = ""
        self.db_name = ""


_STATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState]" = (
    weakref.WeakKeyDictionary()
)
_STATES_LOCK = threading.Lock()
_CACHED_URI: str | None = None


def _state_for(loop: asyncio.AbstractEventLoop) -> _LoopState:
    with _STATES_LOCK:
        state = _STATES.get(loop)
        if state is None:
            state = _LoopState()
            _STATES[loop] = state
        return state


def invalidate_current_loop() -> None:
    """Discard cached client state for the current loop (after a transient error)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with _STATES_LOCK:
            states = list(_STATES.values())
            _STATES.clear()
    else:
        states = [_state_for(loop)]
    for state in states:
        state.client = None
        state.db = None
        state.uri = ""
        state.db_name = ""


async def close_for_current_loop(reason: str = "event_loop_shutdown") -> None:
    """Close the Mongo client owned by the current loop (lifecycle shutdown hook)."""
    loop = asyncio.get_running_loop()
    with _STATES_LOCK:
        state = _STATES.pop(loop, None)
    if state is None:
        return
    async with state.lock:
        client = state.client
        state.client = None
        state.db = None
        if client is not None:
            logger.info("closing mongodb client for current loop: reason=%s", reason)
            with contextlib.suppress(Exception):
                await client.close()


def _read_uri() -> str:
    """Resolve the Mongo URI from ASTRABOX_DB_URL / MONGODB_URI / settings.

    The URI comes from the explicit env var or the settings ``db_url`` (a
    ``mongodb://`` URL when the user opts into the [mongo] extra); there is no
    external secret-store resolution.
    """
    global _CACHED_URI
    if _CACHED_URI is not None:
        return _CACHED_URI
    env_uri = str(
        os.getenv("ASTRABOX_DB_URL")
        or os.getenv("ASTRABOX_MONGODB_URI")
        or os.getenv("MONGODB_URI")
        or ""
    ).strip()
    if env_uri and env_uri.startswith("mongodb"):
        _CACHED_URI = env_uri
        return _CACHED_URI
    from astrabox.config.settings import get_settings

    url = str(get_settings().db_url or "").strip()
    if url.startswith("mongodb"):
        _CACHED_URI = url
        return _CACHED_URI
    return ""


def _read_db_name(uri: str) -> str:
    if uri:
        try:
            from pymongo.uri_parser import parse_uri

            parsed = parse_uri(uri)
            database = str(parsed.get("database") or "").strip()
            if database:
                return database
        except Exception:
            pass
    return str(os.getenv("ASTRABOX_DB_NAME") or "astrabox").strip() or "astrabox"


async def _get_database() -> Any:
    uri = _read_uri()
    if not uri:
        return None
    db_name = _read_db_name(uri)
    loop = asyncio.get_running_loop()
    state = _state_for(loop)
    async with state.lock:
        if state.db is not None and state.uri == uri and state.db_name == db_name:
            return state.db
        old_client = state.client
        from pymongo.asynchronous.mongo_client import AsyncMongoClient

        client: Any = AsyncMongoClient(
            uri,
            maxPoolSize=20,
            minPoolSize=2,
            maxIdleTimeMS=45_000,
            connectTimeoutMS=3_000,
            serverSelectionTimeoutMS=3_000,
            socketTimeoutMS=30_000,
            waitQueueTimeoutMS=10_000,
            heartbeatFrequencyMS=5_000,
            retryWrites=True,
            retryReads=True,
            appname="astrabox",
        )
        state.client = client
        state.db = client.get_database(db_name)
        state.uri = uri
        state.db_name = db_name
        logger.info("mongodb client created database=%s (lazy connect)", db_name)
        if old_client is not None:
            with contextlib.suppress(Exception):
                await old_client.close()
        return state.db


_AUTO_RETRY_METHODS = frozenset(
    {
        "insert_one",
        "insert_many",
        "update_one",
        "update_many",
        "replace_one",
        "delete_one",
        "delete_many",
        "find_one",
        "find_one_and_update",
        "find_one_and_replace",
        "find_one_and_delete",
        "count_documents",
    }
)

#: Both the singular and plural index-creation methods must honour the same
#: runtime-index-creation gate: the seam's plural ``create_indexes`` (required
#: by ``AsyncDocumentCollection``, implemented by the sqlite provider) must
#: not bypass the flag and hit pymongo directly.
_INDEX_CREATION_METHODS = frozenset({"create_index", "create_indexes"})


class _RetryingCollectionProxy:
    """Async Mongo collection proxy with automatic transient-error retry."""

    def __init__(self, collection: Any, collection_name: str, run_with_retry: Any) -> None:
        self._collection = collection
        self._collection_name = collection_name
        self._run_with_retry = run_with_retry

    async def _refresh_collection(self) -> Any:
        db = await _get_database()
        if db is not None:
            self._collection = db.get_collection(self._collection_name)
        return self._collection

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._collection, name)
        if (
            name in _INDEX_CREATION_METHODS
            and callable(attr)
            and not runtime_index_creation_enabled()
        ):
            async def _skip_create_index(*args: Any, **kwargs: Any) -> Any:
                logger.debug(
                    "runtime mongo index creation skipped collection=%s method=%s",
                    self._collection_name,
                    name,
                )
                # Mirror each real method's return shape closely enough that a
                # caller iterating/measuring the result doesn't blow up on a
                # no-op: create_index normally returns the index name (str),
                # create_indexes a list of names.
                return [] if name == "create_indexes" else None

            return _skip_create_index
        if not callable(attr) or name not in _AUTO_RETRY_METHODS:
            return attr

        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return await self._run_with_retry(
                f"{self._collection_name}.{name}",
                lambda: getattr(self._collection, name)(*args, **kwargs),
                on_retry=self._refresh_collection,
            )

        return _wrapped


def wrap_collection_with_retry(
    collection_name: str, collection: Any, run_with_retry: Any
) -> Any:
    if collection is None:
        return None
    if isinstance(collection, _RetryingCollectionProxy):
        return collection
    return _RetryingCollectionProxy(collection, collection_name, run_with_retry)


async def get_async_collection(collection_name: str) -> Any:
    """Return the retry-wrapped async Mongo collection (raises if no URI configured).

    Single-arg surface mirroring the sqlite provider's ``get_async_collection(name)``
    — ``..backend`` dispatches to either provider by module, never by name.
    ``run_mongo_with_retry`` is imported from ``..backend`` at call time:
    ``backend.py`` is already fully imported by the time this function runs
    (it is the only caller that resolves this module), so this creates no
    load-time cycle.
    """
    from ..backend import run_mongo_with_retry

    db = await _get_database()
    if db is None:
        from astrabox.common.utils.errors import APIError

        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="mongodb backend selected but no mongodb URI configured "
            "(set ASTRABOX_DB_URL=mongodb://… or ASTRABOX_DB_BACKEND back to postgresql)",
            status_code=503,
        )
    return wrap_collection_with_retry(
        collection_name, db.get_collection(collection_name), run_mongo_with_retry
    )
