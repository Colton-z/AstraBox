"""The console can hide manager-only actions without guessing from copy."""

from __future__ import annotations

import pytest

from astrabox.api.routes.user import get_current_user
from astrabox.common.utils.user_context import (
    UserContext,
    reset_current_user_context,
    set_current_user_context,
)


@pytest.mark.asyncio
async def test_current_user_includes_non_secret_role_names() -> None:
    token = set_current_user_context(
        UserContext(
            "admin-1",
            display_name="Admin One",
            email="admin@example.test",
            roles=["admin"],
        )
    )
    try:
        response = await get_current_user(object())  # type: ignore[arg-type]
    finally:
        reset_current_user_context(token)

    assert response["data"] == {
        "user_id": "admin-1",
        "display_name": "Admin One",
        "email": "admin@example.test",
        "avatar_url": None,
        "roles": ["admin"],
        "is_admin": True,
    }
