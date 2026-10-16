"""A pooled box that cannot prove isolation is refused before preparation and destroyed
after acquisition.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import client_pool
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.seams.model import ResolvedModelAccess
from astrabox.seams.sandbox import (
    SandboxClientPoolMember,
    SandboxClientPoolReadiness,
    SandboxClientPoolSpec,
)
from astrabox.seams.sandbox_disposal import SandboxDestruction


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "agent_prewarm_redis_url": "redis://redis.test:6379/0",
        "sandbox_credential_vault_enabled": False,
        "sandbox_ready_timeout_seconds": 30,
        "sandbox_request_timeout_seconds": 15,
        "sandbox_lease_seconds": 3600,
        "sandbox_workspace_volume": "astrabox-workspaces",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _template(**overrides: Any) -> SimpleNamespace:
    values = {
        "agent_id": "agent-1",
        "engine_kind": "claude_code",
        "prepared_capacity": 1,
        "runtime_generation": "a" * 64,
        "sandbox_generation": "a" * 64,
        "client_pool_epoch": "b" * 32,
        "sandbox_backend": "open_sandbox",
        "sandbox_tenancy": "agent",
        "sandbox_permission_level": "advanced",
        "runtime_template_name": "astrabox/sandbox-claude-code:test",
        "networking": {"type": "limited", "allow_mcp_servers": True},
        "skills": [],
        "packages": [],
        "mcp_servers": {},
        "model_config": {
            "base_url": "https://model.test",
            "api_key": "real-model-secret",
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _spec(**overrides: Any) -> SandboxClientPoolSpec:
    values = {
        "pool_name": "astrabox-agent-agent-1-aaaaaaaaaaaa",
        "creation_image": "astrabox/sandbox-claude-code:test",
        "max_idle": 1,
        "idle_timeout_seconds": 3600,
        "preparation_timeout_seconds": 600,
    }
    values.update(overrides)
    return SandboxClientPoolSpec(**values)


@pytest.fixture(autouse=True)
def _stable_provider_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "astrabox.providers.open_sandbox.agent_pool.load_astrabox_settings",
        lambda: _settings(),
    )
    monkeypatch.setattr(
        OpenSandboxSandboxProvider,
        "find_sandbox_by_assignment",
        AsyncMock(return_value=None),
    )


class _FakeSdkSandbox:
    def __init__(self, sandbox_id: str = "sbx-warm") -> None:
        self.id = sandbox_id
        self.metadata_updates: list[dict[str, str]] = []
        self.killed = 0
        self.closed = 0

    async def patch_metadata(self, updates: dict[str, str]) -> None:
        self.metadata_updates.append(dict(updates))

    async def kill(self) -> None:
        self.killed += 1

    async def close(self) -> None:
        self.closed += 1


async def _creator(_member: SandboxClientPoolMember) -> OpenSandboxHandle:
    return OpenSandboxHandle(_FakeSdkSandbox("created"))


async def _preparer(_handle: Any) -> None:
    return None


# Platform-owned pool composition and claim.


class _PlatformProvider:
    name = "open_sandbox"
    supports_client_pool = True

    def client_pool_readiness(self) -> SandboxClientPoolReadiness:
        return SandboxClientPoolReadiness(available=True)

    def workspace_route(self) -> Any:
        from astrabox.providers.open_sandbox.workspace_route.mergerfs import workspace_router

        return workspace_router

    def __init__(self) -> None:
        self.created_specs: list[Any] = []
        self.events: list[str] = []
        self.acquired: Any = None
        self.confirmed: list[str] = []

    def fit_resources(self, limits: Any, requests: Any) -> tuple[dict[str, str], dict[str, str]]:
        return dict(limits), dict(requests)

    async def create_sandbox(self, spec: Any) -> OpenSandboxHandle:
        self.created_specs.append(spec)
        return OpenSandboxHandle(_FakeSdkSandbox("base-box"))

    async def find_sandbox_by_assignment(self, _assignment_id: str) -> None:
        return None

    async def acquire_client_pool(self, _spec: Any) -> Any:
        self.events.append("acquire")
        return self.acquired

    async def adopt_sandbox_identity(
        self,
        _handle: Any,
        *,
        session_id: str,
        assignment_id: str,
    ) -> None:
        self.events.append(f"adopt:{session_id}:{assignment_id}")

    async def read_isolation_capability(self, sandbox_id: str) -> Any:
        self.events.append(f"isolation:{sandbox_id}")
        return SimpleNamespace(available=True, detail=None)

    async def confirm_destroyed(self, sandbox_id: str) -> SandboxDestruction:
        self.confirmed.append(sandbox_id)
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="gone")


class _Adapter:
    def sandbox_request(self, *, template: Any, model_access: Any) -> Any:
        return SimpleNamespace(
            credential=object(),
            required_network_hosts=("engine.vendor.test",),
            cwd="/workspace",
            entrypoint=("/opt/astrabox/start",),
            env={"PUBLIC_SETTING": "yes"},
            credential_env_var="MODEL_API_KEY",
            cwd_env_var="ASTRABOX_WORKSPACE",
            publish_ports=(8000,),
            wait_for_inbox_service_port=8000,
        )

    @property
    def capabilities(self) -> Any:
        return SimpleNamespace(session_log=None)


class _RuntimeManager(RemoteAgentRuntimeManager):
    def __init__(self) -> None:
        super().__init__(sessions_repo=SimpleNamespace(record_startup_allocation=AsyncMock()))

    def resolve_model_access(self, config: dict[str, Any]) -> ResolvedModelAccess:
        return ResolvedModelAccess(
            configuration=dict(config),
            base_url="https://model.test",
            model_name="model",
            credential="real-model-secret",
            credential_kind="bearer",
            endpoint_provider="test",
        )


async def test_platform_preparer_refuses_a_box_without_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PlatformProvider()

    async def unavailable(_sandbox_id: str) -> Any:
        return SimpleNamespace(available=False, detail="user namespaces disabled")

    provider.read_isolation_capability = unavailable  # type: ignore[method-assign]
    plugins = AsyncMock()
    skills = AsyncMock()
    monkeypatch.setattr(client_pool, "load_astrabox_settings", lambda: _settings())
    monkeypatch.setattr(client_pool, "sandbox_for_template", lambda _template: provider)
    monkeypatch.setattr(client_pool, "get_engine_adapter", lambda _kind: _Adapter())
    monkeypatch.setattr(client_pool, "resolve_runtime_profile", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(client_pool, "probe_sandbox_runtime_profile", AsyncMock())
    monkeypatch.setattr(client_pool, "prepare_agent_runtime_plugin_cache", plugins)
    monkeypatch.setattr(client_pool, "prepare_agent_runtime_skill_cache", skills)

    plan = await client_pool.build_agent_client_pool_plan(
        _template(), runtime_manager=_RuntimeManager()
    )
    assert plan is not None
    with pytest.raises(APIError) as caught:
        await plan.preparer(OpenSandboxHandle(_FakeSdkSandbox("base-box")))

    assert caught.value.code == "SANDBOX_ISOLATION_UNSUPPORTED"
    assert "user namespaces disabled" in caught.value.message
    plugins.assert_not_awaited()
    skills.assert_not_awaited()


async def test_platform_destroys_an_acquired_box_that_lost_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _PlatformProvider()
    provider.acquired = OpenSandboxHandle(_FakeSdkSandbox("pooled-box"))

    async def unavailable(_sandbox_id: str) -> Any:
        provider.events.append("isolation:pooled-box")
        return SimpleNamespace(available=False, detail="isolation disappeared")

    provider.read_isolation_capability = unavailable  # type: ignore[method-assign]
    plan = client_pool.AgentClientPoolPlan(
        backend_name=provider.name,
        spec=_spec(),
        creator=_creator,
        preparer=_preparer,
    )
    monkeypatch.setattr(
        client_pool,
        "ensure_agent_client_pool",
        AsyncMock(return_value=plan),
    )
    monkeypatch.setattr(client_pool, "sandbox_for_name", lambda _name: provider)

    with pytest.raises(APIError) as caught:
        await client_pool.acquire_agent_client_pool(
            _template(),
            runtime_manager=_RuntimeManager(),
            session_id="session-7",
            assignment_id="assignment-7",
        )

    assert caught.value.code == "SANDBOX_ISOLATION_UNSUPPORTED"
    assert provider.events == [
        "acquire",
        "adopt:agent-runtime:agent-1:assignment-7",
        "isolation:pooled-box",
    ]
    assert provider.confirmed == ["pooled-box"]
