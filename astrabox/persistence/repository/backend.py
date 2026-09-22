"""DAL ingress — the single ``get_async_collection`` seam + backend-aware helpers.

This is the one place every metadata access funnels through
(``await get_async_collection(name)`` → an async, Mongo-collection-shaped object
the store-agnostic repository bodies call). It makes the store **pluggable**
while keeping the funnel and the exact public symbol contract the repos + core
import:

    get_async_collection            the collection-shim ingress
    run_mongo_with_retry            backend-aware op wrapper
    collect_async_cursor            async-cursor -> list
    is_mongo_transient_error        backend-aware transient classifier
    mongo_fail_fast_context         fail-fast contextvar token
    mongo_fail_fast_reset           fail-fast contextvar reset
    close_direct_mongo_for_current_loop   lifecycle shutdown hook

Plus it re-exports the persistence *vocabulary* (``ReturnDocument`` + the
``DuplicateKeyError``/``WriteError``/``OperationFailure``/``BulkWriteError`` error
types) from :mod:`._compat`, so the repository bodies import their exception types
from one backend-neutral place instead of unconditionally importing ``pymongo``.

Backend selection (fail-loud, no silent fallback):

* **default ``postgresql``** — the SQLAlchemy+asyncpg JSONB collection adapter
  (:mod:`.postgresql`). A fresh database works with no manual migrations
  (:func:`create_all`).
* **``sqlite``** — retained for isolated compatibility tests, not used by the
  development or container deployment defaults.
* **``mongo``** (opt-in, ``[mongo]`` extra) — the pymongo path
  (:mod:`.mongo`), imported lazily *only when selected* (so the core imports with
  no pymongo installed). Selected by ``ASTRABOX_DB_BACKEND=mongo`` or a
  ``mongodb://`` ``ASTRABOX_DB_URL``.

Every name (the builtins above plus any third-party backend) resolves through
one path — :func:`_resolve_backend`, an entry-point-driven resolver at the
``astrabox.providers.repository`` group (:data:`REPOSITORY_GROUP`) — so the
dispatch functions above never special-case a backend by name, only by the
module/object it hands back. An unknown backend name raises ``RuntimeError``
listing the known names.
"""

from __future__ import annotations

import contextvars
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.retry_utils import retry_async_call
from tenacity import RetryCallState

# Re-export the backend-neutral persistence vocabulary (the repos import these
# exception types + ReturnDocument from here / from ._compat — never from pymongo).
from ._compat import (  # noqa: F401  (re-exported on purpose)
    AutoReconnect,
    BulkWriteError,
    ConnectionFailure,
    DuplicateKeyError,
    NetworkTimeout,
    OperationFailure,
    PyMongoError,
    PYMONGO_AVAILABLE,
    ReturnDocument,
    ServerSelectionTimeoutError,
    WriteError,
)

logger = get_logger(__name__)


#: A persistence op slower than this logs itself (seconds). Crossing it is not
#: a failure: the write funnel is a single queue, so one silent multi-second op
#: starves everything queued behind it.
_SLOW_OP_THRESHOLD_S = float(os.getenv("ASTRABOX_PERSISTENCE_SLOW_OP_S", "1.0") or 1.0)

_T = TypeVar("_T")


def _pool_report() -> str:
    """Pool occupancy for the slow-op line, or ``-`` when there is no pool.

    Slowness at this funnel is often connection-pool contention rather than the
    query itself. The pool's own error says only that it is full, so the line
    has to carry enough occupancy data to identify what is holding it.
    """
    if active_backend_name() not in {"sqlite", "postgresql"}:
        return "-"
    from .sqlite.engine import pool_report

    return pool_report()

__all__ = [
    "get_async_collection",
    "run_mongo_with_retry",
    "collect_async_cursor",
    "is_mongo_transient_error",
    "mongo_fail_fast_context",
    "mongo_fail_fast_reset",
    "close_direct_mongo_for_current_loop",
    "create_all",
    "active_backend_name",
    "REPOSITORY_GROUP",
    # re-exported vocabulary
    "ReturnDocument",
    "DuplicateKeyError",
    "WriteError",
    "OperationFailure",
    "BulkWriteError",
    "PyMongoError",
    "AutoReconnect",
    "ConnectionFailure",
    "NetworkTimeout",
    "ServerSelectionTimeoutError",
]


