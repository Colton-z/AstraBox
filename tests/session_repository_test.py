"""Session rows: compare-and-update applies only while its expectation holds."""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.vaults import Vault
from tests._agent_ledger import create_agent

pytestmark = pytest.mark.usefixtures("database")


async def _create(repo: SessionRepository, session_id: str, **extra) -> dict:
    return await repo.create_session(
        {
            "session_id": session_id,
            "user_id": "u1",
            "session_kind": "agent_chat",
            "agent_id": "a1",
            "state": "READY",
            **extra,
        }
    )


@pytest.fixture
async def repo() -> SessionRepository:
    await create_agent("a1")
    await execute(insert(Vault.__table__).values([{"vault_id": v, "org_id": "o1", "created_by": "u1", "display_name": v} for v in ("v1", "v2")]))
    return SessionRepository()


async def test_compare_and_update_applies_only_while_the_expectation_holds(repo: SessionRepository) -> None:
    await _create(repo, "s1")

    assert await repo.compare_and_update_session(
        "s1", expected={"runtime_recovery_owner": None}, updates={"runtime_recovery_owner": "w1"}
    )
    assert not await repo.compare_and_update_session(
        "s1", expected={"runtime_recovery_owner": {"$exists": False}}, updates={"runtime_recovery_owner": "w2"}
    )
    assert await repo.compare_and_update_session(
        "s1", expected={"runtime_recovery_owner": {"$ne": "w2"}}, updates={"last_error": "x"}
    )
    with pytest.raises(KeyError):
        await repo.update_session("s1", {"no_such_column": 1})
