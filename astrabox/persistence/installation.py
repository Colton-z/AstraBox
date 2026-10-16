"""The installation id: which AstraBox installation a database belongs to.

The one row of the ``installation`` table holds a random id, generated the
first time an installation's database is used. Every replica of the
installation reads the same database, so they share the id, and it survives
restarts and upgrades for as long as the database does. A second installation
has its own database and therefore its own id.

Sandboxes carry the id in their create metadata
(:data:`astrabox.seams.sandbox.SANDBOX_INSTALLATION_METADATA_KEY`), which is
how an installation tells its own boxes from another installation's when both
share one Docker daemon or Kubernetes namespace.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import fetch_value, transaction
from astrabox.persistence.sql.models.platform import Installation
from astrabox.persistence.sql.statements import insert_if_absent_primary_key

_INSTALLATION = Installation.__table__


async def load_installation_id() -> str:
    """Return this database's installation id, creating it on first use.

    Replicas starting against a new database race to insert the singleton;
    each reads the winner's id on the same transaction connection.
    """
    stored = await fetch_value(select(_INSTALLATION.c.installation_id))
    if stored:
        return str(stored)
    async def create(conn: AsyncConnection) -> str:
        await conn.execute(insert_if_absent_primary_key(
            conn, _INSTALLATION,
            {"singleton": True, "installation_id": uuid.uuid4().hex},
        ))
        identity = (await conn.execute(
            select(_INSTALLATION.c.installation_id).with_for_update(read=True)
        )).scalar_one_or_none()
        if not identity:
            raise RuntimeError(
                "the installation row disappeared while this replica read it; restore it "
                "from a backup of this database rather than generating a new id, which "
                "would orphan every sandbox this installation created"
            )
        return str(identity)

    return await transaction(create)


__all__ = ["load_installation_id"]
