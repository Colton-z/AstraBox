"""An Agent's or Assistant's ``updated_at`` moves only when an author changes it.

The console shows ``updated_at`` as "Updated" and orders its lists by it. The
runtime keeps its own state on the same rows: sandbox pointers, prepared slots,
pools, the durable workspace id. A timestamp stamped on every write therefore
moved, and reordered the list, while nobody edited anything. These tests drive
real runtime writers and the real authoring services against the real
repositories, and read the rows back.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.user_context import UserContext
from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.credential_binding_service import (
    CredentialBindingService,
)
from astrabox.core.service.orchestrator.runtime.storage._identity import ensure_workspace_id
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
)

_EDITED = "2026-01-01T00:00:00+00:00"
_OWNER = UserContext(user_id="owner", roles=[])


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Environments:
    """An unrestricted Environment, so the authoring checks resolve no hosts."""

    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        return {"name": name, "engine_kind": "claude_code", "networking": {"type": "unrestricted"}}


class _Vaults:
    """Every requested vault is a valid binding; this file is not about vaults."""

    async def validate_binding_ids(
        self, user: Any, vault_ids: list[str], *, target_type: str
    ) -> list[str]:
        _ = (user, target_type)
        return list(vault_ids)

    async def describe_binding(self, user: Any, vault_ids: list[str]) -> list[dict[str, Any]]:
        _ = user
        return [{"vault_id": vault_id} for vault_id in vault_ids]


async def _agent(repo: AgentRepository, agent_id: str, updated_at: str) -> None:
    await repo.create_agent(
        {
            "agent_id": agent_id,
            "name": agent_id,
            "model": "some-model",
            "environment_name": "claude-code",
            "created_by": "owner",
            "user_id": "owner",
            "org_id": "default",
            "version": 1,
            "state": "ACTIVE",
            "enabled": True,
            "visibility": "private",
            "updated_at": updated_at,
        }
    )


@pytest.mark.asyncio
async def test_runtime_writes_leave_an_agents_updated_at_and_the_list_order_alone() -> None:
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    repo = AgentRepository()
    await _agent(repo, "older", _EDITED)
    await _agent(repo, "newer", "2026-02-01T00:00:00+00:00")

    # Two runtime writers: the durable workspace id an Agent-shared box mounts,
    # and a resident-sandbox pointer, in the shape runtime_manager writes it.
    await ensure_workspace_id(subject_kind="deployment_runtime", agent_id="older")
    await repo.compare_and_update_agent(
        "older",
        expected={},
        updates={"sandbox_id": "sb-1", "sandbox_backend": "open_sandbox"},
    )

    row = await repo.get_agent("older")
    assert row is not None and row["workspace_id"] and row["sandbox_id"] == "sb-1"
    assert row["updated_at"] == _EDITED
    assert [agent["agent_id"] for agent in await repo.list_all_agents()] == ["newer", "older"]


@pytest.mark.asyncio
async def test_an_authored_change_moves_an_agents_updated_at_and_an_unchanged_save_does_not() -> None:
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    repo = AgentRepository()
    await _agent(repo, "a-1", _EDITED)
    service = AgentConfigService(repo, environment_repo=_Environments())  # type: ignore[arg-type]
    bindings = CredentialBindingService(
        vault_service=_Vaults(),  # type: ignore[arg-type]
        agent_repo=repo,
        schedule_runtime_reconciliation=lambda _agent_id: None,
    )
    definition = {"name": "a-1", "model": "some-model", "environment_name": "claude-code"}

    await service.upsert_agent_config(_OWNER, "a-1", dict(definition))
    await service.set_agent_access(_OWNER, "a-1", {"visibility": "private"})
    await bindings.set_agent_binding(_OWNER, "a-1", [])
    unchanged = await repo.get_agent("a-1")
    assert unchanged is not None and unchanged["updated_at"] == _EDITED

    await service.upsert_agent_config(_OWNER, "a-1", {**definition, "description": "edited"})
    edited = await repo.get_agent("a-1")
    assert edited is not None and edited["updated_at"] > _EDITED
    assert edited["updated_by"] == "owner"

    await repo.update_agent("a-1", {"updated_at": _EDITED})
    await bindings.set_agent_binding(_OWNER, "a-1", ["vault-1"])
    rebound = await repo.get_agent("a-1")
    assert rebound is not None and rebound["updated_at"] > _EDITED


@pytest.mark.asyncio
async def test_an_assistants_workspace_id_does_not_move_its_updated_at() -> None:
    repo = AssistantCatalogRepository()
    await repo.create_assistant(
        {
            "assistant_id": "as-1",
            "owner_id": "owner",
            "display_name": "Helper",
            "environment_name": "hermes",
            "engine_kind": "assistant",
            "updated_at": _EDITED,
        }
    )

    await ensure_workspace_id(subject_kind="assistant_runtime", assistant_id="as-1")

    row = await repo.get_assistant("as-1")
    assert row is not None and row["workspace_id"]
    assert row["updated_at"] == _EDITED
