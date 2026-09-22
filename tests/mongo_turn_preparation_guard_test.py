"""Real-Mongo CAS and uncertain-write coverage for turn preparation guards."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pymongo = pytest.importorskip("pymongo")

pytestmark = pytest.mark.mongo

from astrabox.persistence.repository import session_repository as repository_module  # noqa: E402
from astrabox.persistence.repository._compat import AutoReconnect  # noqa: E402
from astrabox.persistence.repository.mongo import get_async_collection  # noqa: E402
from astrabox.persistence.repository.session_repository import (  # noqa: E402
    SessionRepository,
)


async def _store(monkeypatch: pytest.MonkeyPatch) -> tuple[SessionRepository, Any]:
    name = f"turn_preparation_guard_{uuid.uuid4().hex}"
    collection = await get_async_collection(name)
    await collection.delete_many({})

    async def _collection(_name: str) -> Any:
        return collection

    monkeypatch.setattr(repository_module, "get_async_collection", _collection)
    repo = SessionRepository()
    repo._collection_name = name
    await collection.insert_one(
        {
            "session_id": "session-1",
            "user_id": "principal-1",
            "sandbox_backend": "exclusive-provider",
            "sandbox_id": "sandbox-1",
            "current_turn_id": "turn-1",
            "deleted": False,
        }
    )
    return repo, collection


async def _claim(
    repo: SessionRepository,
    *,
    attempt_id: str,
    owner_token: str,
    turn_id: str = "turn-1",
) -> dict[str, Any] | None:
    return await repo.claim_turn_preparation_guard(
        "session-1",
        principal_id="principal-1",
        sandbox_backend="exclusive-provider",
        sandbox_id="sandbox-1",
        turn_id=turn_id,
        attempt_id=attempt_id,
        owner_token=owner_token,
    )


class _FaultingCollection:
    def __init__(
        self,
        collection: Any,
        *,
        method: str,
        matches: Callable[[tuple[Any, ...], dict[str, Any]], bool],
        after_apply: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._collection = collection
        self._method = method
        self._matches = matches
        self._after_apply = after_apply
        self._raised = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collection, name)

    async def find_one_and_update(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._collection.find_one_and_update(*args, **kwargs)
        await self._maybe_fail("find_one_and_update", args, kwargs)
        return result

    async def update_one(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._collection.update_one(*args, **kwargs)
        await self._maybe_fail("update_one", args, kwargs)
        return result

    async def _maybe_fail(
        self,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        if self._raised or method != self._method or not self._matches(args, kwargs):
            return
        self._raised = True
        if self._after_apply is not None:
            await self._after_apply()
        raise AutoReconnect("fault injection: response lost after applied write")


def _claim_call(args: tuple[Any, ...], _kwargs: dict[str, Any]) -> bool:
    return bool(args and isinstance(args[0], dict) and "user_id" in args[0])


def _quarantine_call(args: tuple[Any, ...], _kwargs: dict[str, Any]) -> bool:
    return bool(
        len(args) > 1
        and isinstance(args[1], dict)
        and args[1].get("$set", {}).get("turn_preparation_guard.state")
        == "QUARANTINED"
    )


def _release_call(args: tuple[Any, ...], _kwargs: dict[str, Any]) -> bool:
    return bool(
        len(args) > 1
        and isinstance(args[1], dict)
        and args[1].get("$set", {}).get("turn_preparation_guard") is None
    )


async def test_real_mongo_concurrent_claim_has_one_private_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, collection = await _store(monkeypatch)

    results = await asyncio.gather(
        *(
            _claim(
                repo,
                attempt_id="shared-dispatch-attempt",
                owner_token=f"private-owner-{index}",
            )
            for index in range(12)
        )
    )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored["turn_preparation_guard"]["owner_token"] == winners[0][
        "owner_token"
    ]


async def test_real_mongo_apply_then_error_converges_by_private_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, collection = await _store(monkeypatch)
    faulting = _FaultingCollection(
        collection,
        method="find_one_and_update",
        matches=_claim_call,
    )

    async def _collection(_name: str) -> Any:
        return faulting

    monkeypatch.setattr(repository_module, "get_async_collection", _collection)
    claimed = await _claim(
        repo,
        attempt_id="ambiguous-attempt",
        owner_token="ambiguous-private-owner",
    )

    assert claimed is not None
    assert claimed["owner_token"] == "ambiguous-private-owner"


async def test_real_mongo_stale_ambiguous_claim_reaps_its_own_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, collection = await _store(monkeypatch)

    async def _advance_turn() -> None:
        await collection.update_one(
            {"session_id": "session-1"},
            {"$set": {"current_turn_id": "turn-2"}},
        )

    faulting = _FaultingCollection(
        collection,
        method="find_one_and_update",
        matches=_claim_call,
        after_apply=_advance_turn,
    )

    async def _collection(_name: str) -> Any:
        return faulting

    monkeypatch.setattr(repository_module, "get_async_collection", _collection)
    claimed = await _claim(
        repo,
        attempt_id="stale-ambiguous-attempt",
        owner_token="stale-ambiguous-private-owner",
    )

    assert claimed is None
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored["current_turn_id"] == "turn-2"
    assert stored.get("turn_preparation_guard") is None


async def test_real_mongo_quarantine_and_release_uncertain_writes_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, collection = await _store(monkeypatch)
    assert await _claim(
        repo,
        attempt_id="attempt-a",
        owner_token="private-owner-a",
    ) is not None

    quarantine_fault = _FaultingCollection(
        collection,
        method="update_one",
        matches=_quarantine_call,
    )

    async def _quarantine_collection(_name: str) -> Any:
        return quarantine_fault

    monkeypatch.setattr(
        repository_module, "get_async_collection", _quarantine_collection
    )
    assert await repo.quarantine_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="attempt-a",
        owner_token="private-owner-a",
        reason="deadline_expired",
    )

    release_fault = _FaultingCollection(
        collection,
        method="update_one",
        matches=_release_call,
    )

    async def _release_collection(_name: str) -> Any:
        return release_fault

    monkeypatch.setattr(repository_module, "get_async_collection", _release_collection)
    assert await repo.release_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="attempt-a",
        owner_token="private-owner-a",
        expected_state="QUARANTINED",
    )
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored.get("turn_preparation_guard") is None
