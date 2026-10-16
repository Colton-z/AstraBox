"""Bringing a session's runtime back: reattach to its box, or start a new one.

Recovery runs when a session's runtime is gone or in doubt. The reader host
asks for it when inputs wait for a runtime that is confirmed gone, and a
person asks for it from the page. No response of the old runtime is protected
here: a runtime that was answering went with its box, and the session's reader
records that response as lost. Nothing is replayed; the engine resumes its own
conversation from ``engine_session_key`` through the stored SessionStore.

The decision is the box's liveness. A session-owned box the provider reports
alive, on a session not marked unavailable, is reattached under the same id;
any other session-owned box is replaced by a new one. A session bound to an
Assistant workspace re-enters startup instead, because only the workspace may
reuse, resume or destroy that box.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.engine_kind_utils import resolve_session_engine_kind
from astrabox.core.service.orchestrator.sandbox_names import keep_name_updates
from astrabox.core.service.orchestrator.session_lifecycle.ownership import RecoveryOwnership
from astrabox.core.service.orchestrator.session_lifecycle.startup import (
    SessionStartup,
    runtime_initialization_metadata,
)
from astrabox.seams.sandbox import SANDBOX_LIFECYCLE_PROBE_OK
from astrabox.seams.sandbox_disposal import SandboxDestruction

logger = get_logger(__name__)


@dataclass(frozen=True)
class _Restart:
    """A session reset for a new startup: the box generation it starts."""

    sandbox_generation: str
    resume_engine_session_key: str | None


class _GiveBack(Exception):
    """Recovery stops and returns the session as it was, with this reason recorded."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class SessionRecovery:
    """Runs one claimed recovery to a reattached runtime or a new startup."""

    def __init__(
        self,
        *,
        sessions_repo: Any,
        agent_config: Any,
        runtime_manager: Any,
        runtime_subjects: Any,
        startup: SessionStartup,
    ) -> None:
        self._sessions = sessions_repo
        self._agent_config = agent_config
        self._runtime_manager = runtime_manager
        self._runtime_subjects = runtime_subjects
        self._startup = startup

    async def run(self, *, session: dict[str, Any], owner: str) -> None:
        """Recover ``session``, which the caller claimed for ``owner``.

        ``session`` is the row as observed before the claim, a startup past its
        creation timeout reading as ``TERMINATED``. A recovery that ends without
        reattaching or starting a box gives the session back in that state,
        never leaving a ``CREATING`` row with nothing driving it.
        """
        session_id = str(session["session_id"])
        ownership = RecoveryOwnership(self._sessions, session_id, owner)
        observed_runtime = self._runtime_manager.get_runtime(session_id)
        restart: _Restart | None = None
        settled = False
        reason: str | None = None
        error_text: str | None = None
        try:
            with ownership.bind():
                restart = await self._reattach_or_reset(session, ownership, observed_runtime)
            settled = True
        except _GiveBack as exc:
            reason, error_text = exc.reason, str(exc)
            logger.warning("session recovery stopped session=%s reason=%s: %s", session_id, reason, exc)
        except Exception as exc:
            error_text = str(exc.message if isinstance(exc, APIError) else exc) or "runtime recovery failed"
            logger.warning("session recovery failed session=%s: %s", session_id, error_text, exc_info=True)
        finally:
            if not settled:
                updates: dict[str, Any] = {"state": session.get("state")}
                if error_text:
                    updates["last_error"] = error_text
                if reason:
                    updates["recovery_reason"] = reason
                await self._sessions.compare_and_update_session(
                    session_id, expected=ownership.expected, updates=updates
                )
        if restart is not None:
            await self._startup.run(
                session_id=session_id,
                owner=owner,
                sandbox_generation=restart.sandbox_generation,
                resume_engine_session_key=restart.resume_engine_session_key,
                replacing=True,
            )

    async def _reattach_or_reset(
        self,
        session: dict[str, Any],
        ownership: RecoveryOwnership,
        observed_runtime: Any,
    ) -> _Restart | None:
        """Reattach and write ``READY`` (``None``), or reset the session for a new startup."""
        session_id = ownership.session_id
        template = await self._agent_config.resolve_session_harness(session)
        if not template:
            raise APIError(
                code="TEMPLATE_NOT_ALLOWED",
                message="agent configuration not found",
                status_code=403,
            )
        engine_session_key = str(session.get("engine_session_key") or "").strip() or None
        action = self._runtime_subjects.recovery_action_for(session)
        if action == "restart_session_on_subject":
            return await self._reset(
                session, ownership, engine_session_key, destruction=None, clear_binding=False
            )
        if action != "recover_session_allocation":
            raise RuntimeError(f"unknown runtime recovery action: {action!r}")

        previous_sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        box_gone = False
        if previous_sandbox_id:
            # The stored binding and a resident client can both look healthy
            # while the provider has removed the box; only its answer counts.
            probe = await self._runtime_manager.get_sandbox_lifecycle_probe(previous_sandbox_id)
            box_gone = self._runtime_manager._is_terminal_sandbox_lifecycle_probe(probe)
            if not box_gone and probe.probe_status != SANDBOX_LIFECYCLE_PROBE_OK:
                raise RuntimeError(
                    "cannot determine bound sandbox liveness before recovery "
                    f"session={session_id} sandbox={previous_sandbox_id}: {probe.error_text}"
                )
        if (
            previous_sandbox_id
            and not box_gone
            and session.get("state") != SessionState.TERMINATED.value
            and not session.get("runtime_unavailable")
        ):
            await self._reattach(
                session,
                ownership,
                template=template,
                sandbox_id=previous_sandbox_id,
                engine_session_key=engine_session_key,
                observed_runtime=observed_runtime,
            )
            return None

        await ownership.require_current()
        if observed_runtime is not None:
            await self._runtime_manager.evict_runtime_if_current(session_id, observed_runtime)
        # A box the provider confirmed gone needs no destructive action. One it
        # did not is kept on the session's ledger for scoped reconciliation,
        # never deleted here, where it could be a successor's.
        destruction = None
        if previous_sandbox_id:
            destruction = (
                SandboxDestruction.confirmed_gone(
                    previous_sandbox_id, detail="recovery confirmed supplier death"
                )
                if box_gone
                else SandboxDestruction.refused(
                    previous_sandbox_id,
                    detail="recovery retains the observed box for scoped reconciliation",
                )
            )
        return await self._reset(
            session, ownership, engine_session_key, destruction=destruction, clear_binding=True
        )

    async def _reattach(
        self,
        session: dict[str, Any],
        ownership: RecoveryOwnership,
        *,
        template: Any,
        sandbox_id: str,
        engine_session_key: str | None,
        observed_runtime: Any,
    ) -> None:
        session_id = ownership.session_id
        session_kind = require_session_kind(session.get("session_kind"))
        permission_mode = str(session.get("permission_mode") or "").strip() or None
        identity = session.get("runtime_identity") if isinstance(session.get("runtime_identity"), dict) else None
        plan = self._runtime_manager.plan_runtime_attach(
            agent_id=str(session.get("agent_id") or ""),
            session_id=session_id,
            session_kind=session_kind,
            sandbox_id=sandbox_id,
            engine_session_key=engine_session_key,
            existing_terminal_cwd=str(session.get("terminal_cwd") or "").strip() or None,
            engine_kind=resolve_session_engine_kind(session),
            runtime_identity=identity,
        )
        try:
            runtime = await self._runtime_manager.ensure_runtime(
                session_id,
                template,
                sandbox_id=sandbox_id,
                user_id=str(session.get("user_id") or ""),
                engine_session_key=engine_session_key,
                permission_mode=permission_mode,
                session_kind=session_kind,
                workspace_plan=plan,
                runtime_identity=identity,
                startup_guard=ownership.require_current,
            )
        except Exception:
            # A failed attachment does not authorize deleting a live box, and a
            # stale owner must not sweep its successor's runtime.
            if observed_runtime is not None:
                await self._runtime_manager.evict_runtime_if_current(session_id, observed_runtime)
            raise
        # Attaching can renew the box, so its expiry is read afresh.
        try:
            expires_at = await self._runtime_manager.get_sandbox_expires_at(sandbox_id)
        except Exception as exc:
            raise _GiveBack(
                "sandbox_expiry_unavailable", f"cannot read attached sandbox expiry: {exc}"
            ) from exc
        updates: dict[str, Any] = {
            "state": SessionState.READY.value,
            "sandbox_id": getattr(runtime, "sandbox_id", None) or sandbox_id,
            "runtime_unavailable": False,
            "last_error": None,
            "startup_progress": None,
            "model_name": self._runtime_manager.resolve_template_model_name(template),
            "recovery_reason": None,
            "expires_at": expires_at.isoformat() if expires_at is not None else None,
        }
        key = engine_session_key or getattr(runtime, "engine_session_key", None)
        if key:
            updates["engine_session_key"] = key
        updates.update(await runtime_initialization_metadata(session_id, runtime))
        await ownership.update(updates, notify="reader")

    async def _reset(
        self,
        session: dict[str, Any],
        ownership: RecoveryOwnership,
        engine_session_key: str | None,
        *,
        destruction: SandboxDestruction | None,
        clear_binding: bool,
    ) -> _Restart:
        """Give the session a new box generation and hand it to a startup.

        A session-owned recovery moves an unconfirmed old box onto the
        session's undestroyed ledger before clearing the binding. A session on
        a longer-lived subject keeps its routing copy until the startup
        publishes the owner's replacement: it may not sever or destroy the
        owner's last name for the box.
        """
        await ownership.require_current()
        generation = str(uuid.uuid4())
        updates: dict[str, Any] = {
            "state": SessionState.CREATING.value,
            "runtime_unavailable": True,
            "last_error": None,
            "startup_progress": "creating_sandbox",
            "sandbox_generation": generation,
            "sandbox_callback_token": str(uuid.uuid4()),
            "recovery_reason": None,
        }
        if clear_binding:
            updates.update(keep_name_updates(destruction, row=session))
            updates.update({"sandbox_id": None, "expires_at": None})
        await ownership.update(updates)
        return _Restart(sandbox_generation=generation, resume_engine_session_key=engine_session_key)
