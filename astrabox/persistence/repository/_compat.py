"""Backend-neutral persistence vocabulary shared by the DAL ingress and repos.

This module is the **single place** the repository layer reaches for the two bits
of "Mongo vocabulary" the store-agnostic repository bodies use directly:

* :class:`ReturnDocument` — the ``return_document=`` sentinel passed to
  ``find_one_and_update`` (the repositories pass only ``ReturnDocument.AFTER``).
* the write/transient **exception types** the repos catch by name
  (``DuplicateKeyError``, ``WriteError``, ``OperationFailure``, ``BulkWriteError``
  and the four transient connection errors).

pymongo is an optional dependency
---------------------------------
``pymongo`` is **not** a core dependency — it lives behind the ``[mongo]``
extra. But the repository bodies must keep ``except DuplicateKeyError``
working whether the active backend is PostgreSQL, the SQLite compatibility
collection, or the optional Mongo collection. So:

* **When ``pymongo`` is installed** (the ``[mongo]`` extra) this module re-exports
  the *real* classes, so exception identity is unchanged and a Mongo
  ``DuplicateKeyError`` raised deep in the driver is caught by the very same
  ``except`` clause.
* **When ``pymongo`` is absent** (the default install) this module defines
  lightweight stand-ins with the same names. The SQLite shim raises *these* on a unique-key
  violation, so the repos' idempotency ``except DuplicateKeyError`` branches fire
  identically. The stand-ins subclass a common base so tuple-catches
  (``except (DuplicateKeyError, WriteError, OperationFailure)``) keep working.

The shim provides only the exception *types*; whether an operation fails is
decided by the backend and surfaced as one of these exceptions, which the repo
then handles or re-raises.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "PYMONGO_AVAILABLE",
    "ReturnDocument",
    "PyMongoError",
    "DuplicateKeyError",
    "WriteError",
    "OperationFailure",
    "BulkWriteError",
    "AutoReconnect",
    "ConnectionFailure",
    "NetworkTimeout",
    "ServerSelectionTimeoutError",
    "TRANSIENT_CONNECTION_ERRORS",
]


try:  # the [mongo] extra is installed -> use the real driver vocabulary verbatim
    from pymongo import ReturnDocument as ReturnDocument  # type: ignore
    from pymongo.errors import (  # type: ignore
        AutoReconnect as AutoReconnect,
        BulkWriteError as BulkWriteError,
        ConnectionFailure as ConnectionFailure,
        DuplicateKeyError as DuplicateKeyError,
        NetworkTimeout as NetworkTimeout,
        OperationFailure as OperationFailure,
        PyMongoError as PyMongoError,
        ServerSelectionTimeoutError as ServerSelectionTimeoutError,
        WriteError as WriteError,
    )

    PYMONGO_AVAILABLE = True

except ModuleNotFoundError:
    # Default install: no pymongo. Provide name-compatible stand-ins so
    # the repository bodies' `except DuplicateKeyError` / tuple-catches still bind.
    PYMONGO_AVAILABLE = False

    class ReturnDocument(int, Enum):  # mirrors pymongo.collection.ReturnDocument
        """``find_one_and_update`` return-shape sentinel (BEFORE/AFTER).

        Subclasses ``int`` exactly like the real enum so any ``int(...)``
        coercion or truthiness check the callers might do is preserved. The
        repositories pass only ``AFTER``; both members are covered by the
        collection conformance suite
        (:mod:`astrabox.testing.collection_conformance`), so a backend has to
        honour ``BEFORE`` too.
        """

        BEFORE = 0
        AFTER = 1

    class PyMongoError(Exception):
        """Base for the stand-in driver exceptions (mirrors ``pymongo.errors.PyMongoError``)."""

    class OperationFailure(PyMongoError):
        """A database operation failed (mirrors ``pymongo.errors.OperationFailure``).

        The stand-in keeps the ``code``/``details`` constructor kwargs the real
        class accepts so any code that inspects ``exc.code`` keeps working.
        """

        def __init__(
            self,
            error: str = "",
            code: int | None = None,
            details: dict | None = None,
        ) -> None:
            super().__init__(error)
            self.code = code
            self.details = details or {}

    class WriteError(OperationFailure):
        """A single write failed (mirrors ``pymongo.errors.WriteError``)."""

    class DuplicateKeyError(WriteError):
        """A unique-index violation (mirrors ``pymongo.errors.DuplicateKeyError``).

        The SQLite collection-shim raises this on an ``_id`` collision or a
        ``create_index(..., unique=True)`` violation, so the repos' idempotent
        insert/claim paths behave exactly as they do against Mongo.
        """

    class BulkWriteError(PyMongoError):
        """A batched write failed (mirrors ``pymongo.errors.BulkWriteError``).

        Carries ``details`` like the real class; the shim populates
        ``details['writeErrors']`` with the offending index on a bulk insert
        collision so ``ordered=True`` callers can react.
        """

        def __init__(self, results: dict | None = None) -> None:
            super().__init__("batch op errors occurred")
            self.details = results or {}

    class ConnectionFailure(PyMongoError):
        """A connection-level failure (mirrors ``pymongo.errors.ConnectionFailure``)."""

    class AutoReconnect(ConnectionFailure):
        """A transient reconnect-needed error (mirrors ``pymongo.errors.AutoReconnect``)."""

    class NetworkTimeout(AutoReconnect):
        """A network timeout (mirrors ``pymongo.errors.NetworkTimeout``)."""

    class ServerSelectionTimeoutError(ConnectionFailure):
        """Server selection timed out (mirrors ``pymongo.errors.ServerSelectionTimeoutError``)."""


#: The transient connection error types the retry/transient classifier treats as
#: retryable. Against SQLite none of these are ever raised (the shim is local and
#: synchronous-on-a-thread), so the classifier simply returns ``False`` — the
#: tuple lets ``isinstance(exc, TRANSIENT_CONNECTION_ERRORS)`` and the
#: repos' ``except (AutoReconnect, NetworkTimeout, ...)`` branches resolve.
TRANSIENT_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    AutoReconnect,
    NetworkTimeout,
    ServerSelectionTimeoutError,
    ConnectionFailure,
)
