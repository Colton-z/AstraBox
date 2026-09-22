"""Turn execution: iter_sandbox_events, interrupt."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from astrabox.persistence.repository import MessageRepository, SessionRepository
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, utcnow, utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.model.astrabox_models import validate_transition
from astrabox.core.service.orchestrator.assistant_text import (
    coalesce_assistant_text,
)
from astrabox.core.service.orchestrator.engine_turn import (
    iter_engine_client_events,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineChildResourceReconciler,
    EngineChildRunControl,
    EngineClient,
    EnginePermissionModes,
    bound_engine_client_manifest,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    PrivateDiagnostic,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    pop_engine_frame_scope,
)
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.chunk_processing import (
    is_result_payload as _is_result_payload,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    DeliveryCoordinator,
    JournalDeliveryOutbox,
    consumption_carrier,
    journal_input_rows,
)
from astrabox.core.service.orchestrator.engine.input_content import read_engine_content_blocks
from astrabox.core.service.orchestrator.event_broker import SessionEventBroker
from astrabox.core.service.orchestrator.interaction_funnel import InteractionFunnel
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
    PermissionLifecycle,
)
from astrabox.core.service.orchestrator.runtime_ensure import RuntimeEnsure
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.seams.sandbox import TURN_PREPARATION_FAILED

logger = get_logger(__name__)


# Re-exported from stream_errors: turn_service and the session-kernel turn
# worker classify errors against these same classes (real isinstance
# matching — see that module's docstring for the import cycle this avoids),
# under this file's underscored aliases. RUNTIME_ENSURE_ATTACHED /
# RUNTIME_ENSURE_ATTACH_FAILED / get_machine_id /
# AGENT_CHAT_WAKE_TIMEOUT_SECONDS / AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS
# have no readers left in this file, but must stay re-exported here:
# tests/stream_errors_test.py::test_turn_service_reexports_the_same_object
# imports them from this module by name.
from astrabox.core.service.orchestrator.stream_errors import (
    AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS as _AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS,
    AGENT_CHAT_WAKE_TIMEOUT_SECONDS as _AGENT_CHAT_WAKE_TIMEOUT_SECONDS,
    IncompleteStreamError as _IncompleteStreamError,
    RUNTIME_ENSURE_ATTACHED as _RUNTIME_ENSURE_ATTACHED,
    RUNTIME_ENSURE_ATTACH_FAILED as _RUNTIME_ENSURE_ATTACH_FAILED,
    RUNTIME_ENSURE_BINDING_MISSING as _RUNTIME_ENSURE_BINDING_MISSING,
    RuntimeEnsureResult as _RuntimeEnsureResult,
    get_machine_id as _get_machine_id,
    is_active_sandbox_dispatch_error as _is_active_sandbox_dispatch_error,
    is_incomplete_stream_error as _is_incomplete_stream_error,
    is_recoverable_turn_attach_error as _is_recoverable_turn_attach_error,
    is_sandbox_gone_error as _is_sandbox_gone_error,
    normalize_pending_interaction as _normalize_pending_interaction,
    pending_interaction_matches_turn as _pending_interaction_matches_turn,
)


class TurnService:
    async def _transition(
        self,
        session_id: str,
        state: str,
        updates: dict[str, Any] | None = None,
        *,
        event_extra: dict[str, Any] | None = None,
        from_state: str | None = None,
    ) -> None:
        """Atomic state transition: DB write + broker publish (best-effort)."""
        if from_state is not None and not validate_transition(from_state, state):
            logger.warning("invalid state transition session=%s %s->%s", session_id, from_state, state)
        merged = {**(updates or {}), "state": state}
        await self._sessions_repo.update_session(session_id, merged)
        with contextlib.suppress(Exception):
            event: dict[str, Any] = {"type": "status", "state": state, "at": utcnow_iso()}
            if event_extra:
                event.update(event_extra)
            await self._broker.publish(session_id, event)

    def __init__(
        self,
        *,
        sessions_repo: SessionRepository,
        messages_repo: MessageRepository,
        agent_config: AgentConfigService,
        runtime_manager: RemoteAgentRuntimeManager,
        broker: SessionEventBroker,
        must_get_owned_session,
        interaction_snapshots_repo=None,
        session_snapshots_repo=None,
        session_events_repo=None,
        message_view=None,
        agent_repo=None,
        agent_service_getter: Callable[[], Any] | None = None,
        assistant_workspace_service: Any | None = None,
        sandbox_lifecycle_service: Any,
    ) -> None:
        # messages_repo / agent_service_getter are accepted (platform_service.py's
        # wiring passes both by keyword) but not stored: neither attribute is
        # read anywhere in this file or externally.
        self._sessions_repo = sessions_repo
        self._agent_config = agent_config
        self._runtime_manager = runtime_manager
        self._broker = broker
        self._must_get_owned_session = must_get_owned_session
        self._interaction_snapshots_repo = interaction_snapshots_repo
        self._session_snapshots_repo = session_snapshots_repo
        self._session_events_repo = session_events_repo
        if message_view is None:
            raise TypeError("TurnService requires the canonical session message view")
        self._message_view = message_view
        self._agent_repo = agent_repo
        self._assistant_workspace_service = assistant_workspace_service
        self._permission_lifecycle = PermissionLifecycle(
            sessions_repo=sessions_repo,
            apply_engine_permission_mode=self.set_engine_permission_mode,
            session_events_repo=session_events_repo,
            session_snapshots_repo=session_snapshots_repo,
            interaction_snapshots_repo=interaction_snapshots_repo,
        )
        self._interaction_funnel = InteractionFunnel(
            interaction_snapshots_repo=interaction_snapshots_repo,
            session_snapshots_repo=session_snapshots_repo,
        )
        self._runtime_ensure = RuntimeEnsure(
            runtime_manager=runtime_manager,
            sessions_repo=sessions_repo,
            agent_config=agent_config,
            agent_repo=agent_repo,
            permission_lifecycle=self._permission_lifecycle,
            assistant_workspace_service=assistant_workspace_service,
            sandbox_lifecycle_service=sandbox_lifecycle_service,
            has_turn_dispatch_permission_context=self._has_turn_dispatch_permission_context,
        )
        self._input_delivery_locks: dict[str, asyncio.Lock] = {}

    async def _build_runtime_unavailable_events(
        self,
        *,
        session: dict[str, Any],
        turn_id: str,
        ensure_result: _RuntimeEnsureResult,
    ) -> list[dict[str, Any]]:
        session_id = str(session.get("session_id") or "").strip()
        ensure_status = str(ensure_result.status or "").strip()
        logger.warning(
            "runtime unavailable after ensure session=%s turn=%s status=%s err=%s",
            session_id,
            turn_id,
            ensure_status or "<unknown>",
            str(ensure_result.error_text or "").strip() or "<none>",
        )

        is_agent_session = str(session.get("session_kind") or "") == "agent_chat"
        if is_agent_session and ensure_status == _RUNTIME_ENSURE_BINDING_MISSING:
            return [
                {
                    "type": "error",
                    "turn_id": turn_id,
                    "code": "AGENT_REPROVISIONING",
                    "message": "the agent sandbox is restarting, please retry",
                },
                {
                    "type": "status",
                    "state": SessionState.READY.value,
                    "runtime_warning": True,
                    "at": utcnow_iso(),
                    "turn_id": turn_id,
                },
            ]
        expires_at_str = session.get("expires_at")
        sandbox_expired = False
        if expires_at_str:
            try:
                sandbox_expired = parse_iso(expires_at_str) <= utcnow()
            except (ValueError, TypeError):
                pass

        if sandbox_expired:
            return [
                {
                    "type": "error",
                    "turn_id": turn_id,
                    "code": "AGENT_RUNTIME_ERROR",
                    "message": "runtime reconnect failed, sandbox expired — create a new session",
                },
                {
                    "type": "status",
                    "state": SessionState.TERMINATED.value,
                    "at": utcnow_iso(),
                    "turn_id": turn_id,
                },
            ]

        return [
            {
                "type": "error",
                "turn_id": turn_id,
                "code": "AGENT_RUNTIME_ERROR",
                "message": "runtime reconnect failed, recover and retry",
            },
            {
                "type": "status",
                "state": SessionState.READY.value,
                "runtime_warning": True,
                "at": utcnow_iso(),
                "turn_id": turn_id,
            },
        ]

    # ── Interaction funnel ────────────────────────────────────────────────

    # InteractionFunnel owns engine-neutral request gating. Engine-specific
    # permission callbacks live inside each adapter/runtime and answers return
    # through EngineClient.submit_interaction_response.

    _resolve_interaction_permission_mode = staticmethod(
        InteractionFunnel._resolve_interaction_permission_mode
    )
    _has_turn_dispatch_permission_context = staticmethod(
        InteractionFunnel._has_turn_dispatch_permission_context
    )

    # Delegate to raw SDK event helpers.
    _serialize_message = staticmethod(InteractionFunnel._serialize_message)
    _extract_partial_text = staticmethod(InteractionFunnel._extract_partial_text)

    async def _resolve_turn_request(
        self,
        *,
        session: dict[str, Any],
        content: str,
        content_blocks: list[dict[str, Any]] | None,
        interaction_response: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        return await self._interaction_funnel._resolve_turn_request(
            session=session,
            content=content,
            content_blocks=content_blocks,
            interaction_response=interaction_response,
        )

    async def iter_sandbox_events(
        self,
        *,
        user: UserContext | None = None,
        session: dict[str, Any],
        session_id: str,
        content: str,
        turn_id: str,
        command_id: str | None = None,
        interaction_response: dict[str, Any] | None = None,
        permission_mode: str | None = None,
        client_message_id: str | None = None,
        delivery_command: dict[str, Any] | None = None,
        on_query_committed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_timing: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield sandbox communication events for a single turn.

        Pure sandbox I/O: query + receive_messages.  No turn lock, no
        heartbeat, no frame writes, no message persistence.  The caller
        (the kernel turn worker) owns all state management.
        """
        timing_start = time.monotonic()
        timing_last = timing_start

        def _emit_timing(stage: str, **fields: Any) -> None:
            nonlocal timing_last
            if on_timing is None:
                return
            now = time.monotonic()
            payload = {
                "stage": stage,
                "elapsed_ms": round((now - timing_start) * 1000, 3),
                "delta_ms": round((now - timing_last) * 1000, 3),
                **fields,
            }
            timing_last = now
            on_timing(stage, payload)

        # 1. Resolve turn request
        effective_content, answered_pending_interaction = await self._resolve_turn_request(
            session=session,
            content=content,
            content_blocks=read_engine_content_blocks(
                delivery_command.get("content_blocks") if delivery_command is not None else None
            ),
            interaction_response=interaction_response,
        )
        _emit_timing(
            "turn_service.resolve_turn_request",
            answered_interaction=answered_pending_interaction is not None,
        )
        interaction_permission_mode = self._resolve_interaction_permission_mode(
            answered_pending_interaction,
            interaction_response,
        )

        # A sandbox-owned answered interaction reaching this generator is the
        # answer continuation: the external funnel is closed at the dispatch
        # gate (any ai-stream dispatch carrying interaction_response is
        # refused with ENGINE_ANSWER_VIA_ANSWER_COMMAND before a command
        # exists), so the only caller left is the AnswerInteraction worker,
        # whose segment re-enters the parked engine stream below.

        # 3. Validate session state
        # Shared-runtime sessions (agent_chat, assistant_chat) defer termination
        # decisions to the binding resolver in _ensure_runtime_for_session().
        state = str(session.get("state", ""))
        if state in {SessionState.DELETED.value, SessionState.TERMINATED.value}:
            session_kind = str(session.get("session_kind") or "").strip()
            if session_kind not in {"agent_chat", "assistant_chat"}:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="session terminated, create a new session",
                    status_code=409,
                )
        if state == "PROCESSING":
            raise APIError(code="SESSION_BUSY", message="session is busy", status_code=409)
        if state == SessionState.CREATING.value:
            raise APIError(
                code="SESSION_BUSY",
                message="session is still creating, please retry",
                status_code=409,
            )

        _emit_timing(
            "turn_service.validate_and_permission",
            permission_requested=permission_mode is not None,
        )

        # 7. Handle answered pending interaction (clear pending_interaction)
        if answered_pending_interaction is not None:
            interaction_id = str(answered_pending_interaction.get("interaction_id") or "").strip()
            if interaction_id:
                cleared = False
                with contextlib.suppress(Exception):
                    cleared = await self._sessions_repo.clear_pending_interaction(
                        session_id, interaction_id=interaction_id,
                    )
                if not cleared:
                    with contextlib.suppress(Exception):
                        await self._sessions_repo.update_session(
                            session_id, {"pending_interaction": None},
                        )
                session["pending_interaction"] = None
            if interaction_permission_mode:
                await self._permission_lifecycle.ensure_before_turn_dispatch(
                    session_id=session_id,
                    session=session,
                    turn_id=turn_id,
                    command_id=command_id or interaction_id or turn_id,
                    requested_mode=interaction_permission_mode,
                    runtime=self._runtime_manager.get_runtime(
                        session_id,
                        sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
                    ),
                    source="interaction_answer",
                    command_type="AnswerInteraction",
                    interaction_id=interaction_id or None,
                )
            _emit_timing("turn_service.clear_pending_interaction")

        # 8. Get/create runtime. Keep this as the single runtime authority so
        # agent_chat turns cannot be short-circuited by a stale in-memory
        # conversation runtime after the owning agent wakes on a new sandbox.
        try:
            ensure_result = await self._ensure_runtime_for_session(
                session,
                user=user,
                turn_id=turn_id,
                command_id=command_id,
                requested_permission_mode=permission_mode,
            )
        except APIError as exc:
            yield {
                "type": "error",
                "turn_id": turn_id,
                "code": str(exc.code or "AGENT_RUNTIME_ERROR"),
                "message": str(exc.message or exc),
            }
            return
        except Exception as exc:
            yield {
                "type": "error",
                "turn_id": turn_id,
                "code": "AGENT_RUNTIME_ERROR",
                "message": str(exc),
            }
            return
        session = ensure_result.session or session
        runtime = ensure_result.runtime
        _emit_timing(
            "turn_service.ensure_runtime",
            runtime_available=runtime is not None,
            ensure_status=str(getattr(ensure_result, "status", "") or ""),
        )

        # 9. No runtime available
        if runtime is None:
            for event in await self._build_runtime_unavailable_events(
                session=session,
                turn_id=turn_id,
                ensure_result=ensure_result,
            ):
                yield event
            return

        if delivery_command is not None and user is None:
            raise RuntimeError(
                "a delivery command requires the accepting user context to "
                "redeliver against the ensured runtime"
            )
        if delivery_command is not None and user is not None:
            try:
                delivery_command = await self.refresh_delivery_for_stream(
                    user=user,
                    session=session,
                    session_id=session_id,
                    permission_mode=permission_mode,
                    delivery_command=delivery_command,
                )
            except APIError as exc:
                yield {
                    "type": "error",
                    "turn_id": turn_id,
                    "code": str(exc.code or "AGENT_RUNTIME_ERROR"),
                    "message": str(exc.message or exc),
                }
                return

        # Engine dispatch — every adapter implements the EngineClient turn
        # contract.  It is the minimum seam, not an optional capability; a
        # second dispatch path would make plugins inherit whichever vendor the
        # fallback happened to encode.
        # An answered interaction reaches this generator only through the
        # AnswerInteraction worker command.  The answer was already delivered
        # through submit_interaction_response, so this segment resumes the parked engine
        # stream instead of beginning a second turn.
        async for engine_event in self._iter_engine_client_events(
            session=session,
            session_id=session_id,
            effective_content=effective_content,
            turn_id=turn_id,
            runtime=runtime,
            interaction_permission_mode=interaction_permission_mode,
            on_query_committed=on_query_committed,
            emit_timing=_emit_timing,
            client_message_id=client_message_id,
            delivery_command=delivery_command,
            answer_continuation=answered_pending_interaction is not None,
        ):
            yield engine_event
        return

    async def _iter_engine_client_events(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
        effective_content: str,
        turn_id: str,
        runtime: Any,
        interaction_permission_mode: str | None,
        on_query_committed: Callable[[dict[str, Any]], Awaitable[None]] | None,
        emit_timing: Callable[..., None],
        client_message_id: str | None,
        delivery_command: dict[str, Any] | None = None,
        answer_continuation: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Engine-client path: bind, attach a durable FIFO input, then stream.

        Thin delegator onto the module-level generator in engine_turn.py
        (see that module's docstring) — kept at this attribute path because
        iter_sandbox_events calls it by name at its capability fork.
        """
        async for event in iter_engine_client_events(
            session=session,
            session_id=session_id,
            effective_content=effective_content,
            turn_id=turn_id,
            runtime=runtime,
            interaction_permission_mode=interaction_permission_mode,
            on_query_committed=on_query_committed,
            emit_timing=emit_timing,
            client_message_id=client_message_id,
            delivery_command=delivery_command,
            sessions_repo=self._sessions_repo,
            broker=self._broker,
            answer_continuation=answer_continuation,
            # Only a continuation needs it, and only it pays for the read: the
            # engine turn id the parked stream is still running under lives
            # here, not in the memory of the process that started it.
            parked_engine_anchor=(
                (await self._session_snapshots_repo.get_snapshot(session_id) or {}).get(
                    "current_turn_engine_anchor"
                )
                if answer_continuation
                else None
            ),
        ):
            yield event

    # ── Runtime ensure ───────────────────────────────────────────────────

    # The implementation lives in the RuntimeEnsure collaborator
    # (astrabox/core/service/orchestrator/runtime_ensure.py), constructed in
    # __init__ next to PermissionLifecycle. The methods below stay as thin
    # delegators on this class because callers reach them by name on the turn
    # service: session_kernel/service_mixins/reconciliation.py and
    # session_kernel/service_mixins/durable_recovery_assistant.py both look up
    # ``_ensure_runtime_lightweight_for_session`` with getattr.

    async def _ensure_runtime_for_session(
        self,
        session: dict[str, Any],
        *,
        user: UserContext | None = None,
        turn_id: str | None = None,
        command_id: str | None = None,
        requested_permission_mode: str | None = None,
    ) -> _RuntimeEnsureResult:
        return await self._runtime_ensure._ensure_runtime_for_session(
            session,
            user=user,
            turn_id=turn_id,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
        )

    async def ensure_runtime_for_input_delivery(
        self,
        session: dict[str, Any],
        *,
        user: UserContext,
        command_id: str,
        requested_permission_mode: str | None = None,
    ) -> Any:
        """Resolve the resident engine client used by the independent FIFO."""

        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError("input delivery requires session_id")
        await self._runtime_manager.maybe_renew_lease_on_activity(session_id)
        ensured = await self._ensure_runtime_for_session(
            session,
            user=user,
            command_id=command_id,
            requested_permission_mode=requested_permission_mode,
        )
        if ensured.runtime is None:
            # A runtime this platform could not attach is a state it knows, not
            # an internal fault: raising here rendered a 500 whose body said
            # `unexpected RuntimeError`, which tells a caller neither what
            # happened nor whether retrying is worth anything. A sandbox the
            # provider reports absent is retryable, because losing it already
            # armed the re-borrow the next attempt takes.
            gone = bool(getattr(ensured, "sandbox_gone", False))
            raise APIError(
                code="SANDBOX_GONE" if gone else "AGENT_RUNTIME_ERROR",
                message=(
                    "the sandbox this conversation was bound to no longer "
                    "exists; a replacement is being prepared — retry"
                    if gone
                    else "runtime reconnect failed, recover and retry"
                ),
                status_code=409,
                data={
                    "session_id": session_id,
                    "sandbox_gone": gone,
                    "detail": str(ensured.error_text or "").strip() or None,
                },
            )
        # A cold process had no resident runtime for the pre-attach renewal
        # above. Renew again after attach so transport rehydration cannot send
        # the input while leaving the provider lease and durable expiry stale.
        # On a warm process the first call populated the runtime expiry and this
        # second call is the manager's thresholded no-op.
        await self._runtime_manager.maybe_renew_lease_on_activity(session_id)
        return ensured.runtime

    async def pending_input_rows(
        self,
        session_id: str,
        *,
        current_carrier: str | None = None,
    ) -> list[dict[str, Any]]:
        return await journal_input_rows(
            self._session_events_repo, session_id, current_carrier=current_carrier
        )

    async def refresh_delivery_for_stream(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
        permission_mode: str | None,
        delivery_command: dict[str, Any],
    ) -> dict[str, Any]:
        """Remake a worker's delivery judgement against the stream's runtime.

        The worker computes ``delivery_command`` before the stream runs,
        against whatever runtime exists THEN. The stream's own ensure is
        allowed to have replaced that runtime — a box lost between accept and
        stream cost 25 seconds of re-borrow in the run that bit — and a
        judgement made against a dead generation reads its own consumption
        receipt as "done" and starves the new engine. Redeliver whatever the
        current carrier's projection holds open (idempotent at every engine
        client and at the runner), and let the open rows say whether this
        command's consumption still stands.
        """

        command_id = str(delivery_command.get("command_id") or "").strip()
        delivery_runtime = await self.deliver_pending_inputs(
            user=user,
            session=session,
            session_id=session_id,
            requested_command_id=command_id,
            permission_mode=permission_mode,
        )
        open_rows = await self.pending_input_rows(
            session_id,
            current_carrier=consumption_carrier(
                getattr(delivery_runtime, "sandbox_id", None),
                getattr(delivery_runtime, "isolated_session_id", None),
            ),
        )
        return {
            **delivery_command,
            "consumption_confirmed": not any(
                str(row.get("command_id") or "").strip() == command_id
                for row in open_rows
            ),
        }

    async def deliver_pending_inputs(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
        requested_command_id: str,
        permission_mode: str | None,
    ) -> Any:
        """Replay the journal outbox into the resident engine in strict order."""

        lock = self._input_delivery_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # Another delivery may replace the sandbox while this caller waits.
            # Refresh its Session in place so the later stream uses that binding
            # too, rather than reconnecting the box from before the lock.
            current_session = await self._sessions_repo.get_session(session_id)
            if not isinstance(current_session, dict):
                raise APIError(
                    code="SESSION_NOT_FOUND",
                    message="session not found",
                    status_code=404,
                )
            session.update(current_session)
            runtime = await self.ensure_runtime_for_input_delivery(
                session,
                user=user,
                command_id=requested_command_id,
                requested_permission_mode=permission_mode,
            )
            engine_client = getattr(runtime, "engine_client", None)
            if not isinstance(engine_client, EngineClient):
                raise RuntimeError(
                    "runtime engine client does not implement the mandatory "
                    "conversation FIFO"
                )
            outbox = JournalDeliveryOutbox(
                self._session_events_repo,
                session_id=session_id,
                # The ensured runtime is the carrier this delivery feeds; a
                # consumption receipt signed by a different, dead generation
                # reopens its row so this carrier gets the input again.
                current_carrier=consumption_carrier(
                    getattr(runtime, "sandbox_id", None),
                    getattr(runtime, "isolated_session_id", None),
                ),
            )
            delivered = await DeliveryCoordinator(outbox).deliver_pending(
                session_id,
                engine_client,
            )
            if requested_command_id not in delivered:
                existing_delivery = await self._session_events_repo.list_events(
                    session_id,
                    event_type="input.delivered",
                    causation_id=requested_command_id,
                    limit=1,
                )
                existing_consumption = await self._session_events_repo.list_events(
                    session_id,
                    event_type="input.consumed",
                    causation_id=requested_command_id,
                    limit=1,
                )
                if not existing_delivery and not existing_consumption:
                    raise RuntimeError(
                        "engine conversation FIFO did not accept delivery "
                        f"{requested_command_id!r}"
                    )
            return runtime

    async def _ensure_runtime_lightweight_for_session(self, session: dict[str, Any]):
        return await self._runtime_ensure._ensure_runtime_lightweight_for_session(session)

    async def acquire_engine_control_runtime(
        self,
        session: dict[str, Any],
        *,
        operation: str,
    ) -> Any:
        """Attach and validate the one runtime used by every engine control."""

        runtime = await self._runtime_ensure.acquire_engine_control_runtime(session)
        engine_client = getattr(runtime, "engine_client", None)
        if engine_client is None:
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message=(
                    f"engine control {operation!r} attached a runtime without "
                    "an engine client"
                ),
                status_code=409,
            )
        try:
            bound_engine_client_manifest(runtime)
        except TypeError as exc:
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message=str(exc),
                status_code=502,
            ) from exc
        return runtime

    async def stop_engine_child_run(
        self,
        session: dict[str, Any],
        control_ref: str,
    ) -> None:
        """Stop one child on the exact runtime acquired for this control."""

        runtime = await self.acquire_engine_control_runtime(
            session,
            operation="stop child run",
        )
        manifest = bound_engine_client_manifest(runtime)
        client = runtime.engine_client
        if not manifest.supports_child_run_control:
            raise APIError(
                code="ENGINE_CAPABILITY_UNAVAILABLE",
                message=(
                    f"engine_kind={manifest.engine_kind!r} does not support "
                    "child-run control"
                ),
                status_code=409,
            )
        if not isinstance(client, EngineChildRunControl):
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message=(
                    f"engine_kind={manifest.engine_kind!r} declared child-run "
                    "control without implementing it"
                ),
                status_code=502,
            )
        await client.stop_child_run(control_ref)

    async def reconcile_engine_child_resources(
        self,
        session: dict[str, Any],
    ) -> bool:
        """Refresh and persist one adapter's authoritative child-resource facts."""

        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            raise APIError(
                code="ENGINE_RUNTIME_UNAVAILABLE",
                message="child-resource reconcile requires a session_id",
                status_code=409,
            )
        if str(session.get("state") or "").strip() in {
            SessionState.CREATING.value,
            SessionState.TERMINATED.value,
            SessionState.DELETED.value,
        } or not str(session.get("sandbox_id") or "").strip():
            return False

        engine_kind = resolve_session_engine_kind(session)
        client_type = get_engine_adapter(engine_kind).engine_client_type
        if not callable(getattr(client_type, "reconcile_child_resources", None)):
            return False

        runtime = await self.acquire_engine_control_runtime(
            session,
            operation="reconcile child resources",
        )
        client = runtime.engine_client
        if not isinstance(client, EngineChildResourceReconciler):
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message=(
                    f"engine_kind={engine_kind!r} declares child-resource "
                    "reconcile without implementing it"
                ),
                status_code=502,
            )

        emissions = await client.reconcile_child_resources()
        child_frames: list[tuple[dict[str, Any], int | None]] = []
        diagnostics: list[PrivateDiagnostic] = []
        for emission in emissions:
            if isinstance(emission, ChildResourceFact):
                payload = emission.as_frame()
                if pop_engine_frame_scope(payload) != "session":
                    raise RuntimeError(
                        "child-resource reconcile returned a non-Session fact"
                    )
                child_frames.append((payload, emission.engine_sequence_number))
                continue
            if isinstance(emission, PrivateDiagnostic):
                diagnostics.append(emission)
                continue
            raise TypeError(
                "child-resource reconcile crossed the seam with unsupported "
                f"output: {type(emission).__name__}"
            )

        if child_frames:
            frame_seq = await self._session_events_repo.allocate_session_frame_seq(
                session_id,
                count=len(child_frames),
            )
            docs: list[dict[str, Any]] = []
            for payload, engine_sequence_number in child_frames:
                doc: dict[str, Any] = {
                    "session_id": session_id,
                    "turn_id": None,
                    "scope": "session",
                    "command_id": None,
                    "source_kind": "engine_child_reconcile",
                    "frame_seq": int(frame_seq),
                    "payload": payload,
                    "engine_kind": engine_kind,
                    "created_at": utcnow_iso(),
                }
                if engine_sequence_number is not None:
                    doc["engine_sequence_number"] = engine_sequence_number
                docs.append(doc)
                frame_seq += 1
            append_frames = getattr(self._session_events_repo, "append_frames", None)
            if callable(append_frames):
                await append_frames(docs)
            else:
                for doc in docs:
                    await self._session_events_repo.append_frame(doc)

        for diagnostic in diagnostics:
            await self._session_events_repo.append_event(
                {
                    "session_id": session_id,
                    "channel": "engine",
                    "turn_id": None,
                    "event_type": "engine.diagnostic",
                    "causation_id": f"child-reconcile:{uuid.uuid4()}",
                    "correlation_id": None,
                    "payload": {
                        "engine_kind": engine_kind,
                        "event_type": diagnostic.event_type,
                        "subtype": diagnostic.subtype,
                        "raw": diagnostic.raw,
                    },
                }
            )
        return True

    async def interrupt_engine_turn(self, session: dict[str, Any]) -> None:
        """Interrupt the active turn on the exact acquired engine client."""

        runtime = await self.acquire_engine_control_runtime(
            session,
            operation="interrupt turn",
        )
        runtime.interrupting = True
        accepted = await runtime.engine_client.interrupt_active_turn()
        if not accepted:
            logger.info(
                "interrupt found no active engine turn session=%s — treating as settled",
                str(session.get("session_id") or "").strip(),
            )

    async def set_engine_permission_mode(
        self,
        session: dict[str, Any],
        permission_mode: str,
    ) -> bool:
        """Apply one adapter-owned permission mode after durable reattachment."""

        mode = str(permission_mode or "").strip()
        if not mode:
            raise APIError(
                code="INVALID_REQUEST",
                message="permission_mode is required",
                status_code=400,
            )
        runtime = await self.acquire_engine_control_runtime(
            session,
            operation="set permission mode",
        )
        manifest = bound_engine_client_manifest(runtime)
        client = runtime.engine_client
        if not manifest.permission_modes:
            raise APIError(
                code="ENGINE_CAPABILITY_UNAVAILABLE",
                message=(
                    f"engine_kind={manifest.engine_kind!r} has no "
                    "permission-mode capability"
                ),
                status_code=409,
            )
        if mode not in manifest.permission_modes:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"permission_mode={mode!r} is not supported by "
                    f"engine_kind={manifest.engine_kind!r}; "
                    f"available={manifest.permission_modes!r}"
                ),
                status_code=400,
            )
        if not isinstance(client, EnginePermissionModes):
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message=(
                    f"engine_kind={manifest.engine_kind!r} declared permission "
                    "modes without implementing them"
                ),
                status_code=502,
            )
        await client.set_permission_mode(mode)
        runtime.permission_mode = mode
        runtime.permission_mode_verified = True
        return True

    # Serialization helpers delegated to raw SDK event helpers via
    # class-level staticmethod assignments above.
