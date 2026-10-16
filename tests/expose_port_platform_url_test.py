"""A signed browser URL is minted only for its own user's session."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.expose_port_service import ExposePortService


class _Repo:
    def __init__(self, value: dict | None) -> None:
        self.value = value

    async def get_binding(self, _key: str) -> dict | None:
        return self.value

    async def get_session(self, _key: str) -> dict | None:
        return self.value


def _service(session: dict | None = None, binding: dict | None = None) -> ExposePortService:
    return ExposePortService(
        sessions_repo=_Repo(session),
        binding_repo=_Repo(binding),
    )


@pytest.mark.asyncio
async def test_refresh_does_not_mint_a_url_for_another_user() -> None:
    provider = SimpleNamespace(resolve_browser_endpoint=AsyncMock())
    service = _service(session={"sandbox_id": "sb-1", "user_id": "owner"})
    with (
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.sandbox_for_name",
            return_value=provider,
        ),
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.load_astrabox_settings",
            return_value=SimpleNamespace(sandbox_secure_access_enabled=False),
        ),
        pytest.raises(APIError) as caught,
    ):
        await service.refresh_port_url(
            deployment_id="session-1",
            port=5173,
            user=UserContext(user_id="someone-else"),
        )

    assert caught.value.status_code == 404
    provider.resolve_browser_endpoint.assert_not_awaited()
