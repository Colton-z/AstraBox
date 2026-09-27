"""The installation id: one per database, stable across restarts and replicas.

Sandboxes carry the id so an installation can tell its own boxes from another
installation's on a shared Docker daemon. A second read that minted a new id
would orphan every box already created; two databases that shared an id would
reap each other's boxes again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.persistence.installation import load_installation_id


def _use_database(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(directory))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings() -> Any:
    yield
    get_settings.cache_clear()


async def test_one_database_keeps_one_id_across_reads_and_racing_replicas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_database(monkeypatch, tmp_path / "one")
    # Replicas booting a new database at once must all settle on one id.
    first = set(await asyncio.gather(*(load_installation_id() for _ in range(5))))
    again = await load_installation_id()

    assert len(first) == 1
    assert first == {again}


async def test_two_databases_are_two_installations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_database(monkeypatch, tmp_path / "one")
    one = await load_installation_id()
    _use_database(monkeypatch, tmp_path / "two")
    two = await load_installation_id()

    assert one != two
