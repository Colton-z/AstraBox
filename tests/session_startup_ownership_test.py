"""Concurrent startup claims and receipt writes, raced through the real store."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_lifecycle.ownership import RecoveryOwnership
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.session_repository import SessionRepository

pytestmark = pytest.mark.usefixtures("database")


async def _session(sessions: SessionRepository, session_id: str, **columns: Any) -> None:
    await sessions.create_session(
        {"session_id": session_id, "user_id": "u", "session_kind": "agent_chat", **columns}
    )


async def test_only_the_first_attempt_under_an_assignment_skips_the_provider_lookup() -> None:
    sessions = SessionRepository()
    await _session(sessions, "s", state="CREATING", sandbox_generation="generation-1")

    async with get_persistence().coordination.startup_lock("s") as check_lock:
        def attempt(assignment: str) -> RecoveryOwnership:
            return RecoveryOwnership(
                sessions, "s", None, check_lock, sandbox_generation="generation-1", assignment_id=assignment
            )

        outcomes = await asyncio.gather(
            attempt("startup-1").claim_first_provider_attempt(),
            attempt("startup-1").claim_first_provider_attempt(),
        )

        assert sorted(outcomes) == [False, True]
        assert await attempt("startup-1").claim_first_provider_attempt() is False
        assert await attempt("startup-2").claim_first_provider_attempt() is True



def _receipt(assignment: str) -> dict[str, Any]:
    return {
        "allocation": {"sandbox_id": "shared-box", "sandbox_backend": "open_sandbox",
                       "scope": "isolated_session", "isolated_session_ids": [assignment]},
        "sandbox_generation": "old-generation", "assignment_id": assignment,
    }


async def test_concurrent_receipt_append_and_remove_preserve_the_successor_binding() -> None:
    sessions = SessionRepository()
    old, new = _receipt("old-placement"), _receipt("new-placement")
    successor = {
        "sandbox_id": "successor-box",
        "state": "READY",
        "sandbox_generation": "new-generation",
        "runtime_recovery_owner": "new-owner",
        "startup_allocation": {"sandbox_id": "active-box"},
    }
    await _session(sessions, "s", retained_startup_allocations=[old], **successor)

    await asyncio.wait_for(
        asyncio.gather(
            sessions.retain_startup_allocation("s", new),
            sessions.clear_retained_startup_allocation("s", old),
        ),
        timeout=10,
    )

    current = await sessions.get_session("s")
    assert current is not None
    assert current["retained_startup_allocations"] == [new]
    assert {key: current[key] for key in successor} == successor
