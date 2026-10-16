"""Statement execution shared by the artifact repositories.

An artifact write runs several statements in one transaction
(:func:`astrabox.persistence.sql.transaction`) under the lock on the artifact's
row. Repository methods that take part in such a write accept the
transaction's connection as ``conn`` and run on it; without one, the statement
runs on its own in autocommit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Executable

from astrabox.persistence.sql import execute, fetch_all, fetch_one


async def one(statement: Executable, conn: AsyncConnection | None) -> dict[str, Any] | None:
    """The first row ``statement`` returns, or ``None``."""
    if conn is None:
        return await fetch_one(statement)
    row = (await conn.execute(statement)).first()
    return dict(row._mapping) if row is not None else None


async def every(statement: Executable, conn: AsyncConnection | None) -> list[dict[str, Any]]:
    """Every row ``statement`` returns."""
    if conn is None:
        return await fetch_all(statement)
    return [dict(row._mapping) for row in (await conn.execute(statement)).all()]


async def run(statement: Executable, conn: AsyncConnection | None) -> int:
    """Run ``statement``; the number of rows it affected."""
    if conn is None:
        return await execute(statement)
    return int((await conn.execute(statement)).rowcount)
