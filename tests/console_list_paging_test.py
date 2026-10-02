"""The Agent and Assistant lists reach every record, a page at a time.

The lists read one capped page of the whole collection, newest first, and then
dropped what the caller may not see: 200 Agents, and 50 Assistants across every
owner. A deployment past the cap lost Agents from the console list, the rail
count and the admin session scope, and an owner whose Assistants were not
among the deployment's 50 most recently edited saw none of them. These tests
drive the real repositories on SQLite past those caps.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.admin_service import AdminService
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.core.service.orchestrator.assistant.assistant_service import AssistantService
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
)
from astrabox.core.service.orchestrator.session_kernel.service import SessionKernelService
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
)
from astrabox.persistence.repository.assistant_workspace_repository import (
    AssistantWorkspaceRepository,
)
from astrabox.persistence.repository.backend import get_async_collection

#: More Agents than the former page of 200.
_AGENTS = 250
_VIEWER = UserContext(user_id="viewer", roles=[])


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stamp(index: int) -> str:
    """An ``updated_at`` that increases with ``index``."""

    return f"2026-01-01T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}+00:00"


async def _agents() -> dict[str, list[str]]:
    """250 Agents: every seventh is someone else's private one, every fifth disabled."""

    repo = AgentRepository()
    visible: list[str] = []
    enabled: list[str] = []
    for index in range(_AGENTS):
        name = f"agent-{index:03d}"
        hidden = index % 7 == 3
        disabled = index % 5 == 4
        await repo.create_agent(
            {
                "agent_id": f"id-{(_AGENTS - index):03d}",
                "name": name,
                "model": "model-x" if index % 2 else "model-y",
                "state": "ACTIVE",
                "enabled": not disabled,
                "visibility": "private" if hidden else "public",
                "created_by": "someone-else",
                "updated_at": _stamp(index),
            }
        )
        if not hidden:
            visible.append(name)
            if not disabled:
                enabled.append(name)
    return {"visible": visible, "enabled": enabled}


def _agent_service() -> AgentService:
    service = AgentService.__new__(AgentService)
    service._agent_repo = AgentRepository()  # type: ignore[attr-defined]

    async def _bootstrapped() -> None:
        return None

    service.ensure_bootstrap = _bootstrapped  # type: ignore[method-assign]
    return service


