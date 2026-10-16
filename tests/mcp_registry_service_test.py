"""An organization's MCP server names stay unique when registrations race."""

from __future__ import annotations

import asyncio

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.mcp_registry_service import MCPRegistryService

pytestmark = pytest.mark.usefixtures("database")

_ADMIN = UserContext(user_id="admin", roles=["admin"])


async def test_two_concurrent_registrations_of_one_name_leave_one_server() -> None:
    service = MCPRegistryService()

    async def register() -> dict | APIError:
        try:
            return await service.create_server(
                _ADMIN, name="search", url="https://mcp.example.test/search",
                transport="streamable_http",
            )
        except APIError as exc:
            return exc

    outcomes = await asyncio.gather(*(register() for _ in range(8)))

    created = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, APIError)]
    assert len(created) == 1
    assert {error.code for error in refused} == {"MCP_SERVER_NAME_EXISTS"}
    assert {error.status_code for error in refused} == {409}
    assert [server["name"] for server in await service.list_servers(_ADMIN)] == ["search"]
