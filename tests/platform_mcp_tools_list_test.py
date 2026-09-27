"""The platform MCP server answers the requests its client sends.

`tools/list` follows initialization, and a helper signature error appears as a
JSON-RPC error inside an HTTP 200 response. These tests invoke the request
helpers through the class exactly as the route does.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.platform_mcp_service import PlatformMCPService


def test_tools_list_describes_the_platform_capability() -> None:
    server = {
        "deployment_id": "dep-1",
        "server_name": "preview",
        "platform_server": "html_preview",
        "allowed_tools": {"expose_port"},
    }

    tools = PlatformMCPService._tools(server)

    assert [tool["name"] for tool in tools] == ["expose_port"]
    assert all("inputSchema" in tool for tool in tools)


def test_every_request_helper_stays_callable_off_the_class() -> None:
    """All four request helpers support the route's unbound class calls.

    Calling every helper through ``PlatformMCPService`` verifies their static
    method descriptors as well as their return shapes.
    """
    server = {"allowed_tools": {"expose_port"}}

    assert isinstance(PlatformMCPService._tools(server), list)
    assert PlatformMCPService._result(1, {"ok": True})["id"] == 1
    assert PlatformMCPService._error(2, -32601, "nope")["error"]["code"] == -32601
    assert PlatformMCPService._tool_result(3, {"x": 1})["id"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["expose_port", "publish_html_preview"])
@pytest.mark.parametrize("arguments", [{}, {"path": "index.html"}, {"port": "8080"}])
async def test_preview_tool_requires_an_explicit_integer_port(
    tool_name: str, arguments: dict[str, object]
) -> None:
    expose = AsyncMock()
    service = PlatformMCPService(
        sessions_repo=None,
        agent_config=None,
        binding_repo=None,
        expose_port_service=expose,
    )
    response = await service._handle_tools_call(
        1,
        {"deployment_id": "dep-1", "allowed_tools": {tool_name}},
        {"params": {"name": tool_name, "arguments": arguments}},
    )

    assert response["error"]["code"] == -32602
    assert "port is required" in response["error"]["message"]
    expose.expose_port.assert_not_awaited()
