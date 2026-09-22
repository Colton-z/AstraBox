from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    assistant_profile_marker_key,
)
from astrabox.core.service.orchestrator.runtime_subject import (
    RuntimeSubjectCoordinator,
    RuntimeStartupTarget,
)
from astrabox.core.service.orchestrator.runtime_manager import StartupAllocationCleanup
from astrabox.providers import register_builtin_providers
from astrabox.seams.sandbox_disposal import SandboxDestruction


register_builtin_providers()


class _Sessions:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {
            str(row["session_id"]): {"sandbox_generation": "generation-1", **row} for row in rows
        }

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.rows.get(session_id)
        return dict(row) if row is not None else None

    async def update_session(
        self,
        session_id: str,
        updates: dict[str, Any],
        touch_updated_at: bool = True,
    ) -> dict[str, Any]:
        _ = touch_updated_at
        self.rows[session_id].update(updates)
        return dict(self.rows[session_id])


class _WorkspaceService:
    def __init__(self, workspaces: list[dict[str, Any]]) -> None:
        self._workspaces = [dict(workspace) for workspace in workspaces]
        self._index = 0

    async def get_workspace(
        self,
        *,
        user_id: str,
        assistant_id: str,
    ) -> dict[str, Any]:
        _ = (user_id, assistant_id)
        index = min(self._index, len(self._workspaces) - 1)
        self._index += 1
        return dict(self._workspaces[index])


def _assistant_session() -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "session_kind": "assistant_chat",
        "state": "CREATING",
        "user_id": "user-1",
        "workspace_ref": {
            "kind": "assistant",
            "user_id": "user-1",
            "assistant_id": "assistant-1",
            "engine_kind": "assistant",
        },
    }


async def test_agent_startup_resolves_to_the_same_create_action() -> None:
    session = {
        "session_id": "session-1",
        "session_kind": "agent_chat",
        "workspace_ref": {"kind": "agent", "agent_id": "agent-1"},
    }
    sessions = _Sessions([session])
    runtime_manager = Mock()
    plan = SimpleNamespace(subject_kind="deployment_conversation")
    runtime_manager.plan_agent_chat_runtime_start.return_value = plan
    lifecycle = AsyncMock()
    coordinator = RuntimeSubjectCoordinator(
        runtime_manager=runtime_manager,
        sessions_repo=sessions,
        assistant_workspace_service=AsyncMock(),
        assistant_lifecycle_getter=lambda: lifecycle,
    )

    target = await coordinator.acquire_startup(
        session_id="session-1",
        sandbox_generation=None,
        template=object(),
        resume_engine_session_key=None,
        on_progress=None,
    )

    assert target.action == "create_runtime"
    assert target.workspace_plan is plan
    lifecycle.acquire_workspace_for_session.assert_not_awaited()


async def test_assistant_startup_waits_on_the_owner_and_reuses_its_ready_binding() -> None:
    session = _assistant_session()
    provisioning = {
        "session_id": "bootstrap-1",
        "startup_progress": "mounting_nas",
    }
    sessions = _Sessions([session, provisioning])
    marker_key = assistant_profile_marker_key(
        user_id="user-1",
        assistant_id="assistant-1",
    )
    materializing = {
        "state": "MATERIALIZING",
        "engine_kind": "assistant",
        "provisioning_session_id": "bootstrap-1",
    }
    ready = {
        "state": "READY",
        "engine_kind": "assistant",
        "current_sandbox_id": "sandbox-2",
        "current_sandbox_expires_at": "2026-08-18T00:00:00+00:00",
        "assistant_profiles": {
            marker_key: {
                "status": "ready",
                "user_id": "user-1",
                "assistant_id": "assistant-1",
                "sandbox_id": "sandbox-2",
            }
        },
    }
    workspace_service = _WorkspaceService([materializing, materializing, ready, ready])
    runtime_manager = Mock()
    plan = SimpleNamespace(
        subject_kind="assistant_runtime",
        sandbox_id="sandbox-2",
    )
    runtime_manager.plan_assistant_runtime_attach.return_value = plan
    lifecycle = AsyncMock()
    progress: list[str] = []

    async def _record_progress(value: str) -> None:
        progress.append(value)

    coordinator = RuntimeSubjectCoordinator(
        runtime_manager=runtime_manager,
        sessions_repo=sessions,
        assistant_workspace_service=workspace_service,
        assistant_lifecycle_getter=lambda: lifecycle,
        ready_timeout_seconds=1,
        ready_poll_seconds=0.001,
    )

    target = await coordinator.acquire_startup(
        session_id="session-1",
        sandbox_generation=None,
        template=object(),
        resume_engine_session_key=None,
        on_progress=_record_progress,
    )

    assert target.action == "use_ready_binding"
    assert target.workspace_plan is plan
    assert target.binding_expires_at == "2026-08-18T00:00:00+00:00"
    assert progress == ["mounting_nas"]
    lifecycle.acquire_workspace_for_session.assert_awaited_once()
    acquisition_call = lifecycle.acquire_workspace_for_session.await_args
    assert acquisition_call.args[0].user_id == "user-1"
    assert acquisition_call.args[1] == "assistant-1"
    assert acquisition_call.kwargs == {
        "provisioning_session_id": "session-1",
        "provisioning_sandbox_generation": "generation-1",
    }


