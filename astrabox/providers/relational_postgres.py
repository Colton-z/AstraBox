"""PostgreSQL services for the platform's shared relational repositories."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from astrabox.persistence.errors import ForeignKeyViolation, UniqueViolation
from astrabox.providers.postgres import engine
from astrabox.providers.postgres.coordination import PostgresCoordination
from astrabox.providers.relational_statements import PostgresStatements
from astrabox.seams.relational_persistence import RelationalStatements
from astrabox.seams.persistence_coordination import PersistenceCoordination

_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"


class PostgresPersistenceProvider:
    """The existing asyncpg pools, schema upgrades and PostgreSQL coordination."""

    def __init__(self, *, database_url: str, environment_name: str) -> None:
        self._database_url = engine.database_url(database_url)
        self._environment_name = environment_name
        self._coordination = PostgresCoordination(
            database_url=self._database_url, environment_name=environment_name,
        )
        self._statements = PostgresStatements()

    @property
    def environment_name(self) -> str:
        return self._environment_name

    @property
    def database_url(self) -> str:
        return self._database_url

    @property
    def coordination(self) -> PersistenceCoordination:
        return self._coordination

    @property
    def statements(self) -> RelationalStatements:
        return self._statements

    def get_engine(self) -> AsyncEngine:
        return engine.get_engine(self._database_url, environment_name=self._environment_name)

    def pool_report(self, *, holders: int = 5) -> str:
        return engine.pool_report(
            self._database_url, environment_name=self._environment_name, holders=holders,
        )

    async def initialize_schema(self) -> None:
        from astrabox.persistence.sql.migrate import upgrade_to_head

        await upgrade_to_head(self._database_url, environment_name=self._environment_name)

    async def dispose(self) -> None:
        await engine.dispose_engine(self._database_url, environment_name=self._environment_name)

    def translate_error(self, error: IntegrityError) -> Exception:
        return translate_error(error)


def translate_error(exc: IntegrityError) -> Exception:
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(
        getattr(original, "__cause__", None), "sqlstate", None
    )
    constraint = getattr(original, "constraint_name", None) or getattr(
        getattr(original, "__cause__", None), "constraint_name", None
    )
    if sqlstate == _UNIQUE_VIOLATION:
        return UniqueViolation(constraint)
    if sqlstate == _FOREIGN_KEY_VIOLATION:
        return ForeignKeyViolation(constraint)
    return exc
