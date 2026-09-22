"""Durable workspace identity is one-winner state, not a last-write hint."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_concurrent_agent_box_creators_mount_the_same_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool refill and cold start cannot publish different Agent roots."""

    from astrabox.core.service.orchestrator.runtime.storage._identity import (
        WORKSPACE_ID_FIELD,
        _ensure_agent_workspace_id,
    )
    from astrabox.persistence.repository import agent_repository as repository_module

    class _Repo:
        def __init__(self) -> None:
            self.row: dict[str, Any] = {"agent_id": "agent-1"}
            self.initial_reads = 0
            self.both_read = asyncio.Event()

        async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
            assert agent_id == "agent-1"
            snapshot = dict(self.row)
            if WORKSPACE_ID_FIELD not in snapshot:
                self.initial_reads += 1
                if self.initial_reads == 2:
                    self.both_read.set()
                await self.both_read.wait()
            return snapshot

        async def compare_and_update_agent(
            self,
            agent_id: str,
            *,
            expected: dict[str, Any],
            updates: dict[str, Any],
        ) -> bool:
            assert agent_id == "agent-1"
            wanted = expected[WORKSPACE_ID_FIELD]
            matches = (
                WORKSPACE_ID_FIELD not in self.row
                if wanted == {"$exists": False}
                else self.row.get(WORKSPACE_ID_FIELD) == wanted
            )
            if not matches:
                return False
            self.row.update(updates)
            return True

    repo = _Repo()
    monkeypatch.setattr(repository_module, "AgentRepository", lambda: repo)

    first, second = await asyncio.gather(
        _ensure_agent_workspace_id("agent-1"),
        _ensure_agent_workspace_id("agent-1"),
    )

    assert first == second == repo.row[WORKSPACE_ID_FIELD]
