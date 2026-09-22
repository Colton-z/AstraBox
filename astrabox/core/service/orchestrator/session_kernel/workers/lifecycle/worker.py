"""Worker entrypoint & command dispatch shell.

``SessionLifecycleWorker``'s single external entry point and its immediate
dispatch tree: ``__init__`` wires the repo/service bag and builds
``self._permission_lifecycle = PermissionLifecycle(...)``; ``run_once``
loads the ``command.accepted`` event, special-cases
``CreateSession``/``StartSessionStartup``/``SetPermissionMode``, funnels
everything else through ``_execute_command``, and wraps the result in
success/failure journal events + snapshot projection; ``_execute_command`` is
the switch to the "_direct" command handlers; ``_run_set_permission_mode_command``
and ``_build_user_context`` are thin adapters (permission-mode logic itself
lives entirely in ``PermissionLifecycle``, a sibling module).

Every other method the worker exposes is mixed in from this package's sibling
modules -- ``retry.py``, ``assistant_workspace.py``, ``projection.py``,
``create.py``, ``commands.py``, ``recover.py``, ``startup.py``.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
    PermissionLifecycle,
)
from astrabox.core.service.orchestrator.session_kernel.workers.base import KernelWorkerBase
from astrabox.core.service.orchestrator.session_kernel.workers.models import (
    WorkerOutcome,
    WorkerWakeup,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.retry import (
    _LifecycleRetryMixin,
    _startup_settle_retry_window_seconds,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.assistant_workspace import (
    _AssistantWorkspaceMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.projection import (
    _LifecycleProjectionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.create import (
    _CreateSessionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.commands import (
    _LifecycleCommandsMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recover import (
    _RecoverSessionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.startup import (
    _StartupOrchestrationMixin,
)


class SessionLifecycleWorker(
    _LifecycleRetryMixin,
    _AssistantWorkspaceMixin,
    _LifecycleProjectionMixin,
    _CreateSessionMixin,
    _LifecycleCommandsMixin,
    _RecoverSessionMixin,
    _StartupOrchestrationMixin,
    KernelWorkerBase,
):
    channel = "lifecycle"
    _STARTUP_SETTLE_RETRY_WINDOW_S = _startup_settle_retry_window_seconds()
    _STARTUP_SETTLE_RETRY_DELAY_S = 0.5

    def __init__(
        self,
        *,
        worker_id: str,
        sessions_repo: Any,
        session_service: Any,
        turn_service: Any,
        runtime_manager: Any,
        session_events_repo: Any,
        session_snapshots_repo: Any,
        interaction_snapshots_repo: Any,
        agent_repo: Any,
        runtime_subjects: Any,
        assistant_workspace_service: Any | None = None,
    ) -> None:
        super().__init__(worker_id=worker_id)
        self._sessions_repo = sessions_repo
        self._session_service = session_service
        self._turn_service = turn_service
        self._runtime_manager = runtime_manager
        self._session_events_repo = session_events_repo
        self._session_snapshots_repo = session_snapshots_repo
        self._interaction_snapshots_repo = interaction_snapshots_repo
        self._agent_repo = agent_repo
        self._assistant_workspace_service = assistant_workspace_service
        self._runtime_subjects = runtime_subjects
        self._permission_lifecycle = PermissionLifecycle(
            sessions_repo=sessions_repo,
            apply_engine_permission_mode=turn_service.set_engine_permission_mode,
            session_events_repo=session_events_repo,
            session_snapshots_repo=session_snapshots_repo,
            interaction_snapshots_repo=interaction_snapshots_repo,
        )

    async def run_once(
        self,
        wakeup: WorkerWakeup,
    ) -> WorkerOutcome:
        command_event = await self._session_events_repo.get_command_event(
            wakeup.session_id,
            command_id=wakeup.command_id,
        )
        if not isinstance(command_event, dict):
            raise RuntimeError(
                f"missing command.accepted event session_id={wakeup.session_id} command_id={wakeup.command_id}"
            )

        payload = command_event.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"command.accepted payload missing session_id={wakeup.session_id} command_id={wakeup.command_id}"
            )

        command_type = str(payload.get("command_type") or "").strip()
        if command_type == "CreateSession":
            return await self._run_create_session_command(
                wakeup=wakeup,
                command_event=command_event,
                payload=payload,
            )
        if command_type == "StartSessionStartup":
            return await self._run_startup_command(
                wakeup=wakeup,
                command_event=command_event,
                payload=payload,
            )

        session = await self._sessions_repo.get_session(wakeup.session_id)
        if not isinstance(session, dict):
            raise RuntimeError(f"missing session {wakeup.session_id}")

        user = self._build_user_context(session, payload)
        if command_type == "SetPermissionMode":
            return await self._run_set_permission_mode_command(
                wakeup=wakeup,
                command_event=command_event,
                session=session,
                payload=payload,
            )

        try:
            result = await self._execute_command(
                user=user,
                session=session,
                session_id=wakeup.session_id,
                command_type=command_type,
                payload=payload,
                command_event=command_event,
            )
        except Exception as exc:
            event = await self._session_events_repo.append_event(
                {
                    "session_id": wakeup.session_id,
                    "channel": self.channel,
                    "event_type": self._failure_event_type(command_type),
                    "causation_id": str(command_event.get("causation_id") or "").strip() or None,
                    "correlation_id": str(command_event.get("correlation_id") or "").strip() or None,
                    "payload": {
                        "command_type": command_type,
                        "error_text": str(exc),
                    },
                }
            )
            latest = await self._sessions_repo.get_session(wakeup.session_id)
            failure_fallback_permission_mode = (
                None
                if command_type == "SetPermissionMode"
                else str(
                    payload.get("permission_mode")
                    or session.get("permission_mode")
                    or ""
                ).strip() or None
            )
            await self._project_snapshot(
                session_id=wakeup.session_id,
                event_seq=int(event.get("event_seq") or 0),
                session=latest or session,
                fallback_permission_mode=failure_fallback_permission_mode,
            )
            raise

        event = await self._session_events_repo.append_event(
            {
                "session_id": wakeup.session_id,
                "channel": self.channel,
                "event_type": self._success_event_type(command_type),
                "causation_id": str(command_event.get("causation_id") or "").strip() or None,
                "correlation_id": str(command_event.get("correlation_id") or "").strip() or None,
                "payload": self._success_payload(command_type, result),
            }
        )
        latest = await self._sessions_repo.get_session(wakeup.session_id)
        projection_session = self._success_projection_session(
            command_type=command_type,
            latest_session=latest,
            previous_session=session,
            result=result,
        )
        await self._project_snapshot(
            session_id=wakeup.session_id,
            event_seq=int(event.get("event_seq") or 0),
            session=projection_session,
            fallback_permission_mode=self._result_permission_mode(result, payload, session),
        )
        return WorkerOutcome(
            session_id=wakeup.session_id,
            channel=self.channel,
            status="idle",
            processed_event_seq=int(event.get("event_seq") or 0),
            metadata={
                "command_type": command_type,
                "result": result if isinstance(result, dict) else {"ok": bool(result)},
            },
        )

    async def _run_set_permission_mode_command(
        self,
        *,
        wakeup: WorkerWakeup,
        command_event: dict[str, Any],
        session: dict[str, Any],
        payload: dict[str, Any],
    ) -> WorkerOutcome:
        command_id = str(command_event.get("causation_id") or "").strip() or wakeup.command_id
        result = await self._permission_lifecycle.apply_explicit_update(
            session_id=wakeup.session_id,
            session=session,
            command_id=command_id,
            requested_mode=str(payload.get("permission_mode") or ""),
        )
        return WorkerOutcome(
            session_id=wakeup.session_id,
            channel=self.channel,
            status="idle",
            processed_event_seq=int(result.event_seq or command_event.get("event_seq") or 0),
            metadata={
                "command_type": "SetPermissionMode",
                "result": result.to_dict(),
            },
        )

    @staticmethod
    def _build_user_context(
        session: dict[str, Any] | None,
        payload: dict[str, Any],
    ) -> UserContext:
        user_id = str(
            payload.get("author_user_id")
            or (session or {}).get("user_id")
            or ""
        ).strip()
        return UserContext(user_id=user_id)

    async def _execute_command(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
        command_type: str,
        payload: dict[str, Any],
        command_event: dict[str, Any],
    ) -> dict[str, Any]:
        if command_type == "SetPermissionMode":
            raise RuntimeError("SetPermissionMode must be handled by PermissionLifecycle")
        if command_type == "DeleteSession":
            return await self._delete_session_direct(
                user=user,
                session=session,
                session_id=session_id,
            )
        if command_type == "ArchiveSession":
            return await self._archive_session_direct(
                user=user,
                session=session,
                session_id=session_id,
            )
        if command_type == "EndConversation":
            return await self._end_conversation_direct(
                session=session,
                session_id=session_id,
            )
        if command_type == "TerminateSession":
            return await self._terminate_session_direct(
                session=session,
                session_id=session_id,
            )
        if command_type == "RecoverSession":
            return await self._recover_session_direct(
                user=user,
                session=session,
                session_id=session_id,
                command_event=command_event,
            )
        raise RuntimeError(f"unsupported lifecycle command type: {command_type}")
