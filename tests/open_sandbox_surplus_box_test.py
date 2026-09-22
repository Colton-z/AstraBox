"""Concurrent shared placement keeps the winner and disposes of the surplus.

The platform owns the Agent-row claim. OpenSandbox may supply a ready client-
pool member or create a box, but once handed over that candidate follows the
same platform placement recipe. If another Session wins the claim, the unused
candidate is destroyed by provider identity; the supplier's pool scheduler
independently replenishes its inventory.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import client_pool
from astrabox.core.service.orchestrator.engine import provisioning
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
)
from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
    SharedSandboxLease,
)
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.seams.sandbox import SandboxAllocation
from astrabox.seams.sandbox_disposal import SandboxDestruction

AGENT_BOX = "box-agent"
SURPLUS_BOX = "box-surplus"


def _binding(sandbox_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        isolated_session_id="iso-1",
        terminal_isolated_session_id="iso-terminal-1",
        uid=2001,
        gid=2001,
        home_dir="/home/conversations/conv_x",
        workspace_dir="/workspace",
        workspace_source_dir="/home/conversations/conv_x/workspace",
    )


class _Files:
    async def write_file(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Handle:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.files = _Files()


class _Backend:
    name = "open_sandbox"

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.handles: dict[str, _Handle] = {}

    def handle(self, sandbox_id: str) -> _Handle:
        return self.handles.setdefault(sandbox_id, _Handle(sandbox_id))

    async def connect(self, sandbox_id: str) -> _Handle:
        self.events.append(f"connect:{sandbox_id}")
        return self.handle(sandbox_id)

    async def confirm_destroyed(self, sandbox_id: str) -> SandboxDestruction:
        self.events.append(f"destroy:{sandbox_id}")
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="test cleanup")


class _Lease:
    def __init__(self, events: list[str], placements: tuple[Any, ...]) -> None:
        self.events = events
        self.placements = list(placements)

    async def place_in_agent_box(self, **kwargs: Any) -> Any:
        candidate = str(kwargs.get("candidate") or "resident")
        self.events.append(f"place:{candidate}")
        return self.placements.pop(0)

    async def start_runner(self, binding: Any, **_kwargs: Any) -> str:
        self.events.append(f"launch:{binding.sandbox_id}")
        return "9001"


class _Workspace:
    provisioned_runtime_identity = None

    async def mount_and_provision(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Manager(RemoteAgentRuntimeManager):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        super().__init__(sessions_repo=SimpleNamespace(record_startup_allocation=AsyncMock()))

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: SandboxAllocation,
        *,
        replaces: SandboxAllocation | None = None,
    ) -> None:
        self.events.append(f"record:{allocation.sandbox_id}")
        await super().record_startup_allocation(session_id, allocation, replaces=replaces)


class _SessionRepo:
    async def get_session(self, _session_id: str) -> None:
        return None


class _AgentRepo:
    async def get_agent(self, _agent_id: str) -> dict[str, str]:
        return {}


class _Engine:
    engine_kind = "deepseek_harness"

    def shared_conversation_service_launch(self, **_kwargs: Any) -> str:
        return "launch shared engine"


def _request() -> EngineSandboxRequest:
    return EngineSandboxRequest(
        entrypoint=("/opt/gem/run.sh",),
        credential=ModelCredentialRequest(
            access=None,
            request_paths=("chat/completions",),
            missing_code="ENGINE_CAPABILITY_UNAVAILABLE",
            missing_message="no model access",
        ),
        credential_env_var="DEEPSEEK_API_KEY",
        cwd_env_var="ASTRABOX_WORKSPACE",
    )


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    placements: tuple[Any, ...],
    pool_candidate: bool,
) -> SimpleNamespace:
    events: list[str] = []
    manager = _Manager(events)
    backend = _Backend(events)
    lease = _Lease(events, placements)
    candidate = backend.handle(SURPLUS_BOX)

    async def acquire_candidate(*_args: Any, **_kwargs: Any) -> Any:
        if not pool_candidate:
            return None
        await manager.record_startup_allocation(
            "session-1",
            SandboxAllocation(
                sandbox_id=SURPLUS_BOX,
                sandbox_backend=backend.name,
                scope="sandbox",
            ),
        )
        return SimpleNamespace(sandbox=candidate, sandbox_id=SURPLUS_BOX)

    acquire = AsyncMock(side_effect=acquire_candidate)
    create = AsyncMock(return_value=(candidate, SURPLUS_BOX))

    monkeypatch.setattr(provisioning, "SessionRepository", _SessionRepo)
    monkeypatch.setattr(
        "astrabox.persistence.repository.agent_repository.AgentRepository",
        _AgentRepo,
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.shared_sandbox_lease."
        "SharedSandboxLease",
        lambda **_kwargs: lease,
    )
    monkeypatch.setattr(client_pool, "acquire_agent_client_pool", acquire)
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime_manager.sandbox_for_name",
        lambda _name: backend,
    )
    monkeypatch.setattr(provisioning, "create_shared_agent_sandbox", create)
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.workspace.workspace_from_subject_kind",
        lambda *_args, **_kwargs: _Workspace(),
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.engine.registry.get_engine_adapter",
        lambda _kind: _Engine(),
    )

    async def endpoint(_sandbox: Any, port: int) -> str:
        assert port == 9001
        return "ws://box:9001"

    monkeypatch.setattr(provisioning, "resolve_sandbox_websocket_endpoint", endpoint)

    async def run() -> Any:
        return await provisioning._provision_shared_conversation(
            manager,
            backend,
            session_id="session-1",
            assignment_id="assignment-1",
            template=SimpleNamespace(
                agent_id="agent-1",
                engine_kind="deepseek_harness",
                runtime_generation="generation-1",
            ),
            workspace_plan=SimpleNamespace(
                subject_kind="deployment_conversation",
                agent_id="agent-1",
                assistant_id=None,
                engine_kind="deepseek_harness",
            ),
            user_id="user-1",
            request=_request(),
            runtime_identity={
                "sandbox_tenancy": "agent",
                "agent_id": "agent-1",
                "linux_user": "conv_x",
                "home_dir": "/home/conversations/conv_x",
                "workspace_dir": "/workspace",
                "workspace_source_dir": "/home/conversations/conv_x/workspace",
            },
            cwd="/workspace",
            credential="placeholder",
            runtime_env={},
            network_policy=None,
            vault_write=None,
            session_log=None,
        )

    return SimpleNamespace(
        events=events,
        backend=backend,
        acquire=acquire,
        create=create,
        run=run,
    )


async def test_winning_the_claim_gives_nothing_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(
        monkeypatch,
        placements=(None, _binding(SURPLUS_BOX)),
        pool_candidate=True,
    )

    sandbox, sandbox_id, _identity, _runner = await harness.run()

    assert sandbox_id == SURPLUS_BOX
    assert sandbox.sandbox_id == SURPLUS_BOX
    assert not any(event.startswith("destroy:") for event in harness.events)
    assert not any(event.startswith("connect:") for event in harness.events)


async def test_a_candidate_that_cannot_supply_isolation_is_refused_and_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(
        monkeypatch,
        placements=(None, None),
        pool_candidate=True,
    )

    with pytest.raises(APIError) as caught:
        await harness.run()

    assert caught.value.code == "SANDBOX_ISOLATION_UNSUPPORTED"
    assert f"destroy:{SURPLUS_BOX}" in harness.events
