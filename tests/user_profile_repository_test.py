"""A user's display name is recorded once, however many first writes race."""

from __future__ import annotations

import asyncio

import pytest

from astrabox.persistence.repository.user_profile_repository import UserProfileRepository

pytestmark = pytest.mark.usefixtures("database")


async def test_concurrent_first_writes_keep_one_profile_without_failing() -> None:
    # A user's first conversations start together; each records the profile.
    repo = UserProfileRepository()
    await asyncio.gather(*(
        repo.upsert_if_absent("user-1", f"Name {index}") for index in range(48)
    ))
    await repo.upsert_if_absent("user-1", "Renamed later")

    names = await repo.batch_get_display_names(["user-1"])
    assert names["user-1"].startswith("Name "), names
