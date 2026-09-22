"""CreateSession command + initial per-channel snapshot seeding.

Handles the ``CreateSession`` command: materializes the session row via
``session_service.create_session_record``, appends ``session.created`` /
``.create_failed``, projects the lifecycle snapshot, seeds the
conversation/terminal channel snapshots for a session with no prior history,
then synthesizes and appends the nested ``StartSessionStartup`` command that
``startup.py``'s ``_run_startup_command`` picks up on the next wakeup.
Gathered here as :class:`_CreateSessionMixin`, mixed into
``SessionLifecycleWorker``.
"""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.session_kernel.workers.models import (
    WorkerOutcome,
    WorkerWakeup,
)


class _CreateSessionMixin:
    """CreateSession command + per-channel snapshot seeding, mixed into
    :class:`SessionLifecycleWorker`."""

    async def _run_create_session_command(
        self,
        *,
        wakeup: WorkerWakeup,
        command_event: dict[str, Any],
        payload: dict[str, Any],
    ) -> WorkerOutcome:
        user = self._build_user_context(None, payload)
        template_name = str(payload.get("template_name") or "").strip()
        permission_mode = str(payload.get("permission_mode") or "").strip() or None
        session_kind = require_session_kind(payload.get("session_kind"))
        workspace_ref = payload.get("workspace_ref") if isinstance(payload.get("workspace_ref"), dict) else None
        hidden = bool(payload.get("hidden"))
        owner_type = str(payload.get("owner_type") or "").strip() or None
        owner_id = str(payload.get("owner_id") or "").strip() or None
        source_type = str(payload.get("source_type") or "").strip() or None
        agent_id = str(payload.get("agent_id") or "").strip() or None
        deployment_name = str(payload.get("deployment_name") or "").strip() or None
        title = str(payload.get("title") or "").strip() or None
        causation_id = str(command_event.get("causation_id") or "").strip() or None
        correlation_id = str(command_event.get("correlation_id") or "").strip() or None

        try:
            session = await self._session_service.create_session_record(
                user,
                template_name,
                permission_mode=permission_mode,
                session_id=wakeup.session_id,
                session_kind=session_kind,
                workspace_ref=workspace_ref,
                hidden=hidden,
                owner_type=owner_type,
                owner_id=owner_id,
                source_type=source_type,
                agent_id=agent_id,
                deployment_name=deployment_name,
                title=title,
            )
        except Exception as exc:
            await self._session_events_repo.append_event(
                {
                    "session_id": wakeup.session_id,
                    "channel": self.channel,
                    "event_type": "session.create_failed",
                    "causation_id": causation_id,
                    "correlation_id": correlation_id,
                    "payload": {
                        "command_type": "CreateSession",
                        "template_name": template_name,
                        "permission_mode": permission_mode,
                        "error_text": str(exc),
                    },
                }
            )
            raise

        created_event = await self._session_events_repo.append_event(
            {
                "session_id": wakeup.session_id,
                "channel": self.channel,
                "event_type": "session.created",
                "causation_id": causation_id,
                "correlation_id": correlation_id,
                "payload": {
                    "command_type": "CreateSession",
                    "template_name": str(session.get("template_name") or template_name),
                    "permission_mode": str(
                        session.get("permission_mode") or permission_mode or ""
                    ).strip() or None,
                    "session_kind": require_session_kind(session.get("session_kind")),
                },
            }
        )
        created_event_seq = int(created_event.get("event_seq") or 0)
        await self._project_snapshot(
            session_id=wakeup.session_id,
            event_seq=created_event_seq,
            session=session,
            fallback_permission_mode=permission_mode,
        )
        await self._initialize_create_snapshots(
            session_id=wakeup.session_id,
            event_seq=created_event_seq,
        )

        startup_command_id = str(uuid.uuid4())
        startup_command = await self._session_events_repo.append_event(
            {
                "session_id": wakeup.session_id,
                "channel": "command",
                "event_type": "command.accepted",
                "causation_id": startup_command_id,
                "correlation_id": correlation_id,
                "payload": {
                    "command_type": "StartSessionStartup",
                    "sandbox_generation": session["sandbox_generation"],
                    "author_user_id": user.user_id,
                    "template_name": template_name,
                    "permission_mode": permission_mode,
                },
            }
        )
        last_event_seq = int(startup_command.get("event_seq") or created_event_seq)
        await self._project_snapshot(
            session_id=wakeup.session_id,
            event_seq=last_event_seq,
            session=session,
            fallback_permission_mode=permission_mode,
        )
        return WorkerOutcome(
            session_id=wakeup.session_id,
            channel=self.channel,
            status="idle",
            processed_event_seq=last_event_seq,
            metadata={
                "command_type": "CreateSession",
                "result": dict(session),
                "startup_command_id": startup_command_id,
            },
        )

    async def _initialize_create_snapshots(
        self,
        *,
        session_id: str,
        event_seq: int,
    ) -> None:
        await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=event_seq,
            updates={
                "conversation_state": "IDLE",
                "current_turn_id": None,
                "active_interaction_id": None,
            },
        )
        await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="terminal",
            event_seq=event_seq,
            updates={
                "terminal_state": "IDLE",
                "terminal_exit_reason": None,
                "active_terminal_command_id": None,
                "active_terminal_execution_id": None,
                "terminal_pty_session_id": None,
            },
        )
