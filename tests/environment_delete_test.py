"""Deleting a RuntimeProfile serializes with an Agent write that binds it, on the row lock."""
from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from sqlalchemy import text

import astrabox.providers.secret_store  # noqa: F401 - registers the local secret store

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.persistence.repository.assistant_catalog_repository import AssistantCatalogRepository
from astrabox.persistence.sql import fetch_value

ADMIN = UserContext(user_id="admin", roles=["admin"])


@pytest.fixture
def service(database: str, monkeypatch: pytest.MonkeyPatch) -> AgentConfigService:
    monkeypatch.setenv(
        "ASTRABOX_VAULT_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    )
    return AgentConfigService(
        AgentRepository(), RuntimeProfileRepository(), assistant_repo=AssistantCatalogRepository(),
    )


async def environment(service: Any, name: str, **fields: Any) -> dict[str, Any]:
    """Store a minimal RuntimeProfile the way the administrator path stores one."""
    return await service._runtime_profile_repo.put(
        name,
        {
            "engine_kind": "claude_code",
            "idle_action": "terminate",
            "networking": {"type": "limited", "allowed_hosts": [], "allow_mcp_servers": False},
            **fields,
        },
        updated_by="admin",
    )


async def agent(
    service: Any, agent_id: str, runtime_profile_name: str, *, deleted: bool = False, **fields: Any,
) -> dict[str, Any]:
    """Store an Agent naming ``runtime_profile_name``; ``deleted`` stores it already deleted."""
    return await service._agent_repo.create_agent({
        "agent_id": agent_id,
        "name": agent_id,
        "model": "test-model",
        "created_by": "owner",
        "runtime_profile_name": runtime_profile_name,
        "state": "DELETED" if deleted else "ACTIVE",
        **fields,
    })


async def _a_backend_waits_on_a_lock() -> None:
    """Return once some connection to this database is blocked on a row lock."""
    for _ in range(300):
        waiting = await fetch_value(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
        ))
        if waiting:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no connection waited on the RuntimeProfile row")


async def _race(
    first: Any, second: Any, holding: asyncio.Event, release: asyncio.Event,
) -> list[str]:
    """Run ``first`` until it holds the RuntimeProfile row, then ``second`` until it waits on it.

    ``first`` sets ``holding`` while it holds the row and waits for
    ``release``, which is set once ``second`` is seen waiting on a lock.
    Returns each one's outcome.
    """
    tasks = [asyncio.create_task(first())]
    try:
        await asyncio.wait_for(holding.wait(), 5)
        tasks.append(asyncio.create_task(second()))
        await asyncio.wait_for(_a_backend_waits_on_a_lock(), 5)
        release.set()
        return list(await asyncio.wait_for(asyncio.gather(*tasks), 5))
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _attempt_deletion(service: Any) -> str:
    try:
        await service.delete_runtime_profile_config(ADMIN, "target")
        return "deleted"
    except APIError as exc:
        return exc.code


@pytest.mark.parametrize("writer", ["create", "update"])
@pytest.mark.parametrize("first", ["binding", "deletion"])
async def test_deletion_serializes_with_agent_binding_writes(
    service: Any, monkeypatch: pytest.MonkeyPatch, writer: str, first: str,
) -> None:
    """An Agent naming the RuntimeProfile waits on the row through its foreign key."""
    from astrabox.persistence.repository import agent_repository
    from astrabox.persistence.repository.agent_repository import AgentReferenceMissing

    await environment(service, "target")
    await environment(service, "previous")
    if writer == "update":
        await agent(service, "owner", "previous")
    holding, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        holding.set()
        await release.wait()

    if first == "binding":
        # The Agent row is written and the transaction stays open, holding the
        # foreign key's lock on the RuntimeProfile row.
        original_children = agent_repository._replace_children

        async def held_children(conn: Any, agent_id: str, children: Any) -> None:
            await hold()
            await original_children(conn, agent_id, children)

        monkeypatch.setattr(agent_repository, "_replace_children", held_children)
    else:
        original_list = service._agent_repo.list_agents_by_runtime_profile

        async def held_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            await hold()
            return await original_list(*args, **kwargs)

        monkeypatch.setattr(service._agent_repo, "list_agents_by_runtime_profile", held_list)

    async def bind() -> str:
        try:
            if writer == "create":
                await agent(service, "owner", "target")
            else:
                current = await service._agent_repo.get_agent("owner")
                await service._agent_repo.update_definition(
                    "owner", expected_version=current["version"],
                    changes={"runtime_profile_name": "target"}, updated_by="owner",
                )
            return "bound"
        except AgentReferenceMissing as exc:
            return f"{exc.reference} missing"

    async def delete() -> str:
        return await _attempt_deletion(service)

    if first == "binding":
        outcomes = await _race(bind, delete, holding, release)
        assert outcomes == ["bound", "RUNTIME_PROFILE_IN_USE"]
        stored = await service._agent_repo.get_agent("owner")
        assert stored is not None and stored["runtime_profile_name"] == "target"
        assert await service.get_runtime_profile("target") is not None
    else:
        outcomes = await _race(delete, bind, holding, release)
        assert outcomes == ["deleted", "environment missing"]
        stored = await service._agent_repo.get_agent("owner")
        assert stored is None or stored["runtime_profile_name"] == "previous"
        assert await service.get_runtime_profile("target") is None
