"""AstraBox's PostgreSQL store.

* :mod:`.engine`: the shared engine and pool diagnostics.
* :mod:`.base`: the declarative base, naming conventions and time conversions.
* :mod:`.ops`: statement execution with cancellation shielding and constraint errors.
* :mod:`.migrate`: schema upgrades at server start.
* :mod:`.models`: every table, one module per domain.
"""

from __future__ import annotations

from astrabox.persistence.sql.base import Base, from_iso, to_iso
from astrabox.persistence.sql.ops import (
    ForeignKeyViolation,
    UniqueViolation,
    execute,
    fetch_all,
    fetch_one,
    fetch_value,
    transaction,
)

__all__ = [
    "Base",
    "ForeignKeyViolation",
    "UniqueViolation",
    "execute",
    "fetch_all",
    "fetch_one",
    "fetch_value",
    "from_iso",
    "to_iso",
    "transaction",
]
