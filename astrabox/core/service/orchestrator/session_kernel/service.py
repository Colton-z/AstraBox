"""Facade for the session-kernel orchestration service.

:class:`SessionKernelService` is composed entirely from the mixins in
:mod:`astrabox.core.service.orchestrator.session_kernel.service_mixins`; this
module holds only the public class, its mixin composition (MRO leaf-first), and
the single ``__init__`` that wires the ~40 collaborators plus the
``TurnCoordinator`` that every mixin references through ``self``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.runtime_binding import (
    reconcile_session_runtime_binding,
)
from astrabox.core.service.orchestrator.session_child_run_view import (
    SessionChildRunView,
)
from astrabox.core.service.orchestrator.session_kernel.workers import (
    TurnCoordinator,
)

from astrabox.core.service.orchestrator.session_kernel.service_mixins.bootstrap import (
    BootstrapWiringMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.background_continuation import (
    BackgroundContinuationMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.child_runs import (
    ChildRunProjectionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_read import (
    SessionReadRenderingMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.lifecycle import (
    LifecycleCommandsMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.terminal import (
    TerminalExecutionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_checkpoint import (
    DurableRecoveryMaterializationMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.reconciliation import (
    ReconciliationOrchestrationMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)


class SessionKernelService(
    BootstrapWiringMixin,
    ChildRunProjectionMixin,
    BackgroundContinuationMixin,
    SessionReadRenderingMixin,
    LifecycleCommandsMixin,
    TerminalExecutionMixin,
    DurableRecoveryMaterializationMixin,
    ReconciliationOrchestrationMixin,
    TurnDispatchStreamingMixin,
):
    def __init__(
        self,
        *,
        sessions_repo: Any,
        turn_service: Any,
        session_service: Any,
        terminal_service: Any,
        runtime_manager: Any,
        broker: Any,
        session_events_repo: Any,
        session_snapshots_repo: Any,
        interaction_snapshots_repo: Any,
        message_view: Any,
        artifacts_repo: Any,
        transcript_entries_repo: Any,
        agent_repo: Any,
        must_get_owned_session: Callable[[UserContext, str], Awaitable[dict[str, Any]]],
        spawn_background_task: Callable[..., asyncio.Task],
        runtime_subjects: Any,
        title_service: Any | None = None,
        assistant_workspace_service: Any | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._turn_service = turn_service
        self._session_service = session_service
        self._terminal_service = terminal_service
        self._runtime_manager = runtime_manager
        self._broker = broker
        self._session_events_repo = session_events_repo
        self._session_snapshots_repo = session_snapshots_repo
        self._interaction_snapshots_repo = interaction_snapshots_repo
        self._message_view = message_view
        self._child_run_view = SessionChildRunView(
            session_events_repo, transcript_entries_repo=transcript_entries_repo
        )
        self._artifacts_repo = artifacts_repo
        self._transcript_entries_repo = transcript_entries_repo
        self._agent_repo = agent_repo
        self._assistant_workspace_service = assistant_workspace_service
        self._runtime_subjects = runtime_subjects
        self._session_title_service = title_service
        self._must_get_owned_session = must_get_owned_session
        self._raw_spawn_background_task = spawn_background_task
        self._spawn_background_task = self._spawn_tracked_background_task
        self._active_background_tasks: set[asyncio.Task] = set()
        self._poll_interval_s = 0.5
        self._worker_id = f"session-kernel:{uuid.uuid4()}"
        self._bridge_event_stall_timeout_s = 90.0
        self._turn_worker_heartbeat_interval_s = 5.0
        self._turn_live_frame_retry_window_s = 30.0
        self._turn_live_frame_retry_delay_s = 0.5
        self._turn_requested_projection_retry_window_s = 30.0
        self._turn_requested_projection_retry_delay_s = 0.5
        self._turn_terminal_settle_retry_window_s = 30.0
        self._turn_terminal_settle_retry_delay_s = 0.5
        self._recovery_tasks: dict[str, asyncio.Task] = {}
        #: Turn workers spawned by an accepted dispatch, keyed by command id.
        #: A caller that dispatched and then follows the same command's frames
        #: uses this to notice the producer dying; an ack-only caller never
        #: looks, and the entry removes itself when the task ends.
        self._accepted_turn_producers: dict[str, asyncio.Task] = {}
        self._quiesced_reason: str | None = None
        self._turn_coordinator = TurnCoordinator(
            session_events_repo=self._session_events_repo,
            session_snapshots_repo=self._session_snapshots_repo,
            sessions_repo=self._sessions_repo,
            message_view=self._message_view,
            interaction_snapshots_repo=self._interaction_snapshots_repo,
            transcript_entries_repo=self._transcript_entries_repo,
            runtime_manager=self._runtime_manager,
            worker_id=self._worker_id,
        )

    async def _reconcile_runtime_binding(
        self,
        session: dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        old_state = str(session.get("state") or "").strip()
        reconciled, _ = await reconcile_session_runtime_binding(
            session=session,
            sessions_repo=self._sessions_repo,
            agent_repo=self._agent_repo,
            assistant_workspace_service=self._assistant_workspace_service,
            persist=persist,
        )
        new_state = str(reconciled.get("state") or "").strip()
        if persist and old_state == "TERMINATED" and new_state == "READY":
            session_id = str(reconciled.get("session_id") or "").strip()
            if session_id and self._session_snapshots_repo is not None:
                with contextlib.suppress(Exception):
                    await self._session_snapshots_repo.force_update_fields(
                        session_id,
                        {"session_lifecycle_state": "ACTIVE"},
                    )
        return reconciled
