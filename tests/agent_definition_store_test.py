"""Every authored change to an Agent moves its version; catalog writes hold under races."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, insert, select

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent import agent_catalog
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.credential_binding_service import (
    CredentialBindingService,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.persistence.sql import execute, fetch_value
from astrabox.persistence.sql.models.agents import AgentCategoryLink, AgentFavorite
from astrabox.persistence.sql.models.vaults import Vault
from astrabox.providers import register_builtin_providers

pytestmark = pytest.mark.usefixtures("database")

_OWNER = UserContext(user_id="owner", roles=[])


class _Vaults:
    """Every requested vault is a valid binding; this file is not about vaults."""

    async def validate_binding_ids(
        self, user: Any, vault_ids: list[str], *, target_type: str
    ) -> list[str]:
        return list(vault_ids)

    async def describe_binding(self, user: Any, vault_ids: list[str]) -> list[dict[str, Any]]:
        return [{"vault_id": vault_id} for vault_id in vault_ids]


async def _service() -> AgentConfigService:
    register_builtin_providers()
    runtime_profiles = RuntimeProfileRepository()
    await runtime_profiles.put(
        "claude-env",
        {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    repo = AgentRepository()
    await repo.create_agent(
        {
            "agent_id": "agent-1",
            "name": "researcher",
            "model": "some-model",
            "runtime_profile_name": "claude-env",
            "created_by": "owner",
            "visibility": "private",
        }
    )
    return AgentConfigService(repo, runtime_profiles)


def _edit(**fields: Any) -> dict[str, Any]:
    return {"model": "some-model", "runtime_profile_name": "claude-env", **fields}


async def test_two_cosmetic_edits_of_one_version_cannot_both_be_saved() -> None:
    service = await _service()

    async def save(display_name: str) -> str:
        try:
            await service.upsert_agent_config(
                _OWNER,
                "agent-1",
                _edit(version=1, display_meta={"display_name": display_name}),
            )
            return "saved"
        except APIError as exc:
            return exc.code

    outcomes = await asyncio.gather(*(save(f"Name {n}") for n in range(4)))

    assert sorted(outcomes) == ["AGENT_VERSION_CONFLICT"] * 3 + ["saved"]
    stored = await service._agent_repo.get_agent("agent-1")
    assert stored is not None and stored["version"] == 2


async def test_access_and_vault_bindings_move_the_version_a_draft_was_read_at() -> None:
    service = await _service()
    await execute(insert(Vault.__table__).values(
        vault_id="vault-1", org_id="default", created_by="owner", display_name="Vault 1",
    ))
    bindings = CredentialBindingService(
        vault_service=_Vaults(),  # type: ignore[arg-type]
        agent_repo=service._agent_repo,
        schedule_runtime_reconciliation=lambda _agent_id: None,
    )

    await service.set_agent_access(_OWNER, "agent-1", {"visibility": "public"})
    await bindings.set_agent_binding(_OWNER, "agent-1", ["vault-1"])

    stored = await service._agent_repo.get_agent("agent-1")
    assert stored is not None
    assert (stored["version"], stored["visibility"], stored["credential_vault_ids"]) == (
        3, "public", ["vault-1"],
    )
    # A console draft read before either change would put them back; it is refused.
    with pytest.raises(APIError) as refused:
        await service.upsert_agent_config(_OWNER, "agent-1", _edit(version=1, description="draft"))
    assert refused.value.code == "AGENT_VERSION_CONFLICT"


async def test_deleting_a_category_unbinds_it_atomically_and_refuses_a_stale_draft() -> None:
    service = await _service()
    category = await agent_catalog.create_category("Research")
    saved = await service.upsert_agent_config(
        _OWNER, "agent-1", _edit(version=1, categories=[category["category_id"]]),
    )
    assert saved["categories"] == [category["category_id"]]

    assert await agent_catalog.delete_category(category["category_id"]) == 1

    stored = await service._agent_repo.get_agent("agent-1")
    assert stored is not None and stored["categories"] == [] and stored["version"] == 3
    assert await fetch_value(select(func.count()).select_from(AgentCategoryLink.__table__)) == 0
    # A draft read before the delete would save the definition it read,
    # categories included; it is refused as stale.
    with pytest.raises(APIError) as refused:
        await service.upsert_agent_config(_OWNER, "agent-1", _edit(version=2, description="draft"))
    assert refused.value.code == "AGENT_VERSION_CONFLICT"


async def test_two_categories_created_with_one_name_leave_one() -> None:
    async def create() -> str:
        try:
            await agent_catalog.create_category("Research")
            return "created"
        except APIError as exc:
            return exc.code

    outcomes = await asyncio.gather(*(create() for _ in range(6)))

    assert sorted(outcomes) == ["AGENT_CATEGORY_NAME_TAKEN"] * 5 + ["created"]
    assert [row["name"] for row in await agent_catalog.list_categories()] == ["Research"]


async def test_concurrent_first_favorites_of_one_agent_all_succeed_as_one() -> None:
    await _service()
    favorites = agent_catalog.favorite_repository()

    await asyncio.gather(*(favorites.add("viewer", "agent-1") for _ in range(8)))

    assert await favorites.agent_ids_for("viewer") == {"agent-1"}
    assert await fetch_value(select(func.count()).select_from(AgentFavorite.__table__)) == 1
