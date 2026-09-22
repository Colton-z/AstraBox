from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.session_kernel.service import (
    SessionKernelService,
)
from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_read import (
    _SESSION_LIST_AGENT_PROJECTION,
    _SESSION_LIST_ROW_PROJECTION,
    _SESSION_LIST_SNAPSHOT_PROJECTION,
    _SESSION_LIST_SUMMARY_FIELDS,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.backend import get_async_collection
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_snapshot_repository import (
    COLLECTION_NAME as SESSION_SNAPSHOT_COLLECTION,
    SessionSnapshotRepository,
)


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _apply_projection(
    row: dict[str, Any],
    projection: dict[str, int],
) -> dict[str, Any]:
    projected = {
        field: row[field]
        for field, included in projection.items()
        if field != "_id" and included and field in row
    }
    if projection.get("_id", 1) and "_id" in row:
        projected["_id"] = row["_id"]
    return projected


class _SessionsRepository:
    def __init__(self) -> None:
        self.projection: dict[str, int] | None = None

    async def list_user_sessions_page(
        self,
        user_id: str,
        *,
        limit: int,
        cursor: str | None,
        projection: dict[str, int],
    ) -> dict[str, Any]:
        self.projection = dict(projection)
        return {
            "sessions": [
                {
                    "session_id": "session-1",
                    "session_kind": "agent_chat",
                    "engine_kind": "claude_code",
                    "user_id": user_id,
                    "template_name": "Agent",
                    "state": "READY",
                    "updated_at": "2026-08-09T12:00:00+00:00",
                }
            ],
            "has_more": cursor is not None,
            "next_cursor": "next" if limit == 1 else None,
        }


class _HistoricalSessionsRepository:
    async def list_user_sessions_page(
        self,
        user_id: str,
        *,
        limit: int,
        cursor: str | None,
        projection: dict[str, int],
    ) -> dict[str, Any]:
        assert user_id == "user-1"
        assert limit == 50
        assert cursor is None
        assert projection == _SESSION_LIST_ROW_PROJECTION
        return {
            "sessions": [
                {
                    "session_id": "legacy-ready",
                    "session_kind": "agent_chat",
                    "engine_kind": "claude_code",
                    "user_id": user_id,
                    "title": "Historical session",
                    "state": "READY",
                    "runtime_identity": None,
                },
                {
                    "session_id": "current-ready",
                    "session_kind": "agent_chat",
                    "engine_kind": "claude_code",
                    "user_id": user_id,
                    "title": "Current session",
                    "state": "READY",
                    "runtime_identity": {
                        "workspace_dir": "/workspace/current-ready",
                        "sandbox_tenancy": "conversation",
                    },
                },
            ],
            "has_more": False,
            "next_cursor": None,
        }


class _SnapshotRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, int]]] = []
        self.rows = {
            session_id: {
                "_id": f"snapshot-{session_id}",
                "session_id": session_id,
                "session_lifecycle_state": "ACTIVE",
                "runtime_connectivity_state": "CONNECTED",
                "conversation_state": "IDLE",
                "terminal_state": "IDLE",
                "last_turn_status": "COMPLETED",
                "last_turn_id": f"turn-{session_id}",
                "last_turn_command_id": f"command-{session_id}",
                "last_turn_terminal_frame": {
                    "turn_id": f"turn-{session_id}",
                    "command_id": f"command-{session_id}",
                    "type": "finish",
                    "finish_reason": "stop",
                    "frame_seq": 9,
                },
                "messages": ["large detail field"],
                "runtime_versions": {"runner": "large detail field"},
            }
            for session_id in ("session-1", "session-2")
        }

    async def get_snapshots_batch(
        self,
        session_ids: list[str],
        *,
        projection: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        assert projection is not None
        self.calls.append((list(session_ids), dict(projection)))
        return {
            session_id: _apply_projection(self.rows[session_id], projection)
            for session_id in session_ids
        }

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        raise AssertionError(f"session list performed an N+1 snapshot read: {session_id}")


class _AgentRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, int], bool]] = []
        self.rows = {
            agent_id: {
                "_id": f"agent-row-{agent_id}",
                "agent_id": agent_id,
                "name": f"Agent {agent_id}",
                "state": "ACTIVE",
                "slash_command_details": ["large detail field"],
                "runtime_identity": {"linux_user": "detail"},
            }
            for agent_id in ("agent-1", "agent-2")
        }

    async def list_agents_by_ids(
        self,
        agent_ids: list[str],
        *,
        projection: dict[str, int],
        include_deleted: bool = False,
    ) -> dict[str, dict[str, Any]]:
        assert projection is not None
        self.calls.append((list(agent_ids), dict(projection), include_deleted))
        return {
            agent_id: _apply_projection(self.rows[agent_id], projection)
            for agent_id in agent_ids
        }

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        raise AssertionError(f"session list performed an N+1 Agent read: {agent_id}")


