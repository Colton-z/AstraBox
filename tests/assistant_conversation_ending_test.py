"""An Assistant conversation that ended for its own reason stays ended.

A conversation follows its workspace: while the workspace cannot carry it, it
is unavailable, and one the workspace ended rejoins it once the workspace is
ready again. A conversation whose own setup failed is different. Its workspace
was ready the whole time, so the workspace being ready says nothing about it;
bringing it back READY erased the setup error and left the first message to
find the same failure as a 409.

These drive the reconciliation every session read goes through, with the rows
the startup path actually leaves behind: the binding projection it persisted
before attaching, then the terminal write of a failed startup.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.runtime_binding import (
    reconcile_session_runtime_binding,
)
from astrabox.providers import register_builtin_providers

register_builtin_providers()

_SETUP_ERROR = (
    "hermes runtime start failed: AGENT_MCP_NETWORK_ACCESS_DISABLED: this Agent "
    "declares remote MCP endpoints, but its limited Environment has "
    "networking.allow_mcp_servers=false"
)


class _Workspace:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    async def get_workspace(self, *, user_id: str, assistant_id: str) -> Any:
        return dict(self.row)


class _Sessions:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def update_session(
        self, session_id: str, updates: dict[str, Any], **_kwargs: Any
    ) -> None:
        self.writes.append(dict(updates))


_READY = {"state": "READY", "engine_kind": "assistant", "current_sandbox_id": "sb-1"}
_PARKED = {"state": "HIBERNATED", "engine_kind": "assistant", "current_sandbox_id": None}


def _conversation(**fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "session_id": "s-1",
        "session_kind": "assistant_chat",
        "user_id": "u-1",
        "workspace_ref": {
            "kind": "assistant",
            "assistant_id": "a-1",
            "user_id": "u-1",
            "engine_kind": "assistant",
        },
    }
    row.update(fields)
    return row


async def _read(row: dict[str, Any], workspace: dict[str, Any]) -> dict[str, Any]:
    sessions = _Sessions()
    reconciled, _ = await reconcile_session_runtime_binding(
        session=row,
        sessions_repo=sessions,
        assistant_workspace_service=_Workspace(workspace),
        persist=True,
    )
    for write in sessions.writes:
        row.update(write)
    return reconciled


@pytest.mark.asyncio
async def test_a_conversation_whose_setup_failed_stays_failed() -> None:
    row = _conversation(state="CREATING")
    # Startup reconciles the binding (persisted) before it attaches...
    await _read(row, _READY)
    assert row["runtime_binding"]["can_dispatch"] is True
    # ...then the attach fails and startup writes the conversation's ending.
    row.update(state="TERMINATED", runtime_unavailable=True, last_error=_SETUP_ERROR)

    shown = await _read(row, _READY)

    assert shown["state"] == "TERMINATED"
    assert shown["last_error"] == _SETUP_ERROR
    assert row["state"] == "TERMINATED" and row["last_error"] == _SETUP_ERROR


@pytest.mark.asyncio
async def test_a_later_outage_does_not_turn_its_own_ending_into_the_workspaces() -> None:
    row = _conversation(state="CREATING")
    await _read(row, _READY)
    row.update(state="TERMINATED", runtime_unavailable=True, last_error=_SETUP_ERROR)

    await _read(row, _PARKED)
    shown = await _read(row, _READY)

    assert shown["state"] == "TERMINATED"
    assert shown["last_error"] == _SETUP_ERROR


@pytest.mark.asyncio
async def test_a_conversation_the_workspace_ended_rejoins_it_when_ready() -> None:
    row = _conversation(state="CREATING")
    # The workspace could not carry the conversation when startup gave up.
    await _read(row, _PARKED)
    assert row["runtime_binding"]["can_dispatch"] is False
    row.update(
        state="TERMINATED",
        runtime_unavailable=True,
        last_error=row["runtime_binding"]["reason_message"],
    )

    shown = await _read(row, _READY)

    assert shown["state"] == "READY"
    assert shown["last_error"] is None
    assert shown["runtime_unavailable"] is False
