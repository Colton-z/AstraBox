"""Operator views name the turn the conversation snapshot says is running.

The session row records no running turn: the turn worker's StartTurn
projection writes it only into the snapshot's turn slot, and the live worker's
terminal commit clears it there. Each case drives those producers over the
SQLite store and reads the result back through the admin trace and the admin
error list, which build on the session row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator import admin_service as admin_service_module
from astrabox.core.service.orchestrator.admin_service import AdminService
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    build_turn_terminal_snapshot_updates,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.create import (
    _CreateSessionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.bridge_terminal import (
    _commit_turn_terminal_snapshot,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.projections import (
    _TurnProjectionMixin,
)
from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.persistence.repository import session_repository, session_snapshot_repository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_snapshot_repository import (
    SessionSnapshotRepository,
)
from astrabox.persistence.repository.sqlite.collection import AsyncCollection
from astrabox.persistence.repository.sqlite.engine import dispose_engines

_OPERATOR = UserContext(user_id="operator", roles=["admin"])


class _FrameLog:
    """The frames the trace asked for, by turn."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def list_frames(self, session_id: str, *, turn_id: str, **_: Any) -> list[Any]:
        self.requested.append(turn_id)
        return []


async def _earlier_turn_messages(session_id: str, **_: Any) -> tuple[list[Any], bool]:
    # Only an earlier, finished turn has messages; the running turn has
    # produced none yet.
    return (
        [
            {"message_id": "m-1", "turn_id": "turn-0", "role": "user", "content": "first"},
            {"message_id": "m-2", "turn_id": "turn-0", "role": "assistant", "content": "done"},
        ],
        False,
    )


async def _access_doc(agent_id: str) -> dict[str, Any]:
    return {"agent_id": agent_id, "name": agent_id}


async def _no_display_names(user_ids: list[str]) -> dict[str, str]:
    return {}


async def _no_runtime_errors(limit: int = 200) -> list[dict[str, Any]]:
    return []


@pytest.fixture
async def admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AdminService]:
    url = f"sqlite+aiosqlite:///{tmp_path}/admin-running-turn.sqlite"
    opened: dict[str, AsyncCollection] = {}

    async def collection(name: str) -> AsyncCollection:
        if name not in opened:
            opened[name] = AsyncCollection(name, url)
        return opened[name]

    monkeypatch.setattr(session_repository, "get_async_collection", collection)
    monkeypatch.setattr(session_snapshot_repository, "get_async_collection", collection)
    monkeypatch.setattr(admin_service_module, "list_runtime_errors", _no_runtime_errors)
    service = AdminService.__new__(AdminService)
    service._sessions_repo = SessionRepository()
    service._session_snapshots_repo = SessionSnapshotRepository()
    service._sanitize_session = SessionService._sanitize_session
    service._agent_config = SimpleNamespace(get_agent_access_doc=_access_doc)  # type: ignore[assignment]
    service._user_profiles_repo = SimpleNamespace(  # type: ignore[assignment]
        batch_get_display_names=_no_display_names
    )
    service._message_view = SimpleNamespace(list_page=_earlier_turn_messages)  # type: ignore[assignment]
    service._session_events_repo = _FrameLog()  # type: ignore[assignment]
    try:
        yield service
    finally:
        await dispose_engines(url)


async def _create_session(admin: AdminService, session_id: str, **row: Any) -> None:
    """An Agent conversation with an empty turn slot, as creation leaves it."""
    await admin._sessions_repo.create_session(
        {
            "session_id": session_id,
            "user_id": "user-1",
            "state": "READY",
            "session_kind": "agent_chat",
            "agent_id": "agent-1",
            "workspace_ref": {"kind": "agent", "agent_id": "agent-1"},
            **row,
        }
    )
    await _CreateSessionMixin._initialize_create_snapshots(
        SimpleNamespace(_session_snapshots_repo=admin._session_snapshots_repo),  # type: ignore[arg-type]
        session_id=session_id,
        event_seq=1,
    )


async def _admit(admin: AdminService, session_id: str, turn_id: str) -> None:
    """Admit a StartTurn through the turn worker's own projection."""
    await _TurnProjectionMixin._project_command_accepted(
        SimpleNamespace(_session_snapshots_repo=admin._session_snapshots_repo),  # type: ignore[arg-type]
        session_id=session_id,
        turn_id=turn_id,
        command_event={"causation_id": f"command-{turn_id}", "event_seq": 2},
        command_type="StartTurn",
    )


async def test_the_admin_trace_opens_on_the_turn_the_snapshot_says_is_running(
    admin: AdminService,
) -> None:
    await _create_session(admin, "session-1")
    await _admit(admin, "session-1", "turn-1")

    trace = await admin.admin_get_session_trace(_OPERATOR, "session-1")

    assert trace["current_turn_id"] == "turn-1"
    assert trace["selected_turn_id"] == "turn-1"
    assert trace["turns"][0]["turn_id"] == "turn-1"
    frames = admin._session_events_repo
    assert isinstance(frames, _FrameLog)
    assert frames.requested == ["turn-1"]

    # Once the turn completes the slot is empty, and the trace opens on the
    # newest turn the transcript recorded.
    committed = await _commit_turn_terminal_snapshot(
        SimpleNamespace(_session_snapshots_repo=admin._session_snapshots_repo),
        session_id="session-1",
        turn_id="turn-1",
        event_seq=3,
        updates=build_turn_terminal_snapshot_updates(
            turn_id="turn-1",
            status="COMPLETED",
            error_text=None,
            command_id="command-turn-1",
        ),
    )
    assert committed is not None
    settled = await admin.admin_get_session_trace(_OPERATOR, "session-1")
    assert settled["current_turn_id"] is None
    assert settled["selected_turn_id"] == "turn-0"


async def test_the_admin_error_list_names_the_running_turn_of_a_failing_session(
    admin: AdminService,
) -> None:
    await _create_session(
        admin,
        "session-running",
        runtime_unavailable=True,
        last_error="sandbox unreachable",
    )
    await _admit(admin, "session-running", "turn-1")
    await _create_session(admin, "session-idle", last_error="start failed")

    page = await admin.admin_list_errors(limit=50)

    turns = {item["session_id"]: item["turn_id"] for item in page["errors"]}
    assert turns == {"session-running": "turn-1", "session-idle": None}
