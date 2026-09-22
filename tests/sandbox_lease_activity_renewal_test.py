"""The lease a box is renewed to on activity follows what the box is for."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator import runtime_manager as rm
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime


class _Sessions:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "session_kind": self.kind, "sandbox_id": "box-1"}


class _Repo:
    async def compare_and_update_session(self, *args: Any, **kwargs: Any) -> None:
        return None


def _manager(monkeypatch: pytest.MonkeyPatch, kind: str) -> rm.RemoteAgentRuntimeManager:
    manager = rm.RemoteAgentRuntimeManager(sessions_repo=_Sessions(kind), agent_service_getter=lambda: None)
    manager._settings = SimpleNamespace(  # type: ignore[attr-defined]
        sandbox_lease_seconds=14400,
        sandbox_lease_renew_threshold_seconds=3600,
        agent_sandbox_renew_ttl_seconds=604200,
    )
    manager._runtimes["s-1"] = SessionRuntime(session_id="s-1", agent=None, engine_kind="x", sandbox_id="box-1")
    manager.renewed: list[int] = []  # type: ignore[attr-defined]

    async def _renew(session_id: str, ttl_seconds: int) -> datetime:
        manager.renewed.append(ttl_seconds)  # type: ignore[attr-defined]
        return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    monkeypatch.setattr(manager, "renew_runtime", _renew)
    monkeypatch.setattr(rm, "SessionRepository", _Repo)
    return manager


def test_a_conversation_box_is_renewed_to_the_conversation_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch, "agent_chat")
    asyncio.run(manager.maybe_renew_lease_on_activity("s-1"))
    assert manager.renewed == [14400]  # type: ignore[attr-defined]


def test_an_assistant_workspace_box_keeps_its_own_horizon(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch, "assistant_chat")
    asyncio.run(manager.maybe_renew_lease_on_activity("s-1"))
    assert manager.renewed == [604200]  # type: ignore[attr-defined]


def test_a_lease_with_time_left_is_not_renewed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch, "assistant_chat")
    manager._runtimes["s-1"].sandbox_lease_expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
    asyncio.run(manager.maybe_renew_lease_on_activity("s-1"))
    assert manager.renewed == []  # type: ignore[attr-defined]
