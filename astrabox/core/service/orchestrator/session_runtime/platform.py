"""The reader's connections to the rest of the platform.

* :class:`PlatformRuntimeConnector` finds a session's live runtime through
  :class:`~astrabox.core.service.orchestrator.runtime_ensure.RuntimeEnsure`.
* :func:`reader_hooks` gives a reader what it needs per session: the
  deployment's preparation before each engine write, the box lease renewal
  while the runtime works, permission-mode changes, and the consumers of
  recorded facts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import parse_iso
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.runtime import RuntimeClient, RuntimeInput
from astrabox.core.service.orchestrator.session_runtime.host import (
    ConnectedRuntime,
    RuntimeEnded,
    RuntimeGone,
    RuntimeNotReady,
    RuntimeStartFailed,
)
from astrabox.core.service.orchestrator.session_runtime.reader import InputPreparationRefused, ReaderHooks
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.session_runtime_repository import FactBatch, SessionRuntimeRepository
from astrabox.seams.runtime_preparation import (
    TURN_PREPARATION_FAILED,
    TURN_PREPARATION_USER_ACTION_REQUIRED,
    PreparationUserActionRequired,
    RuntimeMaterials,
    TurnPreparationContext,
    prepares_turns,
    runtime_preparation_provider,
    validated_user_action,
)
from astrabox.seams.sandbox import sandbox_for_name

logger = get_logger(__name__)

#: How long the deployment's preparation may take before an input is refused.
PREPARATION_TIMEOUT_S = 30.0

FactConsumer = Callable[[str, FactBatch], Awaitable[None]]


class PlatformRuntimeConnector:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        facts: SessionRuntimeRepository,
        runtime_ensure: Any,
        runtime_manager: Any,
        replace: Callable[[str], Awaitable[None]],
    ) -> None:
        self._sessions = sessions
        self._facts = facts
        self._ensure = runtime_ensure
        self._runtime_manager = runtime_manager
        self._replace = replace

    async def connect(self, session_id: str) -> ConnectedRuntime:
        async with get_persistence().coordination.runtime_lock(session_id):
            session = await self._sessions.get_session(session_id)
            if (
                session is None
                or session.get("archived_at")
                or session.get("state") in (SessionState.TERMINATED.value, SessionState.DELETED.value)
            ):
                raise RuntimeEnded(session_id)
            if session.get("state") != SessionState.READY.value:
                raise RuntimeNotReady(session_id)
            if not session.get("sandbox_id"):
                if session.get("runtime_unavailable"):
                    raise RuntimeGone(session_id)
                raise RuntimeNotReady(session_id)
            if session.get("sandbox_parked_at"):
                # A parked box wakes for an input and for nothing else.
                if not await self._facts.undelivered_inputs(session_id):
                    raise RuntimeNotReady(session_id)
                await self._runtime_manager.wake_parked_session(session)
                session = await self._sessions.get_session(session_id)
                if session is None:
                    raise RuntimeNotReady(session_id)
            result = await self._ensure.ensure_runtime(session)
            runtime = result.runtime
            if runtime is None:
                if result.sandbox_gone:
                    raise RuntimeGone(session_id)
                raise RuntimeError(result.error_text or f"session {session_id} runtime could not be attached")
            client = runtime.engine_client
            if not isinstance(client, RuntimeClient):
                raise TypeError(f"the {runtime.engine_kind} adapter does not serve the runtime contract")
            return ConnectedRuntime(
                client=client,
                sandbox_id=str(runtime.sandbox_id or session.get("sandbox_id") or ""),
                sandbox_handle=runtime.sandbox,
                runtime_identity=dict(runtime.runtime_identity or {}),
                engine_kind=str(runtime.engine_kind),
                refresh=runtime.prepare_engine_input,
            )

    async def replace(self, session_id: str) -> None:
        """Start a replacement runtime for the waiting inputs, unless one already failed for them.

        Inputs accepted before the last failed replacement get no second
        attempt (:class:`RuntimeStartFailed`); a newer input starts another.
        """
        session = await self._sessions.get_session(session_id) or {}
        failed_at = session.get("runtime_start_failed_at")
        if failed_at:
            waiting = await self._facts.undelivered_inputs(session_id)
            if waiting and all(parse_iso(str(row["created_at"])) <= parse_iso(str(failed_at)) for row in waiting):
                raise RuntimeStartFailed(session_id)
        await self._replace(session_id)

    async def release(self, session_id: str) -> None:
        # The client was this process's only reason to hold the runtime; the
        # box and the engine in it are untouched.
        await self._runtime_manager.evict_runtime(session_id)

    async def sessions_needing_readers(self) -> list[str]:
        return await self._facts.sessions_needing_readers()


def committed_facts(consumers: list[FactConsumer]) -> Callable[[str, FactBatch], Awaitable[None]]:
    """Run every consumer of recorded facts for one batch; one failing consumer does not stop the rest."""

    async def run(session_id: str, batch: FactBatch) -> None:
        for consumer in consumers:
            try:
                await consumer(session_id, batch)
            except Exception:
                logger.exception("session %s fact consumer %r failed", session_id, consumer)

    return run


def reader_hooks(
    *,
    sessions: SessionRepository,
    runtime_manager: Any,
    realign_live_sandbox_owners: Callable[[str], Awaitable[object]],
    committed: Callable[[str, FactBatch], Awaitable[None]],
) -> Callable[[str, ConnectedRuntime], ReaderHooks]:
    def build(session_id: str, connected: ConnectedRuntime) -> ReaderHooks:
        async def prepare(item: RuntimeInput) -> None:
            session = await sessions.get_session(session_id)
            if session is None:
                raise InputPreparationRefused({"code": "SESSION_NOT_FOUND", "message": "session not found"})
            await prepare_input(session, connected, item)
            if connected.refresh is not None:
                await connected.refresh()
            expiry = session.get("expires_at")
            if (
                expiry
                and str(session.get("sandbox_id") or "") == connected.sandbox_id
                and parse_iso(str(expiry)) <= datetime.now(timezone.utc)
            ):
                # A connected box can outlive its stored lease projection.
                # Reuse lifecycle realignment without renewing a healthy lease.
                try:
                    await realign_live_sandbox_owners(connected.sandbox_id)
                except Exception:
                    logger.exception("live lease realignment failed session=%s", session_id)

        recorded_key: list[str | None] = [None]

        async def on_commit(batch: FactBatch) -> None:
            # The runtime names its own conversation once it has one; a
            # replacement resumes that conversation by this key, so it is kept
            # with the session as soon as the runtime reports it.
            key = connected.client.engine_session_key
            if key and key != recorded_key[0]:
                await sessions.compare_and_update_session(
                    session_id,
                    expected={"engine_session_key": {"$ne": key}},
                    updates={"engine_session_key": key},
                    touch_updated_at=False,
                )
                recorded_key[0] = key
            await committed(session_id, batch)

        async def status(value: str) -> None:
            if value in ("working", "waiting"):
                await runtime_manager.maybe_renew_lease_on_activity(session_id)

        async def permission_mode() -> None:
            session = await sessions.get_session(session_id)
            mode = str((session or {}).get("permission_mode") or "").strip()
            setter = getattr(connected.client, "set_permission_mode", None)
            if mode and callable(setter):
                await setter(mode)

        return ReaderHooks(prepare=prepare, committed=on_commit, status=status, permission_mode=permission_mode)

    return build


async def prepare_input(session: dict[str, Any], connected: ConnectedRuntime, item: RuntimeInput) -> None:
    """Run the deployment's turn preparation for one engine write, and deliver its files.

    Refuses the input (:class:`InputPreparationRefused`) when the deployment
    cannot prepare it; a person-actionable refusal carries the provider's
    validated message and URL.
    """
    provider = runtime_preparation_provider()
    if provider is None or not prepares_turns(provider):
        return
    context = TurnPreparationContext(
        sandbox_backend=str(session.get("sandbox_backend") or "").strip().lower(),
        sandbox_id=connected.sandbox_id,
        session_id=str(session["session_id"]),
        principal_id=str(session.get("user_id") or ""),
        turn_id=item.input_id,
        engine_kind=connected.engine_kind,
        dispatch_attempt_id=item.input_id,
        runtime_identity=connected.runtime_identity,
        sandbox_handle=connected.sandbox_handle,
    )
    try:
        backend = sandbox_for_name(context.sandbox_backend)
        if not bool(getattr(backend, "sandbox_is_profile_exclusive", False)):
            raise ValueError("turn preparation requires a profile-exclusive sandbox")
        await asyncio.wait_for(_prepare_and_deliver(provider, context), timeout=PREPARATION_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except PreparationUserActionRequired as exc:
        action = validated_user_action(
            {
                "message": getattr(exc, "message", None),
                "status": getattr(exc, "status", "user_action_required"),
                "url": getattr(exc, "url", None),
            }
        )
        if action is not None:
            raise InputPreparationRefused(
                {"code": TURN_PREPARATION_USER_ACTION_REQUIRED, "message": action["message"], "user_action": action}
            ) from None
        raise InputPreparationRefused(_failure()) from None
    except Exception:
        # Provider errors can carry secrets; the person sees the code only.
        logger.exception("turn preparation failed session=%s input=%s", context.session_id, item.input_id)
        raise InputPreparationRefused(_failure()) from None


def _failure() -> dict[str, Any]:
    return {"code": TURN_PREPARATION_FAILED, "message": "sandbox turn preparation failed"}


async def _prepare_and_deliver(provider: Any, context: TurnPreparationContext) -> None:
    from astrabox.core.service.orchestrator.runtime.preparation import deliver_runtime_files

    materials = await provider.prepare_turn(context=context)
    if not isinstance(materials, RuntimeMaterials) or materials.environment:
        raise ValueError("turn preparation returns files only")
    if materials.files:
        if context.sandbox_handle is None:
            raise ValueError("turn materials require the live sandbox")
        await deliver_runtime_files(
            context.sandbox_handle,
            runtime_identity=context.runtime_identity,
            session_id=context.session_id,
            sandbox_id=context.sandbox_id,
            files=materials.files,
        )
