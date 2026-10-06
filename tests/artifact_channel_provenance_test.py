"""Channel provenance excludes personal artifact tools before start and on recovery."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.channel_ingress_service import ChannelIngressService
from astrabox.core.service.orchestrator.engine.platform_mcp import platform_mcp_servers
from tests.agent_service_visibility_test import _service


@pytest.mark.asyncio
async def test_channel_creation_records_source_before_startup_and_normal_runs_keep_tools() -> None:
    service, _ = _service()
    service._agent_config = SimpleNamespace(resolve_agent_harness=AsyncMock(return_value=AgentView(agent_id="pub")))
    kernel = SimpleNamespace(create_session=AsyncMock(return_value={"session_id": "new"}))
    service._platform = SimpleNamespace(_session_kernel=kernel)
    user = UserContext("executor")
    for execution_user_id in ("executor", ""):
        await ChannelIngressService._start_channel_conversation(service, user, "pub", execution_user_id)
        assert kernel.create_session.call_args.kwargs["source_type"] == "channel"
    await service.start_conversation(user, "pub")
    assert kernel.create_session.call_args.kwargs["source_type"] == "agent"


@pytest.mark.asyncio
async def test_session_resolution_carries_channel_exclusion_through_start_and_resume() -> None:
    config = AgentConfigService.__new__(AgentConfigService)
    view = AgentView(agent_id="agent", mcp_servers={})
    config.resolve_agent_harness = AsyncMock(return_value=view)
    for _ in range(2):
        harness = await config.resolve_session_harness({"agent_id": "agent", "source_type": "channel"})
        assert platform_mcp_servers(harness, deployment_settings=SimpleNamespace(mcp_proxy_base_url="http://platform.test"), deployment_id="session") == {}
    normal = await config.resolve_session_harness({"agent_id": "agent", "source_type": "agent"})
    assert "artifacts" in platform_mcp_servers(normal, deployment_settings=SimpleNamespace(mcp_proxy_base_url="http://platform.test"), deployment_id="normal")
    assert view.session_source_type is None


@pytest.mark.asyncio
async def test_channel_cannot_claim_a_prepared_process_with_frozen_artifact_tools(monkeypatch) -> None:
    from astrabox.core.service.orchestrator.agent import prepared_slots
    from astrabox.core.service.orchestrator.engine.provisioning import (
        EngineSandboxRequest, claim_prepared_engine_sandbox, _claim_conversation_pool_box,
    )
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(prepared_slots, "claim_prepared_slot", claim)
    template = SimpleNamespace(session_source_type="channel", agent_id="agent", runtime_generation="generation", prewarm_enabled=True)
    assert await claim_prepared_engine_sandbox(None, session_id="channel", assignment_id="assignment", template=template, workspace_plan=None, user_id="viewer", request=EngineSandboxRequest(entrypoint=("/boot",), credential=None), backend_adapter=None, credential="", mcp_vault_write=None, vault_enabled=False, session_log=None) is None
    claim.assert_not_awaited()
    assert await _claim_conversation_pool_box(None, session_id="channel", assignment_id="assignment", template=template, workspace_plan=None, backend_adapter=None, recovered_assignment=None) is None
