from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from astrabox.core.service.orchestrator.bootstrap_reconciler import BootstrapReconciler
from astrabox.core.service.orchestrator.platform_service import AgentPlatformService
from astrabox.common.utils.time_utils import utcnow


class _SessionKernel:
    def __init__(self) -> None:
        self.background_starts = 0
        self.bootstrap_calls = 0
        self.list_page_calls: list[dict[str, object]] = []

    def ensure_background_tasks_started(self) -> None:
        self.background_starts += 1

    async def ensure_bootstrap(self) -> None:
        self.bootstrap_calls += 1

    async def list_sessions_page(
        self,
        user: SimpleNamespace,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        self.list_page_calls.append(
            {"user_id": user.user_id, "limit": limit, "cursor": cursor}
        )
        return {"sessions": [], "has_more": False, "next_cursor": None}

    async def project_lifecycle_snapshot_from_session(self, **_kwargs: Any) -> None:
        return None


class _SessionsRepository:
    def __init__(
        self,
        *,
        index_error: Exception | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.index_error = index_error
        self.rows = [dict(row) for row in (rows or [])]
        self.ensure_index_calls = 0
        self.bootstrap_candidate_calls: list[int] = []
        self.cas_updates: list[
            tuple[str, dict[str, object], dict[str, object], bool]
        ] = []
        self.allow_compare_and_update = True

    async def ensure_indexes(self) -> None:
        self.ensure_index_calls += 1
        if self.index_error is not None:
            raise self.index_error

    async def list_bootstrap_reconcile_candidates(
        self, *, limit: int
    ) -> list[dict[str, object]]:
        self.bootstrap_candidate_calls.append(limit)
        return [dict(row) for row in self.rows]

    async def compare_and_update_session(
        self,
        session_id: str,
        *,
        expected: dict[str, object],
        updates: dict[str, object],
        touch_updated_at: bool,
    ) -> bool:
        self.cas_updates.append(
            (session_id, dict(expected), dict(updates), touch_updated_at)
        )
        return self.allow_compare_and_update


class _SessionEventsRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(dict(event))
        return {**event, "event_seq": len(self.events)}


class _BackgroundOwner:
    def __init__(self) -> None:
        self.starts = 0

    def ensure_started(self) -> None:
        self.starts += 1


def _service(
    *,
    sessions_repo: _SessionsRepository | None = None,
) -> tuple[
    AgentPlatformService,
    _SessionKernel,
    _SessionsRepository,
    _BackgroundOwner,
]:
    service = AgentPlatformService.__new__(AgentPlatformService)
    kernel = _SessionKernel()
    repo = sessions_repo or _SessionsRepository()
    expiration_watcher = _BackgroundOwner()
    service._quiesced_reason = None
    service._bootstrapped = False
    service._session_list_bootstrapped = False
    service._bootstrap_lock = asyncio.Lock()
    service._session_kernel = kernel
    service._sessions_repo = repo
    service._runtime_manager = SimpleNamespace(
        reconcile_startup_allocations=AsyncMock(return_value={})
    )
    service._session_events_repo = _SessionEventsRepository()
    service._expiration_watcher = expiration_watcher
    service._channel_spine_reconciler = _BackgroundOwner()
    service._channel_source_host = _BackgroundOwner()
    service._bootstrap_reconciler = BootstrapReconciler(platform_service=service)
    return service, kernel, repo, expiration_watcher


def test_runtime_manager_uses_the_platform_session_repository() -> None:
    service = AgentPlatformService()

    assert service._runtime_manager._sessions_repo is service._sessions_repo


@pytest.mark.asyncio
async def test_list_page_starts_read_prerequisites_without_global_reconcile() -> None:
    service, kernel, repo, expiration_watcher = _service()

    await service.list_sessions_page(
        SimpleNamespace(user_id="user-1"),
        limit=20,
        cursor="cursor-1",
    )

    assert repo.ensure_index_calls == 1
    assert repo.bootstrap_candidate_calls == []
    assert kernel.bootstrap_calls == 1
    assert kernel.background_starts >= 1
    assert kernel.list_page_calls == [
        {"user_id": "user-1", "limit": 20, "cursor": "cursor-1"}
    ]
    assert expiration_watcher.starts == 1
    assert service._session_list_bootstrapped is True
    assert service._bootstrapped is False


@pytest.mark.asyncio
async def test_full_bootstrap_reconciles_once_after_a_list_only_bootstrap() -> None:
    service, _kernel, repo, _expiration_watcher = _service()

    await service.list_sessions_page(SimpleNamespace(user_id="user-1"))
    await service.ensure_bootstrap()
    await service.ensure_bootstrap()

    assert repo.bootstrap_candidate_calls == [10_000]
    assert service._session_list_bootstrapped is True
    assert service._bootstrapped is True


@pytest.mark.asyncio
async def test_list_bootstrap_failure_is_not_marked_or_hidden() -> None:
    repo = _SessionsRepository(index_error=RuntimeError("index unavailable"))
    service, kernel, _repo, expiration_watcher = _service(sessions_repo=repo)

    with pytest.raises(RuntimeError, match="index unavailable"):
        await service.list_sessions_page(SimpleNamespace(user_id="user-1"))

    assert service._session_list_bootstrapped is False
    assert service._bootstrapped is False
    assert kernel.list_page_calls == []
    assert expiration_watcher.starts == 0


@pytest.mark.asyncio
async def test_bootstrap_terminates_an_abandoned_agent_startup() -> None:
    stale = (utcnow() - timedelta(minutes=6)).isoformat()
    repo = _SessionsRepository(
        rows=[
            {
                "session_id": "session-stale",
                "session_kind": "agent_chat",
                "agent_id": "agent-1",
                "state": "CREATING",
                "created_at": stale,
                "updated_at": stale,
                "startup_progress": "starting_engine",
            }
        ]
    )
    service, _kernel, _repo, _watcher = _service(sessions_repo=repo)

    await service.ensure_bootstrap()

    assert repo.cas_updates == [
        (
            "session-stale",
            {"state": "CREATING", "updated_at": stale},
            {
                "state": "TERMINATED",
                "runtime_unavailable": True,
                "last_error": "startup abandoned during process restart",
                "startup_progress": None,
            },
            False,
        )
    ]
    assert service._session_events_repo.events[0]["payload"] == {
        "reason": "stale_creating",
        "previous_state": "CREATING",
        "state": "TERMINATED",
        "runtime_unavailable": True,
        "last_error": "startup abandoned during process restart",
    }
    assert service._session_events_repo.events[0]["event_type"] == (
        "session.lifecycle_reconciled"
    )


@pytest.mark.asyncio
async def test_bootstrap_does_not_terminate_a_startup_that_advanced_after_scan() -> None:
    stale = (utcnow() - timedelta(minutes=6)).isoformat()
    repo = _SessionsRepository(
        rows=[
            {
                "session_id": "session-racing",
                "state": "CREATING",
                "created_at": stale,
                "updated_at": stale,
            }
        ]
    )
    repo.allow_compare_and_update = False
    service, _kernel, _repo, _watcher = _service(sessions_repo=repo)

    await service.ensure_bootstrap()

    assert repo.cas_updates[0][1] == {"state": "CREATING", "updated_at": stale}
    assert service._session_events_repo.events == []


@pytest.mark.asyncio
async def test_bootstrap_candidate_query_is_bounded_to_creating_sessions() -> None:
    captured: dict[str, object] = {}

    class _FakeCursor:
        def sort(self, field: str, direction: int) -> "_FakeCursor":
            captured["sort"] = (field, direction)
            return self

        def limit(self, limit: int) -> "_FakeCursor":
            captured["limit"] = limit
            return self

        def __aiter__(self):
            async def _gen():
                if False:  # pragma: no cover - empty async iterator
                    yield None

            return _gen()

    class _FakeCollection:
        def find(self, query: dict[str, object]) -> _FakeCursor:
            captured["query"] = dict(query)
            return _FakeCursor()

    async def _get_collection(_name: str) -> _FakeCollection:
        return _FakeCollection()

    async def _run(_label: str, op, **_kwargs):
        return await op()

    with (
        patch(
            "astrabox.persistence.repository.session_repository.load_astrabox_settings",
            return_value=SimpleNamespace(sessions_collection="sessions"),
        ),
        patch(
            "astrabox.persistence.repository.session_repository.get_async_collection",
            new=_get_collection,
        ),
        patch(
            "astrabox.persistence.repository.session_repository.run_mongo_with_retry",
            new=_run,
        ),
    ):
        from astrabox.persistence.repository.session_repository import SessionRepository

        rows = await SessionRepository().list_bootstrap_reconcile_candidates(limit=123)

    assert rows == []
    assert captured["query"] == {
        "deleted": {"$ne": True},
        "state": "CREATING",
    }
    assert captured["sort"] == ("updated_at", 1)
    assert captured["limit"] == 123
