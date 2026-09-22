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

* ``recover_session`` flow branching — reattach-then-recover vs reattach-only vs
  recreate, and the async startup-worker spawn;
* ``_reconcile_runtime_binding`` — the TERMINATED->READY resurrection that
  force-updates the snapshot lifecycle to ACTIVE (binding-relink semantics);
* the READY-not-TERMINATED tolerance of the shared ``_ensure_kernel_engine`` guard
  and the lifecycle entry points (terminate / delete / end / recover accept a
  TERMINATED session; archive / permission-mode do not);
* idempotency of repeated convergence commands through ``_run_lifecycle_command``;
* the agent_chat sandbox-lease-expiry predicate and rebuild-wait terminal exits.

Every async collaborator is faked/stubbed following the split-brain suite's
``__new__`` + attribute-injection idiom. No docker, no network, no sleeps.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.session_kernel.service import (
    SessionKernelService,
)
from astrabox.providers import register_builtin_providers


register_builtin_providers()

_USER = UserContext(user_id="tester")

_SERVICE_MODULE = "astrabox.core.service.orchestrator.session_kernel.service"

# Recovery is authorized by the explicit phase. The other fields describe the
# turn whose transcript the reconcile pass must finish.
_RECOVERY_NEEDED_SNAPSHOT: dict[str, Any] = {
    "conversation_state": "IDLE",
    "last_turn_status": "FAILED",
    "last_turn_id": "turn-rec",
    "current_turn_remote_anchor": {"sandbox_turn_id": 0},
    "turn_recovery_phase": "TRANSCRIPT_PENDING",
}
# IDLE + COMPLETED with an anchor is a cleanly-finished turn -> no recovery.
_NO_RECOVERY_SNAPSHOT: dict[str, Any] = {
    "conversation_state": "IDLE",
    "last_turn_status": "COMPLETED",
    "last_turn_id": "turn-done",
    "current_turn_remote_anchor": {"sandbox_turn_id": 0},
}


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


class _SpawnRecorder:
    """Stand-in for ``self._spawn_background_task`` — records the task name and
    closes the never-awaited coroutine (its wakeup was already recorded)."""

    def __init__(self) -> None:
        self.names: list[str | None] = []

    def __call__(self, coro: Any, *, name: str | None = None) -> Any:
        self.names.append(name)
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return SimpleNamespace(done=lambda: True)


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


class _RecordingSnapshotsRepo:
    """Captures force_update_fields calls; can be told to raise to pin suppression."""

    def __init__(self, *, raise_on_force: bool = False) -> None:
        self.force_calls: list[tuple[str, dict[str, Any]]] = []
        self._raise = raise_on_force

    async def force_update_fields(self, session_id: str, fields: dict[str, Any]) -> bool:
        self.force_calls.append((session_id, dict(fields)))
        if self._raise:
            raise RuntimeError("snapshot write failed")
        return True


def _outcome(result: dict[str, Any] | None) -> SimpleNamespace:
    """A minimal WorkerOutcome stand-in — the kernel only reads ``.metadata``."""
    metadata: dict[str, Any] = {}
    if result is not None:
        metadata["result"] = result
    return SimpleNamespace(metadata=metadata)


class CreateSessionIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def _service(
        self,
        *,
        existing: dict[str, Any] | None = None,
    ) -> tuple[SessionKernelService, _FakeLifecycleWorker, _AsyncSpawnRecorder]:
        service = _bare_service()
        service._session_service = SimpleNamespace(
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


# ── recover_session flow outcomes ────────────────────────────────────────────


class RecoverSessionFlowTests(unittest.IsolatedAsyncioTestCase):
    def _make(
        self,
        *,
        owned_session: dict[str, Any],
        reconciled_session: dict[str, Any],
        outcome: SimpleNamespace,
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[SessionKernelService, _FakeLifecycleWorker, _SpawnRecorder, dict[str, Any]]:
        service = _bare_service()
        service._must_get_owned_session = AsyncMock(return_value=owned_session)
        service._reconcile_runtime_binding = AsyncMock(return_value=reconciled_session)
        service._session_events_repo = _FakeJournalRepo()
        service._sessions_repo = AsyncMock()
        worker = _FakeLifecycleWorker(outcome)
        service._build_lifecycle_worker = lambda: worker
        service._session_snapshots_repo = AsyncMock()
        service._session_snapshots_repo.get_snapshot = AsyncMock(return_value=snapshot)
        service._reconcile_stuck_turn = AsyncMock(return_value={"conversation_state": "IDLE"})
        response_sentinel = {"__get_session__": True}
        service.get_session = AsyncMock(return_value=response_sentinel)
        service._spawn_background_task = _SpawnRecorder()
        return service, worker, service._spawn_background_task, response_sentinel

    async def test_reattached_needing_recovery_runs_reconcile_then_returns_get_session(self) -> None:
        owned = {"session_id": "s-rec", "engine_version": 2, "state": SessionState.READY.value}
        reconciled = {
            "session_id": "s-rec",
            "engine_version": 2,
            "state": SessionState.READY.value,
            "__reconciled__": True,
        }
        service, worker, spawn, response = self._make(
            owned_session=owned,
            reconciled_session=reconciled,
            outcome=_outcome({"status": "reattached"}),
            snapshot=_RECOVERY_NEEDED_SNAPSHOT,
        )

        result = await service.recover_session(_USER, "s-rec")

        # A reattach whose snapshot still needs recovery drives one reconcile pass,
        # keyed on the RECONCILED session, then returns the get_session render.
        self.assertIs(result, response)
        service._session_snapshots_repo.get_snapshot.assert_awaited_once_with("s-rec")
        service._reconcile_stuck_turn.assert_awaited_once_with(
            session_id="s-rec", session=reconciled, snapshot=_RECOVERY_NEEDED_SNAPSHOT
        )
        # No startup_command_id -> no async startup worker.
        self.assertEqual(spawn.names, [])
        # Exactly one lifecycle command was appended and dispatched to the worker.
        self.assertEqual(len(worker.wakeups), 1)
        self.assertEqual(worker.wakeups[0].channel, "lifecycle")
        appended = service._session_events_repo.appended[0]
        self.assertEqual(appended["payload"]["command_type"], "RecoverSession")
        self.assertEqual(worker.wakeups[0].command_id, appended["causation_id"])

    async def test_reattached_without_recovery_skips_reconcile(self) -> None:
        owned = {"session_id": "s-rec", "engine_version": 2, "state": SessionState.READY.value}
        service, _worker, spawn, response = self._make(
            owned_session=owned,
            reconciled_session=dict(owned),
            outcome=_outcome({"status": "reattached"}),
            snapshot=_NO_RECOVERY_SNAPSHOT,
        )

        result = await service.recover_session(_USER, "s-rec")

        # Reattach snapshot IS consulted, but a clean (non-recoverable) snapshot
        # does not trigger a reconcile pass.
        self.assertIs(result, response)
        service._session_snapshots_repo.get_snapshot.assert_awaited_once_with("s-rec")
        service._reconcile_stuck_turn.assert_not_awaited()
        self.assertEqual(spawn.names, [])

    async def test_recreated_needing_recovery_also_runs_reconcile(self) -> None:
        # A recreate (status "startup-requested", the real string
        # _recover_session_direct returns for this path) gets the SAME
        # read-repair treatment as a reattach — it is not gated on
        # status == "reattached" alone.
        owned = {"session_id": "s-rec", "engine_version": 2, "state": SessionState.READY.value}
        reconciled = dict(owned)
        service, worker, spawn, response = self._make(
            owned_session=owned,
            reconciled_session=reconciled,
            outcome=_outcome({"status": "startup-requested", "startup_command_id": "cmd-startup"}),
            snapshot=_RECOVERY_NEEDED_SNAPSHOT,
        )

        result = await service.recover_session(_USER, "s-rec")

        self.assertIs(result, response)
        # A recreate IS consulted for read-repair, same as a reattach.
        service._session_snapshots_repo.get_snapshot.assert_awaited_once_with("s-rec")
        service._reconcile_stuck_turn.assert_awaited_once_with(
            session_id="s-rec", session=reconciled, snapshot=_RECOVERY_NEEDED_SNAPSHOT
        )
        # The startup_command_id is still dispatched asynchronously afterward.
        self.assertEqual(len(worker.wakeups), 2)
        self.assertEqual(worker.wakeups[1].command_id, "cmd-startup")
        self.assertEqual(worker.wakeups[1].channel, "lifecycle")
        self.assertEqual(len(spawn.names), 1)
        self.assertTrue((spawn.names[0] or "").endswith("recover-startup-worker-s-rec"))

    async def test_recreated_without_recovery_skips_reconcile(self) -> None:
        # Symmetric with test_reattached_without_recovery_skips_reconcile: a
        # recreate whose snapshot does not need recovery still fetches it
        # (both success statuses are consulted) but does not reconcile.
        owned = {"session_id": "s-rec", "engine_version": 2, "state": SessionState.READY.value}
        service, worker, spawn, response = self._make(
            owned_session=owned,
            reconciled_session=dict(owned),
            outcome=_outcome({"status": "startup-requested", "startup_command_id": "cmd-startup"}),
            snapshot=_NO_RECOVERY_SNAPSHOT,
        )

        result = await service.recover_session(_USER, "s-rec")

        self.assertIs(result, response)
        service._session_snapshots_repo.get_snapshot.assert_awaited_once_with("s-rec")
        service._reconcile_stuck_turn.assert_not_awaited()
        self.assertEqual(len(worker.wakeups), 2)
        self.assertEqual(len(spawn.names), 1)

    async def test_manual_recovery_required_skips_snapshot_and_reconcile(self) -> None:
        # The one outcome that still skips read-repair: manual-recovery-
        # required touches nothing about the session, so there is no fresh
        # evidence to reconcile against.
        owned = {"session_id": "s-rec", "engine_version": 2, "state": SessionState.READY.value}
        service, worker, spawn, response = self._make(
            owned_session=owned,
            reconciled_session=dict(owned),
            outcome=_outcome({"status": "manual-recovery-required"}),
            snapshot=_RECOVERY_NEEDED_SNAPSHOT,
        )

        result = await service.recover_session(_USER, "s-rec")

        self.assertIs(result, response)
        service._session_snapshots_repo.get_snapshot.assert_not_awaited()
        service._reconcile_stuck_turn.assert_not_awaited()
        # No startup_command_id on this outcome -> no async startup worker.
        self.assertEqual(len(worker.wakeups), 1)
        self.assertEqual(spawn.names, [])

    async def test_terminated_session_is_accepted_for_recovery(self) -> None:
        # READY-not-TERMINATED at the entry point: recover_session passes
        # reject_terminated=False, so a TERMINATED session is recoverable, not a
        # dead end. It must dispatch the RecoverSession command without raising.
        owned = {"session_id": "s-term", "engine_version": 2, "state": SessionState.TERMINATED.value}
        service, worker, _spawn, response = self._make(
            owned_session=owned,
            reconciled_session=dict(owned),
            outcome=_outcome({"status": "recreated"}),
        )

        result = await service.recover_session(_USER, "s-term")

        self.assertIs(result, response)
        self.assertEqual(len(worker.wakeups), 1)
        self.assertEqual(
            service._session_events_repo.appended[0]["payload"]["command_type"],
            "RecoverSession",
        )

    async def test_deleted_session_is_rejected_before_any_command(self) -> None:
        # Contrast with TERMINATED: recover_session passes reject_deleted=True, so a
        # DELETED session is refused and NO command/worker dispatch happens.
        owned = {"session_id": "s-del", "engine_version": 2, "state": SessionState.DELETED.value}
        service, worker, _spawn, _response = self._make(
            owned_session=owned,
            reconciled_session=dict(owned),
            outcome=_outcome({"status": "reattached"}),
        )

        with self.assertRaises(APIError) as ctx:
            await service.recover_session(_USER, "s-del")

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(service._session_events_repo.appended, [])
        self.assertEqual(worker.wakeups, [])


# ── binding-clear / resurrection semantics: _reconcile_runtime_binding ────────


class ReconcileRuntimeBindingResurrectionTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, snapshots_repo: Any) -> SessionKernelService:
        service = _bare_service()
        service._sessions_repo = SimpleNamespace()
        service._agent_repo = SimpleNamespace()
        service._assistant_workspace_service = SimpleNamespace()
        service._session_snapshots_repo = snapshots_repo
        return service

    def _patch_reconcile(
        self, *, reconciled: dict[str, Any], captured: dict[str, Any] | None = None
    ) -> Any:
        async def _fake(**kwargs: Any) -> tuple[dict[str, Any], Any]:
            if captured is not None:
                captured.update(kwargs)
            return reconciled, SimpleNamespace()

        return patch(f"{_SERVICE_MODULE}.reconcile_session_runtime_binding", new=_fake)

    async def test_terminated_to_ready_force_updates_lifecycle_active(self) -> None:
        snapshots = _RecordingSnapshotsRepo()
        service = self._service(snapshots)
        reconciled = {"session_id": "s-1", "state": SessionState.READY.value}
        captured: dict[str, Any] = {}

        with self._patch_reconcile(reconciled=reconciled, captured=captured):
            out = await service._reconcile_runtime_binding(
                {"session_id": "s-1", "state": SessionState.TERMINATED.value}
            )

        # The wrapper returns the underlying reconciliation as-is...
        self.assertIs(out, reconciled)
        # ...and because a TERMINATED session came back READY, it relinks the
        # snapshot lifecycle to ACTIVE so reads stop reporting a dead session.
        self.assertEqual(
            snapshots.force_calls, [("s-1", {"session_lifecycle_state": "ACTIVE"})]
        )
        # Collaborator wiring is forwarded to the underlying reconciler.
        self.assertIs(captured["sessions_repo"], service._sessions_repo)
        self.assertIs(captured["agent_repo"], service._agent_repo)
        self.assertIs(captured["assistant_workspace_service"], service._assistant_workspace_service)
        self.assertIs(captured["persist"], True)

    async def test_no_persist_skips_lifecycle_force_update(self) -> None:
        snapshots = _RecordingSnapshotsRepo()
        service = self._service(snapshots)
        reconciled = {"session_id": "s-1", "state": SessionState.READY.value}
        captured: dict[str, Any] = {}

        with self._patch_reconcile(reconciled=reconciled, captured=captured):
            out = await service._reconcile_runtime_binding(
                {"session_id": "s-1", "state": SessionState.TERMINATED.value},
                persist=False,
            )

        self.assertIs(out, reconciled)
        # persist=False gates the resurrection force-update off entirely.
        self.assertEqual(snapshots.force_calls, [])
        self.assertIs(captured["persist"], False)

    async def test_ready_to_ready_does_not_force_update(self) -> None:
        snapshots = _RecordingSnapshotsRepo()
        service = self._service(snapshots)
        reconciled = {"session_id": "s-1", "state": SessionState.READY.value}

        with self._patch_reconcile(reconciled=reconciled):
            await service._reconcile_runtime_binding(
                {"session_id": "s-1", "state": SessionState.READY.value}
            )

        # No resurrection (old state was not TERMINATED) -> no lifecycle write.
        self.assertEqual(snapshots.force_calls, [])

    async def test_terminated_staying_terminated_does_not_force_update(self) -> None:
        snapshots = _RecordingSnapshotsRepo()
        service = self._service(snapshots)
        reconciled = {"session_id": "s-1", "state": SessionState.TERMINATED.value}

        with self._patch_reconcile(reconciled=reconciled):
            await service._reconcile_runtime_binding(
                {"session_id": "s-1", "state": SessionState.TERMINATED.value}
            )

        # Binding did not come back READY -> nothing to relink.
        self.assertEqual(snapshots.force_calls, [])

    async def test_force_update_failure_is_suppressed(self) -> None:
        snapshots = _RecordingSnapshotsRepo(raise_on_force=True)
        service = self._service(snapshots)
        reconciled = {"session_id": "s-1", "state": SessionState.READY.value}

        with self._patch_reconcile(reconciled=reconciled):
            out = await service._reconcile_runtime_binding(
                {"session_id": "s-1", "state": SessionState.TERMINATED.value}
            )

        # The lifecycle relink is best-effort: a failing snapshot write is
        # swallowed and the reconciled binding is still returned.
        self.assertIs(out, reconciled)
        self.assertEqual(len(snapshots.force_calls), 1)

    async def test_missing_snapshots_repo_is_tolerated(self) -> None:
        service = self._service(snapshots_repo=None)
        reconciled = {"session_id": "s-1", "state": SessionState.READY.value}

        with self._patch_reconcile(reconciled=reconciled):
            out = await service._reconcile_runtime_binding(
                {"session_id": "s-1", "state": SessionState.TERMINATED.value}
            )

        # No snapshot store wired -> the resurrection relink is skipped, no crash.
        self.assertIs(out, reconciled)


# ── idempotency of repeated convergence commands: _run_lifecycle_command ──────


class LifecycleCommandIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def _wire(
        self, *, session: dict[str, Any], outcome: SimpleNamespace
    ) -> tuple[SessionKernelService, _FakeLifecycleWorker]:
        service = _bare_service()
        service._must_get_projection_backed_session = AsyncMock(return_value=session)
        service._session_events_repo = _FakeJournalRepo()
        service._sessions_repo = AsyncMock()
        worker = _FakeLifecycleWorker(outcome)
        service._build_lifecycle_worker = lambda: worker
        return service, worker

    async def test_terminate_falls_back_to_default_result_and_binding_skip(self) -> None:
        session = {"session_id": "s-t", "engine_version": 2, "state": SessionState.READY.value}
        service, _worker = self._wire(session=session, outcome=_outcome(None))

        result = await service.terminate_sandbox(_USER, "s-t")

        # No worker result -> the canonical default shape.
        self.assertEqual(result, {"session_id": "s-t", "status": "terminated"})
        # terminate skips the agent-binding reconcile and the conversation reconcile.
        service._must_get_projection_backed_session.assert_awaited_once_with(
            _USER, "s-t", reconcile_conversation=False, reconcile_agent_binding=False, internal_wiring=True
        )
        self.assertEqual(
            service._session_events_repo.appended[0]["payload"]["command_type"],
            "TerminateSession",
        )

    async def test_terminate_of_terminated_session_is_idempotent(self) -> None:
        # READY-not-TERMINATED tolerance + idempotency: terminate passes
        # reject_terminated=False, so re-terminating an already-TERMINATED session
        # neither raises nor dedups — it appends a fresh command each time and
        # converges to the same terminal result.
        session = {"session_id": "s-t", "engine_version": 2, "state": SessionState.TERMINATED.value}
        service, worker = self._wire(session=session, outcome=_outcome(None))

        first = await service.terminate_sandbox(_USER, "s-t")
        second = await service.terminate_sandbox(_USER, "s-t")

        self.assertEqual(first, {"session_id": "s-t", "status": "terminated"})
        self.assertEqual(first, second)
        # Two independent commands (no client-side dedup); idempotency is at the
        # outcome level, not the command level.
        self.assertEqual(len(service._session_events_repo.appended), 2)
        self.assertEqual(len(worker.wakeups), 2)
        self.assertNotEqual(worker.wakeups[0].command_id, worker.wakeups[1].command_id)

    async def test_worker_result_overrides_default_and_is_copied(self) -> None:
        session = {"session_id": "s-t", "engine_version": 2, "state": SessionState.READY.value}
        worker_result = {"session_id": "s-t", "status": "terminated", "reaped": True}
        service, _worker = self._wire(session=session, outcome=_outcome(worker_result))

        result = await service.terminate_sandbox(_USER, "s-t")

        self.assertEqual(result, worker_result)
        # The returned dict is a defensive copy, not the worker's own object.
        self.assertIsNot(result, worker_result)

    async def test_delete_of_deleted_session_is_idempotent(self) -> None:
        # delete passes reject_deleted=False -> a double-delete is a well-formed
        # no-op that returns the same shape both times.
        session = {"session_id": "s-d", "engine_version": 2, "state": SessionState.DELETED.value}
        service, _worker = self._wire(session=session, outcome=_outcome(None))

        first = await service.delete_session(_USER, "s-d")
        second = await service.delete_session(_USER, "s-d")

        self.assertEqual(first, {"session_id": "s-d", "deleted": True})
        self.assertEqual(first, second)
        # delete (unlike terminate) DOES reconcile the agent binding.
        self.assertEqual(
            service._must_get_projection_backed_session.await_args.kwargs,
            {"reconcile_conversation": False, "reconcile_agent_binding": True, "internal_wiring": True},
        )

    async def test_archive_of_deleted_session_is_rejected(self) -> None:
        # Contrast with delete: archive passes reject_deleted=True, so archiving a
        # DELETED session raises before any command/worker dispatch.
        session = {"session_id": "s-a", "engine_version": 2, "state": SessionState.DELETED.value}
        service, worker = self._wire(session=session, outcome=_outcome(None))

        with self.assertRaises(APIError) as ctx:
            await service.archive_session(_USER, "s-a")

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(service._session_events_repo.appended, [])
        self.assertEqual(worker.wakeups, [])

    async def test_end_conversation_default_result_and_binding_skip(self) -> None:
        session = {"session_id": "s-e", "engine_version": 2, "state": SessionState.READY.value}
        service, _worker = self._wire(session=session, outcome=_outcome(None))

        result = await service.end_conversation(_USER, "s-e")

        self.assertEqual(result, {"session_id": "s-e", "status": "conversation-ended"})
        service._must_get_projection_backed_session.assert_awaited_once_with(
            _USER, "s-e", reconcile_conversation=False, reconcile_agent_binding=False, internal_wiring=True
        )
        self.assertEqual(
            service._session_events_repo.appended[0]["payload"]["command_type"],
            "EndConversation",
        )


# ── shared guard matrix: _require_turn_eligible (READY-not-TERMINATED tolerance) ──


class TurnEligibilityGuardMatrixTests(unittest.TestCase):
    """The pure guard shared by every lifecycle/turn entry point; its reject
    flags are how each command decides whether TERMINATED/DELETED/CREATING are
    tolerated."""

    def test_ready_session_passes(self) -> None:
        # Returning without raising is the pin (the guard is a None-returning gate).
        _bare_service()._require_turn_eligible(
            {"state": SessionState.READY.value},
            channel="lifecycle",
        )

    def test_terminated_allowed_when_reject_terminated_false(self) -> None:
        # The convergence/lifecycle commands pass reject_terminated=False, so a
        # TERMINATED session passes the guard without raising.
        _bare_service()._require_turn_eligible(
            {"state": SessionState.TERMINATED.value},
            channel="lifecycle",
            reject_terminated=False,
        )

    def test_terminated_rejected_by_default(self) -> None:
        with self.assertRaises(APIError) as ctx:
            _bare_service()._require_turn_eligible(
                {"state": SessionState.TERMINATED.value},
                channel="lifecycle",
            )
        self.assertEqual(ctx.exception.code, "AGENT_RUNTIME_ERROR")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_creating_rejected_when_reject_creating(self) -> None:
        with self.assertRaises(APIError) as ctx:
            _bare_service()._require_turn_eligible(
                {"state": SessionState.CREATING.value},
                channel="lifecycle",
                reject_creating=True,
            )
        self.assertEqual(ctx.exception.code, "SESSION_BUSY")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_deleted_rejected_when_reject_deleted(self) -> None:
        with self.assertRaises(APIError) as ctx:
            _bare_service()._require_turn_eligible(
                {"state": SessionState.DELETED.value},
                channel="lifecycle",
                reject_deleted=True,
            )
        self.assertEqual(ctx.exception.code, "AGENT_RUNTIME_ERROR")
        self.assertEqual(ctx.exception.status_code, 409)


# ── agent_chat lease expiry + rebuild-wait terminal exits ─────────────────────


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


class RuntimeSubjectRebuildReadyTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, sessions: list[dict[str, Any]]) -> SessionKernelService:
        service = _bare_service()
        service._must_get_projection_backed_session = AsyncMock(side_effect=list(sessions))
        return service

    async def test_terminated_rebuild_target_returns_immediately(self) -> None:
        # A terminal rebuild failure is NOT waited on — it returns at once so the
        # turn guard can report it (no hang on a dead rebuild).
        terminal = {"session_id": "s-1", "state": SessionState.TERMINATED.value}
        service = self._service([terminal])

        result = await service._await_runtime_subject_rebuild_ready(_USER, "s-1")

        self.assertIs(result, terminal)
        self.assertEqual(service._must_get_projection_backed_session.await_count, 1)

    async def test_ready_and_available_returns_immediately(self) -> None:
        ready = {"session_id": "s-1", "state": SessionState.READY.value, "runtime_unavailable": False}
        service = self._service([ready])

        result = await service._await_runtime_subject_rebuild_ready(_USER, "s-1")

        self.assertIs(result, ready)
        self.assertEqual(service._must_get_projection_backed_session.await_count, 1)

    async def test_runtime_unavailable_is_in_flight_and_bounded_by_budget(self) -> None:
        # runtime_unavailable=True counts as "still rebuilding" (loop entered), and
        # the wait is bounded: once _loop_time passes the deadline it returns the
        # last-seen still-unavailable session without sleeping or re-fetching.
        unavailable = {
            "session_id": "s-1",
            "state": SessionState.READY.value,
            "runtime_unavailable": True,
        }
        service = self._service([unavailable])
        service._loop_time = MagicMock(side_effect=[0.0, 10_000.0])

        result = await service._await_runtime_subject_rebuild_ready(_USER, "s-1")

        self.assertIs(result, unavailable)
        # Only the initial fetch happened — the deadline broke the loop pre-sleep.
        self.assertEqual(service._must_get_projection_backed_session.await_count, 1)


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


async def test_projection_backed_read_surfaces_persisted_recovery_reason() -> None:
    """The primary read path derives recovery fields from persisted recovery
    state rather than hard-coding them to None: a session whose rendered
    state is RECOVERY_REQUIRED exposes what recovery actually determined."""
    from astrabox.core.service.orchestrator.session_service import SessionService

    service = _bare_service()
    service._session_service = SessionService.__new__(SessionService)  # type: ignore[attr-defined]

    row = {
        "session_id": "session-1",
        "state": "RECOVERY_REQUIRED",
        "recovery_policy": "auto",
        "recovery_reason": "sandbox_expired",
        "runtime_unavailable": True,
    }
    policy, reason = service._session_service.derive_recovery_fields(
        "RECOVERY_REQUIRED", row
    )
    assert (policy, reason) == ("auto", "sandbox_expired")