async def test_assistant_startup_fails_loud_when_owner_recovery_cannot_advance() -> None:
    session = _assistant_session()
    sessions = _Sessions([session])
    recovery_required = {
        "state": "RECOVERY_REQUIRED",
        "engine_kind": "assistant",
        "current_sandbox_id": "sandbox-dead",
        "last_error": "sandbox destruction is not confirmed",
    }
    workspace_service = _WorkspaceService([recovery_required, recovery_required])
    lifecycle = AsyncMock()
    coordinator = RuntimeSubjectCoordinator(
        runtime_manager=Mock(),
        sessions_repo=sessions,
        assistant_workspace_service=workspace_service,
        assistant_lifecycle_getter=lambda: lifecycle,
        ready_timeout_seconds=1,
        ready_poll_seconds=0.001,
    )

    with pytest.raises(APIError) as raised:
        await coordinator.acquire_startup(
            session_id="session-1",
            sandbox_generation=None,
            template=object(),
            resume_engine_session_key=None,
            on_progress=None,
        )

    assert raised.value.code == "ASSISTANT_WORKSPACE_RECOVERY_REQUIRED"
    assert raised.value.status_code == 409
    lifecycle.acquire_workspace_for_session.assert_awaited_once()


async def test_stale_hidden_materializer_cannot_create_after_losing_its_claim() -> None:
    session = {
        **_assistant_session(),
        "hidden": True,
        "owner_type": "assistant_workspace",
        "owner_id": "assistant-1",
    }
    sessions = _Sessions([session, {"session_id": "session-new"}])
    workspace = {
        "state": "MATERIALIZING",
        "engine_kind": "assistant",
        "provisioning_session_id": "session-new",
    }
    workspace_service = _WorkspaceService([workspace, workspace])
    runtime_manager = Mock()
    coordinator = RuntimeSubjectCoordinator(
        runtime_manager=runtime_manager,
        sessions_repo=sessions,
        assistant_workspace_service=workspace_service,
        assistant_lifecycle_getter=lambda: AsyncMock(),
        ready_timeout_seconds=0.01,
        ready_poll_seconds=0.001,
    )

    with pytest.raises(APIError) as raised:
        await coordinator.acquire_startup(
            session_id="session-1",
            sandbox_generation=None,
            template=object(),
            resume_engine_session_key=None,
            on_progress=None,
        )

    assert raised.value.code == "RUNTIME_SUBJECT_MATERIALIZATION_LOST"
    runtime_manager.plan_assistant_runtime_start.assert_not_called()


async def test_unknown_session_shape_has_no_fallback_runtime_subject() -> None:
    session = {
        "session_id": "session-1",
        "session_kind": "assistant_chat",
        "workspace_ref": {"kind": "agent", "agent_id": "agent-1"},
    }
    coordinator = RuntimeSubjectCoordinator(
        runtime_manager=Mock(),
        sessions_repo=_Sessions([session]),
        assistant_workspace_service=AsyncMock(),
        assistant_lifecycle_getter=lambda: AsyncMock(),
    )

    with pytest.raises(APIError) as raised:
        coordinator.provider_for(session)

    assert raised.value.code == "RUNTIME_SUBJECT_INVALID"


async def test_recovery_strategy_follows_the_runtime_allocation_owner() -> None:
    coordinator = RuntimeSubjectCoordinator(
        runtime_manager=Mock(),
        sessions_repo=_Sessions([]),
        assistant_workspace_service=AsyncMock(),
        assistant_lifecycle_getter=lambda: AsyncMock(),
    )

    assert (
        coordinator.recovery_action_for(
            {
                "session_id": "agent-session",
                "session_kind": "agent_chat",
                "workspace_ref": {"kind": "agent", "agent_id": "agent-1"},
            }
        )
        == "recover_session_allocation"
    )
    assert (
        coordinator.recovery_action_for(_assistant_session())
        == "restart_session_on_subject"
    )


async def test_attach_failure_never_destroys_the_shared_owner_runtime() -> None:
    runtime_manager = AsyncMock()
    coordinator = RuntimeSubjectCoordinator(
        runtime_manager=runtime_manager,
        sessions_repo=_Sessions([]),
        assistant_workspace_service=AsyncMock(),
        assistant_lifecycle_getter=lambda: AsyncMock(),
    )
    target = RuntimeStartupTarget(
        action="attach_runtime",
        session=_assistant_session(),
        workspace_plan=SimpleNamespace(),
    )

    cleanup = await coordinator.cleanup_failed_startup_runtime(
        session_id="session-1",
        target=target,
        sandbox_id="shared-sandbox",
    )

    assert cleanup.destruction is None
    assert cleanup.leaked_sandbox_id is None
    runtime_manager.terminate_runtime.assert_not_awaited()


async def test_released_shared_placement_is_not_reported_as_a_leaked_sandbox() -> None:
    runtime_manager = AsyncMock()
    destruction = SandboxDestruction.retained(
        "shared-sandbox",
        detail="the Session placement was released and the Agent still owns the box",
    )
    runtime_manager.cleanup_startup_allocation.return_value = StartupAllocationCleanup(
        allocation=None,
        released=True,
        destruction=destruction,
    )
    coordinator = RuntimeSubjectCoordinator(
        runtime_manager=runtime_manager,
        sessions_repo=_Sessions([]),
        assistant_workspace_service=AsyncMock(),
        assistant_lifecycle_getter=lambda: AsyncMock(),
    )
    target = RuntimeStartupTarget(
        action="create_runtime",
        session={
            "session_id": "session-1",
            "session_kind": "agent_chat",
            "workspace_ref": {"kind": "agent", "agent_id": "agent-1"},
        },
        workspace_plan=SimpleNamespace(),
    )

    cleanup = await coordinator.cleanup_failed_startup_runtime(
        session_id="session-1",
        target=target,
        sandbox_id="shared-sandbox",
    )

    assert cleanup.destruction is destruction
    assert cleanup.leaked_sandbox_id is None
    runtime_manager.cleanup_startup_allocation.assert_awaited_once_with(
        "session-1",
        fallback_sandbox_id="shared-sandbox",
    )
