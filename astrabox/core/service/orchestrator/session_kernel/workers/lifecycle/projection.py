"""Snapshot projection and event/result shaping for the lifecycle worker.

Turns a session dict into lifecycle-channel snapshot updates
(``_project_snapshot`` delegating to the two ``@staticmethod`` derive_*
functions), and turns a ``command_type`` + result dict into event-type
strings, payloads, and the "what session does the post-command snapshot
represent" resolution. Gathered here as :class:`_LifecycleProjectionMixin`,
mixed into ``SessionLifecycleWorker``.

``service_mixins/lifecycle.py``'s ``_project_lifecycle_snapshot_from_session``
calls ``_derive_lifecycle_state`` and ``_derive_runtime_connectivity_state``
directly as bare class statics
(``SessionLifecycleWorker._derive_lifecycle_state(...)``); mixin inheritance
keeps those names on ``SessionLifecycleWorker`` via its MRO.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.model import SessionState


class _LifecycleProjectionMixin:
    """Snapshot-projection & event/result shaping."""

    async def _project_snapshot(
        self,
        *,
        session_id: str,
        event_seq: int,
        session: dict[str, Any],
        fallback_permission_mode: str | None,
    ) -> None:
        permission_mode = str(
            (session or {}).get("permission_mode")
            or fallback_permission_mode
            or ""
        ).strip() or None
        updates: dict[str, Any] = {
            "session_lifecycle_state": self._derive_lifecycle_state(session),
            "runtime_connectivity_state": self._derive_runtime_connectivity_state(session),
            "permission_mode": permission_mode,
            "last_error": str((session or {}).get("last_error") or "").strip() or None,
            "startup_progress": str((session or {}).get("startup_progress") or "").strip() or None,
        }
        agent_id = str((session or {}).get("agent_id") or "").strip()
        if agent_id:
            updates["agent_binding"] = {
                "agent_id": agent_id,
                "session_kind": str((session or {}).get("session_kind") or "").strip() or None,
            }
        await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel=self.channel,
            event_seq=event_seq,
            updates=updates,
        )

    @staticmethod
    def _derive_lifecycle_state(session: dict[str, Any] | None) -> str:
        state = str((session or {}).get("state") or "").strip()
        if state == SessionState.CREATING.value:
            return "CREATING"
        if state == SessionState.TERMINATED.value:
            return "TERMINATED"
        if state == SessionState.DELETED.value:
            return "DELETED"
        # RECOVERY_REQUIRED is not a lifecycle state here; degradation is
        # expressed via conversation_state in the snapshot.
        return "ACTIVE"

    @staticmethod
    def _derive_runtime_connectivity_state(session: dict[str, Any] | None) -> str:
        state = str((session or {}).get("state") or "").strip()
        runtime_unavailable = bool((session or {}).get("runtime_unavailable"))
        if state == SessionState.CREATING.value:
            return "CONNECTING"
        if state in {SessionState.TERMINATED.value, SessionState.DELETED.value}:
            return "LOST"
        if runtime_unavailable:
            return "DEGRADED"
        return "CONNECTED"

    @staticmethod
    def _success_event_type(command_type: str) -> str:
        return {
            "SetPermissionMode": "session.permission_mode_updated",
            "DeleteSession": "session.deleted",
            "ArchiveSession": "session.archived",
            "EndConversation": "session.conversation_ended",
            "TerminateSession": "session.terminated",
            "RecoverSession": "session.recovered",
        }.get(command_type, "session.lifecycle_updated")

    @staticmethod
    def _failure_event_type(command_type: str) -> str:
        return {
            "SetPermissionMode": "session.permission_mode_update_failed",
            "DeleteSession": "session.delete_failed",
            "ArchiveSession": "session.archive_failed",
            "EndConversation": "session.conversation_end_failed",
            "TerminateSession": "session.terminate_failed",
            "RecoverSession": "session.recover_failed",
        }.get(command_type, "session.lifecycle_update_failed")

    @staticmethod
    def _success_payload(command_type: str, result: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"command_type": command_type}
        if command_type == "SetPermissionMode":
            payload.update(
                {
                    "permission_mode": result.get("permission_mode"),
                    "applied": bool(result.get("applied")),
                }
            )
            return payload
        for key in (
            "session_id",
            "deleted",
            "archived",
            "sandbox_id",
            "status",
            "killed",
            "state",
            "permission_mode",
            "current_turn_id",
            "startup_command_id",
            "startup_command_event_seq",
        ):
            if key in result:
                payload[key] = result.get(key)
        return payload

    @staticmethod
    def _success_projection_session(
        *,
        command_type: str,
        latest_session: dict[str, Any] | None,
        previous_session: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        # After a successful DeleteSession, get_session() returns None (deleted
        # sessions are filtered from the primary read), so
        # `latest_session or previous_session` would project the stale
        # pre-delete state (ACTIVE/CONNECTED + stale last_error). Synthesize
        # the DELETED projection instead.
        if isinstance(latest_session, dict):
            return latest_session
        projected = dict(previous_session)
        if command_type == "DeleteSession" and bool(result.get("deleted")):
            projected.update(
                {
                    "deleted": True,
                    "state": SessionState.DELETED.value,
                    "runtime_unavailable": False,
                    "last_error": None,
                }
            )
        return projected

    @staticmethod
    def _result_permission_mode(
        result: dict[str, Any],
        payload: dict[str, Any],
        session: dict[str, Any],
    ) -> str | None:
        return str(
            result.get("permission_mode")
            or payload.get("permission_mode")
            or session.get("permission_mode")
            or ""
        ).strip() or None
