"""An Assistant is one owner's: missing and foreign Assistants share one
non-disclosing 404.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from typing import Any

import pytest

import astrabox.providers as providers
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.assistant.assistant_service import (
    AssistantService,
)


_SPAWNED: list[Any] = []


def _run_now(coroutine, *, name: str | None = None):
    """Collect a spawned commit so a test can await it deliberately.

    The production spawner detaches the commit from the request, which is the
    point: the rollback must run whether or not a caller is listening. A test
    that asserts on the commit's outcome therefore has to say when it wants it,
    rather than race a task it never named.
    """
    import asyncio

    task = asyncio.ensure_future(coroutine)
    _SPAWNED.append(task)
    return task


providers.register_builtin_providers()


class _FakeCatalogRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create_assistant(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.rows[payload["assistant_id"]] = {**payload, "version": 1}
        return dict(self.rows[payload["assistant_id"]])

    async def delete_assistant(self, assistant_id: str, *, owner_id: str) -> bool:
        row = self.rows.get(assistant_id)
        if row is None or row.get("owner_id") != owner_id:
            return False
        del self.rows[assistant_id]
        return True

    async def get_assistant(self, assistant_id: str) -> dict[str, Any] | None:
        row = self.rows.get(assistant_id)
        return dict(row) if row else None


class _FakeTemplateService:
    async def get_runtime_profile(self, name: str) -> Any:
        # The Assistant selects a RuntimeProfile, including its Agent program and
        # model access; it does not reference a separate template. It is
        # unrestricted so the example MCP host reaches no resolver; what a
        # limited RuntimeProfile admits is author_boundary_test's subject.
        return {
            "name": name,
            "enabled": True,
            "engine_kind": "assistant",
            "networking": {"type": "unrestricted"},
        }


class _FakeWorkspaceService:
    def __init__(self) -> None:
        self.materialized: list[str] = []
        self.workspace: dict[str, Any] | None = None
        self.post_commit_failures: list[dict[str, Any]] = []
        self.hibernated: list[str] = []
        self.transitions: list[dict[str, Any]] = []
        self.released: list[dict[str, Any]] = []
        #: whether the release compare-and-set wins (False = a concurrent
        #: transition took the workspace out of RECOVERY_REQUIRED first).
        self.release_wins = True
        self.hibernate_calls: list[dict[str, Any]] = []
        self.recovery_required: list[dict[str, Any]] = []
        self.adopted: list[dict[str, Any]] = []
        self.hibernation_begun: list[dict[str, Any]] = []
        self.release_required: list[dict[str, Any]] = []
        #: whether the hibernate compare-and-set wins (False = the row moved to a
        #: different sandbox while this hibernate was reading it).
        self.hibernate_wins = True

    async def materialize_workspace_if_absent(self, **kwargs: Any) -> dict[str, Any]:
        self.materialized.append(str(kwargs.get("assistant_id")))
        if self.workspace is None:
            self.workspace = {
                "assistant_id": kwargs.get("assistant_id"),
                "engine_kind": kwargs.get("engine_kind"),
                "state": "MATERIALIZING",
                "current_sandbox_id": None,
                "provisioning_session_id": kwargs.get(
                    "provisioning_session_id"
                ),
            }
        return dict(self.workspace or {})

    async def claim_materialization(self, **kwargs: Any) -> bool:
        self.transitions.append(
            {
                "expected_state": kwargs.get("expected_state"),
                "new_state": "MATERIALIZING",
                "extra_updates": {
                    "provisioning_session_id": kwargs.get(
                        "provisioning_session_id"
                    )
                },
            }
        )
        if self.workspace is not None:
            self.workspace.update(
                {
                    "state": "MATERIALIZING",
                    "provisioning_session_id": kwargs.get(
                        "provisioning_session_id"
                    ),
                }
            )
        return True

    async def mark_materialization_failed(self, **kwargs: Any) -> bool:
        return True

    async def has_assistant_workspace(self, *, assistant_id: str) -> bool:
        _ = assistant_id
        return False

    async def get_workspace(
        self, *, user_id: str, assistant_id: str
    ) -> dict[str, Any] | None:
        _ = (user_id, assistant_id)
        return self.workspace

    async def mark_post_commit_failure(self, **kwargs: Any) -> bool:
        self.post_commit_failures.append(dict(kwargs))
        return True

    async def finish_hibernation(self, **kwargs: Any) -> bool:
        self.hibernated.append(str(kwargs.get("assistant_id")))
        self.hibernate_calls.append(dict(kwargs))
        if self.workspace is not None:
            self.workspace.update(
                {
                    "state": "HIBERNATING",
                    "current_sandbox_id": None,
                    "runtime_identity": None,
                    "provisioning_session_id": None,
                    "provisioning_sandbox_generation": None,
                }
            )
        return True

    async def mark_recovery_required(self, **kwargs: Any) -> bool:
        self.recovery_required.append(dict(kwargs))
        return True

    async def adopt_undestroyed_sandbox(self, **kwargs: Any) -> bool:
        self.adopted.append(dict(kwargs))
        return True

    async def begin_hibernation(self, **kwargs: Any) -> bool:
        self.hibernation_begun.append(dict(kwargs))
        if self.hibernate_wins and self.workspace is not None:
            self.workspace.update(
                {
                    "state": "HIBERNATING",
                    "hibernated_at": kwargs.get("hibernated_at"),
                }
            )
        return self.hibernate_wins

    async def mark_hibernation_release_required(self, **kwargs: Any) -> bool:
        self.release_required.append(dict(kwargs))
        if self.workspace is not None:
            self.workspace["state"] = "RECOVERY_REQUIRED"
        return True

    async def release_recovered_sandbox(self, **kwargs: Any) -> bool:
        self.released.append(dict(kwargs))
        if self.release_wins and self.workspace is not None:
            self.workspace["current_sandbox_id"] = None
        return self.release_wins


class _FakeSessionLifecycle:
    """Records the hidden bootstrap sessions a rebuild asks for."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_session(self, user: Any, **kwargs: Any):
        _ = user
        self.created.append(dict(kwargs))
        return {"session_id": kwargs["session_id"]}


