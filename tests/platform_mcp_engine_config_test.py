"""Deterministic platform MCP projection; URLs belong to the binding renderer."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrabox.core.service.orchestrator.engine.platform_mcp import (
    PLATFORM_MCP_SERVERS_ENV_VAR,
    platform_mcp_environment,
    platform_mcp_servers,
    request_with_platform_mcp,
)
from astrabox.core.service.orchestrator.engine.provisioning import EngineSandboxRequest


SERVERS = {"artifacts": {"type": "http", "url": "http://platform.test/opaque-capability/mcp"}}


def test_platform_projection_consumes_synthesized_output_and_excludes_remote_declarations() -> None:
    template = SimpleNamespace(mcp_servers={
        "preview": {"platform_server": "html_preview"},
        "remote": {"type": "http", "url": "http://user.test/mcp"},
    })
    with patch(
        "astrabox.core.service.orchestrator.engine.platform_mcp.runtime_mcp_servers_for_binding",
        return_value=SERVERS,
    ) as render:
        assert platform_mcp_servers(
            template, deployment_settings=SimpleNamespace(mcp_proxy_base_url="http://platform.test"),
            deployment_id="session-1",
        ) == SERVERS
    assert render.call_args.args == ({"preview": {"platform_server": "html_preview"}},)
    assert render.call_args.kwargs["deployment_id"] == "session-1"


@pytest.mark.parametrize("deployment_id", ["session-1", "mcp_slot-address"])
def test_resident_image_environment_is_composed_before_boot(deployment_id: str) -> None:
    request = EngineSandboxRequest(
        entrypoint=("/boot",), credential=None, platform_mcp_servers_env=True, env={"MODEL_URL": "http://model"},
    )
    with patch(
        "astrabox.core.service.orchestrator.engine.platform_mcp.runtime_mcp_servers_for_binding",
        return_value=SERVERS,
    ):
        bound = request_with_platform_mcp(
            request, template=SimpleNamespace(), deployment_settings=None, deployment_id=deployment_id,
        )
    assert bound.env == {
        "MODEL_URL": "http://model",
        PLATFORM_MCP_SERVERS_ENV_VAR: '[{"name":"artifacts","url":"http://platform.test/opaque-capability/mcp"}]',
    }
    assert request.env == {"MODEL_URL": "http://model"}


def test_platform_environment_rejects_a_non_http_renderer_entry() -> None:
    with pytest.raises(ValueError, match="requires an HTTP URL"):
        platform_mcp_environment({"artifacts": {"type": "stdio", "command": "server"}})
