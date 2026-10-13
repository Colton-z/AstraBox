"""A user's first conversations may record the profile concurrently."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from astrabox.persistence.repository import user_profile_repository
from astrabox.persistence.repository.sqlite.collection import AsyncCollection
from astrabox.persistence.repository.sqlite.engine import create_all, dispose_engines
from astrabox.persistence.repository.user_profile_repository import UserProfileRepository
from tests._postgresql_support import resolve_postgresql_test_url


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        # Concurrent inserts race only across real database transactions.
        pytest.param("postgresql", id="postgresql", marks=pytest.mark.postgresql),
    ]
)
async def repo(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[UserProfileRepository]:
    url = (
        resolve_postgresql_test_url()
        if request.param == "postgresql"
        else f"sqlite+aiosqlite:///{tmp_path}/profiles.sqlite"
    )
    await create_all(url)
    collection = AsyncCollection(f"profiles_{uuid.uuid4().hex[:12]}", url)

    async def open_collection(name: str) -> AsyncCollection:
        return collection

    monkeypatch.setattr(user_profile_repository, "get_async_collection", open_collection)
    try:
        yield UserProfileRepository()
    finally:
        await collection.delete_many({})
        await dispose_engines(url)


async def test_concurrent_first_writes_keep_one_profile_without_failing(
    repo: UserProfileRepository,
) -> None:
    await asyncio.gather(*(
        repo.upsert_if_absent("user-1", f"Name {index}") for index in range(48)
    ))
    await repo.upsert_if_absent("user-1", "Renamed later")

    names = await repo.batch_get_display_names(["user-1"])
    assert names["user-1"].startswith("Name "), names
