"""The resident harness reads platform MCP bindings before cold or slot boot."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrabox.core.service.orchestrator.engine.deepseek_harness import DeepSeekHarnessEngineAdapter
from astrabox.core.service.orchestrator.engine.platform_mcp import (
    PLATFORM_MCP_SERVERS_ENV_VAR,
    request_with_platform_mcp,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import prepared_slot_mcp_deployment_id


@pytest.mark.parametrize("prepared", [False, True])
def test_harness_boot_environment_contains_the_session_or_slot_address(prepared: bool) -> None:
    template = SimpleNamespace()
    adapter = DeepSeekHarnessEngineAdapter()
    request = adapter.sandbox_request(
        template=template, model_access=SimpleNamespace(base_url="https://model.test"),
    )
    assert request.platform_mcp_servers_env is True
    deployment_id = prepared_slot_mcp_deployment_id("slot-1") if prepared else "session-1"
    url = "http://platform.test/opaque-capability/mcp"
    with patch(
        "astrabox.core.service.orchestrator.engine.platform_mcp.runtime_mcp_servers_for_binding",
        return_value={"artifacts": {"type": "http", "url": url}},
    ) as render:
        bound = request_with_platform_mcp(
            request, template=template, deployment_settings=None, deployment_id=deployment_id,
        )
    assert bound.env == {
        **request.env,
        PLATFORM_MCP_SERVERS_ENV_VAR: '[{"name":"artifacts","url":"' + url + '"}]',
    }
    assert render.call_args.kwargs["deployment_id"] == deployment_id
    assert PLATFORM_MCP_SERVERS_ENV_VAR not in request.env
