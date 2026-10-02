"""Deletion policy sees durable startup receipts through the repository projection.

The collection below supplies rows in memory. These cases verify the service
and projected resource references, not native database transaction behavior.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.sqlite.collection import _project
from astrabox.persistence.repository.sqlite.query import matches
from astrabox.seams.sandbox import SandboxAllocation


class _SessionRows:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def find(self, query: dict[str, Any], *, projection: dict[str, Any]) -> Any:
        async def rows() -> Any:
            if matches(self.row, query):
                yield _project(deepcopy(self.row), projection)

        return rows()


@pytest.mark.parametrize("retained", [False, True], ids=["current-startup", "superseded-startup"])
@pytest.mark.parametrize("scope", ["sandbox", "isolated_sessions"])
async def test_failed_startup_keeps_environment_until_its_allocation_is_cleared(
    retained: bool, scope: Literal["sandbox", "isolated_sessions"],
) -> None:
    allocation = SandboxAllocation(
        sandbox_id="pending-resource", sandbox_backend="test-backend", scope=scope,
        isolated_session_ids=("pending-engine",) if scope == "isolated_sessions" else (),
    ).as_record()
    row = {
        "session_id": "failed-startup", "agent_id": "retired-agent", "state": "TERMINATED",
        "deleted": True, "hidden": True, "sandbox_id": None, "undestroyed_sandbox_ids": [],
        "title": "private historical title",
        "startup_allocation": None if retained else allocation,
        "_retained_startup_allocations": [{
            "allocation": allocation, "sandbox_generation": "previous", "assignment_id": "old-start",
        }] if retained else [],
    }
    sessions = SessionRepository()
    collection = _SessionRows(row)
    transaction = SimpleNamespace(collection=lambda name: collection)
    deleted = False

    async def remove(name: str, check_references: Any) -> None:
        nonlocal deleted
        assert name == "retired"
        await check_references(transaction)
        deleted = True

    service = AgentConfigService(
        SimpleNamespace(list_agents_by_environment=AsyncMock(return_value=[{
            "agent_id": "retired-agent", "name": "Retired Agent", "deleted": True,
        }])),
        SimpleNamespace(delete_after_reference_check=remove),
        assistant_repo=SimpleNamespace(list_assistants_by_environment=AsyncMock(return_value=[])),
        sessions_repo=sessions,
    )
    user = UserContext(user_id="admin", roles=["admin"])
    with pytest.raises(APIError) as refused:
        await service.delete_environment_config(user, "retired")
    assert refused.value.code == "ENVIRONMENT_IN_USE"
    assert refused.value.data == {"holders": [{
        "target_type": "agent", "target_id": "retired-agent", "target_name": "Retired Agent",
    }]}
    assert deleted is False
    assert "pending-resource" not in str(refused.value.data)
    assert "private historical title" not in str(refused.value.data)

    row["startup_allocation"] = None
    row["_retained_startup_allocations"] = []
    assert await service.delete_environment_config(user, "retired") == {"name": "retired", "deleted": True}
    assert deleted is True
    assert row["title"] == "private historical title"
    assert row["state"] == "TERMINATED"
