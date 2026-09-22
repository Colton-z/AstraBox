"""Agent lifecycle owns client-pool and prepared-runtime reconciliation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent import client_pool, prepared_slots
from astrabox.core.service.orchestrator.agent import runtime_generation as runtime_generation_module
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.core.service.orchestrator.platform_service import AgentPlatformService
from astrabox.seams.egress_credentials import EgressCredential
from astrabox.seams.sandbox_disposal import SandboxDestruction


class _Repo:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = dict(row) if row is not None else None
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.compares: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.soft_deleted: list[tuple[str, str]] = []

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        if self.row is None or self.row.get("agent_id") != agent_id:
            return None
        return dict(self.row)

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> bool:
        self.updates.append((agent_id, dict(updates)))
        if self.row is not None:
            self.row.update(updates)
        return True

    async def compare_and_update_agent(
        self,
        agent_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        self.compares.append((agent_id, dict(expected), dict(updates)))
        if self.row is None:
            return False
        if any(self.row.get(key) != value for key, value in expected.items()):
            return False
        self.row.update(updates)
        return True

    async def list_all_agents(self) -> list[dict[str, Any]]:
        return [dict(self.row)] if self.row is not None else []

    async def soft_delete(self, agent_id: str, user_id: str) -> bool:
        self.soft_deleted.append((agent_id, user_id))
        if self.row is not None:
            self.row.update({"deleted": True, "state": "DELETED"})
        return True


class _AgentConfig:
    def __init__(self, template: Any) -> None:
        self.template = template

    async def resolve_agent_harness(self, agent_id: str) -> Any:
        return self.template if self.template.agent_id == agent_id else None

    async def get_environment(self, name: str) -> dict[str, Any]:
        return {"name": name, "enabled": True}


def _service(repo: _Repo, template: Any) -> AgentService:
    return AgentService(
        platform_service=SimpleNamespace(),
        sessions_repo=object(),
        runtime_manager=SimpleNamespace(name="runtime-manager"),
        agent_config=_AgentConfig(template),
        turn_service=object(),
        broker=object(),
        agent_repo=repo,
    )


def _patch_generation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    generation: str = "generation-new",
) -> None:
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.agent.runtime_generation.reconcile_runtime_generation",
        AsyncMock(return_value=generation),
    )


def test_runtime_generation_credential_contract_ignores_secret_rotation() -> None:
    def credential(secret: str) -> EgressCredential:
        return EgressCredential(
            credential_id="credential-1",
            secret_name="SERVICE_TOKEN",
            secret_value=secret,
            placeholder=f"placeholder-{secret}",
            networking={"type": "limited", "allowed_hosts": ["api.example.test"]},
            injection_location={"header": True, "body": False},
            allowed_requests={"methods": ["POST"], "paths": ["/v1/*"]},
        )

    before = runtime_generation_module.environment_credentials_generation_contract(
        [credential("before")]
    )
    after = runtime_generation_module.environment_credentials_generation_contract(
        [credential("after")]
    )

    assert before == after
    assert "before" not in str(before)
    assert "placeholder" not in str(before)


def test_runtime_generation_credential_contract_rotates_on_policy_change() -> None:
    original = EgressCredential(
        credential_id="credential-1",
        secret_name="SERVICE_TOKEN",
        secret_value="secret",
        networking={"type": "limited", "allowed_hosts": ["api.example.test"]},
        injection_location={"header": True, "body": False},
    )
    widened = EgressCredential(
        credential_id="credential-1",
        secret_name="SERVICE_TOKEN",
        secret_value="secret",
        networking={
            "type": "limited",
            "allowed_hosts": ["api.example.test", "other.example.test"],
        },
        injection_location={"header": True, "body": False},
    )

    assert runtime_generation_module.environment_credentials_generation_contract(
        [original]
    ) != runtime_generation_module.environment_credentials_generation_contract([widened])


async def test_disabling_preparation_retires_slot_then_pool_before_clearing_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "agent_id": "agent-1",
        "version": 3,
        "enabled": True,
        "state": "ACTIVE",
        "prewarm_enabled": False,
        "_client_pool_name": "pool-old",
        "_client_pool_backend": "open_sandbox",
    }
    template = SimpleNamespace(agent_id="agent-1", runtime_generation=None)
    repo = _Repo(row)
    events: list[str] = []

    async def retire_slot(current: Any, *, reason: str, agent_repo: Any) -> None:
        assert current == "agent-1"
        assert reason == "Agent runtime preparation is disabled"
        assert agent_repo is repo
        events.append("retire-slot")

    async def retire_pool(pool_name: str, *, backend_name: str) -> None:
        assert (pool_name, backend_name) == ("pool-old", "open_sandbox")
        events.append("retire-pool")

    _patch_generation(monkeypatch)
    monkeypatch.setattr(prepared_slots, "retire_prepared_runtime", retire_slot)
    monkeypatch.setattr(client_pool, "retire_agent_client_pool", retire_pool)

    await _service(repo, template)._reconcile_agent_runtime("agent-1")

    assert events == ["retire-slot", "retire-pool"]
    assert repo.updates == [
        (
            "agent-1",
            {
                "_client_pool_name": None,
                "_client_pool_backend": None,
                "_client_pool_epoch": None,
            },
        )
    ]


async def test_agent_delete_retires_prepared_capacity_before_hiding_owner_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "agent_id": "agent-1",
        "version": 3,
        "created_by": "owner-1",
        "visibility": "private",
        "enabled": True,
        "state": "ACTIVE",
        "prewarm_enabled": True,
        "_client_pool_name": "pool-old",
        "_client_pool_backend": "open_sandbox",
    }
    repo = _Repo(row)
    template = SimpleNamespace(agent_id="agent-1", runtime_generation=None)
    events: list[str] = []

    async def retire_slot(*_args: Any, **_kwargs: Any) -> None:
        assert repo.soft_deleted == []
        events.append("retire-slot")

    async def retire_pool(*_args: Any, **_kwargs: Any) -> None:
        assert repo.soft_deleted == []
        events.append("retire-pool")

    _patch_generation(monkeypatch)
    monkeypatch.setattr(prepared_slots, "retire_prepared_runtime", retire_slot)
    monkeypatch.setattr(client_pool, "retire_agent_client_pool", retire_pool)
    service = _service(repo, template)
    service._runtime_manager = SimpleNamespace(
        terminate_runtime=AsyncMock(
            return_value=SandboxDestruction.nothing_named(
                detail="the Agent had no resident sandbox"
            )
        )
    )

    result = await service.delete_agent(UserContext("owner-1"), "agent-1")

    assert events == ["retire-slot", "retire-pool"]
    assert repo.soft_deleted == [("agent-1", "owner-1")]
    assert result["state"] == "DELETED"


async def test_environment_update_schedules_bound_agent_runtime() -> None:
    row = {
        "agent_id": "agent-1",
        "environment_name": "research",
        "prewarm_enabled": True,
    }
    service = _service(_Repo(row), SimpleNamespace(agent_id="agent-1"))
    scheduled: list[str] = []
    service._schedule_runtime_reconciliation = scheduled.append  # type: ignore[method-assign]

    assert await service.reconcile_environment_runtimes("research") == 1
    assert scheduled == ["agent-1"]
    assert await service.reconcile_environment_runtimes("another-environment") == 0
    assert scheduled == ["agent-1"]


async def test_environment_write_notifies_runtime_reconciliation_after_commit() -> None:
    calls: list[tuple[str, object]] = []

    class _EnvironmentConfig:
        async def upsert_environment_config(
            self, user: UserContext, name: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            calls.append(("write", payload))
            return {"name": name, **payload}

    class _EnvironmentAgentService:
        async def reconcile_environment_runtimes(self, name: str) -> int:
            calls.append(("reconcile", name))
            return 1

    platform = object.__new__(AgentPlatformService)
    platform._agent_config = _EnvironmentConfig()
    platform._agent_service_getter = lambda: _EnvironmentAgentService()

    result = await platform.upsert_environment_config(
        UserContext("admin"),
        "research",
        {"engine_kind": "claude_code"},
    )

    assert result == {"name": "research", "engine_kind": "claude_code"}
    assert calls == [
        ("write", {"engine_kind": "claude_code"}),
        ("reconcile", "research"),
    ]


async def test_agent_manager_reads_platform_prepared_runtime_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "agent_id": "agent-1",
        "created_by": "owner-1",
        "visibility": "public",
        "prewarm_enabled": True,
        "_runtime_generation": "generation-new",
        "_prepared_runtime_generation": "generation-new",
        "_client_pool_name": "supplier-pool-new",
        "_prepared_slot": {
            "state": "prepared",
            "placement": "shared_slot",
            "sandbox_id": "box-ready",
            "runtime_generation": "generation-new",
        },
    }
    service = _service(
        _Repo(row),
        SimpleNamespace(agent_id="agent-1", sandbox_tenancy="agent"),
    )
    monkeypatch.setattr(
        runtime_generation_module,
        "runtime_generations",
        AsyncMock(return_value=("generation-new", "box-generation")),
    )

    status = await service.get_prepared_runtime_status(UserContext("owner-1"), "agent-1")

    assert status == {
        "enabled": True,
        "ready": True,
        "prepared_count": 1,
        "state": "prepared",
        "placement": "shared_slot",
        "runtime_generation": "generation-new",
        "client_pool_name": "supplier-pool-new",
        "sandbox_id": "box-ready",
        "last_error": None,
    }


async def test_regular_viewer_cannot_read_prepared_runtime_status() -> None:
    row = {
        "agent_id": "agent-1",
        "created_by": "owner-1",
        "visibility": "public",
        "prewarm_enabled": True,
    }
    service = _service(_Repo(row), SimpleNamespace(agent_id="agent-1"))

    with pytest.raises(APIError) as caught:
        await service.get_prepared_runtime_status(UserContext("viewer-1"), "agent-1")

    assert caught.value.code == "FORBIDDEN"
