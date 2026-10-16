"""The installation id: one per database, stable across restarts and replicas.

Sandboxes carry the id so an installation can tell its own boxes from another
installation's on a shared Docker daemon. A second read that minted a new id
would orphan every box already created; two databases that shared an id would
reap each other's boxes again.
"""

from __future__ import annotations

import asyncio

import pytest

from astrabox.persistence.installation import load_installation_id

pytestmark = pytest.mark.usefixtures("database")


async def test_one_database_keeps_one_id_across_reads_and_racing_replicas() -> None:
    # Replicas booting a new database at once must all settle on one id.
    first = set(await asyncio.gather(*(load_installation_id() for _ in range(5))))
    again = await load_installation_id()

    assert len(first) == 1
    assert first == {again}
