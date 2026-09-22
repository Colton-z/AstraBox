"""A startup outcome the session row holds must reach the reader that renders it.

Startup settles in two writes — the fenced ``sessions`` row, then the journal
event and the lifecycle projection (``workers/lifecycle/startup.py``) — and
every read derives the user-visible state from the projection alone
(``derive_ui_state``). A process that dies between the two writes therefore
leaves a conversation whose row is READY with its sandbox, and whose projection
still says CREATING: the API reports CREATING, ``_require_turn_eligible``
refuses input as "still creating", and recovery derives the same state and
refuses to run. Nothing selected that pair — ``BootstrapReconciler`` scans rows
whose own state is CREATING, and this row is not one.

These tests drive the read path over that torn pair and require the reader to
republish the row's own outcome, without starting a second startup or naming a
second sandbox.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from unittest.mock import AsyncMock

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_kernel.service import (
    SessionKernelService,
)
from astrabox.providers import register_builtin_providers


register_builtin_providers()

_USER = UserContext(user_id="tester")
_SESSION_ID = "d183f714-25c7-4001-9676-1bd6983164ef"
_SANDBOX_ID = "19e197c2-4a1f-4a3a-9a4f-7d1b2f0c5e10"


def _ready_row(**overrides: Any) -> dict[str, Any]:
    """The row a startup settles before it publishes readiness."""
    row = {
        "session_id": _SESSION_ID,
        "user_id": _USER.user_id,
        "state": "READY",
        "sandbox_id": _SANDBOX_ID,
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "agent_id": "agent-1",
        "permission_mode": "default",
        "runtime_unavailable": False,
        "last_error": None,
        "startup_progress": None,
        "terminal_cwd": "/home/agent/workspace",
    }
    row.update(overrides)
    return row


def _creating_projection(**overrides: Any) -> dict[str, Any]:
    """The lifecycle projection left behind by an interrupted publication."""
    snapshot = {
        "session_id": _SESSION_ID,
        "session_lifecycle_state": "CREATING",
        "runtime_connectivity_state": "CONNECTING",
        "conversation_state": "IDLE",
        "terminal_state": "IDLE",
        "permission_mode": "default",
        "lifecycle_event_seq_applied": 38,
    }
    snapshot.update(overrides)
    return snapshot


class _SessionsRepo:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def get_session(self, _session_id: str) -> dict[str, Any] | None:
        return dict(self.row) if isinstance(self.row, dict) else None


class _EventsRepo:
    """Hands out the monotonic event_seq the journal assigns on append."""

    def __init__(self, *, last_seq: int = 38) -> None:
        self.appended: list[dict[str, Any]] = []
        self._seq = last_seq

    async def append_event(self, doc: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        self.appended.append(dict(doc))
        return {**doc, "event_seq": self._seq}

    async def list_events(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    @property
    def event_types(self) -> list[str]:
        return [str(event.get("event_type") or "") for event in self.appended]


class _SnapshotsRepo:
    """Applies the real per-channel watermark rule to every projection write."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = dict(snapshot)
        self.applied: list[dict[str, Any]] = []

    async def get_snapshot(self, _session_id: str) -> dict[str, Any]:
        return dict(self.snapshot)

    async def get_snapshots_batch(
        self,
        session_ids: list[str],
        *,
        projection: dict[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        stored = dict(self.snapshot)
        if isinstance(projection, dict):
            stored = {
                key: value
                for key, value in stored.items()
                if int(projection.get(key, 0)) == 1
            }
        return {session_id: dict(stored) for session_id in session_ids}

    async def apply_channel_update(
        self,
        session_id: str,
        *,
        channel: str,
        event_seq: int,
        updates: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any] | None:
        self.applied.append(
            {
                "session_id": session_id,
                "channel": channel,
                "event_seq": int(event_seq),
                "updates": dict(updates),
            }
        )
        watermark = f"{channel}_event_seq_applied"
        if int(event_seq) <= int(self.snapshot.get(watermark) or 0):
            return None
        self.snapshot.update(updates)
        self.snapshot[watermark] = int(event_seq)
        return dict(self.snapshot)


class _InteractionSnapshotsRepo:
    async def get_active_interaction(self, _session_id: str) -> dict[str, Any] | None:
        return None


def _read_service(
    *,
    row: dict[str, Any] | None,
    snapshot: dict[str, Any],
    rendered_row: dict[str, Any] | None = None,
) -> SessionKernelService:
    """The kernel read surface over one session, with every repo faked.

    ``rendered_row`` is what the read path holds in memory — the runtime-binding
    overlay hands the renderer a session it has not persisted — while ``row`` is
    what the database returns.
    """
    service = SessionKernelService.__new__(SessionKernelService)
    service._sessions_repo = _SessionsRepo(row)
    service._session_events_repo = _EventsRepo()
    service._session_snapshots_repo = _SnapshotsRepo(snapshot)
    service._interaction_snapshots_repo = _InteractionSnapshotsRepo()
    service._session_service = SimpleNamespace(
        _sanitize_session=lambda session: dict(session),
        derive_recovery_fields=lambda _state, _session: (None, None),
    )
    # Naming a runtime operation here would mean the read path tried to start
    # something; this namespace only answers the renderer's cwd question.
    service._runtime_manager = SimpleNamespace(
        resolve_session_terminal_cwd=lambda *_args, **_kwargs: None,
    )
    service._turn_service = SimpleNamespace(pending_input_rows=AsyncMock(return_value=[]))
    in_memory = dict(rendered_row if isinstance(rendered_row, dict) else (row or {}))
    service._must_get_owned_session = AsyncMock(return_value=dict(in_memory))
    service._reconcile_runtime_binding = AsyncMock(side_effect=lambda session, **_kw: dict(in_memory))
    service._get_background_task_state = AsyncMock(return_value=None)
    service._load_agent_runtime_map = AsyncMock(return_value={})
    return service


@pytest.mark.asyncio
async def test_a_startup_interrupted_before_it_published_still_reads_ready() -> None:
    service = _read_service(row=_ready_row(), snapshot=_creating_projection())

    rendered = await service.get_session(_USER, _SESSION_ID)

    assert rendered["state"] == "READY"
    # The conversation keeps the box its startup already bound: republishing a
    # state must never look like a second startup.
    assert rendered["sandbox_id"] == _SANDBOX_ID
    assert service._session_events_repo.event_types == ["session.lifecycle_reconciled"]
    # The journal is where an operator sees that a startup outcome went
    # unpublished, so the record names the projection it replaced.
    assert service._session_events_repo.appended[0]["payload"] == {
        "reason": "unpublished_startup_outcome",
        "previous_state": "CREATING",
        "observed_session_state": "READY",
    }


@pytest.mark.asyncio
async def test_the_republished_outcome_is_durable_for_recovery_and_admin_reads() -> None:
    """Rendering alone would leave recovery and the admin view refusing the row.

    ``_derive_effective_session_state_for_recover`` and
    ``AdminService._derive_conversation_state`` read the stored projection
    without the session row, so the convergence has to land in the database.
    """
    service = _read_service(row=_ready_row(), snapshot=_creating_projection())

    await service.get_session(_USER, _SESSION_ID)

    stored = service._session_snapshots_repo.snapshot
    assert stored["session_lifecycle_state"] == "ACTIVE"
    assert stored["runtime_connectivity_state"] == "CONNECTED"
    # The append is the ordering point: the projection carries the seq of the
    # event just journaled, so a lifecycle write that appends later still wins
    # the watermark.
    assert stored["lifecycle_event_seq_applied"] == 39


@pytest.mark.asyncio
async def test_a_startup_still_in_flight_is_not_published_as_ready() -> None:
    service = _read_service(row=_ready_row(state="CREATING"), snapshot=_creating_projection())

    rendered = await service.get_session(_USER, _SESSION_ID)

    assert rendered["state"] == "CREATING"
    assert service._session_events_repo.appended == []
    assert service._session_snapshots_repo.applied == []


@pytest.mark.asyncio
async def test_an_interrupted_failed_startup_publishes_the_failure_it_recorded() -> None:
    service = _read_service(
        row=_ready_row(
            state="TERMINATED",
            runtime_unavailable=True,
            last_error="failed to start remote-agent runtime",
        ),
        snapshot=_creating_projection(),
    )

    rendered = await service.get_session(_USER, _SESSION_ID)

    assert rendered["state"] == "TERMINATED"
    assert service._session_snapshots_repo.snapshot["session_lifecycle_state"] == "TERMINATED"


@pytest.mark.asyncio
async def test_the_published_state_comes_from_the_row_not_the_in_memory_overlay() -> None:
    """The overlay may say READY over a row the database still holds TERMINATED.

    ``get_session`` reconciles the runtime binding in memory and deliberately
    does not persist it, so the convergence must read the row back instead of
    writing what the renderer was handed.
    """
    service = _read_service(
        row=_ready_row(state="TERMINATED", runtime_unavailable=True),
        snapshot=_creating_projection(),
        rendered_row=_ready_row(state="READY"),
    )

    await service.get_session(_USER, _SESSION_ID)

    stored = service._session_snapshots_repo.snapshot
    assert stored["session_lifecycle_state"] == "TERMINATED"
    assert stored["runtime_connectivity_state"] == "LOST"


@pytest.mark.asyncio
async def test_a_second_read_neither_journals_nor_projects_again() -> None:
    service = _read_service(row=_ready_row(), snapshot=_creating_projection())

    await service.get_session(_USER, _SESSION_ID)
    await service.get_session(_USER, _SESSION_ID)

    assert service._session_events_repo.event_types == ["session.lifecycle_reconciled"]
    assert len(service._session_snapshots_repo.applied) == 1


@pytest.mark.asyncio
async def test_the_conversation_list_shows_the_published_state() -> None:
    service = _read_service(row=_ready_row(), snapshot=_creating_projection())

    rendered = await service._render_session_rows([_ready_row()])

    assert [row["state"] for row in rendered] == ["READY"]
    assert service._session_snapshots_repo.snapshot["session_lifecycle_state"] == "ACTIVE"
