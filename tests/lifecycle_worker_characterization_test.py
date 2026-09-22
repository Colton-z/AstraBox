"""Characterization tests for ``SessionLifecycleWorker`` itself.

``astrabox/core/service/orchestrator/session_kernel/workers/lifecycle/`` contains
the worker entry class in ``worker.py`` and its sibling helper modules. These
tests cover the concrete worker between two adjacent seams:

* ``tests/kernel_lifecycle_characterization_test.py`` pins
  ``SessionKernelService``'s outer command-dispatch/wiring contract, but
  substitutes a ``_FakeLifecycleWorker`` for ``_build_lifecycle_worker()`` —
  so it never runs a line of this file's real code.
* ``tests/sandbox_death_convergence_test.py`` /
  ``tests/sandbox_lifecycle_reliability_test.py`` pin the sibling dead-binding
  convergence owner (``sandbox_lifecycle.py`` / ``expiration_watcher.py``).
  This worker does not own that convergence and these tests do not duplicate
  it — they only pin how THIS file *routes* work, respecting that
  single-owner design.

This file directly instantiates ``SessionLifecycleWorker`` (via its real
``__init__``, with fake repo/collaborator objects — no docker, no network, no
mongo) and pins the decision outcomes on its genuinely-uncovered surface:

* ``run_once``'s command-type dispatch tree — ``CreateSession`` and
  ``StartSessionStartup`` are routed to their own handlers and bypass the
  generic session-load + ``_execute_command`` path entirely;
  ``SetPermissionMode`` loads the session but is still routed around
  ``_execute_command`` to ``PermissionLifecycle``.
* the generic success/failure event-append + snapshot-projection wrapping
  ``run_once`` applies around every other command type (``_execute_command``
  callers), including the DELETED-projection synthesis on a successful delete
  and the re-fetch-latest-session behavior on a failed command.
* ``_execute_command``'s own switch (the unsupported-command and
  wrongly-routed-SetPermissionMode guard rails).
* ``_run_create_session_command``'s success/failure shape — failure appends
  only ``session.create_failed`` and re-raises untouched; success seeds the
  conversation/terminal channel snapshots and synthesizes the nested
  ``StartSessionStartup`` follow-up command with a fresh command id.
* the entry guard of ``_run_startup_command`` and its delegation to the
  runtime-subject coordinator.

Every collaborator is a bare fake (``AsyncMock`` / a tiny recording repo
stand-in), following the ``__new__``-adjacent "real ``__init__`` + injected
fakes" idiom the other worker characterizations use. Deterministic, no
sleeps, no docker, no network.
"""

