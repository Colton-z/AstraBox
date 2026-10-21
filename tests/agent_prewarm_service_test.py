"""Agent lifecycle owns client-pool and prepared-runtime reconciliation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator.agent import client_pool
from astrabox.core.service.orchestrator.agent import runtime_generation as runtime_generation_module
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.seams.sandbox import SandboxClientPoolReadiness


class _Repo:
    """One Agent's row and ledger, readable and never written: a status read writes nothing."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = dict(row) if row is not None else None
        self.soft_deleted: list[str] = []

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        if self.row is None or self.row.get("agent_id") != agent_id:
            return None
        return dict(self.row)

    async def read_ledger(self, agent_id: str) -> dict[str, Any] | None:
        return await self.get_agent(agent_id)

    async def list_live_agents_page(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        if self.row is None or self.row.get("deleted") or after_agent_id is not None:
            return []
        return [dict(self.row)]

    async def list_agent_ids_by_runtime_profile(
        self, name: str, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[str]:
        if (
            self.row is None
            or self.row.get("runtime_profile_name") != name
            or after_agent_id is not None
        ):
            return []
        return [str(self.row["agent_id"])]

    async def mark_deleting(self, agent_id: str) -> bool:
        if self.row is not None:
            self.row.update({"state": "DELETING"})
        return self.row is not None

    async def soft_delete(self, agent_id: str) -> bool:
        self.soft_deleted.append(agent_id)
        if self.row is not None:
            self.row.update({"state": "DELETED"})
        return True


class _AgentConfig:
    def __init__(self, template: Any) -> None:
        self.template = template

    async def resolve_agent_harness(self, agent_id: str) -> Any:
        return self.template if self.template.agent_id == agent_id else None

    async def get_runtime_profile(self, name: str) -> dict[str, Any]:
        return {"name": name, "enabled": True}


def _service(repo: _Repo, template: Any) -> AgentService:
    return AgentService(
        platform_service=SimpleNamespace(),
        sessions_repo=object(),
        runtime_manager=SimpleNamespace(name="runtime-manager"),
        agent_config=_AgentConfig(template),
        agent_repo=repo,
    )


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


@pytest.mark.usefixtures("database")
async def test_an_agent_edit_during_a_pass_leaves_no_client_pool_unnamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every namespace a pass starts is recorded or retired, whatever lands meanwhile.

    The demo seed converges the RuntimeProfile and then edits the Agent's prompt.
    The edit landed while the RuntimeProfile's pass was starting its namespace:
    that namespace was never recorded, the follow-up pass retired only the one
    recorded before it, and its scheduler kept a box with nothing naming it.
    """

    from tests._agent_ledger import create_agent, set_runtime

    agent_id = "198c0391-3448-4484-9910-8ca4834a6186"
    first_epoch = "0" * 32
    first_pool = client_pool._pool_name(agent_id, first_epoch)
    repo = await create_agent(agent_id, system="prompt-1", prepared_capacity=1)
    await set_runtime(
        agent_id,
        # The RuntimeProfile write changed the recipe this generation was made from.
        runtime_generation="generation-before-environment-write",
        sandbox_generation="generation-before-environment-write",
        client_pool_epoch=first_epoch,
        client_pool_name=first_pool,
        client_pool_backend="open_sandbox",
    )

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
                runtime_profile_name=row["runtime_profile_name"],
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
        assert await repo.update_definition(
            agent_id, expected_version=1, changes={"system": "prompt-2"}, updated_by="owner",
        ) is not None
        service.schedule_runtime_reconciliation(agent_id)

    class _Supplier:
        name = "open_sandbox"
        supports_client_pool = True

        def __init__(self) -> None:
            self.started: list[str] = []
            self.retired: list[str] = []

        def client_pool_readiness(self) -> SandboxClientPoolReadiness:
            return SandboxClientPoolReadiness(available=True)

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

    # The RuntimeProfile write's reconciliation.
    assert service.schedule_runtime_reconciliation(agent_id) is True
    while service._preparation_tasks:
        await asyncio.gather(*service._preparation_tasks, return_exceptions=True)
        await asyncio.sleep(0)

    assert len(supplier.started) == 2
    stored = await repo.get_agent(agent_id)
    assert stored is not None
    recorded = stored["_client_pool_name"]
    assert recorded == supplier.started[-1]
    assert [
        name for name in [first_pool, *supplier.started]
        if name != recorded and name not in supplier.retired
    ] == []
    assert await repo.list_retiring_client_pools(agent_id) == []
