"""A capability a backend declares is one it implements, and features need them.

Registration checks every flag in ``SANDBOX_CAPABILITY_RULES``: declared True,
the flag brings the methods and flags its rule names, so a half-declared
capability is refused when the provider is installed instead of failing at the
first conversation that needs it. The write paths then refuse configuration the
selected backend has not declared it can run: shared tenancy without isolated
sessions, any Environment on a backend that cannot create a box under a network
policy, and prepared capacity on a backend whose client pool cannot run here.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.agent_schema import default_prepared_capacity
from astrabox.core.service.orchestrator.environment_schema import (
    validate_environment_payload,
)
from astrabox.providers import register_builtin_providers
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.sandbox import (
    SandboxClientPoolReadiness,
    SandboxIsolationCapability,
    SandboxProvider,
    register_sandbox,
)


class _Backend(SandboxProvider):
    """The abstract lifecycle pairs stubbed; nothing here reaches a box."""

    name = "rules-test"
    supports_create_network_policy = True

    def connection_config(self, **kwargs: Any) -> Any:
        return None

    def secret_material(self, *, settings: Any) -> str:
        return ""

    def build_dataplane(self, **kwargs: Any) -> Any:
        raise RuntimeError("no dataplane")

    async def connect(self, sandbox_id: str) -> Any:
        raise RuntimeError("no connect")

    async def kill(self, sandbox_id: str) -> bool:
        raise RuntimeError("no kill")

    async def admit_runtime_egress(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no egress")


class _PoolingBackend(_Backend):
    """Declares a client pool whose readiness the test chooses."""

    name = "pooling-test"
    supports_client_pool = True
    ready = True

    async def ensure_client_pool(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def acquire_client_pool(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def describe_client_pool(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def retire_client_pool(self, *args: Any, **kwargs: Any) -> None:
        return None

    def owns_unclaimed_sandbox(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def client_pool_readiness(self) -> SandboxClientPoolReadiness:
        if self.ready:
            return SandboxClientPoolReadiness(available=True)
        return SandboxClientPoolReadiness(
            available=False, detail="its pool needs a coordination store"
        )


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", {})
    monkeypatch.setattr(sandbox_seam, "_ENABLED_BACKENDS", None)
    monkeypatch.setattr(sandbox_seam, "_DEFAULT_BACKEND", "")


def test_a_flag_without_its_methods_is_refused_at_registration() -> None:
    class _HalfPausing(_Backend):
        name = "half-pausing"
        supports_pause = True

        async def pause(self, sandbox_id: str) -> bool:
            return True

    class _PoolWithoutReadiness(_PoolingBackend):
        name = "pool-without-readiness"
        client_pool_readiness = SandboxProvider.client_pool_readiness  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="supports_pause but does not override resume"):
        register_sandbox(_HalfPausing())
    with pytest.raises(RuntimeError, match="does not override client_pool_readiness"):
        register_sandbox(_PoolWithoutReadiness())


def test_a_flag_without_the_flag_it_requires_is_refused() -> None:
    class _RecoversWithoutSessions(_Backend):
        name = "recovers-without-sessions"
        supports_isolated_session_recovery = True

        async def find_isolated_sessions_by_workspace(self, *args: Any, **kwargs: Any) -> Any:
            return ()

    with pytest.raises(RuntimeError, match="requires supports_isolated_sessions"):
        register_sandbox(_RecoversWithoutSessions())


#: An engine that runs Agent conversations, so tenancy and prepared capacity
#: are meaningful for it.
_AGENT_ENGINE = "claude_code"


def _environment(**fields: Any) -> dict[str, Any]:
    register_builtin_providers()
    return {"name": "rules-env", "engine_kind": _AGENT_ENGINE, **fields}


def test_shared_tenancy_needs_a_backend_with_isolated_sessions() -> None:
    class _AdvancedWithoutSessions(_Backend):
        """Can attest the advanced level, cannot run isolated sessions."""

        name = "advanced-without-sessions"
        supported_permission_levels = ("default", "advanced")

        async def read_isolation_capability(self, sandbox_id: str) -> SandboxIsolationCapability:
            return SandboxIsolationCapability(sandbox_id=sandbox_id, available=False)

    register_sandbox(_AdvancedWithoutSessions())

    with pytest.raises(APIError) as refused:
        validate_environment_payload(
            _environment(
                sandbox_backend="advanced-without-sessions",
                sandbox_tenancy="agent",
                sandbox_permission_level="advanced",
            )
        )

    assert refused.value.status_code == 400
    assert "'advanced-without-sessions'" in refused.value.message
    assert "supports_isolated_sessions" in refused.value.message


def test_an_environment_needs_a_backend_that_creates_boxes_under_its_network_policy() -> None:
    class _NoPolicy(_Backend):
        name = "no-policy"
        supports_create_network_policy = False

    register_sandbox(_NoPolicy())

    with pytest.raises(APIError) as refused:
        validate_environment_payload(_environment(sandbox_backend="no-policy"))

    assert refused.value.status_code == 400
    assert "supports_create_network_policy" in refused.value.message


class _Agents:
    def __init__(self) -> None:
        self.agent: dict[str, Any] | None = None

    async def total_prepared_capacity(self, *, excluding_agent_id: str = "") -> int:
        return 0

    async def create_agent(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.agent = dict(doc)
        return dict(doc)


class _Environments:
    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        if name != "pooling-env":
            return None
        return {
            "name": name,
            "engine_kind": _AGENT_ENGINE,
            "sandbox_backend": "pooling-test",
        }


class _Author:
    user_id = "owner"
    roles: list[str] = []
    org_id = None


async def test_prepared_capacity_needs_a_pool_that_can_run_here() -> None:
    register_builtin_providers()
    backend = _PoolingBackend()
    backend.ready = False
    register_sandbox(backend)
    agents = _Agents()
    service = AgentConfigService(agents, environment_repo=_Environments())  # type: ignore[arg-type]

    # Created without a choice, the Agent gets no capacity it could not prepare.
    assert default_prepared_capacity("pooling-test") == 0
    with pytest.raises(APIError) as refused:
        await service.create_agent_config(
            _Author(),  # type: ignore[arg-type]
            {
                "name": "warm",
                "model": "some-model",
                "environment_name": "pooling-env",
                "prepared_capacity": 1,
            },
        )

    assert (refused.value.code, refused.value.status_code) == ("AGENT_PREWARM_UNSUPPORTED", 409)
    assert "coordination store" in refused.value.message
    assert agents.agent is None

    backend.ready = True
    assert default_prepared_capacity("pooling-test") == 1
