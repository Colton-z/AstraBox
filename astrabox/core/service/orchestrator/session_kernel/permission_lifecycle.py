from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.base import (
    bound_engine_client_manifest,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
    default_permission_mode_for_engine,
    require_session_kind,
)
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)


@dataclass(frozen=True)
class PermissionLifecycleResult:
    session_id: str
    permission_mode: str | None
    previous_permission_mode: str | None
    runtime_applied: bool
    changed: bool
    event_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "applied": self.runtime_applied,
            "runtime_applied": self.runtime_applied,
            "changed": self.changed,
        }
        if self.permission_mode is not None:
            payload["permission_mode"] = self.permission_mode
        if self.previous_permission_mode:
            payload["previous_permission_mode"] = self.previous_permission_mode
        if self.event_seq is not None:
            payload["event_seq"] = self.event_seq
        return payload


class PermissionLifecycle:
    """Lifecycle-owned writer for session permission mode state."""

    def __init__(
        self,
        *,
        sessions_repo: Any,
        apply_engine_permission_mode: Callable[[dict[str, Any], str], Awaitable[bool]],
        session_events_repo: Any,
        session_snapshots_repo: Any,
        interaction_snapshots_repo: Any | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._apply_engine_permission_mode = apply_engine_permission_mode
        self._session_events_repo = session_events_repo
        self._session_snapshots_repo = session_snapshots_repo
        self._interaction_snapshots_repo = interaction_snapshots_repo

    @staticmethod
    def default_for_session_kind(
        session_kind: str | None,
        *,
        engine_kind: str,
    ) -> str | None:
        return default_permission_mode_for_engine(
            engine_kind,
            require_session_kind(session_kind),
        )

    @classmethod
    def validate_requested_mode(
        cls,
        value: str | None,
        *,
        allow_none: bool = False,
        field_name: str = "permission_mode",
    ) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            if allow_none:
                return None
            raise APIError(
                code="INVALID_REQUEST",
                message=f"{field_name} is required",
                status_code=400,
            )
        return raw

    @classmethod
    def resolve_initial_mode(
        cls,
        requested_mode: str | None,
        *,
        session_kind: str | None,
        engine_kind: str,
    ) -> str | None:
        requested = cls.validate_requested_mode(requested_mode, allow_none=True)
        capabilities = capabilities_for_engine_kind(engine_kind)
        if not capabilities.permission_modes:
            if requested is not None:
                raise APIError(
                    code="ENGINE_CAPABILITY_UNAVAILABLE",
                    message=(
                        f"engine_kind={engine_kind!r} has no permission-mode "
                        "capability"
                    ),
                    status_code=400,
                )
            return None
        effective = requested or cls.default_for_session_kind(
            session_kind,
            engine_kind=engine_kind,
        )
        if effective is None:
            raise APIError(
                code="ENGINE_PERMISSION_MODE_REQUIRED",
                message=(
                    f"engine_kind={engine_kind!r} requires an explicit permission "
                    "mode for this session kind"
                ),
                status_code=400,
            )
        if effective not in capabilities.permission_modes:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"permission_mode={effective!r} is not supported by "
                    f"engine_kind={engine_kind!r}; "
                    f"available={list(capabilities.permission_modes)!r}"
                ),
                status_code=400,
            )
        return effective

    @classmethod
    def observed_permission_modes(
        cls,
        session: dict[str, Any],
        *,
        runtime: Any | None = None,
    ) -> tuple[str, ...]:
        """Return the exact vocabulary verified for this Session.

        Registration describes what bootstrap requires. Once the engine has
        connected, its manifest is the operation-time authority; using the
        registration table here would reject a mode the vendor just added.
        """

        engine_kind = resolve_session_engine_kind(session, runtime=runtime)
        if runtime is not None:
            try:
                manifest = bound_engine_client_manifest(runtime)
            except TypeError as exc:
                raise APIError(
                    code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                    message=f"live engine manifest is invalid: {exc}",
                    status_code=502,
                ) from exc
            manifest_kind = manifest.engine_kind
            raw_modes: Any = manifest.permission_modes
        else:
            stored = session.get("engine_capabilities")
            if not isinstance(stored, dict):
                raise APIError(
                    code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                    message="session has no verified engine capability manifest",
                    status_code=502,
                )
            manifest_kind = str(stored.get("engine_kind") or "").strip()
            raw_modes = stored.get("permission_modes")
        if manifest_kind != engine_kind:
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message=(
                    "engine capability identity mismatch: "
                    f"session={engine_kind!r} manifest={manifest_kind!r}"
                ),
                status_code=502,
            )
        if not isinstance(raw_modes, list) or any(
            not isinstance(mode, str) or not mode.strip() for mode in raw_modes
        ):
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message="engine capability permission_modes must be a list of names",
                status_code=502,
            )
        modes = tuple(raw_modes)
        if len(set(modes)) != len(modes):
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message="engine capability permission_modes must be unique",
                status_code=502,
            )
        return modes

    @classmethod
    def validate_mode_for_session(
        cls,
        value: str,
        *,
        session: dict[str, Any],
        runtime: Any | None = None,
    ) -> str:
        desired = cls.validate_requested_mode(value)
        assert desired is not None
        engine_kind = resolve_session_engine_kind(session, runtime=runtime)
        modes = cls.observed_permission_modes(session, runtime=runtime)
        if not modes:
            raise APIError(
                code="ENGINE_CAPABILITY_UNAVAILABLE",
                message=(
                    f"engine_kind={engine_kind!r} has no permission-mode capability"
                ),
                status_code=400,
            )
        if desired not in modes:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"permission_mode={desired!r} is not supported by "
                    f"engine_kind={engine_kind!r}; available={list(modes)!r}"
                ),
                status_code=400,
            )
        return desired

    def _current_mode(
        self,
        session: dict[str, Any],
        *,
        engine_kind: str,
        available_modes: tuple[str, ...],
    ) -> str | None:
        raw = str(session.get("permission_mode") or "").strip()
        if raw in available_modes:
            return raw
        default = self.default_for_session_kind(
            require_session_kind(session.get("session_kind")),
            engine_kind=engine_kind,
        )
        return default if default in available_modes else None

    def _runtime_mode_verified(
        self,
        *,
        runtime: Any | None,
        session: dict[str, Any],
        desired_mode: str | None,
    ) -> bool:
        if runtime is None or desired_mode is None:
            return False
        if not bool(getattr(runtime, "permission_mode_verified", False)):
            return False
        runtime_mode = str(getattr(runtime, "permission_mode", "") or "").strip()
        if runtime_mode != desired_mode:
            return False
        session_sandbox_id = str(session.get("sandbox_id") or "").strip()
        runtime_sandbox_id = str(getattr(runtime, "sandbox_id", "") or "").strip()
        return not session_sandbox_id or runtime_sandbox_id == session_sandbox_id

    async def ensure_before_turn_dispatch(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        turn_id: str | None,
        command_id: str | None,
        requested_mode: str | None,
        runtime: Any | None = None,
        source: str = "start_turn",
        command_type: str = "StartTurn",
        interaction_id: str | None = None,
    ) -> PermissionLifecycleResult:
        requested = self.validate_requested_mode(requested_mode, allow_none=True)
        engine_kind = resolve_session_engine_kind(session, runtime=runtime)
        available_modes = self.observed_permission_modes(session, runtime=runtime)
        previous_mode = self._current_mode(
            session,
            engine_kind=engine_kind,
            available_modes=available_modes,
        )
        if not available_modes:
            if requested is not None:
                raise APIError(
                    code="ENGINE_CAPABILITY_UNAVAILABLE",
                    message=(
                        f"engine_kind={engine_kind!r} has no permission-mode "
                        "capability"
                    ),
                    status_code=400,
                )
            return PermissionLifecycleResult(
                session_id=session_id,
                permission_mode=None,
                previous_permission_mode=None,
                runtime_applied=False,
                changed=False,
            )
        desired_mode = requested or previous_mode
        if desired_mode is None or desired_mode not in available_modes:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"permission_mode={desired_mode!r} is not supported by "
                    f"engine_kind={engine_kind!r}; "
                    f"available={list(available_modes)!r}"
                ),
                status_code=400,
            )

        if self._runtime_mode_verified(
            runtime=runtime,
            session=session,
            desired_mode=desired_mode,
        ):
            if desired_mode != previous_mode:
                event_seq = await self._record_success(
                    session_id=session_id,
                    session=session,
                    command_id=command_id,
                    turn_id=turn_id,
                    permission_mode=desired_mode,
                    previous_mode=previous_mode,
                    source=source,
                    command_type=command_type,
                    interaction_id=interaction_id,
                    runtime_applied=False,
                )
                return PermissionLifecycleResult(
                    session_id=session_id,
                    permission_mode=desired_mode,
                    previous_permission_mode=previous_mode,
                    runtime_applied=False,
                    changed=True,
                    event_seq=event_seq,
                )
            return PermissionLifecycleResult(
                session_id=session_id,
                permission_mode=desired_mode,
                previous_permission_mode=previous_mode,
                runtime_applied=False,
                changed=False,
            )

        try:
            runtime_applied = await self._apply_runtime_mode(
                session_id=session_id,
                session=session,
                permission_mode=desired_mode,
            )
        except APIError as exc:
            await self._record_failure(
                session_id=session_id,
                session=session,
                command_id=command_id,
                turn_id=turn_id,
                requested_mode=desired_mode,
                previous_mode=previous_mode,
                source=source,
                command_type=command_type,
                interaction_id=interaction_id,
                error=exc,
                dispatch_blocked=True,
            )
            raise
        except Exception as exc:
            wrapped = APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    "permission_mode reconciliation failed before dispatch: "
                    f"session={session_id} desired={desired_mode} "
                    f"sandbox_id={str(session.get('sandbox_id') or '').strip() or '<missing>'} "
                    f"endpoint={str(session.get('sandbox_endpoint') or '').strip() or '<missing>'}: {exc}"
                ),
                status_code=502,
            )
            await self._record_failure(
                session_id=session_id,
                session=session,
                command_id=command_id,
                turn_id=turn_id,
                requested_mode=desired_mode,
                previous_mode=previous_mode,
                source=source,
                command_type=command_type,
                interaction_id=interaction_id,
                error=wrapped,
                dispatch_blocked=True,
            )
            raise wrapped from exc
        if not runtime_applied:
            exc = APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    "permission_mode reconciliation was not accepted before dispatch: "
                    f"session={session_id} desired={desired_mode} "
                    f"sandbox_id={str(session.get('sandbox_id') or '').strip() or '<missing>'} "
                    f"endpoint={str(session.get('sandbox_endpoint') or '').strip() or '<missing>'}"
                ),
                status_code=502,
            )
            await self._record_failure(
                session_id=session_id,
                session=session,
                command_id=command_id,
                turn_id=turn_id,
                requested_mode=desired_mode,
                previous_mode=previous_mode,
                source=source,
                command_type=command_type,
                interaction_id=interaction_id,
                error=exc,
                dispatch_blocked=True,
            )
            raise exc

        if desired_mode == previous_mode:
            return PermissionLifecycleResult(
                session_id=session_id,
                permission_mode=desired_mode,
                previous_permission_mode=previous_mode,
                runtime_applied=True,
                changed=False,
            )

        event_seq = await self._record_success(
            session_id=session_id,
            session=session,
            command_id=command_id,
            turn_id=turn_id,
            permission_mode=desired_mode,
            previous_mode=previous_mode,
            source=source,
            command_type=command_type,
            interaction_id=interaction_id,
            runtime_applied=True,
        )
        return PermissionLifecycleResult(
            session_id=session_id,
            permission_mode=desired_mode,
            previous_permission_mode=previous_mode,
            runtime_applied=True,
            changed=True,
            event_seq=event_seq,
        )

    async def apply_explicit_update(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        command_id: str | None,
        requested_mode: str,
    ) -> PermissionLifecycleResult:
        engine_kind = resolve_session_engine_kind(session)
        available_modes = self.observed_permission_modes(session)
        previous_mode = self._current_mode(
            session,
            engine_kind=engine_kind,
            available_modes=available_modes,
        )
        try:
            desired_mode = self.validate_requested_mode(requested_mode)
            if not available_modes:
                raise APIError(
                    code="ENGINE_CAPABILITY_UNAVAILABLE",
                    message=(
                        f"engine_kind={engine_kind!r} has no permission-mode "
                        "capability"
                    ),
                    status_code=400,
                )
            if desired_mode not in available_modes:
                raise APIError(
                    code="INVALID_REQUEST",
                    message=(
                        f"permission_mode={desired_mode!r} is not supported by "
                        f"engine_kind={engine_kind!r}; "
                        f"available={list(available_modes)!r}"
                    ),
                    status_code=400,
                )
            self._assert_session_can_accept_explicit_update(session)
        except APIError as exc:
            await self._record_failure(
                session_id=session_id,
                session=session,
                command_id=command_id,
                turn_id=None,
                requested_mode=str(requested_mode or "").strip(),
                previous_mode=previous_mode,
                source="explicit_update",
                command_type="SetPermissionMode",
                interaction_id=None,
                error=exc,
                dispatch_blocked=False,
            )
            raise
        await self._assert_no_active_conversation_permission_update(
            session_id=session_id,
            command_id=command_id,
            requested_mode=desired_mode,
            previous_mode=previous_mode,
            session=session,
        )

        try:
            runtime_applied = await self._apply_runtime_mode(
                session_id=session_id,
                session=session,
                permission_mode=desired_mode,
            )
        except APIError as exc:
            await self._record_failure(
                session_id=session_id,
                session=session,
                command_id=command_id,
                turn_id=None,
                requested_mode=desired_mode,
                previous_mode=previous_mode,
                source="explicit_update",
                command_type="SetPermissionMode",
                interaction_id=None,
                error=exc,
                dispatch_blocked=False,
            )
            raise
        except Exception as exc:
            wrapped = APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"failed to apply permission mode {desired_mode}: {exc}",
                status_code=502,
            )
            await self._record_failure(
                session_id=session_id,
                session=session,
                command_id=command_id,
                turn_id=None,
                requested_mode=desired_mode,
                previous_mode=previous_mode,
                source="explicit_update",
                command_type="SetPermissionMode",
                interaction_id=None,
                error=wrapped,
                dispatch_blocked=False,
            )
            raise wrapped from exc
        if not runtime_applied:
            exc = APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"failed to apply permission mode {desired_mode}",
                status_code=502,
            )
            await self._record_failure(
                session_id=session_id,
                session=session,
                command_id=command_id,
                turn_id=None,
                requested_mode=desired_mode,
                previous_mode=previous_mode,
                source="explicit_update",
                command_type="SetPermissionMode",
                interaction_id=None,
                error=exc,
                dispatch_blocked=False,
            )
            raise exc

        event_seq = await self._record_success(
            session_id=session_id,
            session=session,
            command_id=command_id,
            turn_id=None,
            permission_mode=desired_mode,
            previous_mode=previous_mode,
            source="explicit_update",
            command_type="SetPermissionMode",
            interaction_id=None,
            runtime_applied=True,
        )
        return PermissionLifecycleResult(
            session_id=session_id,
            permission_mode=desired_mode,
            previous_permission_mode=previous_mode,
            runtime_applied=True,
            changed=desired_mode != previous_mode,
            event_seq=event_seq,
        )

    def _assert_session_can_accept_explicit_update(
        self,
        session: dict[str, Any],
    ) -> None:
        state = str(session.get("state") or "")
        if state in {
            SessionState.CREATING.value,
            SessionState.BUSY.value,
            SessionState.INTERRUPTING.value,
            "PROCESSING",
        }:
            raise APIError(
                code="SESSION_BUSY",
                message="cannot update permission mode while a turn or startup is active",
                status_code=409,
            )
        if state in {SessionState.TERMINATED.value, SessionState.DELETED.value}:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"cannot update permission mode when session state is {state}",
                status_code=409,
            )

    async def _assert_no_active_conversation_permission_update(
        self,
        *,
        session_id: str,
        command_id: str | None,
        requested_mode: str,
        previous_mode: str,
        session: dict[str, Any],
    ) -> None:
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        interaction = None
        if self._interaction_snapshots_repo is not None:
            interaction = await self._interaction_snapshots_repo.get_active_interaction(
                session_id,
            )
        conversation_state = (
            str((snapshot or {}).get("conversation_state") or "").strip()
            if isinstance(snapshot, dict)
            else ""
        )
        current_turn_id = (
            str((snapshot or {}).get("current_turn_id") or "").strip()
            if isinstance(snapshot, dict)
            else ""
        )
        active_interaction_id = (
            str((snapshot or {}).get("active_interaction_id") or "").strip()
            if isinstance(snapshot, dict)
            else ""
        )
        if not (
            isinstance(interaction, dict)
            or active_interaction_id
            or current_turn_id
            or conversation_state in {"PROCESSING", "STREAMING", "WAITING_FOR_INTERACTION"}
        ):
            return

        exc = APIError(
            code="SESSION_BUSY",
            message="cannot update permission mode while a turn or interaction is active",
            status_code=409,
        )
        await self._record_failure(
            session_id=session_id,
            session=session,
            command_id=command_id,
            turn_id=current_turn_id or None,
            requested_mode=requested_mode,
            previous_mode=previous_mode,
            source="explicit_update",
            command_type="SetPermissionMode",
            interaction_id=active_interaction_id or None,
            error=exc,
            dispatch_blocked=False,
        )
        raise exc

    async def _apply_runtime_mode(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        permission_mode: str,
    ) -> bool:
        if str(session.get("session_id") or "").strip() != session_id:
            raise RuntimeError("permission lifecycle session identity mismatch")
        return bool(await self._apply_engine_permission_mode(session, permission_mode))

    async def _record_success(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        command_id: str | None,
        turn_id: str | None,
        permission_mode: str,
        previous_mode: str | None,
        source: str,
        command_type: str,
        interaction_id: str | None,
        runtime_applied: bool,
    ) -> int:
        if str(session.get("permission_mode") or "").strip() != permission_mode:
            await self._sessions_repo.update_session(
                session_id,
                {"permission_mode": permission_mode},
            )
        session["permission_mode"] = permission_mode
        event = await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "lifecycle",
                "turn_id": turn_id,
                "event_type": "session.permission_mode_updated",
                "causation_id": command_id,
                "correlation_id": command_id,
                "payload": {
                    "command_type": command_type,
                    "source": source,
                    "turn_id": turn_id,
                    "interaction_id": interaction_id,
                    "permission_mode": permission_mode,
                    "previous_permission_mode": previous_mode,
                    "runtime_applied": runtime_applied,
                },
            }
        )
        event_seq = int(event.get("event_seq") or 0)
        await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="lifecycle",
            event_seq=event_seq,
            updates={
                "permission_mode": permission_mode,
            },
        )
        return event_seq

    async def _record_failure(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        command_id: str | None,
        turn_id: str | None,
        requested_mode: str,
        previous_mode: str | None,
        source: str,
        command_type: str,
        interaction_id: str | None,
        error: BaseException,
        dispatch_blocked: bool,
    ) -> int:
        event = await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "lifecycle",
                "turn_id": turn_id,
                "event_type": "session.permission_mode_update_failed",
                "causation_id": command_id,
                "correlation_id": command_id,
                "payload": {
                    "command_type": command_type,
                    "source": source,
                    "turn_id": turn_id,
                    "interaction_id": interaction_id,
                    "permission_mode": requested_mode,
                    "previous_permission_mode": previous_mode,
                    "error_code": getattr(error, "code", type(error).__name__),
                    "error_text": str(error),
                    "dispatch_blocked": dispatch_blocked,
                    "occurred_at": utcnow_iso(),
                },
            }
        )
        event_seq = int(event.get("event_seq") or 0)
        # `previous_mode` is already this session's current mode: both callers
        # compute it through `_current_mode` with the engine's own vocabulary
        # before entering the try block. Recomputing it here would answer the
        # same value from the same inputs, and an engine with no permission-mode
        # capability — whose current mode is None — is exactly the case that
        # reaches this line, where a second, differently-argued call replaced its
        # 400 with a 500 on the way out.
        fallback_mode = previous_mode
        await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="lifecycle",
            event_seq=event_seq,
            updates={
                "permission_mode": fallback_mode,
            },
        )
        return event_seq
