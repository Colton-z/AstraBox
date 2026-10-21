"""Gateway keys of claimed prepared slots: one owner per slot, released once, by their issuer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.core.service.orchestrator.agent import prepared_slots as ps
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository

pytestmark = pytest.mark.usefixtures("database")


async def _agent() -> AgentRepository:
    await RuntimeProfileRepository().put(
        "env",
        {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    repo = AgentRepository()
    await repo.create_agent(
        {
            "agent_id": "agent-1", "name": "agent-1", "model": "model",
            "runtime_profile_name": "env", "created_by": "owner",
        }
    )
    return repo


class _Sessions:
    """Sessions whose id names their state: ``ended-*`` ones are terminated."""

    async def get_session(self, session_id: str) -> dict[str, str]:
        return {"state": "TERMINATED" if session_id.startswith("ended") else "READY"}


class _Endpoints:
    def __init__(self) -> None:
        self.released: list[tuple[str | None, str]] = []

    def __call__(self, provider: str | None) -> Any:
        endpoints = self

        class _Endpoint:
            async def release_session_credential(self, *, context: Any) -> bool:
                endpoints.released.append((provider, context.conversation_id))
                return True

        return _Endpoint()


@pytest.fixture
def endpoints(monkeypatch: pytest.MonkeyPatch) -> _Endpoints:
    from astrabox.persistence.repository import session_repository
    from astrabox.seams import model as model_seam

    released = _Endpoints()
    monkeypatch.setattr(session_repository, "SessionRepository", _Sessions)
    monkeypatch.setattr(model_seam, "model_endpoint_for_name", released)
    return released


async def test_concurrent_claims_each_keep_their_own_entry() -> None:
    repo = await _agent()

    await asyncio.gather(
        *(
            ps.record_gateway_entry(repo, "agent-1", slot_id=f"slot-{n}", session_id=f"session-{n}")
            for n in range(8)
        )
    )

    entries = await repo.list_gateway_entries("agent-1")
    assert sorted((entry["slot_id"], entry["session_id"]) for entry in entries) == [
        (f"slot-{n}", f"session-{n}") for n in range(8)
    ]


async def test_a_slot_owned_by_one_session_is_refused_to_another() -> None:
    repo = await _agent()
    await ps.record_gateway_entry(repo, "agent-1", slot_id="slot", session_id="session-a")
    # Recording the same owner again is idempotent.
    await ps.record_gateway_entry(repo, "agent-1", slot_id="slot", session_id="session-a")

    with pytest.raises(Exception) as refused:
        await ps.record_gateway_entry(repo, "agent-1", slot_id="slot", session_id="session-b")

    assert getattr(refused.value, "status_code", None) == 409
    assert [entry["session_id"] for entry in await repo.list_gateway_entries("agent-1")] == [
        "session-a"
    ]


async def test_an_ended_sessions_key_is_released_once_by_the_provider_that_issued_it(
    endpoints: _Endpoints,
) -> None:
    repo = await _agent()
    await ps.record_gateway_entry(
        repo, "agent-1", slot_id="slot-ended", session_id="ended-1", endpoint_provider="vendor",
    )
    await ps.record_gateway_entry(repo, "agent-1", slot_id="slot-live", session_id="live-1")

    # Two pruners read the same ended entry; only the one that removes it releases its key.
    kept = await asyncio.gather(
        ps._prune_gateway_entries(repo, "agent-1"),
        ps._prune_gateway_entries(repo, "agent-1"),
    )

    assert endpoints.released == [("vendor", "ended-1")]
    assert [[entry["slot_id"] for entry in run] for run in kept] == [["slot-live"], ["slot-live"]]
    assert [entry["slot_id"] for entry in await repo.list_gateway_entries("agent-1")] == [
        "slot-live"
    ]
