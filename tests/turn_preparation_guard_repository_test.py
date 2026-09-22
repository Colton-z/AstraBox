"""Durable CAS guarantees for the session-row turn-preparation guard."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.core.service.orchestrator.admin_service import AdminService
from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.persistence.repository import session_repository as repository_module
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.sqlite.collection import AsyncCollection


@pytest.fixture()
def guard_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/turn-preparation.sqlite"
    collection = AsyncCollection("sessions", db_url)

    async def _collection(_name: str) -> AsyncCollection:
        return collection

    async def _run(_name: str, operation: Any) -> Any:
        return await operation()

    monkeypatch.setattr(repository_module, "get_async_collection", _collection)
    monkeypatch.setattr(repository_module, "run_mongo_with_retry", _run)
    repo = SessionRepository()
    repo._collection_name = "sessions"
    return repo, collection


async def _insert_session(collection: AsyncCollection) -> None:
    await collection.insert_one(
        {
            "_id": "session-1",
            "session_id": "session-1",
            "user_id": "principal-1",
            "sandbox_backend": "exclusive-provider",
            "sandbox_id": "sandbox-1",
            "current_turn_id": "turn-1",
            "deleted": False,
        }
    )


async def _claim(
    repo: SessionRepository,
    attempt_id: str,
    *,
    sandbox_id: str = "sandbox-1",
    owner_token: str | None = None,
) -> dict[str, Any] | None:
    return await repo.claim_turn_preparation_guard(
        "session-1",
        principal_id="principal-1",
        sandbox_backend="exclusive-provider",
        sandbox_id=sandbox_id,
        turn_id="turn-1",
        attempt_id=attempt_id,
        owner_token=owner_token or f"owner-{attempt_id}",
    )


async def test_two_replicas_have_exactly_one_guard_winner(guard_store: Any) -> None:
    repo, collection = guard_store
    await _insert_session(collection)

    results = await asyncio.gather(
        *(_claim(repo, f"attempt-{index}") for index in range(8))
    )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored is not None
    assert stored["turn_preparation_guard"]["attempt_id"] == winners[0]["attempt_id"]


async def test_same_dispatch_attempt_still_has_one_private_owner(
    guard_store: Any,
) -> None:
    repo, collection = guard_store
    await _insert_session(collection)

    results = await asyncio.gather(
        *(
            _claim(
                repo,
                "shared-dispatch-attempt",
                owner_token=f"private-owner-{index}",
            )
            for index in range(8)
        )
    )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored is not None
    assert stored["turn_preparation_guard"]["owner_token"] == winners[0][
        "owner_token"
    ]


async def test_exact_owner_readback_is_idempotent_but_other_owner_is_not(
    guard_store: Any,
) -> None:
    repo, collection = guard_store
    await _insert_session(collection)

    first = await _claim(
        repo,
        "attempt-a",
        owner_token="private-owner-a",
    )
    readback = await _claim(
        repo,
        "attempt-a",
        owner_token="private-owner-a",
    )
    other_owner = await _claim(
        repo,
        "attempt-a",
        owner_token="private-owner-b",
    )

    assert first is not None
    assert readback == first
    assert other_owner is None


async def test_guard_has_no_time_based_takeover(guard_store: Any) -> None:
    repo, collection = guard_store
    await _insert_session(collection)
    assert await _claim(repo, "attempt-a") is not None

    # There is deliberately no expiry/TTL field or steal path.
    assert await _claim(repo, "attempt-b") is None
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored is not None
    assert stored["turn_preparation_guard"]["attempt_id"] == "attempt-a"


async def test_stale_attempt_cannot_release_newer_guard(guard_store: Any) -> None:
    repo, collection = guard_store
    await _insert_session(collection)
    assert await _claim(repo, "attempt-a") is not None
    assert await repo.release_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="attempt-a",
        owner_token="owner-attempt-a",
        expected_state="ACTIVE",
    )
    assert await _claim(repo, "attempt-b") is not None

    assert not await repo.release_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="attempt-a",
        owner_token="owner-attempt-a",
        expected_state="ACTIVE",
    )
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored is not None
    assert stored["turn_preparation_guard"]["attempt_id"] == "attempt-b"


async def test_quarantine_requires_exact_attempt_and_state(guard_store: Any) -> None:
    repo, collection = guard_store
    await _insert_session(collection)
    assert await _claim(repo, "attempt-a") is not None

    assert not await repo.quarantine_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="stale-attempt",
        owner_token="owner-stale-attempt",
        reason="deadline_expired",
    )
    assert await repo.quarantine_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="attempt-a",
        owner_token="owner-attempt-a",
        reason="deadline_expired",
    )
    assert await _claim(repo, "attempt-b") is None
    assert not await repo.release_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="attempt-a",
        owner_token="owner-attempt-a",
        expected_state="ACTIVE",
    )
    assert await repo.release_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="attempt-a",
        owner_token="owner-attempt-a",
        expected_state="QUARANTINED",
    )


async def test_quarantine_persists_only_bounded_reason_codes(guard_store: Any) -> None:
    repo, collection = guard_store
    await _insert_session(collection)
    assert await _claim(repo, "attempt-a") is not None

    assert await repo.quarantine_turn_preparation_guard(
        "session-1",
        sandbox_id="sandbox-1",
        attempt_id="attempt-a",
        owner_token="owner-attempt-a",
        reason="credential SECRET-must-not-persist",
    )

    stored = await collection.find_one({"session_id": "session-1"})
    assert stored is not None
    assert stored["turn_preparation_guard"]["reason"] == "abandoned"
    assert "SECRET-must-not-persist" not in repr(stored)


async def test_new_sandbox_binding_can_replace_stale_guard(guard_store: Any) -> None:
    repo, collection = guard_store
    await _insert_session(collection)
    assert await _claim(repo, "attempt-old") is not None
    await collection.update_one(
        {"session_id": "session-1"},
        {"$set": {"sandbox_id": "sandbox-2"}},
    )

    replacement = await _claim(repo, "attempt-new", sandbox_id="sandbox-2")

    assert replacement is not None
    assert replacement["sandbox_id"] == "sandbox-2"


async def test_claim_atomically_matches_owner_backend_and_sandbox(
    guard_store: Any,
) -> None:
    repo, collection = guard_store
    await _insert_session(collection)

    assert (
        await repo.claim_turn_preparation_guard(
            "session-1",
            principal_id="request-actor",
            sandbox_backend="exclusive-provider",
            sandbox_id="sandbox-1",
            turn_id="turn-1",
            attempt_id="wrong-owner",
            owner_token="owner-wrong-owner",
        )
        is None
    )
    assert (
        await repo.claim_turn_preparation_guard(
            "session-1",
            principal_id="principal-1",
            sandbox_backend="other-provider",
            sandbox_id="sandbox-1",
            turn_id="turn-1",
            attempt_id="wrong-backend",
            owner_token="owner-wrong-backend",
        )
        is None
    )
    assert (
        await repo.claim_turn_preparation_guard(
            "session-1",
            principal_id="principal-1",
            sandbox_backend="exclusive-provider",
            sandbox_id="other-sandbox",
            turn_id="turn-1",
            attempt_id="wrong-sandbox",
            owner_token="owner-wrong-sandbox",
        )
        is None
    )


async def test_claim_atomically_matches_current_logical_turn(
    guard_store: Any,
) -> None:
    repo, collection = guard_store
    await _insert_session(collection)

    stale = await repo.claim_turn_preparation_guard(
        "session-1",
        principal_id="principal-1",
        sandbox_backend="exclusive-provider",
        sandbox_id="sandbox-1",
        turn_id="turn-stale",
        attempt_id="stale-attempt",
        owner_token="stale-owner",
    )

    assert stale is None
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored is not None
    assert stored.get("turn_preparation_guard") is None

    await collection.update_one(
        {"session_id": "session-1"},
        {"$set": {"current_turn_id": "turn-new", "busy_turn_id": "turn-stale"}},
    )
    assert (
        await repo.claim_turn_preparation_guard(
            "session-1",
            principal_id="principal-1",
            sandbox_backend="exclusive-provider",
            sandbox_id="sandbox-1",
            turn_id="turn-stale",
            attempt_id="stale-busy-attempt",
            owner_token="stale-busy-owner",
        )
        is None
    )


async def test_ambiguous_claim_write_is_reaped_if_turn_changes_before_readback(
    guard_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, collection = guard_store
    await _insert_session(collection)
    original_find_one_and_update = collection.find_one_and_update
    first_call = True

    class _AppliedThenResponseLost(RuntimeError):
        pass

    async def _uncertain_find_one_and_update(*args: Any, **kwargs: Any) -> Any:
        nonlocal first_call
        result = await original_find_one_and_update(*args, **kwargs)
        if first_call:
            first_call = False
            await collection.update_one(
                {"session_id": "session-1"},
                {"$set": {"current_turn_id": "turn-2"}},
            )
            raise _AppliedThenResponseLost("server applied write before disconnect")
        return result

    async def _retry_once(_name: str, operation: Any) -> Any:
        try:
            return await operation()
        except _AppliedThenResponseLost:
            return await operation()

    monkeypatch.setattr(collection, "find_one_and_update", _uncertain_find_one_and_update)
    monkeypatch.setattr(repository_module, "run_mongo_with_retry", _retry_once)

    claimed = await _claim(
        repo,
        "ambiguous-attempt",
        owner_token="ambiguous-private-owner",
    )

    assert claimed is None
    stored = await collection.find_one({"session_id": "session-1"})
    assert stored is not None
    assert stored["current_turn_id"] == "turn-2"
    assert stored.get("turn_preparation_guard") is None


def test_internal_guard_never_appears_in_public_session_projection() -> None:
    sanitized = SessionService._sanitize_session(
        {
            "session_id": "session-1",
            "state": "READY",
            "turn_preparation_fence_epoch": 9,
            "turn_preparation_guard": {
                "state": "ACTIVE",
                "attempt_id": "attempt-secret-internal",
            },
        }
    )
    assert "turn_preparation_guard" not in sanitized
    assert "turn_preparation_fence_epoch" not in sanitized


async def test_admin_session_list_never_exposes_guard_owner_token() -> None:
    secret = "private-owner-token-must-not-leak"

    class _SessionsRepo:
        async def list_all_sessions(self, *, limit: int) -> list[dict[str, Any]]:
            assert limit == 25
            return [
                {
                    "session_id": "session-1",
                    "state": "READY",
                    "turn_preparation_guard": {
                        "state": "ACTIVE",
                        "owner_token": secret,
                    },
                }
            ]

    service = AdminService.__new__(AdminService)
    service._sessions_repo = _SessionsRepo()  # type: ignore[assignment]
    service._sanitize_session = SessionService._sanitize_session
    service._user_profiles_repo = None  # type: ignore[assignment]

    rows = await service.admin_list_global_sessions(limit=25)

    assert "turn_preparation_guard" not in rows[0]
    assert secret not in repr(rows)
