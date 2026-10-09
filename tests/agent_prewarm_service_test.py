"""Agent lifecycle owns client-pool and prepared-runtime reconciliation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator.agent import client_pool
from astrabox.core.service.orchestrator.agent import prepared_slots
from astrabox.core.service.orchestrator.agent import runtime_generation as runtime_generation_module
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.seams.egress_credentials import EgressCredential


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
        for key, value in expected.items():
            if isinstance(value, dict) and set(value) == {"$exists"}:
                if (key in self.row) != bool(value["$exists"]):
                    return False
            elif self.row.get(key) != value:
                return False
        self.row.update(updates)
        return True

    async def list_all_agents(self) -> list[dict[str, Any]]:
        return [dict(self.row)] if self.row is not None else []

    async def list_live_agents_page(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        if self.row is None or self.row.get("deleted") or after_agent_id is not None:
            return []
        return [dict(self.row)]

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


async def test_environment_update_schedules_bound_agent_runtime() -> None:
    row = {
        "agent_id": "agent-1",
        "environment_name": "research",
        "prepared_capacity": 1,
    }
    service = _service(_Repo(row), SimpleNamespace(agent_id="agent-1"))
    scheduled: list[str] = []
    service._schedule_runtime_reconciliation = scheduled.append  # type: ignore[method-assign]

    assert await service.reconcile_environment_runtimes("research") == 1
    assert scheduled == ["agent-1"]
    assert await service.reconcile_environment_runtimes("another-environment") == 0
    assert scheduled == ["agent-1"]


async def test_agent_manager_reads_platform_prepared_runtime_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_at = datetime.now(timezone.utc).isoformat()
    row = {
        "agent_id": "agent-1",
        "created_by": "owner-1",
        "visibility": "public",
        "prepared_capacity": 1,
        "sandbox_id": "box-ready", "sandbox_backend": "fake",
        "_runtime_generation": "generation-new",
        "_prepared_runtime_generation": "generation-new",
        "_client_pool_name": "supplier-pool-new",
        "_client_pool_backend": "fake",
        "_seats_per_box": 3,
        "_prepared_slots": [{
            "state": "prepared",
            "placement": "shared_slot",
            "sandbox_id": "box-ready", "sandbox_backend": "fake",
            "runtime_generation": "generation-new",
            "prepared_at": prepared_at,
        }],
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
    from astrabox.seams import sandbox as sandbox_seam

    pool = SimpleNamespace(
        ready=True, idle_count=2, idle_sandbox_ids=("pool-box",),
        lifecycle_state="RUNNING", last_error=None,
    )
    monkeypatch.setattr(
        sandbox_seam,
        "sandbox_for_name",
        lambda _name: SimpleNamespace(describe_client_pool=AsyncMock(return_value=pool)),
    )

    status = await service.get_prepared_runtime_status(UserContext("owner-1"), "agent-1")

    # One seat waiting in a box the Agent holds, three in each of two pool boxes.
    assert status == {
        "enabled": True,
        "target": 1,
        "ready": True,
        "prepared_count": 7,
        "last_miss_at": None,
        "state": "prepared",
        "placement": "shared_slot",
        "runtime_generation": "generation-new",
        "client_pool_name": "supplier-pool-new",
        "sandbox_id": "box-ready",
        "prepared_at": prepared_at,
        "last_error": None,
    }


@pytest.mark.parametrize("stamp", ["expired", "missing", "malformed", "naive", "near_expiry"])
async def test_prepared_capacity_read_obeys_the_claim_expiry_boundary(
    monkeypatch: pytest.MonkeyPatch, stamp: str,
) -> None:
    age = prepared_slots.prepared_seat_ttl_seconds() + (60 if stamp == "expired" else -60)
    prepared_at = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
    if stamp == "missing":
        prepared_at = ""
    elif stamp == "malformed":
        prepared_at = "not-a-timestamp"
    elif stamp == "naive":
        prepared_at = "2026-09-28T09:00:00"
    row = {
        "agent_id": "agent-1", "created_by": "owner-1", "prepared_capacity": 1,
        "sandbox_id": "box-ready", "sandbox_backend": "fake",
        "_prepared_runtime_generation": "generation-new",
        "_prepared_slots": [{
            "state": "prepared", "placement": "shared_slot", "sandbox_id": "box-ready",
            "sandbox_backend": "fake",
            "runtime_generation": "generation-new", "prepared_at": prepared_at,
        }],
    }
    repo = _Repo(row)
    service = _service(repo, SimpleNamespace(agent_id="agent-1", sandbox_tenancy="agent"))
    monkeypatch.setattr(runtime_generation_module, "runtime_generations",
                        AsyncMock(return_value=("generation-new", "box-generation")))

    status = await service.get_prepared_runtime_status(UserContext("owner-1"), "agent-1")

    ready = stamp == "near_expiry"
    assert status["ready"] is ready
    assert status["prepared_count"] == int(ready)
    assert status["state"] == ("prepared" if ready else "expired")
    assert status["prepared_at"] == (prepared_at if ready else None)
    assert status["sandbox_id"] == ("box-ready" if ready else None)
    assert repo.updates == []
    assert repo.compares == []


@pytest.mark.parametrize("pool_name", [None, "supplier-pool-existing"])
async def test_config_refresh_keeps_pool_identity_without_advertising_stale_capacity(
    monkeypatch: pytest.MonkeyPatch, pool_name: str | None,
) -> None:
    row = {
        "agent_id": "agent-1",
        "created_by": "owner-1",
        "prepared_capacity": 1,
        "_client_pool_name": pool_name,
        "_prepared_runtime_generation": "generation-old",
        "_prepared_slots": [{
            "state": "prepared",
            "placement": "shared_slot",
            "sandbox_id": "box-existing",
            "runtime_generation": "generation-old",
        }],
    }
    repo = _Repo(row)
    service = _service(repo, SimpleNamespace(agent_id="agent-1", sandbox_tenancy="agent"))
    monkeypatch.setattr(
        runtime_generation_module, "runtime_generations",
        AsyncMock(return_value=("generation-new", "box-generation")),
    )

    status = await service.get_prepared_runtime_status(UserContext("owner-1"), "agent-1")

    assert status["client_pool_name"] == pool_name
    assert status["state"] == "preparing"
    assert status["ready"] is False
    assert status["prepared_count"] == 0
    assert repo.updates == []
    assert repo.compares == []


async def test_regular_viewer_cannot_read_prepared_runtime_status() -> None:
    row = {
        "agent_id": "agent-1",
        "created_by": "owner-1",
        "visibility": "public",
        "prepared_capacity": 1,
    }
    service = _service(_Repo(row), SimpleNamespace(agent_id="agent-1"))

    with pytest.raises(APIError) as caught:
        await service.get_prepared_runtime_status(UserContext("viewer-1"), "agent-1")

    assert caught.value.code == "FORBIDDEN"


async def test_an_agent_edit_during_a_pass_leaves_no_client_pool_unnamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every namespace a pass starts is recorded or retired, whatever lands meanwhile.

    The demo seed converges the Environment and then edits the Agent's prompt.
    The edit landed while the Environment's pass was starting its namespace:
    that namespace was never recorded, the follow-up pass retired only the one
    recorded before it, and its scheduler kept a box with nothing naming it.
    """

    agent_id = "198c0391-3448-4484-9910-8ca4834a6186"
    first_epoch = "0" * 32
    first_pool = client_pool._pool_name(agent_id, first_epoch)
    repo = _Repo({
        "agent_id": agent_id,
        "environment_name": "demo",
        "enabled": True,
        "version": 1,
        "system": "prompt-1",
        "prepared_capacity": 1,
        # The Environment write changed the recipe this generation was made from.
        "_runtime_generation": "generation-before-environment-write",
        "_sandbox_generation": "generation-before-environment-write",
        "_client_pool_epoch": first_epoch,
        "_client_pool_name": first_pool,
        "_client_pool_backend": "open_sandbox",
        "_retiring_client_pools": [],
    })

    class _StoredAgentConfig(_AgentConfig):
        """Resolve the Agent from its stored row, as the configuration service does."""

        async def resolve_agent_harness(self, target: str) -> Any:
            row = await repo.get_agent(target)
            assert row is not None
            return AgentView(
                agent_id=target,
                system=row["system"],
                version=row["version"],
                engine_kind="pi",
                environment_name=row["environment_name"],
                prepared_capacity=row["prepared_capacity"],
                runtime_template_name="astrabox/sandbox-pi:test",
                sandbox_backend="open_sandbox",
                sandbox_tenancy="conversation",
                runtime_generation=row.get("_runtime_generation"),
                sandbox_generation=row.get("_sandbox_generation"),
                client_pool_epoch=row.get("_client_pool_epoch"),
            )

    service = _service(repo, None)
    service._agent_config = _StoredAgentConfig(None)

    async def edit_agent_prompt() -> None:
        # ``upsert_agent_config``'s write, then ``update_agent``'s scheduling.
        assert await repo.compare_and_update_agent(
            agent_id,
            expected={"version": 1},
            updates={"system": "prompt-2", "version": 2},
        )
        service.schedule_runtime_reconciliation(agent_id)

    class _Supplier:
        name = "open_sandbox"
        supports_client_pool = True

        def __init__(self) -> None:
            self.started: list[str] = []
            self.retired: list[str] = []

        async def ensure_client_pool(self, spec: Any, *, creator: Any, preparer: Any) -> None:
            self.started.append(spec.pool_name)
            if len(self.started) == 1:
                await edit_agent_prompt()

        async def retire_client_pool(self, pool_name: str) -> None:
            self.retired.append(pool_name)

    supplier = _Supplier()
    monkeypatch.setattr(
        client_pool,
        "load_astrabox_settings",
        lambda: SimpleNamespace(
            agent_prewarm_redis_url="redis://redis.test:6379/0",
            sandbox_lease_seconds=3600,
            sandbox_ready_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(client_pool, "sandbox_for_template", lambda _template: supplier)
    monkeypatch.setattr(client_pool, "sandbox_for_name", lambda _name: supplier)
    monkeypatch.setattr(client_pool, "get_engine_adapter", lambda _kind: object())

    async def generations(template: Any, *, runtime_manager: Any) -> tuple[str, str]:
        return (f"generation-{template.system}",) * 2

    monkeypatch.setattr(runtime_generation_module, "runtime_generations", generations)

    # The Environment write's reconciliation.
    assert service.schedule_runtime_reconciliation(agent_id) is True
    while service._preparation_tasks:
        await asyncio.gather(*service._preparation_tasks, return_exceptions=True)
        await asyncio.sleep(0)

    assert len(supplier.started) == 2
    recorded = repo.row["_client_pool_name"]
    assert recorded == supplier.started[-1]
    assert [
        name for name in [first_pool, *supplier.started]
        if name != recorded and name not in supplier.retired
    ] == []
    assert repo.row["_retiring_client_pools"] == []