@pytest.mark.asyncio
async def test_paginated_list_returns_only_summary_fields() -> None:
    service = object.__new__(SessionKernelService)
    sessions_repo = _SessionsRepository()
    service._sessions_repo = sessions_repo

    async def _render_session_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **rows[0],
                "agent_runtime": {"agent_id": "agent-1", "state": "ACTIVE"},
                "slash_command_details": [{"name": "/heavy"}],
                "runtime_identity": {"linux_user": "detail"},
                "runtime_versions": {"runner": {"revision": "detail"}},
            }
        ]

    service._render_session_rows = _render_session_rows

    page = await SessionKernelService.list_sessions_page(
        service,
        SimpleNamespace(user_id="user-1"),
        limit=1,
        cursor="cursor-1",
    )

    assert sessions_repo.projection == _SESSION_LIST_ROW_PROJECTION
    row = page["sessions"][0]
    assert set(row) <= _SESSION_LIST_SUMMARY_FIELDS
    assert row["agent_runtime"] == {"agent_id": "agent-1", "state": "ACTIVE"}
    assert {
        "slash_command_details",
        "runtime_identity",
        "runtime_versions",
    }.isdisjoint(row)


@pytest.mark.asyncio
async def test_historical_ready_without_runtime_workspace_cannot_poison_session_list() -> None:
    service = object.__new__(SessionKernelService)
    service._sessions_repo = _HistoricalSessionsRepository()
    service._session_snapshots_repo = SimpleNamespace(
        get_snapshots_batch=lambda *_args, **_kwargs: None
    )
    service._session_service = SimpleNamespace(
        _sanitize_session=SessionService._sanitize_session,
        derive_recovery_fields=SessionService.derive_recovery_fields,
    )
    service._runtime_manager = SimpleNamespace(
        resolve_session_terminal_cwd=lambda *_args, **_kwargs: None
    )

    async def _reconcile_runtime_binding(
        row: dict[str, Any],
        *,
        persist: bool,
    ) -> dict[str, Any]:
        assert persist is False
        return dict(row)

    async def _get_snapshots_batch(
        _session_ids: list[str],
        *,
        projection: dict[str, int],
    ) -> dict[str, dict[str, Any]]:
        assert projection == _SESSION_LIST_SNAPSHOT_PROJECTION
        return {}

    service._reconcile_runtime_binding = _reconcile_runtime_binding
    service._session_snapshots_repo.get_snapshots_batch = _get_snapshots_batch

    page = await SessionKernelService.list_sessions_page(
        service,
        SimpleNamespace(user_id="user-1"),
    )

    assert [row["session_id"] for row in page["sessions"]] == [
        "legacy-ready",
        "current-ready",
    ]
    assert [row["state"] for row in page["sessions"]] == ["READY", "READY"]


@pytest.mark.asyncio
async def test_snapshot_and_agent_overlays_are_projected_batch_reads() -> None:
    service = object.__new__(SessionKernelService)
    snapshot_repo = _SnapshotRepository()
    agent_repo = _AgentRepository()
    service._session_snapshots_repo = snapshot_repo
    service._agent_repo = agent_repo
    service._session_service = SimpleNamespace(
        _sanitize_session=lambda row: dict(row),
        derive_recovery_fields=lambda _state, _row: (None, None),
    )
    service._runtime_manager = SimpleNamespace(
        resolve_session_terminal_cwd=lambda *args, **kwargs: None
    )

    async def _reconcile_runtime_binding(
        row: dict[str, Any],
        *,
        persist: bool,
    ) -> dict[str, Any]:
        assert persist is False
        return dict(row)

    service._reconcile_runtime_binding = _reconcile_runtime_binding
    rows = [
        {
            "session_id": f"session-{index}",
            "template_name": f"Agent {index}",
            "state": "READY",
            "session_kind": "agent_chat",
            "engine_kind": "claude_code",
            "agent_id": f"agent-{index}",
        }
        for index in (1, 2)
    ]

    rendered = await SessionKernelService._render_session_rows(service, rows)

    assert snapshot_repo.calls == [
        (["session-1", "session-2"], _SESSION_LIST_SNAPSHOT_PROJECTION)
    ]
    assert agent_repo.calls == [
        (["agent-1", "agent-2"], _SESSION_LIST_AGENT_PROJECTION, False)
    ]
    assert _SESSION_LIST_SNAPSHOT_PROJECTION["last_turn_terminal_frame"] == 1
    assert "messages" not in _SESSION_LIST_SNAPSHOT_PROJECTION
    assert "slash_command_details" not in _SESSION_LIST_AGENT_PROJECTION
    assert [row["state"] for row in rendered] == ["READY", "READY"]
    assert [row["deployment_name"] for row in rendered] == [
        "Agent agent-1",
        "Agent agent-2",
    ]


