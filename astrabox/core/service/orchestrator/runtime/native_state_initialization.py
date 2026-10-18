"""Fence recovery of an unpublished native profile to its current allocation."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.repository.preparation_subject_repository import lock_subject, subject_conflict
from astrabox.persistence.repository.runtime_state_snapshot_repository import RuntimeStateOwner
from astrabox.persistence.sql.models.sessions import Session
from astrabox.seams.runtime_preparation import PreparationSubject


@dataclass(frozen=True)
class NativeStateInitializationGuard:
    owner: RuntimeStateOwner
    session_id: str
    recovery_owner: str
    sandbox_generation: str
    sandbox_id: str

    async def validate(self, connection: AsyncConnection) -> None:
        if self.owner.subject_kind != "assistant":
            raise subject_conflict()
        state = await lock_subject(connection, PreparationSubject(kind="assistant", id=self.owner.subject_id))
        workspace = state.runtime or {}
        session = state.materializer_session
        if session is None:
            session = (await connection.execute(
                select(Session.__table__).where(Session.session_id == self.session_id).with_for_update(read=True)
            )).mappings().first()
        session = dict(session or {})
        allocation = session.get("startup_allocation") or {}
        materializing = (
            workspace.get("state") == "MATERIALIZING"
            and workspace.get("provisioning_session_id") == self.session_id
            and workspace.get("provisioning_sandbox_generation") == self.sandbox_generation
            and session.get("state") == "CREATING"
            and session.get("runtime_recovery_owner") == self.recovery_owner
        )
        retained = (
            workspace.get("state") == "RECOVERY_REQUIRED"
            and workspace.get("current_sandbox_id") == self.sandbox_id
            and not workspace.get("post_commit_cleanup_pending")
            and any(
                record.get("native_state_save_required")
                and record.get("sandbox_generation") == self.sandbox_generation
                and record.get("recovery_owner") == self.recovery_owner
                and (record.get("allocation") or {}).get("sandbox_id") == self.sandbox_id
                for record in session.get("retained_startup_allocations") or ()
            )
        )
        if (
            str(state.definition.get("owner_id") or "") != self.owner.user_id
            or not (materializing or retained)
            or session.get("session_id") != self.session_id
            or session.get("assistant_id") != self.owner.subject_id
            or session.get("user_id") != self.owner.user_id
            or session.get("sandbox_generation") != self.sandbox_generation
            or allocation.get("sandbox_id") != self.sandbox_id
            or allocation.get("scope") != "sandbox"
        ):
            raise subject_conflict()
