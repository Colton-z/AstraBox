"""Shared unique-index verification — the one place a create_index attempt
becomes a hard guarantee that the index is actually enforced.

Background: Mongo's collection proxy (:mod:`.mongo`) intercepts every
``create_index``/``create_indexes`` call and, when
``ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED`` is false, turns it into a no-op —
a managed/shared Mongo cluster may forbid or centrally control index
provisioning, so a self-hosted process must not always assume it can create
indexes itself. SQLite has no such gate: its ``create_index`` always builds a
real index (see ``sqlite/collection.py``). "``create_index`` didn't raise" is
therefore not the same thing as "the index exists".

:func:`ensure_unique_index` is the shared helper every unique-index repo calls
instead of a bare ``collection.create_index(..., unique=True)``:

* It always attempts the create (benign "already exists" driver errors are
  swallowed; anything else is logged and does not abort — the verification
  step below is the actual authority, not whether ``create_index`` itself
  raised).
* It then verifies presence via ``list_indexes()``, matching by normalized
  key-spec — never by name, since a caller may leave ``name`` unset and let
  the backend auto-generate one, and SQLite/Mongo pick *different* default
  names for the identical logical index.
* Index present → returns normally. Index absent → raises ``RuntimeError``
  naming the collection, the key spec, and the env var, so a misconfigured
  deployment fails loud the first time it is used rather than silently
  admitting duplicate "unique" rows forever.

This module never branches on the flag itself. On Mongo the create attempt is
either real (flag on) or a no-op absorbed by :mod:`.mongo`'s proxy (flag off)
before verification ever runs; on SQLite the create attempt is unconditionally
real, so verification always finds the index and the flag has no effect there.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

from astrabox.common.logger.logger_factory import get_logger

from ._compat import OperationFailure

logger = get_logger(__name__)

__all__ = [
    "ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED",
    "runtime_index_creation_enabled",
    "ensure_unique_index",
]

#: The env var name itself, kept as one constant so both the flag reader below
#: and every error message that tells an operator what to set stay in sync.
ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED = "ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED"


def runtime_index_creation_enabled() -> bool:
    """Whether this process should attempt to create missing indexes.

    Default **true** (community self-hosted deployments get real unique-index
    enforcement out of the box on every backend, with no env var to discover
    first). Set to ``false`` to opt out — for a managed-Mongo operator whose
    DBAs pre-create indexes out-of-band (or centrally forbid ad hoc index
    creation from the application). Either way, :func:`ensure_unique_index`
    still verifies the index is actually there and refuses to proceed
    silently if it is not.
    """
    raw = str(
        os.getenv(ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED, "true")
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _normalize_index_keys(key_spec: Any) -> list[tuple[str, int]]:
    """Normalize a ``create_index``-style ``keys`` argument or a reported ``key`` mapping.

    Accepts a bare field name (``"session_id"``), a list of ``(field,
    direction)`` pairs, or a mapping (what ``list_indexes()`` reports back) —
    returns the same ``[(field, direction), ...]`` shape either way, so a
    caller's creation argument and a driver-reported spec can be compared for
    equality regardless of which of these shapes each happened to arrive in.
    """
    if isinstance(key_spec, str):
        return [(key_spec, 1)]
    if hasattr(key_spec, "items"):
        return [(str(field), int(direction)) for field, direction in key_spec.items()]
    try:
        return [(str(field), int(direction)) for field, direction in key_spec]
    except Exception:
        return []


async def _open_index_specs(collection: Any) -> list[dict[str, Any]]:
    """Normalize ``collection.list_indexes()`` into a plain list of spec dicts.

    Handles every shape a Tier-A-conformant collection may hand back: a plain,
    already-materialized list (SQLite), an awaitable resolving to one, or an
    async-iterable cursor (pymongo).
    """
    list_indexes = getattr(collection, "list_indexes", None)
    if not callable(list_indexes):
        return []
    specs = list_indexes()
    if inspect.isawaitable(specs):
        specs = await specs
    if hasattr(specs, "__aiter__"):
        collected: list[dict[str, Any]] = []
        async for spec in specs:
            collected.append(spec)
        return collected
    return list(specs)


def _is_benign_create_error(exc: Exception) -> bool:
    """An index-creation error that's safe to ignore because the index already exists.

    This module is imported by the repository classes (never the reverse), so
    it cannot reuse ``session_repository._safe_create_index`` /
    ``message_repository``'s equivalent local check without an import cycle.
    """
    if not isinstance(exc, OperationFailure):
        return False
    message = str(exc)
    return (
        "Duplicate entry" in message
        or "idx_indexname" in message
        or "already exists" in message
        or "IndexOptionsConflict" in message
    )


async def _unique_index_present(collection: Any, keys: list[tuple[str, int]]) -> bool:
    """True iff a unique index over exactly ``keys`` is reported by ``list_indexes()``.

    Matches by normalized key-spec, never by name: callers often pass no
    explicit ``name``, and SQLite and Mongo each auto-generate a different
    default name for the identical logical index, so key-spec is the only
    backend-agnostic way to recognise "yes, this is the intended index".
    ``sparse``/``partialFilterExpression`` are not compared: SQLite's
    ``list_indexes()`` does not echo ``partialFilterExpression`` back at all
    (see ``sqlite/collection.py``'s ``_IndexRegistry.register_spec``), so
    requiring it to match would make this check fail on SQLite for a partial
    unique index.
    """
    from .backend import run_mongo_with_retry

    async def _list() -> list[dict[str, Any]]:
        return await _open_index_specs(collection)

    try:
        specs = await run_mongo_with_retry("index_verification.list_indexes", _list)
    except Exception as exc:
        logger.warning("list_indexes failed while verifying a unique index: %s", exc)
        return False
    for spec in specs:
        if not isinstance(spec, dict) or not spec.get("unique"):
            continue
        if _normalize_index_keys(spec.get("key")) == keys:
            return True
    return False


async def ensure_unique_index(
    collection: Any,
    keys: Any,
    *,
    collection_name: str,
    name: str | None = None,
    **create_kwargs: Any,
) -> None:
    """Create (if enabled) a unique index, then verify it is actually enforced.

    ``keys``/``name``/``create_kwargs`` (``sparse``, ``partialFilterExpression``,
    …) are passed straight through to ``collection.create_index`` exactly as
    any ordinary ``create_index(..., unique=True)`` call would use them —
    this changes nothing about how the index itself gets created. It then
    verifies via ``list_indexes()`` that the index is actually there, and
    raises loud if it is not, rather than trusting that ``create_index`` not
    raising means the index exists.

    Uniform regardless of backend or the
    ``ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED`` flag:

    * **flag on** (the default) — ``create_index`` actually runs; verify
      confirms it landed.
    * **flag off** (explicit opt-out, e.g. a managed Mongo cluster whose DBAs
      pre-create indexes out-of-band) — Mongo's proxy turns the create
      attempt into a no-op; verify then checks whether the index was already
      there. Present → returns normally. Absent → raises.

    Raises:
        RuntimeError: the index is missing after the attempt — names the
            collection, the key spec (and ``name`` when given), and the env
            var that controls whether astrabox may create it itself.
    """
    normalized_keys = _normalize_index_keys(keys)
    kwargs: dict[str, Any] = dict(create_kwargs)
    if name is not None:
        kwargs["name"] = name

    create_error: Exception | None = None
    try:
        await collection.create_index(keys, unique=True, **kwargs)
    except Exception as exc:
        create_error = exc
        log = logger.debug if _is_benign_create_error(exc) else logger.warning
        log(
            "unique index create_index attempt collection=%s keys=%s: %s",
            collection_name,
            normalized_keys,
            exc,
        )

    if await _unique_index_present(collection, normalized_keys):
        return

    detail = f" (create_index raised: {create_error})" if create_error is not None else ""
    raise RuntimeError(
        f"required unique index is missing: collection={collection_name!r} "
        f"fields={normalized_keys!r}"
        + (f" name={name!r}" if name else "")
        + f"{detail}. Set {ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED}=true (the default) to "
        "let astrabox create it automatically, or pre-create it out-of-band on the "
        f"managed database and keep {ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED}=false."
    )