from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
import pytest
from astrabox.seams.sandbox_disposal import (
    SANDBOX_DESTRUCTION_RETAINED,
    SANDBOX_DESTRUCTION_UNCONFIRMED,
    SandboxDestruction,
)
from unittest.mock import AsyncMock, Mock, patch

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
    PermissionLifecycleResult,
)
from astrabox.core.service.orchestrator.runtime_subject import (
    RuntimeStartupCleanup,
    RuntimeStartupTarget,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.worker import (
    SessionLifecycleWorker,
)
from astrabox.core.service.orchestrator.session_kernel.workers.models import (
    WorkerOutcome,
    WorkerWakeup,
)
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest

_LIFECYCLE_MODULE = (
    "astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.worker"
)


class _FakeJournalRepo:
    """Hands back a fixed ``command.accepted`` doc; records every append.

    ``append_event`` assigns a monotonically increasing ``event_seq`` (starting
    at 1) so tests can assert the exact sequence numbers threaded through the
    worker's success/failure wrapping.
    """

    def __init__(self, *, command_event: dict[str, Any] | None = None) -> None:
        self._command_event = command_event
        self.appended: list[dict[str, Any]] = []
        self._seq = 0

    async def get_command_event(
        self, session_id: str, *, command_id: str
    ) -> dict[str, Any] | None:
        return self._command_event

    async def append_event(self, doc: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        self.appended.append(dict(doc))
        return {**doc, "event_seq": self._seq}

    async def list_events(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _make_worker(
    *,
    sessions_repo: Any = None,
    session_service: Any = None,
    turn_service: Any = None,
    runtime_manager: Any = None,
    session_events_repo: Any = None,
    session_snapshots_repo: Any = None,
    interaction_snapshots_repo: Any = None,
    agent_repo: Any = None,
    runtime_subjects: Any = None,
    assistant_workspace_service: Any = None,
) -> SessionLifecycleWorker:
    """A real ``SessionLifecycleWorker`` with every collaborator faked."""
    return SessionLifecycleWorker(
        worker_id="worker-test",
        sessions_repo=sessions_repo if sessions_repo is not None else AsyncMock(),
        session_service=session_service if session_service is not None else AsyncMock(),
        turn_service=turn_service if turn_service is not None else AsyncMock(),
        runtime_manager=runtime_manager if runtime_manager is not None else AsyncMock(),
        session_events_repo=(
            session_events_repo if session_events_repo is not None else _FakeJournalRepo()
        ),
        session_snapshots_repo=(
            session_snapshots_repo if session_snapshots_repo is not None else AsyncMock()
        ),
        interaction_snapshots_repo=(
            interaction_snapshots_repo
            if interaction_snapshots_repo is not None
            else AsyncMock()
        ),
        agent_repo=agent_repo if agent_repo is not None else AsyncMock(),
        runtime_subjects=(runtime_subjects if runtime_subjects is not None else AsyncMock()),
        assistant_workspace_service=(
            assistant_workspace_service if assistant_workspace_service is not None else AsyncMock()
        ),
    )


# ── run_once: command-type dispatch tree ─────────────────────────────────────


class RunOnceDispatchRoutingTests(unittest.IsolatedAsyncioTestCase):
    """CreateSession/StartSessionStartup never touch the generic
    session-load + ``_execute_command`` path; SetPermissionMode loads the
    session but is still routed around ``_execute_command``."""

    async def test_create_session_bypasses_session_load_and_execute_command(self) -> None:
        sessions_repo = AsyncMock()
        journal = _FakeJournalRepo(
            command_event={
                "causation_id": "cause-1",
                "correlation_id": "corr-1",
                "payload": {"command_type": "CreateSession", "template_name": "default"},
            }
        )
        worker = _make_worker(sessions_repo=sessions_repo, session_events_repo=journal)
        sentinel = WorkerOutcome(session_id="s-1", channel="lifecycle", status="idle")
        worker._run_create_session_command = AsyncMock(return_value=sentinel)
        worker._execute_command = AsyncMock(
            side_effect=AssertionError("_execute_command must not run for CreateSession")
        )
        wakeup = WorkerWakeup(session_id="s-1", channel="lifecycle", command_id="cmd-1")

        result = await worker.run_once(wakeup)

        self.assertIs(result, sentinel)
        worker._run_create_session_command.assert_awaited_once()
        sessions_repo.get_session.assert_not_awaited()

    async def test_start_session_startup_bypasses_session_load_and_execute_command(self) -> None:
        sessions_repo = AsyncMock()
        journal = _FakeJournalRepo(
            command_event={
                "causation_id": "cause-1",
                "correlation_id": "corr-1",
                "payload": {"command_type": "StartSessionStartup"},
            }
        )
        worker = _make_worker(sessions_repo=sessions_repo, session_events_repo=journal)
        sentinel = WorkerOutcome(session_id="s-1", channel="lifecycle", status="idle")
        worker._run_startup_command = AsyncMock(return_value=sentinel)
        worker._execute_command = AsyncMock(
            side_effect=AssertionError("_execute_command must not run for StartSessionStartup")
        )
        wakeup = WorkerWakeup(session_id="s-1", channel="lifecycle", command_id="cmd-1")

        result = await worker.run_once(wakeup)

        self.assertIs(result, sentinel)
        worker._run_startup_command.assert_awaited_once()
        sessions_repo.get_session.assert_not_awaited()

    async def test_set_permission_mode_loads_session_but_bypasses_execute_command(self) -> None:
        session = {"session_id": "s-1", "permission_mode": "default"}
        sessions_repo = AsyncMock()
        sessions_repo.get_session = AsyncMock(return_value=session)
        journal = _FakeJournalRepo(
            command_event={
                "causation_id": "cause-1",
                "event_seq": 5,
                "payload": {"command_type": "SetPermissionMode", "permission_mode": "plan"},
            }
        )
        worker = _make_worker(sessions_repo=sessions_repo, session_events_repo=journal)
        worker._execute_command = AsyncMock(
            side_effect=AssertionError("_execute_command must not run for SetPermissionMode")
        )
        sentinel_result = PermissionLifecycleResult(
            session_id="s-1",
            permission_mode="plan",
            previous_permission_mode="default",
            runtime_applied=True,
            changed=True,
            event_seq=9,
        )
        worker._permission_lifecycle.apply_explicit_update = AsyncMock(
            return_value=sentinel_result
        )
        wakeup = WorkerWakeup(session_id="s-1", channel="lifecycle", command_id="cmd-1")

        result = await worker.run_once(wakeup)

        sessions_repo.get_session.assert_awaited_once_with("s-1")
        worker._permission_lifecycle.apply_explicit_update.assert_awaited_once_with(
            session_id="s-1", session=session, command_id="cause-1", requested_mode="plan"
        )
        self.assertEqual(result.metadata["command_type"], "SetPermissionMode")
        self.assertEqual(result.metadata["result"], sentinel_result.to_dict())
        # int(result.event_seq or command_event["event_seq"] or 0) -> result wins.
        self.assertEqual(result.processed_event_seq, 9)


class RunOnceMissingCommandEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_command_accepted_event_raises_runtime_error(self) -> None:
        journal = _FakeJournalRepo(command_event=None)
        worker = _make_worker(session_events_repo=journal)
        wakeup = WorkerWakeup(session_id="s-1", channel="lifecycle", command_id="cmd-missing")

        with self.assertRaises(RuntimeError) as ctx:
            await worker.run_once(wakeup)

        self.assertIn("missing command.accepted event", str(ctx.exception))
        self.assertIn("cmd-missing", str(ctx.exception))


# ── run_once: generic success/failure event + snapshot wrapping ─────────────


class RunOnceSuccessFailureWrapTests(unittest.IsolatedAsyncioTestCase):
    """The wrapping every non-Create/Startup/SetPermissionMode command gets
    from ``_execute_command``'s result."""

    async def test_success_synthesizes_deleted_projection_when_post_delete_read_is_none(
        self,
    ) -> None:
        session_before = {
            "session_id": "s-1",
            "state": SessionState.READY.value,
            "permission_mode": "default",
        }
        sessions_repo = AsyncMock()
        # Real DeleteSession behavior: the post-command re-read returns None
        # (deleted sessions are filtered from the primary get_session read).
        sessions_repo.get_session = AsyncMock(side_effect=[session_before, None])
        journal = _FakeJournalRepo(
            command_event={
                "causation_id": "cause-1",
                "correlation_id": "corr-1",
                "payload": {"command_type": "DeleteSession"},
            }
        )
        worker = _make_worker(sessions_repo=sessions_repo, session_events_repo=journal)
        worker._execute_command = AsyncMock(
            return_value={"session_id": "s-1", "deleted": True}
        )
        worker._project_snapshot = AsyncMock()
        wakeup = WorkerWakeup(session_id="s-1", channel="lifecycle", command_id="cmd-1")

        outcome = await worker.run_once(wakeup)

        self.assertEqual(journal.appended[-1]["event_type"], "session.deleted")
        self.assertEqual(
            journal.appended[-1]["payload"],
            {"command_type": "DeleteSession", "session_id": "s-1", "deleted": True},
        )
        # A None post-delete read must NOT project the stale pre-delete session
        # (which would still read ACTIVE/CONNECTED) -- it synthesizes DELETED.
        worker._project_snapshot.assert_awaited_once_with(
            session_id="s-1",
            event_seq=1,
            session={
                **session_before,
                "deleted": True,
                "state": SessionState.DELETED.value,
                "runtime_unavailable": False,
                "last_error": None,
            },
            fallback_permission_mode="default",
        )
        self.assertEqual(outcome.processed_event_seq, 1)
        self.assertIs(outcome.metadata["result"], worker._execute_command.return_value)

    async def test_execute_command_failure_projects_latest_session_and_reraises_original(
        self,
    ) -> None:
        session_before = {
            "session_id": "s-1",
            "state": SessionState.READY.value,
            "permission_mode": "default",
        }
        session_after_failure = {**session_before, "last_error": "boom"}
        sessions_repo = AsyncMock()
        sessions_repo.get_session = AsyncMock(
            side_effect=[session_before, session_after_failure]
        )
        journal = _FakeJournalRepo(
            command_event={
                "causation_id": "cause-1",
                "correlation_id": "corr-1",
                "payload": {"command_type": "ArchiveSession"},
            }
        )
        worker = _make_worker(sessions_repo=sessions_repo, session_events_repo=journal)
        boom = RuntimeError("kaboom")
        worker._execute_command = AsyncMock(side_effect=boom)
        worker._project_snapshot = AsyncMock()
        wakeup = WorkerWakeup(session_id="s-1", channel="lifecycle", command_id="cmd-1")

        with self.assertRaises(RuntimeError) as ctx:
            await worker.run_once(wakeup)

        # The ORIGINAL exception propagates unchanged -- never swallowed/wrapped.
        self.assertIs(ctx.exception, boom)
        self.assertEqual(journal.appended[-1]["event_type"], "session.archive_failed")
        self.assertEqual(journal.appended[-1]["payload"]["error_text"], "kaboom")
        # The failure projection re-reads the session and, when that re-read is
        # truthy, uses THAT latest state rather than the pre-command snapshot.
        worker._project_snapshot.assert_awaited_once_with(
            session_id="s-1",
            event_seq=1,
            session=session_after_failure,
            fallback_permission_mode="default",
        )


class ExecuteCommandSwitchTests(unittest.IsolatedAsyncioTestCase):
    """The pure switch inside ``_execute_command``, called directly."""

    async def test_set_permission_mode_is_rejected_as_a_misroute_guard(self) -> None:
        worker = _make_worker()

        with self.assertRaises(RuntimeError) as ctx:
            await worker._execute_command(
                user=UserContext(user_id="u1"),
                session={"session_id": "s-1"},
                session_id="s-1",
                command_type="SetPermissionMode",
                payload={},
                command_event={},
            )

        self.assertIn("PermissionLifecycle", str(ctx.exception))

    async def test_unsupported_command_type_raises(self) -> None:
        worker = _make_worker()

        with self.assertRaises(RuntimeError) as ctx:
            await worker._execute_command(
                user=UserContext(user_id="u1"),
                session={"session_id": "s-1"},
                session_id="s-1",
                command_type="NotARealCommand",
                payload={},
                command_event={},
            )

        self.assertIn("unsupported lifecycle command type: NotARealCommand", str(ctx.exception))


# ── CreateSession command + per-channel snapshot seeding ─────────────────────


class CreateSessionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_record_failure_appends_create_failed_only_and_reraises(self) -> None:
        session_service = AsyncMock()
        boom = RuntimeError("template rejected")
        session_service.create_session_record = AsyncMock(side_effect=boom)
        journal = _FakeJournalRepo()
        worker = _make_worker(session_service=session_service, session_events_repo=journal)
        worker._project_snapshot = AsyncMock()
        worker._initialize_create_snapshots = AsyncMock()
        wakeup = WorkerWakeup(session_id="s-1", channel="lifecycle", command_id="cmd-1")
        command_event = {"causation_id": "cause-1", "correlation_id": "corr-1"}
        payload = {
            "command_type": "CreateSession",
            "template_name": "default",
            "permission_mode": "plan",
            "session_kind": "agent_chat",
            "sandbox_generation": "generation-1",
        }

        with self.assertRaises(RuntimeError) as ctx:
            await worker._run_create_session_command(
                wakeup=wakeup, command_event=command_event, payload=payload
            )

        self.assertIs(ctx.exception, boom)
        self.assertEqual(len(journal.appended), 1)
        self.assertEqual(journal.appended[0]["event_type"], "session.create_failed")
        self.assertEqual(journal.appended[0]["payload"]["error_text"], "template rejected")
        worker._project_snapshot.assert_not_awaited()
        worker._initialize_create_snapshots.assert_not_awaited()

    async def test_create_success_seeds_channel_snapshots_and_synthesizes_startup_command(
        self,
    ) -> None:
        created_session = {
            "session_id": "s-1",
            "template_name": "default",
            "permission_mode": "plan",
            "session_kind": "agent_chat",
            "sandbox_generation": "generation-1",
        }
        session_service = AsyncMock()
        session_service.create_session_record = AsyncMock(return_value=created_session)
        journal = _FakeJournalRepo()
        worker = _make_worker(session_service=session_service, session_events_repo=journal)
        worker._project_snapshot = AsyncMock()
        worker._initialize_create_snapshots = AsyncMock()
        wakeup = WorkerWakeup(session_id="s-1", channel="lifecycle", command_id="cmd-1")
        command_event = {"causation_id": "cause-1", "correlation_id": "corr-1"}
        payload = {
            "command_type": "CreateSession",
            "template_name": "default",
            "permission_mode": "plan",
            "session_kind": "agent_chat",
            "sandbox_generation": "generation-1",
        }

        outcome = await worker._run_create_session_command(
            wakeup=wakeup, command_event=command_event, payload=payload
        )

        event_types = [e["event_type"] for e in journal.appended]
        self.assertEqual(event_types, ["session.created", "command.accepted"])
        startup_doc = journal.appended[1]
        self.assertEqual(startup_doc["channel"], "command")
        self.assertEqual(startup_doc["payload"]["command_type"], "StartSessionStartup")
        self.assertEqual(startup_doc["causation_id"], outcome.metadata["startup_command_id"])
        # Fresh synthesized command id -- not a copy of the inbound command's causation.
        self.assertNotEqual(outcome.metadata["startup_command_id"], "cause-1")
        uuid.UUID(outcome.metadata["startup_command_id"])  # well-formed uuid4 string
        worker._initialize_create_snapshots.assert_awaited_once_with(
            session_id="s-1", event_seq=1
        )
        self.assertEqual(worker._project_snapshot.await_count, 2)
        self.assertEqual(outcome.metadata["command_type"], "CreateSession")
        self.assertEqual(outcome.metadata["result"], created_session)
        self.assertEqual(outcome.processed_event_seq, 2)


# ── startup god-method entry guard + assistant-profile-reuse fast path ───────


class RunStartupCommandGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_session_raises_runtime_error(self) -> None:
        sessions_repo = AsyncMock()
        sessions_repo.get_session = AsyncMock(return_value=None)
        worker = _make_worker(sessions_repo=sessions_repo)
        wakeup = WorkerWakeup(session_id="s-missing", channel="lifecycle", command_id="cmd-1")
        command_event = {"causation_id": "cause-1", "correlation_id": "corr-1", "event_seq": 3}
        payload = {"command_type": "StartSessionStartup"}

        with self.assertRaises(RuntimeError) as ctx:
            await worker._run_startup_command(
                wakeup=wakeup, command_event=command_event, payload=payload
            )

        self.assertIn("missing session s-missing", str(ctx.exception))

    async def test_startup_command_id_is_the_sandbox_create_assignment(self) -> None:
        session = {
            "session_id": "s-1",
            "sandbox_generation": "generation-1",
            "user_id": "user-1",
            "state": SessionState.CREATING.value,
            "template_name": "default",
        }
        sessions_repo = AsyncMock()
        sessions_repo.get_session.return_value = session
        session_service = Mock()
        template = SimpleNamespace()
        session_service._agent_config.resolve_session_harness = AsyncMock(
            return_value=template
        )
        worker = _make_worker(
            sessions_repo=sessions_repo,
            session_service=session_service,
            session_events_repo=_FakeJournalRepo(),
        )
        worker._run_session_startup_direct = AsyncMock(
            return_value={"status": "ready", "session": session}
        )

        await worker._run_startup_command(
            wakeup=WorkerWakeup(
                session_id="s-1",
                channel="lifecycle",
                command_id="startup-command-1",
            ),
            command_event={
                "causation_id": "startup-command-1",
                "correlation_id": "create-command-1",
                "event_seq": 7,
            },
            payload={
                "command_type": "StartSessionStartup",
                "sandbox_generation": "generation-1",
                "author_user_id": "user-1",
                "template_name": "default",
            },
        )

        kwargs = worker._run_session_startup_direct.await_args.kwargs
        self.assertEqual(kwargs["assignment_id"], "startup-command-1")
        self.assertIs(kwargs["template"], template)

    async def test_startup_metadata_carries_the_verified_engine_manifest(self) -> None:
        manifest = EngineCapabilityManifest(
            engine_kind="test-engine",
            tools=["vendor-tool"],
            extra={"vendor_extension": {"level": 7}},
        )
        engine_client = SimpleNamespace(
            get_server_info=AsyncMock(return_value={"commands": []})
        )
        runtime = SimpleNamespace(
            engine_kind="test-engine",
            engine_client=engine_client,
            engine_manifest=manifest,
            conversation_bound=True,
        )
        runtime_manager = Mock()
        runtime_manager.get_runtime.return_value = runtime
        worker = _make_worker(runtime_manager=runtime_manager)

        metadata = await worker._fetch_runtime_initialization_metadata(
            "s-1", runtime
        )

        self.assertEqual(
            metadata["engine_capabilities"],
            {
                "engine_kind": "test-engine",
                "tools": ["vendor-tool"],
                "input_content_types": ["text"],
                "permission_modes": [],
                "supports_interaction": False,
                "supports_child_run_control": False,
                "supports_server_info": False,
                "extra": {"vendor_extension": {"level": 7}},
            },
        )
        engine_client.get_server_info.assert_not_awaited()

    async def test_startup_metadata_reads_declared_server_info(self) -> None:
        manifest = EngineCapabilityManifest(
            engine_kind="test-engine",
            supports_server_info=True,
        )
        engine_client = SimpleNamespace(
            get_server_info=AsyncMock(
                return_value={
                    "subtype": "init",
                    "commands": [
                        {
                            "name": "/inspect",
                            "description": "Inspect the current workspace",
                        }
                    ]
                }
            )
        )
        runtime = SimpleNamespace(
            engine_kind="test-engine",
            engine_client=engine_client,
            engine_manifest=manifest,
            conversation_bound=True,
        )
        worker = _make_worker(runtime_manager=Mock())

        metadata = await worker._fetch_runtime_initialization_metadata(
            "s-1", runtime
        )

        engine_client.get_server_info.assert_awaited_once_with()
        self.assertEqual(metadata["slash_commands"], ["inspect"])

    async def test_started_runtime_without_a_manifest_fails_readiness(self) -> None:
        worker = _make_worker(runtime_manager=Mock())

        with self.assertRaises(APIError) as raised:
            await worker._fetch_runtime_initialization_metadata("s-1", None)

        self.assertEqual(raised.exception.code, "ENGINE_CAPABILITY_CONTRACT_VIOLATION")

    async def test_startup_persists_the_effective_conversation_binding(self) -> None:
        for runtime_key, expected_key in (
            ("native-new", "native-new"),
            (None, "native-old"),
        ):
            with self.subTest(runtime_key=runtime_key):
                session = {
                    "session_id": "s-1",
                    "session_kind": "agent_chat",
                    "sandbox_generation": "generation-1",
                    "state": SessionState.CREATING.value,
                }
                sessions_repo = AsyncMock()
                sessions_repo.get_session.return_value = session
                runtime = SimpleNamespace(
                    sandbox_id="sandbox-new",
                    engine_session_key=runtime_key,
                    terminal_cwd="/workspace",
                    runtime_identity=None,
                )
                runtime_manager = Mock()
                runtime_manager.ensure_runtime = AsyncMock(return_value=runtime)
                runtime_manager.get_sandbox_expires_at = AsyncMock(return_value=None)
                runtime_manager.resolve_template_model_name.return_value = "model"
                runtime_subjects = AsyncMock()
                runtime_subjects.acquire_startup.return_value = RuntimeStartupTarget(
                    action="attach_runtime",
                    session=session,
                    workspace_plan=SimpleNamespace(
                        sandbox_id="sandbox-new",
                        session_kind="agent_chat",
                    ),
                )
                worker = _make_worker(
                    sessions_repo=sessions_repo,
                    session_service=SimpleNamespace(_ttl_seconds=600),
                    runtime_manager=runtime_manager,
                    runtime_subjects=runtime_subjects,
                )
                worker._fetch_runtime_initialization_metadata = AsyncMock(
                    return_value={}
                )
                worker._update_session_with_settle_retry = AsyncMock(
                    side_effect=lambda *, updates, **_kwargs: {**session, **updates}
                )

                result = await worker._run_session_startup_direct(
                    session_id="s-1",
                    assignment_id="assignment-1",
                    template=SimpleNamespace(),
                    user_id="user-1",
                    permission_mode=None,
                    resume_session_id="native-old",
                    on_progress=None,
                    on_ready=None,
                    on_failed=None,
                )

                self.assertEqual(result["status"], "ready")
                updates = worker._update_session_with_settle_retry.await_args.kwargs[
                    "updates"
                ]
                self.assertEqual(updates["engine_session_key"], expected_key)

    async def test_create_runtime_receives_the_durable_assignment(self) -> None:
        session = {
            "session_id": "s-1",
            "session_kind": "agent_chat",
            "sandbox_generation": "generation-1",
            "state": SessionState.CREATING.value,
            "sandbox_callback_token": "callback-token-1",
        }
        sessions_repo = AsyncMock()
        sessions_repo.get_session.return_value = session
        runtime = SimpleNamespace(
            sandbox_id="sandbox-new",
            engine_session_key=None,
            terminal_cwd="/workspace",
            runtime_identity=None,
        )
        runtime_manager = Mock()
        runtime_manager.create_runtime = AsyncMock(return_value=runtime)
        runtime_manager.get_sandbox_expires_at = AsyncMock(return_value=None)
        runtime_manager.resolve_template_model_name.return_value = "model"
        runtime_subjects = AsyncMock()
        runtime_subjects.acquire_startup.return_value = RuntimeStartupTarget(
            action="create_runtime",
            session=session,
            workspace_plan=SimpleNamespace(
                sandbox_id=None,
                session_kind="agent_chat",
                subject_kind="deployment_conversation",
                cwd="/workspace",
            ),
        )
        worker = _make_worker(
            sessions_repo=sessions_repo,
            session_service=SimpleNamespace(_ttl_seconds=600),
            runtime_manager=runtime_manager,
            runtime_subjects=runtime_subjects,
        )
        worker._fetch_runtime_initialization_metadata = AsyncMock(
            return_value={}
        )
        worker._update_session_with_settle_retry = AsyncMock(
            side_effect=lambda *, updates, **_kwargs: {**session, **updates}
        )

        with patch(
            "astrabox.core.service.orchestrator.session_kernel.workers."
            "lifecycle.startup.build_sandbox_callback_url_from_record",
            return_value="https://astrabox.test/callback",
        ):
            result = await worker._run_session_startup_direct(
                session_id="s-1",
                assignment_id="startup-command-1",
                template=SimpleNamespace(),
                user_id="user-1",
                permission_mode=None,
                resume_session_id=None,
                on_progress=None,
                on_ready=None,
                on_failed=None,
            )

        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(
            runtime_manager.create_runtime.await_args.kwargs["assignment_id"],
            "startup-command-1",
        )


class StartupAbortOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_aborted_attach_does_not_destroy_the_shared_owner_runtime(self) -> None:
        session = {
            "session_id": "s-1",
            "session_kind": "assistant_chat",
            "state": SessionState.CREATING.value,
            "workspace_ref": {
                "kind": "assistant",
                "user_id": "user-1",
                "assistant_id": "assistant-1",
                "engine_kind": "assistant",
            },
        }
        terminated = {**session, "state": SessionState.TERMINATED.value}
        sessions_repo = AsyncMock()
        sessions_repo.get_session = AsyncMock(
            side_effect=[terminated, terminated, terminated]
        )
        runtime_manager = AsyncMock()
        runtime_manager.ensure_runtime.return_value = SimpleNamespace(
            sandbox_id="shared-sandbox"
        )
        runtime_subjects = AsyncMock()
        runtime_subjects.acquire_startup.return_value = RuntimeStartupTarget(
            action="attach_runtime",
            session=session,
            workspace_plan=SimpleNamespace(
                sandbox_id="shared-sandbox",
                session_kind="assistant_chat",
            ),
        )
        runtime_subjects.cleanup_failed_startup_runtime.return_value = (
            RuntimeStartupCleanup(destruction=None, leaked_sandbox_id=None)
        )
        worker = _make_worker(
            sessions_repo=sessions_repo,
            runtime_manager=runtime_manager,
            runtime_subjects=runtime_subjects,
        )

        result = await worker._run_session_startup_direct(
            session_id="s-1",
            assignment_id="assignment-1",
            template=SimpleNamespace(),
            user_id="user-1",
            permission_mode=None,
            resume_session_id=None,
            on_progress=None,
            on_ready=None,
            on_failed=None,
        )

        self.assertEqual(result["status"], "aborted")
        runtime_subjects.cleanup_failed_startup_runtime.assert_awaited_once_with(
            session_id="s-1",
            target=runtime_subjects.acquire_startup.return_value,
            sandbox_id="shared-sandbox",
        )
        runtime_manager.terminate_runtime.assert_not_awaited()


# ── sandbox reclaim: parked-turn settle ──────────────────────────────────────


class RecoverRuntimeSubjectOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_subject_recovery_never_terminates_the_owner_sandbox(self) -> None:
        session = {
            "session_id": "assistant-session",
            "session_kind": "assistant_chat",
            "state": SessionState.READY.value,
            "runtime_unavailable": True,
            "sandbox_id": "shared-sandbox",
            "permission_mode": "default",
            "engine_session_key": "engine-session",
            "workspace_ref": {
                "kind": "assistant",
                "user_id": "user-1",
                "assistant_id": "assistant-1",
                "engine_kind": "assistant",
            },
        }
        sessions_repo = AsyncMock()
        sessions_repo.get_session.return_value = {
            **session,
            "state": SessionState.CREATING.value,
            "_runtime_recovery_owner": "recover-1",
        }
        session_service = Mock()
        session_service._sanitize_session.return_value = {
            **session,
            "state": SessionState.RECOVERY_REQUIRED.value,
            "recovery_policy": "auto",
            "recovery_reason": "runtime_detached",
        }
        session_service._agent_config.resolve_session_harness = AsyncMock(
            return_value=SimpleNamespace()
        )
        session_service._is_session_expired.return_value = False
        session_snapshots_repo = AsyncMock()
        session_snapshots_repo.get_snapshot.return_value = {
            "session_lifecycle_state": "ACTIVE",
            "conversation_state": "FAILED",
            "terminal_state": "FAILED",
        }
        interaction_snapshots_repo = AsyncMock()
        interaction_snapshots_repo.get_active_interaction.return_value = None
        journal = _FakeJournalRepo()
        runtime_manager = AsyncMock()
        runtime_manager.get_runtime = Mock(return_value=None)
        runtime_subjects = Mock()
        runtime_subjects.recovery_action_for.return_value = "restart_session_on_subject"
        worker = _make_worker(
            sessions_repo=sessions_repo,
            session_service=session_service,
            runtime_manager=runtime_manager,
            runtime_subjects=runtime_subjects,
            session_events_repo=journal,
            session_snapshots_repo=session_snapshots_repo,
            interaction_snapshots_repo=interaction_snapshots_repo,
        )

        result = await worker._recover_session_direct(
            user=UserContext(user_id="user-1"),
            session=session,
            session_id="assistant-session",
            command_event={"event_seq": 4, "correlation_id": "corr-1", "causation_id": "recover-1"},
        )

        self.assertEqual(result["status"], "startup-requested")
        runtime_subjects.recovery_action_for.assert_called_once()
        recovery_session = runtime_subjects.recovery_action_for.call_args.args[0]
        self.assertEqual(recovery_session["session_kind"], "assistant_chat")
        self.assertEqual(recovery_session["workspace_ref"], session["workspace_ref"])
        runtime_manager.ensure_runtime.assert_not_awaited()
        runtime_manager.terminate_runtime.assert_not_awaited()
        creating_update = next(
            call.kwargs["updates"]
            for call in sessions_repo.compare_and_update_session.await_args_list
            if call.kwargs["updates"].get("startup_progress") == "creating_sandbox"
        )
        self.assertNotIn("sandbox_id", creating_update)
        self.assertNotIn("expires_at", creating_update)
        self.assertEqual(journal.appended[-1]["payload"]["command_type"], "StartSessionStartup")


class SettleParkedTurnOnReclaimTests(unittest.IsolatedAsyncioTestCase):
    """A reclaim that lands while a turn is parked on an approval settles the
    turn (the approval wait died with the box — nothing can answer it), and
    leaves non-parked sessions untouched."""

    async def test_waiting_snapshot_is_settled_and_interaction_deactivated(self) -> None:
        journal = _FakeJournalRepo()
        snapshots = AsyncMock()
        snapshots.get_snapshot.return_value = {
            "conversation_state": "WAITING_FOR_INTERACTION",
            "current_turn_id": "turn-1",
        }
        snapshots.apply_channel_update.return_value = {"conversation_state": "IDLE"}
        interactions = AsyncMock()
        interactions.deactivate_active_for_turn.return_value = 1
        frames = AsyncMock()
        # The held Write's card is OPEN on the frame stream (input, no output).
        frames.list_frames.return_value = [
            {"payload": {"type": "tool-input-available", "toolCallId": "call-1"}},
        ]
        frames.get_next_session_frame_seq.return_value = 9
        journal.list_frames = frames.list_frames
        journal.get_next_session_frame_seq = frames.get_next_session_frame_seq
        journal.append_frame = frames.append_frame
        worker = _make_worker(
            session_events_repo=journal,
            session_snapshots_repo=snapshots,
            interaction_snapshots_repo=interactions,
        )

        await worker._settle_parked_turn_on_reclaim("s-1")

        settle_events = [
            doc for doc in journal.appended if doc.get("event_type") == "turn.failed"
        ]
        self.assertEqual(len(settle_events), 1)
        self.assertEqual(settle_events[0]["turn_id"], "turn-1")
        self.assertIn("reclaimed", settle_events[0]["payload"]["error_text"])
        kwargs = snapshots.apply_channel_update.await_args.kwargs
        self.assertEqual(kwargs["expected_conversation_state"], "WAITING_FOR_INTERACTION")
        self.assertEqual(kwargs["updates"]["last_turn_status"], "FAILED")
        interactions.deactivate_active_for_turn.assert_awaited_once_with("s-1", "turn-1")
        # The settle CLOSES the open tool card: without a tool-output-error
        # frame carrying its call id, no later frame clears it and the tool
        # renders as spinning forever.
        appended = frames.append_frame.await_args.args[0]
        self.assertEqual(appended["payload"]["type"], "tool-output-error")
        self.assertEqual(appended["payload"]["toolCallId"], "call-1")
        self.assertIn("reclaimed", appended["payload"]["errorText"])

    async def test_non_waiting_snapshot_is_left_alone(self) -> None:
        journal = _FakeJournalRepo()
        snapshots = AsyncMock()
        snapshots.get_snapshot.return_value = {
            "conversation_state": "IDLE",
            "current_turn_id": "turn-1",
        }
        worker = _make_worker(
            session_events_repo=journal,
            session_snapshots_repo=snapshots,
        )

        await worker._settle_parked_turn_on_reclaim("s-1")

        self.assertEqual(journal.appended, [])
        snapshots.apply_channel_update.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


# ── terminate: the fifth destruction outcome ─────────────────────────────────


class TerminateRetainsTheAgentsBoxTests(unittest.IsolatedAsyncioTestCase):
    """Closing a shared conversation must not read as a failed termination.

    A conversation of an Agent lives in an isolated session inside a box its
    Agent owns, so ending it releases the session and deliberately leaves the
    box running for the siblings still working in it. The seam calls that
    RETAINED and documents it as a success. Gating on `confirmed` alone turned
    the only correct outcome for every shared conversation into a 502 whose
    message explained, in words, why it was fine.
    """

    async def _terminate(self, destruction: SandboxDestruction) -> dict[str, Any]:
        runtime_manager = SimpleNamespace(
            get_runtime=Mock(return_value=None),
            terminate_runtime=AsyncMock(return_value=destruction),
        )
        sessions_repo = AsyncMock()
        sessions_repo.update_session = AsyncMock(return_value=True)
        snapshots = AsyncMock()
        snapshots.get_snapshot.return_value = {
            "conversation_state": "IDLE",
            "current_turn_id": None,
        }
        worker = _make_worker(
            runtime_manager=runtime_manager,
            sessions_repo=sessions_repo,
            session_snapshots_repo=snapshots,
        )
        worker._assert_conversation_safe_to_take_offline = AsyncMock()  # type: ignore[method-assign]
        worker._is_assistant_user_conversation = lambda _session: False  # type: ignore[method-assign]
        return await worker._terminate_session_direct(
            session={
                "session_id": "s-1",
                "state": SessionState.READY.value,
                "sandbox_id": "box-1",
                "session_kind": "agent_chat",
                "sandbox_generation": "generation-1",
            },
            session_id="s-1",
        )

    async def test_a_retained_box_is_a_successful_termination(self) -> None:
        result = await self._terminate(
            SandboxDestruction(
                outcome=SANDBOX_DESTRUCTION_RETAINED,
                sandbox_id="box-1",
                detail=(
                    "session s-1 ran in isolated session iso-1 of box box-1; the "
                    "session was closed and the agent-owned box remains durably named"
                ),
            )
        )
        assert result["status"] == "sandbox-reclaimed"
        # Not killed: the box is still serving this Agent's other conversations.
        assert result["killed"] is False

    async def test_an_unconfirmed_destruction_still_refuses(self) -> None:
        """The control. Without it the change above would read as "accept anything".

        UNCONFIRMED means nobody established the box died, so the row must keep
        naming it and the caller must be told.
        """
        with pytest.raises(APIError) as caught:
            await self._terminate(
                SandboxDestruction(
                    outcome=SANDBOX_DESTRUCTION_UNCONFIRMED,
                    sandbox_id="box-1",
                    detail="the delete request never reached the control plane",
                )
            )
        assert caught.value.status_code == 502
        assert "UNCONFIRMED" in caught.value.message