async def _all_pages(service: AgentService, **narrowing: Any) -> tuple[list[str], list[dict[str, Any]]]:
    names: list[str] = []
    pages: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = await service.list_agents_page(_VIEWER, limit=50, cursor=cursor, **narrowing)
        pages.append(page)
        names.extend(row["name"] for row in page["agents"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            return names, pages
        cursor = page["next_cursor"]


async def test_the_whole_agent_list_holds_every_agent_the_caller_may_see() -> None:
    seeded = await _agents()

    listed = await _agent_service().list_agents(_VIEWER)

    assert sorted(row["name"] for row in listed) == seeded["visible"]


async def test_the_agent_pages_reach_every_visible_agent_in_name_order() -> None:
    seeded = await _agents()

    names, pages = await _all_pages(_agent_service())

    assert names == seeded["visible"]
    assert [len(page["agents"]) for page in pages[:-1]] == [50] * (len(pages) - 1)
    # The first page counts the whole visible list; later pages do not repeat it.
    assert pages[0]["total"] == len(seeded["visible"])
    assert pages[0]["enabled"] == len(seeded["enabled"])
    assert all("total" not in page for page in pages[1:])


async def test_search_and_status_narrow_the_whole_list_before_it_is_paged() -> None:
    seeded = await _agents()
    service = _agent_service()

    # Every match lies past the first 200 Agents in either order.
    found, _pages = await _all_pages(service, query="AGENT-24")
    assert found == [name for name in seeded["visible"] if name.startswith("agent-24")]

    disabled, pages = await _all_pages(service, status="disabled")
    assert disabled == [name for name in seeded["visible"] if name not in seeded["enabled"]]
    # The counts describe the whole list, whatever the narrowing.
    assert pages[0]["total"] == len(seeded["visible"])

    by_model, _pages = await _all_pages(service, query="model-y", status="enabled")
    assert by_model == [
        name for name in seeded["enabled"] if int(name.split("-")[1]) % 2 == 0
    ]


async def test_a_cursor_or_status_the_list_did_not_hand_out_is_refused() -> None:
    service = _agent_service()

    with pytest.raises(APIError) as bad_cursor:
        await service.list_agents_page(_VIEWER, limit=50, cursor="not-a-cursor")
    with pytest.raises(APIError) as bad_status:
        await service.list_agents_page(_VIEWER, limit=50, status="paused")

    assert bad_cursor.value.status_code == bad_status.value.status_code == 400


class _AgentConfig:
    """The two reads the rail summary makes, the Agent one on the real repository."""

    def __init__(self) -> None:
        self.list_agent_access_docs = AgentRepository().list_agent_access_docs

    async def count_environment_configs(self) -> int:
        return 0


class _Sessions:
    def __init__(self) -> None:
        self.scopes: list[list[str]] = []

    async def count_all_sessions(self, *, template_names: list[str]) -> int:
        self.scopes.append(template_names)
        return 0


async def test_the_rail_and_the_admin_session_scope_count_every_agent() -> None:
    seeded = await _agents()
    admin = AdminService.__new__(AdminService)
    admin._agent_config = _AgentConfig()  # type: ignore[attr-defined]
    sessions = _Sessions()
    admin._sessions_repo = sessions  # type: ignore[attr-defined]
    operator = UserContext(user_id="operator", roles=["admin"])

    viewer_summary = await admin.admin_navigation_summary(_VIEWER)
    operator_summary = await admin.admin_navigation_summary(operator)

    assert viewer_summary["agents"] == len(seeded["visible"])
    assert operator_summary["agents"] == _AGENTS
    # An administrator manages every Agent, so every Agent's sessions are in scope.
    assert len(sessions.scopes[-1]) == _AGENTS


def _assistant_service() -> AssistantService:
    service = AssistantService.__new__(AssistantService)
    service._catalog_repo = AssistantCatalogRepository()  # type: ignore[attr-defined]
    service._workspace_service = AssistantWorkspaceService()  # type: ignore[attr-defined]
    return service


async def _assistant(repo: AssistantCatalogRepository, assistant_id: str, owner: str, updated_at: str) -> None:
    await repo.create_assistant(
        {
            "assistant_id": assistant_id,
            "owner_id": owner,
            "display_name": assistant_id.replace("-", " "),
            "engine_kind": "assistant",
            "environment_name": "env-1",
            "updated_at": updated_at,
        }
    )


async def test_an_owner_sees_their_assistants_behind_fifty_newer_ones_of_others() -> None:
    await EnvironmentRepository().upsert("env-1", {"engine_kind": "assistant"})
    repo = AssistantCatalogRepository()
    for index in range(3):
        await _assistant(repo, f"mine-{index}", "viewer", _stamp(index))
    for index in range(60):
        await _assistant(repo, f"theirs-{index:02d}", "someone-else", _stamp(100 + index))

    listed = await _assistant_service().list_assistants(_VIEWER)

    assert [row["assistant_id"] for row in listed] == ["mine-2", "mine-1", "mine-0"]


async def test_the_assistant_pages_reach_every_owned_assistant_with_its_workspace() -> None:
    await EnvironmentRepository().upsert("env-1", {"engine_kind": "assistant"})
    repo = AssistantCatalogRepository()
    owned = [f"mine-{index:03d}" for index in range(120)]
    for index, assistant_id in enumerate(owned):
        await _assistant(repo, assistant_id, "viewer", _stamp(index))
    await _assistant(repo, "theirs", "someone-else", _stamp(500))
    ready = owned[::3]
    workspaces = await get_async_collection(AssistantWorkspaceRepository()._collection_name)
    await workspaces.insert_many(
        [
            {"_id": f"ws-{assistant_id}", "assistant_id": assistant_id,
             "created_by_user_id": "viewer", "state": "READY", "current_sandbox_id": f"box-{assistant_id}"}
            for assistant_id in ready
        ]
    )
    service = _assistant_service()

    seen: list[str] = []
    states: dict[str, str] = {}
    cursor = None
    first: dict[str, Any] | None = None
    while True:
        page = await service.list_assistants_page(_VIEWER, limit=50, cursor=cursor)
        first = first or page
        seen.extend(row["assistant_id"] for row in page["assistants"])
        states.update({row["assistant_id"]: row["workspace_state"] for row in page["assistants"]})
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]

    assert seen == list(reversed(owned))
    assert {key for key, state in states.items() if state == "READY"} == set(ready)
    assert first is not None and first["total"] == len(owned) and first["ready"] == len(ready)

    dormant = await service.list_assistants_page(_VIEWER, limit=200, status="dormant", query="MINE-11")
    assert [row["assistant_id"] for row in dormant["assistants"]] == [
        assistant_id for assistant_id in reversed(owned)
        if assistant_id.startswith("mine-11") and assistant_id not in ready
    ]


async def test_agent_pages_order_names_regardless_of_case_across_page_boundaries() -> None:
    # Names that differ only in case, and ids that do not follow insertion,
    # so a page boundary falls inside a run of names that fold to one key.
    names = ["beta", "Alpha", "alpha", "Charlie", "bravo", "ALPHA", "delta", "Echo", "echo", "Foxtrot"]
    ids = ["id-7", "id-3", "id-9", "id-1", "id-5", "id-2", "id-8", "id-6", "id-4", "id-0"]
    repo = AgentRepository()
    for name, agent_id in zip(names, ids, strict=True):
        await repo.create_agent(
            {"agent_id": agent_id, "name": name, "state": "ACTIVE", "visibility": "public"}
        )
    service = _agent_service()

    seen: list[tuple[str, str]] = []
    cursor = None
    while True:
        page = await service.list_agents_page(_VIEWER, limit=2, cursor=cursor)
        seen.extend((row["name"], row["agent_id"]) for row in page["agents"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]

    expected = sorted(zip(names, ids, strict=True), key=lambda pair: (pair[0].casefold(), pair[1]))
    assert seen == expected, f"pages {seen}"
    # A rename moves the Agent to its new place in the order.
    await repo.update_agent("id-0", {"name": "aardvark"})
    first = await service.list_agents_page(_VIEWER, limit=1)
    assert [row["agent_id"] for row in first["agents"]] == ["id-0"]


async def test_reading_the_agent_list_writes_nothing() -> None:
    """A list read is a read: rows lacking the sort key are keyed at startup, not here.

    Counted at the database: every INSERT, UPDATE or DELETE statement either
    SQLite engine executes while the list is read.
    """
    from sqlalchemy import event

    from astrabox.persistence.repository.sqlite.engine import get_engine

    repo = AgentRepository()
    for name, agent_id in (("bravo", "id-2"), ("delta", "id-4")):
        await repo.create_agent({"agent_id": agent_id, "name": name, "state": "ACTIVE"})
    collection = await get_async_collection(repo._collection_name)
    await collection.insert_one({"agent_id": "id-3", "name": "Charlie", "state": "ACTIVE", "deleted": False})
    writes: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement)

    engines = [get_engine(mode="write").sync_engine, get_engine(mode="read").sync_engine]
    for engine in engines:
        event.listen(engine, "before_cursor_execute", _record)
    try:
        await _agent_service().list_agents_page(_VIEWER, limit=50)
        await _agent_service().list_agents_page(_VIEWER, limit=1)
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", _record)

    assert writes == []
    raw = await collection.find_one({"agent_id": "id-3"})
    assert raw is not None and "_name_key" not in raw


async def test_a_session_list_cursor_the_list_did_not_hand_out_is_a_400() -> None:
    service = SessionKernelService.__new__(SessionKernelService)
    service._sessions_repo = SessionRepository()  # type: ignore[attr-defined]

    for cursor in ("not-a-cursor", "e30"):
        with pytest.raises(APIError) as refused:
            await SessionKernelService.list_sessions_page(service, _VIEWER, limit=5, cursor=cursor)
        assert (refused.value.code, refused.value.status_code) == ("INVALID_REQUEST", 400), cursor
