"""Assistant cleanup policy with real repository projections and in-memory rows.

These cases do not execute native database transactions or browser E2E.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.persistence.repository.assistant_catalog_repository import AssistantCatalogRepository
from astrabox.persistence.repository.assistant_workspace_repository import AssistantWorkspaceRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.sqlite.collection import _project
from astrabox.persistence.repository.sqlite.query import matches


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def find(self, query: dict[str, Any], *, projection: dict[str, Any]) -> Any:
        async def iterate() -> Any:
            for row in self.rows:
                if matches(row, query):
                    yield _project(deepcopy(row), projection)
        return iterate()


@pytest.fixture
def cleanup() -> Any:
    catalog = AssistantCatalogRepository()
    workspaces = AssistantWorkspaceRepository()
    sessions = SessionRepository()
    owner = {
        "assistant_id": "retired-assistant", "environment_name": "retired",
        "display_name": "Retired Assistant", "deleted": True, "system": "private prompt",
    }
    workspace = {
        "assistant_id": owner["assistant_id"], "state": "RECOVERY_REQUIRED",
        "current_sandbox_id": None, "provisioning_session_id": "historical-materializer",
        "assistant_profiles": {"private-user": {"private": "profile"}},
    }
    session = {
        "session_id": "historical-materializer", "state": "TERMINATED",
        "workspace_ref": {"kind": "assistant", "assistant_id": owner["assistant_id"]},
        "owner_id": owner["assistant_id"], "deleted": True, "hidden": True,
        "sandbox_id": None, "title": "private title",
    }
    rows = {
        catalog._collection_name: _Rows([owner]),
        workspaces._collection_name: _Rows([workspace]),
        sessions._collection_name: _Rows([session]),
    }
    state = SimpleNamespace(owner=owner, workspace=workspace, session=session, deleted=False)

    async def remove(name: str, check: Any) -> None:
        assert name == "retired"
        await check(SimpleNamespace(collection=rows.__getitem__))
        state.deleted = True

    state.service = AgentConfigService(
        SimpleNamespace(list_agents_by_environment=AsyncMock(return_value=[])),
        SimpleNamespace(delete_after_reference_check=remove),
        assistant_repo=catalog, sessions_repo=sessions,
    )
    return state


ADMIN = UserContext(user_id="admin", roles=["admin"])


async def _refused(cleanup: Any) -> None:
    with pytest.raises(APIError) as refused:
        await cleanup.service.delete_environment_config(ADMIN, "retired")
    assert refused.value.code == "ENVIRONMENT_IN_USE"
    assert refused.value.data == {"holders": [{
        "target_type": "assistant", "target_id": "retired-assistant", "target_name": "Retired Assistant",
    }]}
    assert cleanup.deleted is False


@pytest.mark.parametrize("fallback", [False, True], ids=["workspace-identity", "owner-fallback"])
@pytest.mark.parametrize("remaining", [
    {"sandbox_id": "pending-box"},
    {"undestroyed_sandbox_ids": ["old-box"]},
    {"state": "CREATING"},
    {"startup_allocation": {"sandbox_id": "pending-box", "sandbox_backend": "test", "scope": "sandbox"}},
    {"_retained_startup_allocations": [{"allocation": {
        "sandbox_id": "shared-box", "sandbox_backend": "test", "scope": "isolated_sessions",
        "isolated_session_ids": ["pending-engine"],
    }}]},
])
async def test_deleted_assistant_session_retains_environment_until_cleanup(
    cleanup: Any, fallback: bool, remaining: dict[str, Any],
) -> None:
    if fallback:
        cleanup.session["workspace_ref"].pop("assistant_id")
    cleanup.session.update(remaining)
    await _refused(cleanup)
    cleanup.session.update({
        "state": "TERMINATED", "sandbox_id": None, "undestroyed_sandbox_ids": [],
        "startup_allocation": None, "_retained_startup_allocations": [],
    })
    assert await cleanup.service.delete_environment_config(ADMIN, "retired") == {
        "name": "retired", "deleted": True,
    }
    assert cleanup.session["title"] == "private title"
    assert cleanup.workspace["provisioning_session_id"] == "historical-materializer"


@pytest.mark.parametrize("remaining", [
    {"current_sandbox_id": "pending-box"},
    {"state": "MATERIALIZING", "provisioning_session_id": "in-flight"},
])
async def test_deleted_assistant_workspace_retains_environment_until_cleanup(
    cleanup: Any, remaining: dict[str, Any],
) -> None:
    cleanup.workspace.update(remaining)
    await _refused(cleanup)
    cleanup.workspace.update({"state": "RECOVERY_REQUIRED", "current_sandbox_id": None})
    assert await cleanup.service.delete_environment_config(ADMIN, "retired") == {
        "name": "retired", "deleted": True,
    }


@pytest.mark.parametrize("workspace_ref", [
    {"kind": "agent", "assistant_id": "retired-assistant"},
    {"kind": "assistant", "assistant_id": "another-assistant"},
])
async def test_unrelated_session_owner_does_not_retain_environment(cleanup: Any, workspace_ref: Any) -> None:
    cleanup.session.update({"workspace_ref": workspace_ref, "sandbox_id": "unrelated-box"})
    assert await cleanup.service.delete_environment_config(ADMIN, "retired") == {
        "name": "retired", "deleted": True,
    }
    assert cleanup.session["sandbox_id"] == "unrelated-box"


async def test_resource_free_assistant_history_does_not_retain_environment(cleanup: Any) -> None:
    assert await cleanup.service.delete_environment_config(ADMIN, "retired") == {
        "name": "retired", "deleted": True,
    }
    assert cleanup.session["title"] == "private title"
    assert cleanup.workspace["assistant_profiles"] == {"private-user": {"private": "profile"}}
