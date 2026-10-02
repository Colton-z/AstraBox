"""Preset deletion refuses every live configuration reference, across owners."""
from __future__ import annotations

import asyncio
import importlib
import os
import uuid

from pathlib import Path
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.persistence.repository import AgentRepository, EnvironmentRepository
from astrabox.persistence.repository.assistant_catalog_repository import AssistantCatalogRepository
from astrabox.persistence.repository.backend import get_async_collection
from tests._postgresql_support import resolve_postgresql_test_url


@pytest.fixture(params=[
    pytest.param("sqlite", id="sqlite"),
    pytest.param("postgresql", id="postgresql", marks=pytest.mark.postgresql),
    pytest.param("mongo", id="mongo", marks=pytest.mark.mongo_transaction),
])
async def service(request: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = request.param
    url = (
        resolve_postgresql_test_url() if backend == "postgresql"
        else os.environ.get("ASTRABOX_DB_URL", "") if backend == "mongo"
        else f"sqlite+aiosqlite:///{tmp_path}/environment-delete.sqlite"
    )
    assert url, "configure ASTRABOX_DB_URL for the Mongo replica-set lane"
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", backend)
    monkeypatch.setenv("ASTRABOX_DB_URL", url)
    monkeypatch.setenv("ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED", "true")
    get_settings.cache_clear()
    # Only these UUID namespaces belong to this test, even on a shared DB.
    prefix = f"ed_{uuid.uuid4().hex}"
    names = {
        field: f"{prefix}_{index}" for index, field in enumerate((
            "environment_collection", "agents_collection", "sessions_collection",
            "assistant_catalog_collection", "assistant_workspace_collection",
        ))
    }
    settings = load_astrabox_settings().model_copy(update=names)
    for module in (
        "environment_repository", "agent_repository", "session_repository",
        "assistant_catalog_repository", "assistant_workspace_repository",
    ):
        monkeypatch.setattr(
            importlib.import_module(f"astrabox.persistence.repository.{module}"),
            "load_astrabox_settings", lambda: settings,
        )
    try:
        yield AgentConfigService(
            AgentRepository(), EnvironmentRepository(), assistant_repo=AssistantCatalogRepository(),
        )
    finally:
        try:
            if backend == "mongo":
                from astrabox.persistence.repository import mongo
                database = await mongo._get_database()
                for name in names.values():
                    await database.drop_collection(name)
            else:
                for name in names.values():
                    collection = await get_async_collection(name)
                    await collection.delete_many({})
        finally:
            if backend == "mongo":
                from astrabox.persistence.repository import mongo
                await mongo.close_for_current_loop("environment_delete_acceptance")
            else:
                from astrabox.persistence.repository.sqlite.engine import dispose_engines
                await dispose_engines(url)
            get_settings.cache_clear()


ADMIN = UserContext(user_id="admin", roles=["admin"])


async def test_unused_environment_is_removed_without_touching_its_neighbor(service: Any) -> None:
    await service._environment_repo.upsert("unused", {"provider_access": {"api_key": "secret"}})
    await service._environment_repo.upsert("keep", {"enabled": True})
    result = await service.delete_environment_config(ADMIN, "unused")
    assert result == {"name": "unused", "deleted": True}
    assert await service.get_environment("unused") is None
    assert await service.get_environment("keep") is not None
    assert [row["name"] for row in await service.list_environment_configs(ADMIN)] == ["keep"]
    assert await service.count_environment_configs() == 1
    with pytest.raises(APIError) as missing:
        await service.delete_environment_config(ADMIN, "unused")
    assert missing.value.status_code == 404


@pytest.mark.parametrize("enabled", [True, False])
async def test_live_agent_refuses_deletion_even_if_disabled(service: Any, enabled: bool) -> None:
    await service._environment_repo.upsert("used", {"enabled": False})
    await service._agent_repo.create_agent({
        "agent_id": "agent-a", "name": "a", "enabled": enabled,
        "environment_name": "used", "display_meta": {"display_name": "Research"},
        "system": "private prompt",
    })
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(ADMIN, "used")
    assert (refused.value.code, refused.value.status_code) == ("ENVIRONMENT_IN_USE", 409)
    assert refused.value.data == {"holders": [{
        "target_type": "agent", "target_id": "agent-a", "target_name": "Research",
    }]}
    assert "private prompt" not in str(refused.value.data)
    assert await service.get_environment("used") is not None
    assert await service._agent_repo.get_agent("agent-a") is not None


async def test_all_owners_assistants_and_agents_are_reported_together(service: Any) -> None:
    await service._environment_repo.upsert("used", {})
    await service._agent_repo.create_agent({
        "agent_id": "agent-a", "name": "a", "environment_name": "used",
    })
    for owner in ("admin", "another-user"):
        await service._assistant_repo.create_assistant({
            "assistant_id": owner, "owner_id": owner, "environment_name": "used",
            "display_name": f"Workspace {owner}",
        })
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(ADMIN, "used")
    assert {holder["target_id"] for holder in refused.value.data["holders"]} == {
        "agent-a", "admin", "another-user",
    }
    assert await service.get_environment("used") is not None


async def test_deleted_owners_do_not_keep_an_unused_preset(service: Any) -> None:
    await service._environment_repo.upsert("retired", {})
    await service._agent_repo.create_agent({
        "agent_id": "agent-a", "name": "a", "environment_name": "retired", "deleted": True,
    })
    await service._assistant_repo.create_assistant({
        "assistant_id": "asst-a", "environment_name": "retired", "deleted": True,
    })
    assert await service.delete_environment_config(ADMIN, "retired") == {
        "name": "retired", "deleted": True,
    }


@pytest.mark.parametrize("remaining", [
    {"sandbox_id": "pending-box"},
    {"sandbox_id": None, "undestroyed_sandbox_ids": ["pending-box"]},
    {"sandbox_id": "current-box", "undestroyed_sandbox_ids": ["older-box"]},
    {"_prepared_slot": {"state": "claimed", "sandbox_id": "claimed-box", "claimed_session_id": "handoff"}},
    {"_prepared_slot": {"state": "prepared", "sandbox_id": "prepared-box"}},
    {"_client_pool_name": "pending-supplier"},
    {"_retiring_client_pools": [{"name": "previous-supplier", "backend": "open_sandbox"}]},
])
async def test_deleted_agent_retains_environment_until_compute_cleanup_finishes(
    service: Any, remaining: dict[str, Any],
) -> None:
    await service._environment_repo.upsert("retired", {"enabled": False})
    await service._agent_repo.create_agent({
        "agent_id": "deleted-owner", "name": "Retired Agent", "deleted": True,
        "environment_name": "retired", **remaining,
    })
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(ADMIN, "retired")
    assert (refused.value.code, refused.value.status_code) == ("ENVIRONMENT_IN_USE", 409)
    assert refused.value.data == {"holders": [{
        "target_type": "agent", "target_id": "deleted-owner", "target_name": "Retired Agent",
    }]}
    assert await service.get_environment("retired") is not None
    row = await service._agent_repo.get_agent("deleted-owner", include_deleted=True)
    assert row is not None
    for key, value in remaining.items():
        assert row[key] == value

    # The cleanup owner has now confirmed every retained box is gone.
    await service._agent_repo.update_agent("deleted-owner", {
        "sandbox_id": None, "undestroyed_sandbox_ids": [],
        "_prepared_slot": None, "_client_pool_name": None, "_retiring_client_pools": [],
    })
    assert await service.delete_environment_config(ADMIN, "retired") == {
        "name": "retired", "deleted": True,
    }


@pytest.mark.parametrize(("failed_reader", "method"), [
    ("_agent_repo", "list_agents_by_environment"),
    ("_assistant_repo", "list_assistants_by_environment"),
    ("_sessions_repo", "list_agent_runtime_references"),
    ("_sessions_repo", "list_assistant_runtime_references"),
    ("_assistant_workspace_repo", "list_runtime_references"),
])
async def test_reference_read_failure_never_deletes(
    service: Any, monkeypatch: pytest.MonkeyPatch, failed_reader: str, method: str,
) -> None:
    await service._environment_repo.upsert("uncertain", {})

    async def unavailable(name: str, *, transaction: Any = None) -> list[dict[str, Any]]:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(getattr(service, failed_reader), method, unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.delete_environment_config(ADMIN, "uncertain")
    assert await service.get_environment("uncertain") is not None


@pytest.mark.parametrize("owner_fallback", [False, True])
async def test_deleted_assistant_retained_startup_uses_workspace_identity(
    service: Any, owner_fallback: bool,
) -> None:
    await service._environment_repo.upsert("retired", {})
    await service._assistant_repo.create_assistant({
        "assistant_id": "retired-assistant", "environment_name": "retired", "deleted": True,
    })
    workspace_ref = {"kind": "assistant"}
    if not owner_fallback:
        workspace_ref["assistant_id"] = "retired-assistant"
    await service._sessions_repo.create_session({
        "session_id": "materializer", "workspace_ref": workspace_ref,
        "owner_id": "retired-assistant", "hidden": True, "deleted": True,
        "state": "TERMINATED", "sandbox_id": None,
        "_retained_startup_allocations": [{"allocation": {
            "scope": "isolated_sessions", "sandbox_id": "shared-box", "sandbox_backend": "test",
            "isolated_session_ids": ["pending-engine"],
        }}],
    })
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(ADMIN, "retired")
    assert refused.value.data == {"holders": [{
        "target_type": "assistant", "target_id": "retired-assistant", "target_name": "retired-assistant",
    }]}
    assert await service.get_environment("retired") is not None
    await service._sessions_repo.update_session("materializer", {"_retained_startup_allocations": []})
    assert await service.delete_environment_config(ADMIN, "retired") == {"name": "retired", "deleted": True}
    assert await service._sessions_repo.get_session_including_deleted("materializer") is not None


@pytest.mark.parametrize("remaining", [
    {"state": "RECOVERY_REQUIRED", "current_sandbox_id": "pending-box"},
    {"state": "MATERIALIZING", "provisioning_session_id": "starting"},
])
async def test_deleted_assistant_workspace_reference_is_kept_until_disposed(
    service: Any, remaining: dict[str, Any],
) -> None:
    await service._environment_repo.upsert("retired", {})
    await service._assistant_repo.create_assistant({
        "assistant_id": "retired-assistant", "environment_name": "retired", "deleted": True,
    })
    collection = await get_async_collection(service._assistant_workspace_repo._collection_name)
    await collection.insert_one({
        "assistant_id": "retired-assistant", "deleted": True,
        "assistant_profiles": {"private-user": {"profile": "private"}}, **remaining,
    })
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(ADMIN, "retired")
    assert refused.value.code == "ENVIRONMENT_IN_USE"
    assert "private-user" not in str(refused.value.data)
    await collection.update_one({"assistant_id": "retired-assistant"}, {"$set": {
        "state": "RECOVERY_REQUIRED", "current_sandbox_id": None,
    }})
    assert await service.delete_environment_config(ADMIN, "retired") == {"name": "retired", "deleted": True}
    assert (await collection.find_one({"assistant_id": "retired-assistant"}))["assistant_profiles"]


@pytest.mark.parametrize("writer", [
    "agent-create", "agent-update", "agent-compare-update", "assistant-create", "assistant-update",
])
@pytest.mark.parametrize("first", ["binding", "deletion"])
async def test_deletion_serializes_with_every_binding_write(
    service: Any, monkeypatch: pytest.MonkeyPatch, writer: str, first: str,
) -> None:
    if get_settings().db_backend == "mongo":
        from astrabox.persistence.repository.mongo.transaction import _TransactionCollection as Collection
    else:
        from astrabox.persistence.repository.sqlite.collection import AsyncCollection as Collection

    await service._environment_repo.upsert("target", {})
    await service._environment_repo.upsert("previous", {})
    # Lazy index preparation belongs outside the competing transactions.
    await service._agent_repo._ensure_indexes()
    await service._assistant_repo._ensure_indexes()
    if writer in {"agent-update", "agent-compare-update"}:
        await service._agent_repo.create_agent({
            "agent_id": "owner", "name": "Owner", "environment_name": "previous", "version": 1,
        })
    elif writer == "assistant-update":
        await service._assistant_repo.create_assistant({
            "assistant_id": "owner", "environment_name": "previous",
        })

    guarded = asyncio.Event()
    competitor_started = asyncio.Event()
    original_lock = Collection.lock_one
    first_arrived = False

    async def hold_first_parent(self: Any, query: dict[str, Any]) -> Any:
        nonlocal first_arrived
        if query != {"name": "target"}:
            return await original_lock(self, query)
        owns_first = not first_arrived
        first_arrived = True
        if not owns_first:
            competitor_started.set()
        row = await original_lock(self, query)
        if owns_first:
            guarded.set()
            await competitor_started.wait()
        return row

    monkeypatch.setattr(Collection, "lock_one", hold_first_parent)

    async def bind() -> Any:
        if writer == "agent-create":
            return await service._agent_repo.create_agent({
                "agent_id": "owner", "name": "Owner", "environment_name": "target",
            })
        if writer == "agent-update":
            return await service._agent_repo.update_agent("owner", {"environment_name": "target"})
        if writer == "agent-compare-update":
            return await service._agent_repo.compare_and_update_agent(
                "owner", expected={"version": 1},
                updates={"environment_name": "target", "version": 2},
            )
        if writer == "assistant-create":
            return await service._assistant_repo.create_assistant({
                "assistant_id": "owner", "environment_name": "target",
            })
        return await service._assistant_repo.update_assistant("owner", {"environment_name": "target"})

    async def attempt(kind: str) -> str:
        try:
            if kind == "binding":
                await bind()
                return "bound"
            await service.delete_environment_config(ADMIN, "target")
            return "deleted"
        except APIError as exc:
            return exc.code

    tasks = [asyncio.create_task(attempt(first))]
    try:
        await asyncio.wait_for(guarded.wait(), 3)
        other = "deletion" if first == "binding" else "binding"
        tasks.append(asyncio.create_task(attempt(other)))
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 3)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert outcomes == (
        ["bound", "ENVIRONMENT_IN_USE"] if first == "binding"
        else ["deleted", "ENVIRONMENT_NOT_FOUND"]
    )
    if writer.startswith("agent"):
        owner = await service._agent_repo.get_agent("owner")
    else:
        owner = await service._assistant_repo.get_assistant("owner")
    if first == "binding":
        assert owner is not None and owner["environment_name"] == "target"
        assert await service.get_environment("target") is not None
    else:
        assert owner is None or owner["environment_name"] == "previous"
        assert await service.get_environment("target") is None


async def test_ordinary_runtime_updates_do_not_acquire_environment_transactions(
    service: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.persistence.repository import environment_repository

    await service._environment_repo.upsert("env", {})
    await service._agent_repo.create_agent({
        "agent_id": "a", "name": "A", "environment_name": "env",
    })
    await service._assistant_repo.create_assistant({
        "assistant_id": "b", "environment_name": "env",
    })

    async def unexpected_transaction() -> Any:
        raise AssertionError("runtime bookkeeping must not enter the Environment transaction")

    monkeypatch.setattr(environment_repository, "document_transaction_runner", unexpected_transaction)
    assert await service._agent_repo.update_agent("a", {"sandbox_id": "agent-box"})
    assert await service._agent_repo.compare_and_update_agent(
        "a", expected={"sandbox_id": "agent-box"}, updates={"sandbox_id": "next-box"},
    )
    assert await service._assistant_repo.update_assistant("b", {"workspace_id": "workspace"})


async def test_backend_without_transactions_refuses_delete_before_touching_data(
    service: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.persistence.repository import environment_repository

    async def unavailable() -> None:
        return None

    monkeypatch.setattr(environment_repository, "document_transaction_runner", unavailable)
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(ADMIN, "anything")
    assert (refused.value.code, refused.value.status_code) == ("ENVIRONMENT_DELETE_UNSUPPORTED", 503)


@pytest.mark.parametrize("remaining", [
    {"state": "READY", "sandbox_id": "live-conversation-box"},
    {"state": "TERMINATED", "sandbox_id": "pending-destroy"},
    {"state": "RECOVERY_REQUIRED", "undestroyed_sandbox_ids": ["previous-box"]},
    {"state": "CREATING", "sandbox_id": None},
    {"state": "TERMINATED", "startup_allocation": {
        "sandbox_id": "shared-box", "sandbox_backend": "open_sandbox",
        "scope": "isolated_sessions", "isolated_session_ids": ["failed-engine"],
    }},
    {"state": "TERMINATED", "_retained_startup_allocations": [{
        "allocation": {
            "sandbox_id": "superseded-box", "sandbox_backend": "open_sandbox", "scope": "sandbox",
        },
        "sandbox_generation": "previous", "assignment_id": "superseded-start",
    }]},
])
@pytest.mark.parametrize("hidden_deleted", [False, True])
async def test_deleted_agent_conversation_runtime_retains_environment_until_disposed(
    service: Any, remaining: dict[str, Any], hidden_deleted: bool,
) -> None:
    await service._environment_repo.upsert("retired", {})
    await service._agent_repo.create_agent({
        "agent_id": "retired-agent", "name": "Retired", "environment_name": "retired", "deleted": True,
    })
    await service._sessions_repo.create_session({
        "session_id": "conversation", "agent_id": "retired-agent",
        "hidden": hidden_deleted, "deleted": hidden_deleted,
        "user_id": "another-user", "title": "private conversation title", **remaining,
    })
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(ADMIN, "retired")
    assert refused.value.code == "ENVIRONMENT_IN_USE"
    assert refused.value.data == {"holders": [{
        "target_type": "agent", "target_id": "retired-agent", "target_name": "Retired",
    }]}
    assert await service.get_environment("retired") is not None

    await service._sessions_repo.update_session("conversation", {
        "state": "TERMINATED", "sandbox_id": None, "undestroyed_sandbox_ids": [],
        "startup_allocation": None, "_retained_startup_allocations": [],
    })
    assert await service.delete_environment_config(ADMIN, "retired") == {"name": "retired", "deleted": True}
    assert await service._sessions_repo.get_session_including_deleted("conversation") is not None


async def test_cleanup_check_covers_history_beyond_the_normal_list_page(service: Any) -> None:
    await service._environment_repo.upsert("retired", {})
    await service._agent_repo.create_agent({
        "agent_id": "retired-agent", "name": "Retired", "environment_name": "retired", "deleted": True,
    })
    await service._sessions_repo.create_session({
        "session_id": "old-pending", "agent_id": "retired-agent", "state": "TERMINATED",
        "deleted": True, "sandbox_id": "old-unconfirmed-box", "updated_at": "2000-01-01T00:00:00Z",
    })
    for index in range(201):
        await service._sessions_repo.create_session({
            "session_id": f"history-{index}", "agent_id": "retired-agent", "state": "TERMINATED",
            "sandbox_id": None, "updated_at": "2026-01-01T00:00:00Z",
        })
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(ADMIN, "retired")
    assert refused.value.code == "ENVIRONMENT_IN_USE"
    await service._sessions_repo.update_session("old-pending", {"sandbox_id": None})
    assert await service.delete_environment_config(ADMIN, "retired") == {"name": "retired", "deleted": True}
    assert len(await service._sessions_repo.list_sessions_by_agent("retired-agent", limit=1000)) == 201
