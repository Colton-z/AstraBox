"""The installation id: which AstraBox installation a database belongs to.

One document in the ``installation`` collection holds a random id, generated
the first time an installation's database is used. Every replica of the
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

from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository._compat import DuplicateKeyError
from astrabox.persistence.repository.backend import get_async_collection

INSTALLATION_COLLECTION = "installation"
_INSTALLATION_DOC_ID = "installation"


async def load_installation_id() -> str:
    """Return this database's installation id, creating it on first use.

    Replicas starting against a new database race to insert the document;
    the loser's duplicate-key error means the winner's id is already stored,
    so it reads that one back.
    """
    collection = await get_async_collection(INSTALLATION_COLLECTION)
    existing = await collection.find_one({"_id": _INSTALLATION_DOC_ID})
    if existing is None:
        try:
            await collection.insert_one(
                {
                    "_id": _INSTALLATION_DOC_ID,
                    "installation_id": uuid.uuid4().hex,
                    "created_at": utcnow_iso(),
                }
            )
        except DuplicateKeyError:
            pass
        existing = await collection.find_one({"_id": _INSTALLATION_DOC_ID})
    installation_id = str((existing or {}).get("installation_id") or "").strip()
    if not installation_id:
        raise RuntimeError(
            f"the {INSTALLATION_COLLECTION!r} document holds no installation_id; "
            "restore it from a backup of this database rather than generating a "
            "new one, which would orphan every sandbox this installation created"
        )
    return installation_id


__all__ = ["INSTALLATION_COLLECTION", "load_installation_id"]
