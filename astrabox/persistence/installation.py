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
from sqlalchemy.dialects.postgresql import insert

from astrabox.persistence.sql import fetch_value
from astrabox.persistence.sql.models.platform import Installation

_INSTALLATION = Installation.__table__


async def load_installation_id() -> str:
    """Return this database's installation id, creating it on first use.

    Replicas starting against a new database race to insert the row; the
    losers' inserts do nothing, and each reads back the winner's id.
    """
    stored = await fetch_value(select(_INSTALLATION.c.installation_id))
    if stored:
        return str(stored)
    created = await fetch_value(
        insert(_INSTALLATION)
        .values(installation_id=uuid.uuid4().hex)
        .on_conflict_do_nothing(index_elements=[_INSTALLATION.c.singleton])
        .returning(_INSTALLATION.c.installation_id)
    )
    if created:
        return str(created)
    # Another replica inserted first; its row is committed by the time this
    # insert reports the conflict, so a new read sees it.
    stored = await fetch_value(select(_INSTALLATION.c.installation_id))
    if not stored:
        raise RuntimeError(
            "the installation row disappeared while this replica read it; restore it "
            "from a backup of this database rather than generating a new id, which "
            "would orphan every sandbox this installation created"
        )
    return str(stored)


__all__ = ["load_installation_id"]
