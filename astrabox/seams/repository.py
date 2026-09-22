"""Persistence seams — the collection primitive for pluggable stores.

There is ONE store contract. :class:`AsyncDocumentCollection` below is the
MINIMAL store primitive: a Mongo-collection-shaped document store with one hard
semantic requirement (CAS atomicity — see the class docstring). Every concrete
repository class in ``astrabox/persistence/repository/`` (sessions, turn
checkpoints, templates, vaults, journals — and with them ALL coordination logic:
turn locks, attach leases, epoch fencing, interrupt flags) is written ONCE over
this primitive via the ``get_async_collection()`` ingress. A backend that
supplies a conforming collection gets every repository — and the correctness of
the whole control plane's locking — for free. Conformance is testable: run the
reusable suite in ``astrabox/testing/collection_conformance.py`` (bound in-tree
at ``tests/sqlite_collection_conformance_test.py``) against your implementation.

A backend that cannot present a document-collection face (e.g. strict
normalized tables) implements the concrete repository classes in
``astrabox/persistence/repository/`` directly. No generic typed contract backs
that path, so the implementation must guarantee exactly-one-winner semantics.
Those coordination methods are ordinary ownership-scoped CAS
reads/writes over the same primitive: e.g. ``SessionRepository.get_owned_session``
folds the owner into the query filter so a wrong-owner lookup and a missing
session are indistinguishable (``None``), atomically — exactly as the
collection's guarded ``find_one_and_update`` admits one winner.

Registration happens at the ``astrabox.providers.repository`` entry-point
group; ``postgresql``, ``sqlite``, and ``mongo`` are real registrants there
(resolved through the identical path as any third-party plugin — no
backend-name special-casing). The resolved backend object must provide
``get_async_collection(name)``; ``create_all()`` is optional (a no-op for a
backend with nothing to bootstrap at the ingress,
``astrabox/persistence/repository/backend.py``). There is no alternative
``sessions()`` / ``turns()`` / ``templates()`` builder-object shape, and a
backend without ``get_async_collection`` is rejected loudly, never silently
accepted some other way. Backend-NAME selection (``ASTRABOX_DB_BACKEND=<name>`` / a
``mongodb://`` URL) is separate from this resolution path and stays fail-loud
on an unknown name.

Documents are plain ``dict[str, Any]`` at this boundary (the store maps them to
rows/JSON underneath). Construction (``__init__``) and store-specific internals
(cursor encoders, index helpers) are deliberately not part of the contract.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

__all__ = [
    "AsyncDocumentCollection",
]


@runtime_checkable
class AsyncDocumentCollection(Protocol):
    """The minimal store primitive — an async Mongo-collection-shaped namespace.

    This is the exact surface the store-agnostic repository bodies call (the
    reference implementation is
    ``astrabox/persistence/repository/sqlite/collection.py``). Beyond the method
    signatures, a conforming implementation MUST honour these semantics:

    * **CAS atomicity (load-bearing).** ``find_one_and_update`` and a filtered
      ``update_one`` are atomic read-modify-writes: under concurrent callers, a
      guard filter (e.g. ``{"owner": None}`` or ``{"lease_until": {"$lt": now}}``)
      admits EXACTLY ONE winner, and ``$inc`` never collapses (N concurrent
      increments advance the value by N, each observing a distinct result).
      Every turn lock, attach lease and epoch fence in the kernel reduces to
      this property.
    * **Operator subset.** Filters support ``$eq`` (implicit), ``$ne``, ``$in``,
      ``$nin``, ``$gt``/``$gte``/``$lt``/``$lte``, ``$exists``, ``$type``,
      ``$or``/``$and``, and dotted field paths. Updates support ``$set`` (with
      dotted paths — the write must persist even when the dotted parent already
      exists), ``$inc``, ``$setOnInsert``, and whole-document replacement. An
      unsupported operator must raise, never silently mis-match.
    * **Unique indexes.** ``create_index(..., unique=True)`` + a violating write
      raises the backend's ``DuplicateKeyError``
      (``astrabox.persistence.repository._compat``) — the repos' idempotency
      guards catch exactly that type.
    * **Natural order.** A query with no sort returns documents in stable
      insertion order.
    * **Verbatim documents.** Reads return the stored document as a fresh
      ``dict`` (mutating a returned doc must not mutate the store).
    """

    def find(
        self,
        filter: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
    ) -> Any:
        """Chainable async cursor (``.sort()/.skip()/.limit()``, async-iterable)."""
        ...

    async def find_one(
        self,
        filter: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
        *,
        sort: Sequence[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None: ...

    async def insert_one(self, document: dict[str, Any]) -> Any: ...

    async def insert_many(
        self, documents: Sequence[dict[str, Any]], *, ordered: bool = True
    ) -> Any: ...

    async def update_one(
        self,
        filter: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> Any:
        """Atomic guarded update; result exposes ``matched_count``/``modified_count``."""
        ...

    async def update_many(
        self, filter: dict[str, Any], update: dict[str, Any]
    ) -> Any: ...

    async def delete_one(self, filter: dict[str, Any]) -> Any: ...

    async def delete_many(self, filter: dict[str, Any]) -> Any: ...

    async def count_documents(self, filter: dict[str, Any] | None = None) -> int: ...

    async def find_one_and_update(
        self,
        filter: dict[str, Any],
        update: dict[str, Any],
        *,
        return_document: Any = ...,
        sort: Sequence[tuple[str, int]] | None = None,
        projection: dict[str, Any] | None = None,
        upsert: bool = False,
    ) -> dict[str, Any] | None:
        """The CAS primitive: atomically match, update, and return before/after."""
        ...

    def aggregate(self, pipeline: list[dict[str, Any]]) -> Any:
        """Cursor over paging stages and scalar-field ``$sum: 1`` groups."""
        ...

    async def create_index(self, keys: Any, **kwargs: Any) -> Any: ...

    async def create_indexes(self, models: Sequence[Any]) -> Any: ...

    async def list_indexes(self) -> Any: ...
