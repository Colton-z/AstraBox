"""Session child-run reads and controls for :class:`SessionKernelService`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
)


class ChildRunProjectionMixin:
    """Expose one engine-neutral child-run resource for every engine."""

    async def _call_child_run_view(
        self,
        operation: Callable[..., Awaitable[Any]],
        *args: Any,
    ) -> Any:
        try:
            return await operation(*args)
        except ChildRunProjectionError as exc:
            raise APIError(
                code="CHILD_RUN_PROJECTION_INVALID",
                message=str(exc),
                status_code=409,
            ) from exc

    async def _owned_child_run_session(
        self,
        user: UserContext,
        session_id: str,
        session: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return (
            dict(session)
            if isinstance(session, dict)
            else await self._must_get_owned_session(user, session_id)
        )

    @staticmethod
    def _require_child_run_id(child_run_id: str) -> str:
        clean_child_run_id = str(child_run_id or "").strip()
        if not clean_child_run_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="child_run_id is required",
                status_code=400,
            )
        return clean_child_run_id

    async def list_child_runs(
        self,
        user: UserContext,
        session_id: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the canonical child-run projection for one Session."""

        base_session = await self._owned_child_run_session(user, session_id, session)
        effective_session_id = str(base_session.get("session_id") or session_id)
        await self._turn_service.reconcile_engine_child_resources(base_session)
        child_runs = await self._call_child_run_view(
            self._child_run_view.list_child_runs,
            effective_session_id,
        )
        return {"session_id": effective_session_id, "child_runs": child_runs}

    async def get_child_run_messages(
        self,
        user: UserContext,
        session_id: str,
        child_run_id: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return one child run's engine-owned transcript."""

        clean_child_run_id = self._require_child_run_id(child_run_id)
        base_session = await self._owned_child_run_session(user, session_id, session)
        effective_session_id = str(base_session.get("session_id") or session_id)
        await self._turn_service.reconcile_engine_child_resources(base_session)
        messages = await self._call_child_run_view(
            self._child_run_view.get_child_run_messages,
            effective_session_id,
            clean_child_run_id,
        )
        if messages is None:
            raise APIError(
                code="CHILD_RUN_NOT_FOUND",
                message=f"Child run '{clean_child_run_id}' was not found",
                status_code=404,
            )
        return {
            "session_id": effective_session_id,
            "child_run_id": clean_child_run_id,
            "messages": messages,
        }

    async def stop_child_run(
        self,
        user: UserContext,
        session_id: str,
        child_run_id: str,
    ) -> dict[str, Any]:
        """Stop one child run through the control reference its adapter declared."""

        clean_child_run_id = self._require_child_run_id(child_run_id)
        session = await self._must_get_projection_backed_session(
            user,
            session_id,
            reconcile_conversation=False,
            internal_wiring=True,
        )
        self._require_turn_eligible(session, channel="child-run control")
        effective_session_id = str(session.get("session_id") or session_id)
        await self._turn_service.reconcile_engine_child_resources(session)
        child_run = await self._call_child_run_view(
            self._child_run_view.get_child_run_control,
            effective_session_id,
            clean_child_run_id,
        )
        if child_run is None:
            raise APIError(
                code="CHILD_RUN_NOT_FOUND",
                message=f"Child run '{clean_child_run_id}' was not found",
                status_code=404,
            )
        if child_run.get("closed"):
            raise APIError(
                code="CHILD_RUN_ALREADY_TERMINAL",
                message=f"Child run '{clean_child_run_id}' is already terminal",
                status_code=409,
            )
        if "stop" not in child_run.get("operations", []):
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message=f"Child run '{clean_child_run_id}' is not currently stoppable",
                status_code=409,
            )
        control_ref = str(child_run.get("control_ref") or "").strip()
        if not control_ref:
            raise APIError(
                code="CHILD_RUN_CONTROL_UNAVAILABLE",
                message=f"Child run '{clean_child_run_id}' has no control handle",
                status_code=409,
            )
        await self._turn_service.stop_engine_child_run(
            session,
            control_ref,
        )
        return {
            "session_id": effective_session_id,
            "child_run_id": clean_child_run_id,
            "status": "accepted",
        }
