"""Codex native platform MCP config and empty-form tool approvals."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrabox.core.service.orchestrator.engine.codex import _engine_option, _thread_config
from astrabox.core.service.orchestrator.engine.codex_events import (
    approval_response_value,
    build_approval_contract,
)
from astrabox.core.service.orchestrator.engine.platform_mcp import startup_mcp_deployment_id
from astrabox.core.service.orchestrator.runtime.mcp_servers import prepared_slot_mcp_deployment_id


@pytest.mark.parametrize("prepared", [False, True])
def test_native_thread_config_uses_the_cold_or_claimed_binding(prepared: bool) -> None:
    context = SimpleNamespace(
        session_id="session-1", platform_mcp_deployment_id=None,
        prepared_manifest={"slot_id": "slot-1"} if prepared else None,
    )
    expected_id = prepared_slot_mcp_deployment_id("slot-1") if prepared else "session-1"
    servers = {"artifacts": {"type": "http", "url": "http://platform.test/opaque/mcp"}}
    template = SimpleNamespace(engine_options={"config": {"approval_policy": "never"}})
    with patch(
        "astrabox.core.service.orchestrator.engine.platform_mcp.runtime_mcp_servers_for_binding",
        return_value=servers,
    ) as render:
        config = _thread_config(
            template, deployment_settings=None, deployment_id=startup_mcp_deployment_id(context),
        )
    assert render.call_args.kwargs["deployment_id"] == expected_id
    assert config == {
        "approval_policy": "never",
        "mcp_servers": {"artifacts": {"url": "http://platform.test/opaque/mcp", "required": True}},
    }


@pytest.mark.parametrize("key", ["mcp_servers", "mcp_servers.artifacts.url"])
def test_user_cannot_override_platform_mcp_config(key: str) -> None:
    with pytest.raises(ValueError, match="managed by AstraBox"):
        _engine_option(SimpleNamespace(engine_options={"config": {key: {}}}), "config")


@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
def test_mcp_tool_approval_uses_native_elicitation_actions(action: str) -> None:
    method = "mcpServer/elicitation/request"
    params = {
        "threadId": "thread-1", "turnId": "turn-1", "serverName": "artifacts",
        "mode": "form", "message": "Allow artifacts to create a page?",
        "requestedSchema": {"type": "object", "properties": {}},
    }
    contract = build_approval_contract(method, params)
    assert contract["prompt"] == params["message"]
    assert {choice["id"] for choice in contract["options"]} == {"accept", "decline", "cancel"}
    assert approval_response_value(method, params, {"decision": action}) == {
        "action": action, "content": {} if action == "accept" else None, "_meta": None,
    }