# --------------------------------------------------------------------------- #
# Fail-fast contextvar (backend-neutral; controls run_mongo_with_retry)         #
# --------------------------------------------------------------------------- #
_RETRY_ACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "astrabox_retry_active", default=False
)
_FAIL_FAST: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "astrabox_fail_fast", default=False
)


# --------------------------------------------------------------------------- #
# Backend selection                                                             #
# --------------------------------------------------------------------------- #
_BUILTIN_BACKENDS = ("postgresql", "sqlite", "mongo")

#: Entry-point group for repository backends — must match the
#: ``[project.entry-points."astrabox.providers.repository"]`` table in pyproject.
REPOSITORY_GROUP = "astrabox.providers.repository"

#: Resolved collection backends, one per name — postgresql, sqlite, mongo, or
#: any third-party plugin, all resolved the same way (see
#: :func:`_resolve_backend`).
#: ``get_async_collection`` call sites must not re-resolve the entry point,
#: rebuild a Mongo client, or rerun ensure_created on every call.
_RESOLVED_BACKENDS: dict[str, Any] = {}


def _resolve_backend(name: str) -> Any:
    """Resolve the persistence backend module for ``name`` (fail-loud).

    One entry-point-driven path resolves every name — the builtins
    ``postgresql``/``sqlite``/``mongo`` included — at the
    ``astrabox.providers.repository`` group (Tier A of the persistence seam —
    see :mod:`astrabox.seams.repository`), using :mod:`astrabox.providers`'s
    shared ``_select_entry_points``/``load_provider`` resolver. Callers never
    special-case a backend by name, only by the module this hands back.

    The resolved object must provide ``get_async_collection(collection_name)``;
    a backend without it cannot serve the shared repository classes and is
    rejected loudly. Resolution is cached per name (process-wide).
    """
    cached = _RESOLVED_BACKENDS.get(name)
    if cached is not None:
        return cached

    # Call-time import: astrabox.providers resolves entry points lazily, so
    # this cannot create a load-time cycle with the persistence package.
    from astrabox.providers import _select_entry_points, load_provider

    available = _select_entry_points(REPOSITORY_GROUP)
    if not available:
        if name not in _BUILTIN_BACKENDS:
            raise RuntimeError(
                f"no repository backends registered under group={REPOSITORY_GROUP!r} "
                f"and {name!r} is not a built-in default ({', '.join(_BUILTIN_BACKENDS)}); "
                "install the package (so entry-points resolve) or the backend's extra"
            )
        # Editable-checkout bridge: a source checkout with no dist-info
        # installed resolves the entry-point group empty even though the
        # in-tree builtin module is right here, so import the named builtin
        # module directly.
        if name == "postgresql":
            from . import postgresql as backend
        elif name == "sqlite":
            from . import sqlite as backend
        else:
            from . import mongo as backend
    else:
        target = load_provider(REPOSITORY_GROUP, name)
        # Entry-points may target a module (the builtins) or a class (a
        # third-party plugin); instantiate only the latter — mirrors
        # astrabox.providers.get_sandbox_provider's own convention.
        backend = target() if isinstance(target, type) else target

    if not callable(getattr(backend, "get_async_collection", None)):
        raise RuntimeError(
            f"persistence backend {name!r} (resolved at the "
            f"{REPOSITORY_GROUP!r} entry-point group) does not provide "
            "get_async_collection(name) — a Tier-A collection backend must "
            "(see astrabox/seams/repository.py); the shared repository classes "
            "cannot run over it"
        )
    _RESOLVED_BACKENDS[name] = backend
    return backend


def active_backend_name() -> str:
    """Resolve the active persistence backend name (fail-loud on an unknown name).

    Precedence: an explicit database URL selects MongoDB or PostgreSQL;
    otherwise ``AstraBoxSettings.db_backend`` (default ``postgresql``). A name that
    is neither builtin must be registered at the
    ``astrabox.providers.repository`` entry-point group (an installed plugin
    distribution); anything else raises.
    """
    raw_url = str(os.getenv("ASTRABOX_DB_URL") or "").strip()
    if raw_url.startswith("mongodb"):
        return "mongo"
    if raw_url.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
        return "postgresql"
    if raw_url.startswith(("sqlite://", "sqlite+aiosqlite://")):
        return "sqlite"

    from astrabox.config.settings import get_settings

    settings = get_settings()
    # db_url on the settings object can also pin mongo even without the env var.
    settings_url = str(getattr(settings, "db_url", "") or "").strip()
    if settings_url.startswith("mongodb"):
        return "mongo"
    if settings_url.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
        return "postgresql"
    if settings_url.startswith(("sqlite://", "sqlite+aiosqlite://")):
        return "sqlite"

    name = str(getattr(settings, "db_backend", "postgresql") or "postgresql").strip().lower()
    if name in ("postgres", "postgresql"):
        name = "postgresql"
    if name not in _BUILTIN_BACKENDS:
        # Not builtin → it must resolve as an installed Tier-A plugin. Resolution
        # is fail-loud: an unknown/uninstalled name raises here with the
        # entry-point group named.
        _resolve_backend(name)
    return name