@pytest.mark.asyncio
async def test_list_excludes_a_session_deleted_between_row_and_snapshot_reads() -> None:
    service = object.__new__(SessionKernelService)
    sessions_repo = _SessionsRepository()
    snapshot_repo = _SnapshotRepository()
    snapshot_repo.rows["session-1"]["session_lifecycle_state"] = "DELETED"
    service._sessions_repo = sessions_repo
    service._session_snapshots_repo = snapshot_repo
    service._agent_repo = _AgentRepository()
    service._session_service = SimpleNamespace(
        _sanitize_session=lambda row: dict(row),
        derive_recovery_fields=lambda _state, _row: (None, None),
    )
    service._runtime_manager = SimpleNamespace(
        resolve_session_terminal_cwd=lambda *args, **kwargs: None
    )

    async def _reconcile_runtime_binding(
        row: dict[str, Any],
        *,
        persist: bool,
    ) -> dict[str, Any]:
        assert persist is False
        return dict(row)

    service._reconcile_runtime_binding = _reconcile_runtime_binding
    page = await SessionKernelService.list_sessions_page(
        service,
        SimpleNamespace(user_id="user-1"),
    )

    assert sessions_repo.projection == _SESSION_LIST_ROW_PROJECTION
    assert snapshot_repo.calls == [
        (["session-1"], _SESSION_LIST_SNAPSHOT_PROJECTION)
    ]
    assert page == {"sessions": [], "has_more": False, "next_cursor": None}


@pytest.mark.asyncio
async def test_repository_projection_contract_omits_detail_fields_and_keeps_cursor() -> None:
    sessions = SessionRepository()
    agents = AgentRepository()
    snapshots = SessionSnapshotRepository()
    for index, updated_at in ((1, "2026-08-09T12:00:00+00:00"), (2, "2026-08-08T12:00:00+00:00")):
        await sessions.create_session(
            {
                "session_id": f"session-{index}",
                "user_id": "user-1",
                "template_name": f"Agent {index}",
                "state": "READY",
                "updated_at": updated_at,
                "slash_command_details": [{"name": "/heavy"}],
                "runtime_identity": {"linux_user": "detail"},
                "runtime_versions": {"runner": {"revision": "detail"}},
            }
        )

    session_projection = {
        "_id": 0,
        "session_id": 1,
        "template_name": 1,
        "updated_at": 1,
    }
    first_page = await sessions.list_user_sessions_page(
        "user-1",
        limit=1,
        projection=session_projection,
    )
    assert set(first_page["sessions"][0]) == {
        "session_id",
        "template_name",
        "updated_at",
    }
    assert first_page["has_more"] is True
    second_page = await sessions.list_user_sessions_page(
        "user-1",
        limit=1,
        cursor=first_page["next_cursor"],
        projection=session_projection,
    )
    assert second_page["sessions"][0]["session_id"] != first_page["sessions"][0]["session_id"]

    await agents.create_agent(
        {
            "agent_id": "agent-1",
            "user_id": "user-1",
            "name": "Agent 1",
            "state": "ACTIVE",
            "slash_command_details": [{"name": "/heavy"}],
            "runtime_identity": {"linux_user": "detail"},
        }
    )
    agent_rows = await agents.list_agents_by_ids(
        ["agent-1"],
        projection={"_id": 0, "agent_id": 1, "name": 1, "state": 1},
    )
    assert set(agent_rows["agent-1"]) == {"agent_id", "name", "state"}

    snapshot_collection = await get_async_collection(SESSION_SNAPSHOT_COLLECTION)
    await snapshot_collection.insert_one(
        {
            "session_id": "session-1",
            "conversation_state": "IDLE",
            "last_turn_terminal_frame": {"type": "finish"},
            "messages": ["large detail field"],
        }
    )
    snapshot_rows = await snapshots.get_snapshots_batch(
        ["session-1"],
        projection={
            "_id": 0,
            "session_id": 1,
            "conversation_state": 1,
            "last_turn_terminal_frame": 1,
        },
    )
    assert set(snapshot_rows["session-1"]) == {
        "session_id",
        "conversation_state",
        "last_turn_terminal_frame",
    }
