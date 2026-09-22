"""``resolve_runtime_sandbox_backend`` reads only the persisted row.

The template's ``sandbox_backend`` is a mutable authoring-time default; a live
runtime must reconnect to whatever backend actually holds its sandbox, so the
method reads the session / assistant-workspace row directly rather than
deriving it from any provisioning authority. These pin the lookup so the
rewrite cannot silently start sourcing the backend elsewhere.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator import runtime_manager as manager_module
from astrabox.core.service.orchestrator.runtime_manager import (
    RemoteAgentRuntimeManager,
)
from astrabox.core.service.orchestrator.session_workspace_plan import (
    RuntimeWorkspacePlan,
)


class _SessionRepo:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.get_calls: list[str] = []

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        self.get_calls.append(session_id)
        return dict(self.row) if isinstance(self.row, dict) else None


class _WorkspaceRepo:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def get_workspace(
        self, user_id: str, assistant_id: str
    ) -> dict[str, Any] | None:
        return dict(self.row) if isinstance(self.row, dict) else None


def _agent_plan() -> RuntimeWorkspacePlan:
    return RuntimeWorkspacePlan(
        subject_kind="deployment_conversation",
        session_kind="agent_chat",
        operation="runtime_start",
        runtime_key="session-1",
        conversation_session_id="session-1",
        cwd="/workspace",
        resume_engine_session_key=None,
        sandbox_id=None,
        materialize_default_repo=False,
        default_repo_target_cwd=None,
        engine_kind="claude_code",
        agent_id="agent-1",
    )


def _assistant_plan() -> RuntimeWorkspacePlan:
    return RuntimeWorkspacePlan(
        subject_kind="assistant_runtime",
        session_kind="assistant_chat",
        operation="runtime_start",
        runtime_key="assistant-runtime",
        conversation_session_id=None,
        cwd="/home/conversations/profile/assistant-1/workspace",
        resume_engine_session_key=None,
        sandbox_id=None,
        materialize_default_repo=False,
        default_repo_target_cwd=None,
        engine_kind="assistant",
        user_id="profile-lookup",
        assistant_id="assistant-1",
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_row: dict[str, Any] | None = None,
    workspace_row: dict[str, Any] | None = None,
) -> _SessionRepo:
    session_repo = _SessionRepo(session_row)
    monkeypatch.setattr(manager_module, "SessionRepository", lambda: session_repo)
    monkeypatch.setattr(
        manager_module,
        "AssistantWorkspaceRepository",
        lambda: _WorkspaceRepo(workspace_row),
    )
    return session_repo


async def test_session_backend_comes_from_the_session_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        session_row={
            "session_id": "session-1",
            "user_id": "u-1",
            "sandbox_backend": "Open_Sandbox",
        },
    )
    manager = RemoteAgentRuntimeManager()
    backend = await manager.resolve_runtime_sandbox_backend(
        "session-1", workspace_plan=_agent_plan()
    )
    # Normalized to the registry's lower-case key.
    assert backend == "open_sandbox"


async def test_missing_session_row_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, session_row=None)
    manager = RemoteAgentRuntimeManager()
    with pytest.raises(APIError) as raised:
        await manager.resolve_runtime_sandbox_backend(
            "session-1", workspace_plan=_agent_plan()
        )
    assert raised.value.status_code == 502


async def test_session_without_a_persisted_backend_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        session_row={"session_id": "session-1", "user_id": "u-1"},
    )
    manager = RemoteAgentRuntimeManager()
    with pytest.raises(APIError) as raised:
        await manager.resolve_runtime_sandbox_backend(
            "session-1", workspace_plan=_agent_plan()
        )
    assert raised.value.status_code == 502


async def test_assistant_backend_comes_from_the_workspace_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_repo = _wire(
        monkeypatch,
        session_row={"session_id": "boot-1", "sandbox_backend": "other_backend"},
        workspace_row={"assistant_id": "assistant-1", "sandbox_backend": "open_sandbox"},
    )
    manager = RemoteAgentRuntimeManager()
    backend = await manager.resolve_runtime_sandbox_backend(
        "boot-1", workspace_plan=_assistant_plan()
    )
    assert backend == "open_sandbox"
    # A bound workspace never consults the bootstrap session row.
    assert session_repo.get_calls == []


async def test_unbound_assistant_falls_back_to_the_bootstrap_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A freshly materialized workspace starts unbound; its first hidden
    # bootstrap session already carries the backend selected at admission.
    session_repo = _wire(
        monkeypatch,
        session_row={"session_id": "boot-1", "sandbox_backend": "open_sandbox"},
        workspace_row={"assistant_id": "assistant-1"},
    )
    manager = RemoteAgentRuntimeManager()
    backend = await manager.resolve_runtime_sandbox_backend(
        "boot-1", workspace_plan=_assistant_plan()
    )
    assert backend == "open_sandbox"
    assert session_repo.get_calls == ["boot-1"]


async def test_unbound_assistant_with_no_bootstrap_backend_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        session_row={"session_id": "boot-1"},
        workspace_row={"assistant_id": "assistant-1"},
    )
    manager = RemoteAgentRuntimeManager()
    with pytest.raises(APIError) as raised:
        await manager.resolve_runtime_sandbox_backend(
            "boot-1", workspace_plan=_assistant_plan()
        )
    assert raised.value.status_code == 502