# --------------------------------------------------------------------------- #
# get_async_collection — the single ingress                                     #
# --------------------------------------------------------------------------- #
async def get_async_collection(collection_name: str) -> Any:
    """Return an async, Mongo-collection-shaped handle for ``collection_name``.

    Dispatches to the active backend's own ``get_async_collection(name)`` — the
    one path every name (builtin or plugin) resolves through (see
    :func:`_resolve_backend`); this function never special-cases a backend by
    name, only by the module it gets back. Default (``postgresql``): an
    :class:`~astrabox.persistence.repository.sqlite.collection.AsyncCollection`
    over PostgreSQL JSONB. Opt-in (``mongo``): the retry-wrapped async
    pymongo collection. The repository bodies are agnostic to which one they
    get — they call the same ``find/insert_one/update_one/aggregate`` surface.
    """
    backend_name = active_backend_name()
    backend = _resolve_backend(backend_name)
    collection = backend.get_async_collection(collection_name)
    if inspect.isawaitable(collection):
        collection = await collection
    return collection


async def create_all() -> None:
    """Create the active backend's base schema when needed (idempotent).

    PostgreSQL creates the shared JSONB document table and its base indexes;
    SQLite does the equivalent for the compatibility test store. Mongo is a
    no-op here because repositories create their own collections and indexes.
    A backend without ``create_all`` has nothing to bootstrap at this ingress.
    Call once at startup.
    """
    backend_name = active_backend_name()
    backend = _resolve_backend(backend_name)
    bootstrap = getattr(backend, "create_all", None)
    if not callable(bootstrap):
        logger.debug("create_all: backend=%s has no ingress-level bootstrap", backend_name)
        return
    result = bootstrap()
    if inspect.isawaitable(result):
        await result


# --------------------------------------------------------------------------- #
# Cursor collection helper                                                      #
# --------------------------------------------------------------------------- #
async def collect_async_cursor(cursor_or_awaitable: Any) -> list[dict[str, Any]]:
    """Collect an async cursor into a list (awaits an awaitable cursor first).

    Works for the SQL adapter's :class:`SqliteCursor` compatibility type
    (async-iterable + awaitable) and pymongo's async awaitable cursors.
    """
    cursor = (
        await cursor_or_awaitable
        if inspect.isawaitable(cursor_or_awaitable)
        else cursor_or_awaitable
    )
    return [doc async for doc in cursor]


# --------------------------------------------------------------------------- #
# Transient-error classification (backend-aware)                                #
# --------------------------------------------------------------------------- #
def is_mongo_transient_error(exc: Exception) -> bool:
    """Classify ``exc`` as a retryable transient error for the active backend.

    Dispatches to the active backend module's own ``is_transient_error(exc)`` —
    one interface, intentionally different implementations per backend:

    * **postgresql** — retries connection loss, pool timeouts, deadlocks, and
      serialization failures.
    * **sqlite** — always ``False``. The shim is local + synchronous-on-a-thread,
      so there is no transient connection class to retry; a SQLite error is a
      real error, surfaced loudly — no retry papering.
    * **mongo** — the pymongo classifier: pymongo connection/timeout errors, the
      uvloop fd-transport error, and a proxied transient-dial ``OperationFailure``.

    A resolved backend without an ``is_transient_error`` (a third-party plugin
    that doesn't define one) is treated as never-transient rather than silently
    inheriting another backend's exception classes.
    """
    backend_name = active_backend_name()
    backend = _resolve_backend(backend_name)
    classifier = getattr(backend, "is_transient_error", None)
    if not callable(classifier):
        return False
    return bool(classifier(exc))


def mongo_fail_fast_context() -> contextvars.Token[bool]:
    """Enter fail-fast mode: ``run_mongo_with_retry`` skips retries until reset."""
    return _FAIL_FAST.set(True)


