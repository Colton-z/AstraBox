"""Session deletion owns its per-session sandbox cleanup.

A session row is the durable name that lets the platform retry a failed
sandbox destruction. Deletion may hide that row only after the sandbox seam
has synchronously confirmed the box gone; an unconfirmed or refused verdict
must remain visible with its distinct recovery contract. A shared Agent box is
the exception: releasing the conversation's isolated placement intentionally
retains the longer-lived box.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.commands import (
    _LifecycleCommandsMixin,
)
from astrabox.seams.sandbox_disposal import SandboxDestruction


class _SessionsRepo:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.soft_deletes: list[tuple[str, str]] = []

    async def update_session(self, session_id: str, updates: dict[str, Any]) -> None:
        self.updates.append((session_id, dict(updates)))

    async def soft_delete(self, session_id: str, user_id: str) -> None:
        self.soft_deletes.append((session_id, user_id))


class _SessionSnapshotsRepo:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        _ = session_id
        if self.error is not None:
            raise self.error
        return {}


class _SessionEventsRepo:
    async def list_events(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        _ = (args, kwargs)
        return []


class _RuntimeManager:
    def __init__(
        self,
        destruction: SandboxDestruction,
        *,
        release: asyncio.Event | None = None,
        runtime_disposal_error: Exception | None = None,
        terminal_disposal_error: Exception | None = None,
    ) -> None:
        self.destruction = destruction
        self.release = release
        self.runtime_disposal_error = runtime_disposal_error
        self.terminal_disposal_error = terminal_disposal_error
        self.started = asyncio.Event()
        self.calls: list[tuple[str, str | None]] = []
        self.disposal_calls: list[str] = []

    async def terminate_runtime(
        self,
        session_id: str,
        fallback_sandbox_id: str | None = None,
    ) -> SandboxDestruction:
        self.calls.append((session_id, fallback_sandbox_id))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return self.destruction

    async def dispose_runtime_session(self, *args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)
        self.disposal_calls.append("agent process")
        if self.runtime_disposal_error is not None:
            raise self.runtime_disposal_error

    async def dispose_terminal_session(self, *args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)
        self.disposal_calls.append("terminal process")
        if self.terminal_disposal_error is not None:
            raise self.terminal_disposal_error


class _DeleteHarness(_LifecycleCommandsMixin):
    def __init__(
        self,
        runtime_manager: _RuntimeManager,
        *,
        cleanup_state_error: Exception | None = None,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._sessions_repo = _SessionsRepo()
        self._session_snapshots_repo = _SessionSnapshotsRepo(error=cleanup_state_error)
        self._session_events_repo = _SessionEventsRepo()

    async def _assert_conversation_safe_to_take_offline(
        self,
        *,
        session_id: str,
        operation: str,
    ) -> None:
        _ = (session_id, operation)

    @staticmethod
    def _is_assistant_user_conversation(session: dict[str, Any]) -> bool:
        _ = session
        return False


_USER = UserContext(user_id="user-1")
_SESSION = {"session_id": "session-1", "sandbox_id": "sandbox-1"}


def _assert_six_field_envelope(
    error: APIError,
    *,
    code: str,
    status_code: int,
    category: str,
    retryable: bool,
    owner: str,
    user_message: str,
) -> None:
    assert error.to_error_envelope() == {
        "code": code,
        "status_code": status_code,
        "category": category,
        "retryable": retryable,
        "owner": owner,
        "user_message": user_message,
    }


async def test_cleanup_state_read_failure_names_the_record_store_and_stops() -> None:
    runtime = _RuntimeManager(SandboxDestruction.confirmed_gone("sandbox-1", detail="gone"))
    harness = _DeleteHarness(
        runtime,
        cleanup_state_error=RuntimeError("record store unavailable"),
    )

    with pytest.raises(APIError) as caught:
        await harness._dispose_conversation_runtime(
            "session-1",
            sandbox_id="sandbox-1",
        )

    _assert_six_field_envelope(
        caught.value,
        code="SESSION_CLEANUP_STATE_READ_FAILED",
        status_code=503,
        category="persistence",
        retryable=True,
        owner="mongo",
        user_message=(
            "Session cleanup state is temporarily unavailable; retry the operation."
        ),
    )
    assert caught.value.data == {"failed_operations": ["read cleanup state"]}
    assert runtime.disposal_calls == [], (
        "process cleanup must not guess at anchors when its durable state was unreadable"
    )


@pytest.mark.parametrize(
    ("runtime_error", "terminal_error", "failed_operation"),
    [
        (RuntimeError("agent stop failed"), None, "stop agent process"),
        (None, RuntimeError("terminal stop failed"), "stop terminal process"),
    ],
    ids=["agent-process", "terminal-process"],
)
async def test_process_cleanup_failure_names_the_runtime_and_remains_retryable(
    runtime_error: Exception | None,
    terminal_error: Exception | None,
    failed_operation: str,
) -> None:
    runtime = _RuntimeManager(
        SandboxDestruction.confirmed_gone("sandbox-1", detail="gone"),
        runtime_disposal_error=runtime_error,
        terminal_disposal_error=terminal_error,
    )
    harness = _DeleteHarness(runtime)

    with pytest.raises(APIError) as caught:
        await harness._dispose_conversation_runtime(
            "session-1",
            sandbox_id="sandbox-1",
        )

    _assert_six_field_envelope(
        caught.value,
        code="SESSION_PROCESS_CLEANUP_FAILED",
        status_code=502,
        category="runtime.cleanup",
        retryable=True,
        owner="runtime",
        user_message=(
            "Session processes could not be stopped; retry after checking the sandbox runtime."
        ),
    )
    assert caught.value.data == {"failed_operations": [failed_operation]}
    assert runtime.disposal_calls == ["agent process", "terminal process"], (
        "one failed process cleanup must not prevent the independent cleanup attempt"
    )


async def test_delete_waits_for_confirmed_sandbox_destruction_before_hiding_owner() -> None:
    release = asyncio.Event()
    runtime = _RuntimeManager(
        SandboxDestruction.confirmed_gone("sandbox-1", detail="gone"),
        release=release,
    )
    harness = _DeleteHarness(runtime)

    deletion = asyncio.create_task(
        harness._delete_session_direct(
            user=_USER,
            session=dict(_SESSION),
            session_id="session-1",
        )
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=0.5)

    assert harness._sessions_repo.soft_deletes == [], (
        "the owning row must remain visible while sandbox destruction is in flight"
    )
    assert not deletion.done(), "session deletion must await the sandbox seam"

    release.set()
    assert await deletion == {"session_id": "session-1", "deleted": True}
    assert runtime.calls == [("session-1", "sandbox-1")]
    assert harness._sessions_repo.soft_deletes == [("session-1", "user-1")]


@pytest.mark.parametrize(
    ("destruction", "expected_code", "expected_retryable", "expected_user_message"),
    [
        (
            SandboxDestruction.unconfirmed(
                "sandbox-1",
                detail="the control plane still reports the box running",
            ),
            "SESSION_SANDBOX_DESTRUCTION_UNCONFIRMED",
            True,
            "Sandbox destruction could not be confirmed; retry session deletion.",
        ),
        (
            SandboxDestruction.refused(
                "sandbox-1",
                detail="sandbox ownership could not be established",
            ),
            "SESSION_SANDBOX_DESTRUCTION_REFUSED",
            False,
            (
                "Sandbox destruction was refused; resolve sandbox ownership or "
                "backend metadata before retrying session deletion."
            ),
        ),
    ],
    ids=["unconfirmed", "refused"],
)
@pytest.mark.parametrize("already_recorded", [False, True])
async def test_failed_sandbox_destruction_keeps_session_visible_and_fails_loud(
    destruction: SandboxDestruction,
    expected_code: str,
    expected_retryable: bool,
    expected_user_message: str,
    already_recorded: bool,
) -> None:
    runtime = _RuntimeManager(destruction)
    harness = _DeleteHarness(runtime)
    session = dict(_SESSION)
    if already_recorded:
        session["undestroyed_sandbox_ids"] = ["sandbox-1"]

    with pytest.raises(APIError) as caught:
        await harness._delete_session_direct(
            user=_USER,
            session=session,
            session_id="session-1",
        )

    _assert_six_field_envelope(
        caught.value,
        code=expected_code,
        status_code=502,
        category="runtime.cleanup",
        retryable=expected_retryable,
        owner="runtime",
        user_message=expected_user_message,
    )
    assert caught.value.data == {
        "failed_operations": ["destroy sandbox"],
        "sandbox_id": "sandbox-1",
        "destruction_outcome": destruction.outcome,
    }
    assert runtime.calls == [("session-1", "sandbox-1")]
    assert harness._sessions_repo.soft_deletes == [], (
        "a failed retry must not hide the row even when its ledger already names the box"
    )
    expected_updates = (
        []
        if already_recorded
        else [("session-1", {"undestroyed_sandbox_ids": ["sandbox-1"]})]
    )
    assert harness._sessions_repo.updates == expected_updates


async def test_delete_accepts_a_confirmed_release_from_a_shared_agent_box() -> None:
    runtime = _RuntimeManager(
        SandboxDestruction.retained(
            "agent-box-1",
            detail="the isolated session closed; the Agent-owned box remains",
        )
    )
    harness = _DeleteHarness(runtime)

    result = await harness._delete_session_direct(
        user=_USER,
        session={"session_id": "session-1", "sandbox_id": "agent-box-1"},
        session_id="session-1",
    )

    assert result == {"session_id": "session-1", "deleted": True}
    assert harness._sessions_repo.updates == []
    assert harness._sessions_repo.soft_deletes == [("session-1", "user-1")]
