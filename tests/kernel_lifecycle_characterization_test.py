"""Characterization tests for the session-kernel lifecycle command surface.

These pin the kernel's OWN entry-point behavior in
``astrabox/core/service/orchestrator/session_kernel/service.py`` — the layer
ABOVE the convergence machinery that the sibling suites already cover:

* ``tests/sandbox_death_convergence_test.py`` pins the convergence PRODUCER
  (``terminal_session_updates`` / ``converge_dead_sandbox``) at the
  ``SandboxLifecycleService`` / ``ExpirationWatcher`` seam;
* ``tests/sandbox_lifecycle_reliability_test.py`` pins probe classification and
  the exited-container READY-not-TERMINATED convergence at the watcher seam;
* ``tests/turn_reconcile_characterization_test.py`` pins the
  ``_reconcile_stuck_turn`` convergence ladder.

Nothing there exercises the kernel's command methods. This file pins:

* ``create_session`` idempotency: a repeated key returns the existing
  conversation, and a key is scoped to its user and subject;
* the agent_chat sandbox-lease-expiry predicate;
* the recovery posture that ``derive_recovery_fields`` hands to the client.

Every async collaborator is faked/stubbed following the split-brain suite's
``__new__`` + attribute-injection idiom. No docker, no network, no sleeps.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_kernel.service import (
    SessionKernelService,
)
from astrabox.providers import register_builtin_providers


register_builtin_providers()

_USER = UserContext(user_id="tester")


def _bare_service() -> SessionKernelService:
    """A service instance with NO __init__ — attributes injected per test."""
    return SessionKernelService.__new__(SessionKernelService)


class _FakeJournalRepo:
    """Records appended command.accepted docs; hands back a monotonic event_seq.

    ``_append_command_accepted`` stamps the generated command_id into the doc as
    ``causation_id``, so a test can read the emitted command back out here.
    """

    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []
        self._seq = 0

    async def append_event(self, doc: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        self.appended.append(dict(doc))
        return {**doc, "event_seq": self._seq, "occurred_at": "2026-01-01T00:00:00+00:00"}


class _CreateSessionsRepo:
    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.existing = existing

    async def get_session_including_deleted(self, _session_id: str) -> dict[str, Any] | None:
        return dict(self.existing) if isinstance(self.existing, dict) else None

    async def get_session(self, _session_id: str) -> dict[str, Any] | None:
        return dict(self.existing) if isinstance(self.existing, dict) else None


class _FakeLifecycleWorker:
    """One shared worker whose run() records each wakeup and returns a fixed outcome.

    ``_build_lifecycle_worker`` is stubbed to hand back the SAME instance so the
    main-command wakeup and any async startup-command wakeup both land in
    ``wakeups`` in call order.
    """

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.wakeups: list[Any] = []

    def run(self, wakeup: Any) -> Any:
        self.wakeups.append(wakeup)
        return self._coro()

    async def _coro(self) -> Any:
        return self._outcome


class _AsyncSpawnRecorder:
    """Real tiny tasks for create_session, whose first spawn is awaited."""

    def __init__(self) -> None:
        self.names: list[str | None] = []
        self.tasks: list[Any] = []

    def __call__(self, coro: Any, *, name: str | None = None) -> Any:
        import asyncio

        self.names.append(name)
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


class CreateSessionIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        *,
        existing: dict[str, Any] | None = None,
    ) -> tuple[SessionKernelService, _FakeLifecycleWorker, _AsyncSpawnRecorder]:
        service = _bare_service()
        service._session_service = SimpleNamespace(
            record_user_profile=AsyncMock(),
            _agent_config=SimpleNamespace(
                resolve_session_harness=AsyncMock(return_value=SimpleNamespace())
            )
        )
        service._sessions_repo = _CreateSessionsRepo(existing)
        service._session_events_repo = _FakeJournalRepo()
        worker = _FakeLifecycleWorker(
            SimpleNamespace(
                metadata={
                    "result": {
                        "session_id": "materialized",
                        "user_id": _USER.user_id,
                        "agent_id": "agent-1",
                    },
                    "startup_command_id": "startup-1",
                }
            )
        )
        service._build_lifecycle_worker = lambda: worker
        service._spawn_background_task = _AsyncSpawnRecorder()
        service.get_session = AsyncMock(side_effect=lambda _user, sid, **_kwargs: {"session_id": sid})
        return service, worker, service._spawn_background_task

    async def test_same_create_key_returns_the_existing_conversation_without_a_second_command(self) -> None:
        service, first_worker, _spawn = self._service()
        first = await service.create_session(
            _USER,
            "agent-1",
            session_kind="agent_chat",
            workspace_ref={"kind": "agent", "agent_id": "agent-1"},
            agent_id="agent-1",
            idempotency_key="browser-create-1",
        )
        session_id = str(first["session_id"])

        existing = {
            "session_id": session_id,
            "user_id": _USER.user_id,
            "agent_id": "agent-1",
            "deleted": False,
        }
        retry, retry_worker, _retry_spawn = self._service(existing=existing)
        second = await retry.create_session(
            _USER,
            "agent-1",
            session_kind="agent_chat",
            workspace_ref={"kind": "agent", "agent_id": "agent-1"},
            agent_id="agent-1",
            idempotency_key="browser-create-1",
        )

        self.assertEqual(second["session_id"], session_id)
        self.assertEqual(len(first_worker.wakeups), 2)
        self.assertEqual(retry_worker.wakeups, [])
        self.assertEqual(retry._session_events_repo.appended, [])

    async def test_same_create_key_is_scoped_to_user_and_subject(self) -> None:
        service, _worker, _spawn = self._service()
        first = await service.create_session(
            _USER,
            "agent-1",
            session_kind="agent_chat",
            workspace_ref={"kind": "agent", "agent_id": "agent-1"},
            agent_id="agent-1",
            idempotency_key="browser-create-2",
        )
        session_id = str(first["session_id"])
        retry, retry_worker, _retry_spawn = self._service(
            existing={
                "session_id": session_id,
                "user_id": "someone-else",
                "agent_id": "agent-1",
                "deleted": False,
            }
        )

        with self.assertRaises(APIError) as caught:
            await retry.create_session(
                _USER,
                "agent-1",
                session_kind="agent_chat",
                workspace_ref={"kind": "agent", "agent_id": "agent-1"},
                agent_id="agent-1",
                idempotency_key="browser-create-2",
            )

        self.assertEqual(caught.exception.code, "IDEMPOTENCY_KEY_CONFLICT")
        self.assertEqual(retry_worker.wakeups, [])


# ── agent_chat lease expiry ──────────────────────────────────────────────────


class BoundRuntimeLeaseExpiredTests(unittest.TestCase):
    """`_bound_runtime_lease_expired` is conservative: only an actually-past,
    parseable expiry on a still-bound sandbox counts as expired."""

    def _expired(self, session: dict[str, Any]) -> bool:
        return SessionKernelService._bound_runtime_lease_expired(session)

    def test_missing_sandbox_id_never_expired(self) -> None:
        self.assertFalse(self._expired({"expires_at": "2000-01-01T00:00:00+00:00"}))

    def test_bound_sandbox_missing_expiry_never_expired(self) -> None:
        self.assertFalse(self._expired({"sandbox_id": "sbx-1", "expires_at": None}))

    def test_bound_sandbox_empty_expiry_never_expired(self) -> None:
        self.assertFalse(self._expired({"sandbox_id": "sbx-1", "expires_at": ""}))

    def test_unparseable_expiry_never_expired(self) -> None:
        self.assertFalse(self._expired({"sandbox_id": "sbx-1", "expires_at": "not-a-date"}))

    def test_past_expiry_is_expired(self) -> None:
        self.assertTrue(
            self._expired({"sandbox_id": "sbx-1", "expires_at": "2000-01-01T00:00:00+00:00"})
        )

    def test_future_expiry_is_not_expired(self) -> None:
        self.assertFalse(
            self._expired({"sandbox_id": "sbx-1", "expires_at": "2099-01-01T00:00:00+00:00"})
        )

    def test_naive_past_expiry_is_expired(self) -> None:
        # An offset-less timestamp is still treated as past (parse_iso yields UTC).
        self.assertTrue(
            self._expired({"sandbox_id": "sbx-1", "expires_at": "2000-01-01T00:00:00"})
        )


if __name__ == "__main__":
    unittest.main()


# ── recovery posture reaches the client on the primary read path ─────────────


class DeriveRecoveryFieldsTests(unittest.TestCase):
    """One shared derivation for both read paths (legacy sanitize + the
    projection-backed primary): persisted recover-worker reasons win; the
    legacy heuristics are only the fallback vocabulary; outside
    RECOVERY_REQUIRED both fields are None."""

    def test_outside_recovery_required_is_none_none(self) -> None:
        from astrabox.core.service.orchestrator.session_service import SessionService

        assert SessionService.derive_recovery_fields(
            "READY", {"recovery_reason": "sandbox_expired"}
        ) == (None, None)

    def test_persisted_worker_reason_wins(self) -> None:
        from astrabox.core.service.orchestrator.session_service import SessionService

        policy, reason = SessionService.derive_recovery_fields(
            "RECOVERY_REQUIRED",
            {
                "recovery_policy": "manual",
                "recovery_reason": "sandbox_reattach_failed",
                "last_error": "turn lock expired",
                "runtime_unavailable": True,
            },
        )
        assert (policy, reason) == ("manual", "sandbox_reattach_failed")

    def test_fallback_vocabulary_when_no_recovery_pass_classified(self) -> None:
        from astrabox.core.service.orchestrator.session_service import SessionService

        assert SessionService.derive_recovery_fields(
            "RECOVERY_REQUIRED", {"runtime_unavailable": True}
        ) == ("auto", "runtime_detached")
        assert SessionService.derive_recovery_fields(
            "RECOVERY_REQUIRED", {}
        ) == ("auto", "session_recovery_required")
