"""Relational driver, schema and coordination boundary.

Repository statements and atomic business recipes remain in the platform.
Providers supply SQLAlchemy engines and native coordination; they do not replace
repositories or introduce another statement language.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from sqlalchemy import ColumnElement, Table
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.dml import Insert

from astrabox.seams.persistence_coordination import PersistenceCoordination


class RelationalStatements(Protocol):
    """Only native conflict mechanics; callers retain all business predicates.

    The actual connection belongs to the caller. Implementations must not
    commit, retry, replace that connection or suppress unrelated violations.
    Existing SQLAlchemy compilation remains the expression/type extension path.
    """

    def upsert_primary_key(
        self, connection: AsyncConnection, table: Table,
        rows: dict[str, Any] | list[dict[str, Any]], *,
        update_columns: Sequence[str], update_values: Mapping[str, Any] | None = None,
    ) -> Insert:
        """Construct an upsert targeting precisely the declared primary key."""
        ...

    def insert_if_absent_primary_key(
        self, connection: AsyncConnection, table: Table,
        rows: dict[str, Any] | list[dict[str, Any]],
    ) -> Insert:
        """Ensure PK-only rows exist without an insertion outcome claim."""
        ...

    async def insert_if_absent(
        self, connection: AsyncConnection, table: Table, values: dict[str, Any], *,
        index_columns: Sequence[str], where: ColumnElement[bool] | None = None,
    ) -> bool:
        """Insert or prove the exact expected conflict; False for ineligibility.

        Evaluate where in the insert statement, without separate prelocking.
        Preserve NULL-distinct target semantics and reject unknown constraints.
        Duplicate readback is not an exclusive mutation guard.
        """
        ...


class RelationalPersistenceProvider(Protocol):
    """The physical database services required by shared relational repositories."""

    @property
    def database_url(self) -> str:
        """The configured native URL, normalized only by this provider."""
        ...

    @property
    def coordination(self) -> PersistenceCoordination:
        """Transaction-visible notices and runtime ownership for this database."""
        ...

    @property
    def statements(self) -> RelationalStatements:
        """Native mechanics for the three shared conflict operations."""
        ...

    def get_engine(self) -> AsyncEngine:
        """Return the same repository engine for this event loop/configuration.

        Coordination owns the resources needed for its lifetime and capacity
        guarantees; callers do not select its pool through this method.
        """
        ...

    def pool_report(self, *, holders: int = 5) -> str:
        """Pool diagnostics without borrowing a connection."""
        ...

    async def initialize_schema(self) -> None:
        """Initialize this build's schema, serializing concurrent replicas."""
        ...

    async def dispose(self) -> None:
        """Close this event loop's engines after its users have stopped."""
        ...

    def translate_error(self, error: IntegrityError) -> Exception:
        """Classify native constraints; return an unknown error unchanged.

        Never classify an ambiguous commit as a definite rollback or retry work.
        """
        ...