def _service(
    *, runtime_manager: Any = None, session_lifecycle: Any = None
) -> tuple[AssistantService, _FakeCatalogRepo, _FakeWorkspaceService]:
    catalog = _FakeCatalogRepo()
    workspace_service = _FakeWorkspaceService()
    service = AssistantService(
        agent_config=_FakeTemplateService(),
        session_lifecycle=session_lifecycle,
        runtime_manager=runtime_manager,
        catalog_repo=catalog,  # type: ignore[arg-type]
        workspace_service=workspace_service,  # type: ignore[arg-type]
        spawn_background_task=_run_now,
    )
    return service, catalog, workspace_service


async def _create_unprepared(service: AssistantService, user: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Create an Assistant whose workspace these cases prepare themselves."""
    with patch.object(service, "wake_workspace", AsyncMock()):
        return await service.create_assistant(user, body)


def _user(user_id: str = "owner-1") -> Any:
    return SimpleNamespace(user_id=user_id, org_id="org-1", roles=[])


async def test_foreign_and_missing_assistants_share_a_non_disclosing_404() -> None:
    service, _catalog, _workspace = _service()
    created = await _create_unprepared(service,
        _user("owner-1"),
        {"display_name": "Jarvis", "runtime_profile_name": "env-1"},
    )

    with pytest.raises(APIError) as foreign:
        await service.get_assistant(_user("intruder"), created["assistant_id"])
    with pytest.raises(APIError) as missing:
        await service.get_assistant(_user("owner-1"), "asst_does_not_exist")

    assert foreign.value.code == missing.value.code == "ASSISTANT_NOT_FOUND"
    assert foreign.value.status_code == missing.value.status_code == 404