def mongo_fail_fast_reset(token: contextvars.Token[bool]) -> None:
    """Exit fail-fast mode (pass the token returned by :func:`mongo_fail_fast_context`)."""
    _FAIL_FAST.reset(token)


# --------------------------------------------------------------------------- #
# run_mongo_with_retry — backend-aware op wrapper                               #
# --------------------------------------------------------------------------- #
def _retry_attempts() -> int:
    raw = str(os.getenv("ASTRABOX_MONGO_RETRY_ATTEMPTS", "3")).strip()
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(1, min(value, 6))


def _retry_base_delay_seconds() -> float:
    raw = str(os.getenv("ASTRABOX_MONGO_RETRY_BASE_DELAY_SECONDS", "0.2")).strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.2
    if value <= 0:
        return 0.2
    return min(value, 5.0)


async def run_mongo_with_retry(
    operation: str,
    op: Callable[[], Awaitable[_T]],
    *,
    attempts: int | None = None,
    on_retry: Callable[[], Awaitable[Any]] | None = None,
    fault_context: dict[str, Any] | None = None,
) -> _T:
    """Run ``op`` with bounded transient-error retry (backend-aware).

    Every call site shares this signature. PostgreSQL and Mongo retry only the
    errors their backend classifies as transient. SQLite classifies none, so an
    error propagates immediately. ``fault_context`` is accepted for signature
    parity (no fault-injection hook is wired here).
    """
    _ = fault_context  # parity-only; no fault injection here

    async def _timed_op() -> _T:
        # Every persistence operation passes through this named timing funnel,
        # so a slow operation identifies itself even when it eventually
        # succeeds and emits no exception.
        started = time.monotonic()
        try:
            return await op()
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= _SLOW_OP_THRESHOLD_S:
                logger.warning(
                    "slow persistence op=%s elapsed_s=%.2f pools=%s",
                    operation,
                    elapsed,
                    _pool_report(),
                )

    if _RETRY_ACTIVE.get() or _FAIL_FAST.get():
        return await _timed_op()

    total = attempts if attempts is not None else _retry_attempts()
    if total <= 1:
        return await _timed_op()

    async def _before_retry() -> None:
        # Between attempts: drop the cached client so the retry reconnects, then
        # run the caller's optional hook. (Async work tenacity's before_sleep
        # can't do — see retry_async_call's on_before_retry.)
        await _invalidate_for_active_backend()
        if on_retry is not None:
            await on_retry()

    def _warn(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None and outcome.failed else None
        logger.warning(
            "transient persistence error op=%s attempt=%s/%s err=%s",
            operation,
            retry_state.attempt_number,
            total,
            exc,
        )

    # The loop mechanics (including exponential backoff) are tenacity's, via
    # retry_async_call; the re-entrancy guard, the transient classifier and
    # client invalidation stay on this side. Non-transient errors and the last
    # attempt's failure reraise through the retry predicate and
    # stop_after_attempt + reraise=True.
    token = _RETRY_ACTIVE.set(True)
    try:
        return await retry_async_call(
            _timed_op,
            should_retry_exception=is_mongo_transient_error,
            max_attempts=total,
            wait_seconds=_retry_base_delay_seconds(),
            wait_exponential_max=2.0,
            before_sleep=_warn,
            on_before_retry=_before_retry,
        )
    finally:
        _RETRY_ACTIVE.reset(token)


async def _invalidate_for_active_backend() -> None:
    """Invalidate cached client state on the active backend (mongo only)."""
    if active_backend_name() == "mongo":
        from . import mongo as _mongo

        _mongo.invalidate_current_loop()


# --------------------------------------------------------------------------- #
# Lifecycle shutdown hook (routes to the active backend)                        #
# --------------------------------------------------------------------------- #
async def close_direct_mongo_for_current_loop(
    reason: str = "event_loop_shutdown",
) -> None:
    """Close any backend client owned by the current event loop (shutdown hook).

    PostgreSQL and SQLite dispose their SQLAlchemy engines; Mongo closes its
    per-loop ``AsyncMongoClient``. Dispatches to the active backend module's
    ``close_for_current_loop(reason)``; a resolved backend without one is a
    no-op.
    """
    backend_name = active_backend_name()
    backend = _resolve_backend(backend_name)
    close = getattr(backend, "close_for_current_loop", None)
    if callable(close):
        await close(reason)
