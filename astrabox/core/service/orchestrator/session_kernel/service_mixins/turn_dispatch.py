"""Conversation input, interaction and lifecycle commands.

Session output consumption is inherited from SessionOutputSubscriptionMixin,
which serves Web and every installed channel provider.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.base import (
    EngineInteractions,
    EngineStreamDetached,
    bound_engine_client_manifest,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    InputDeliveryRefused,
    input_answered_in_frames,
    refusal_record,
    settled_input_outcome,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    validate_interaction_response,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    build_turn_active_snapshot_updates,
    derive_turn_recovery_phase,
    resident_engine_turn_id,
    settle_parked_turn,
    turn_terminal_frame_matches,
)
from astrabox.core.service.orchestrator.session_kernel.workers import (
    WorkerWakeup,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _ACTIVE_SESSION_STATES,
    _ACTIVE_CONVERSATION_SNAPSHOT_STATES,
    _ACTIVE_TERMINAL_SNAPSHOT_STATES,
)

from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_output import (
    SessionOutputSubscriptionMixin,
)

logger = get_logger(__name__)

#: The engine control path's terminal absence signals for an interaction wait:
#: its runtime is not attached, its sandbox is confirmed gone, or the runner
#: already let the wait expire. They mean the same thing to a superseding input
#: — there is no engine left to record a refusal — and nothing else does.
_SUPERSEDE_ABANDON_CODES = frozenset(
    {"ENGINE_RUNTIME_UNAVAILABLE", "INTERACTION_EXPIRED", "SANDBOX_GONE"}
)

#: Strong references to answer-continuation workers still running after the
#: answer POST returned. Without this the event loop is the only owner of
#: those tasks and may collect them mid-flight (same shape as the sandbox
#: destroy guards' _RELEASES_IN_FLIGHT).
_ANSWER_CONTINUATIONS: set[asyncio.Task[Any]] = set()


class TurnDispatchStreamingMixin(SessionOutputSubscriptionMixin):
    """Conversation write + live streaming surface for :class:`SessionKernelService`."""

    # Tier-2 convergence for PROCESSING/STREAMING snapshots is handled
    # exclusively by ReconcileWorker (periodic stale scan); GET does not wake
    # Tier-2. The write path (stream_message_events_ds) runs _reconcile_stuck_turn
    # inline before starting a new turn.

    async def dispatch_turn_input(
        self,
        user: UserContext,
        session_id: str,
        content: str,
        *,
        content_blocks: list[dict[str, Any]] | None = None,
        interaction_response: dict[str, Any] | None = None,
        permission_mode: str | None = None,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Accept a turn and return a receipt. Delivers nothing.

        Sending and receiving are separate channels. This one admits the input
        — validate, journal the command, pre-project the user message, spawn
        the worker — and answers with the identifiers the caller needs to
        recognise the turn when its frames arrive on the session's stream.

        The coupled alternative, :meth:`stream_ai_stream`, makes one request do
        both: the POST that starts a turn is also the response body that
        carries it. That ties an HTTP request's lifetime to an engine's
        segment, so a turn that pauses for a tool permission ends the stream
        and the client has to decide when to open the next one.
        """
        session = await self._must_get_projection_backed_session(
            user,
            session_id,
            reconcile_conversation=False,
            internal_wiring=True,
        )
        # A message sent again under the client_message_id of one whose turn
        # already ended before delivery gets that outcome back, before anything
        # below starts recovering a runtime for a turn that will not run.
        if str(client_message_id or "").strip():
            settled = await settled_input_outcome(
                self._session_events_repo,
                session_id,
                self._input_command_id(session_id, str(client_message_id)),
            )
            if settled is not None:
                raise settled
        # A missing runtime can be rebuilt only for a conversation still open
        # to input. Recovery must not erase an explicit lifecycle termination
        # before the ordinary turn guard gets to see it.
        self._require_turn_eligible(session, channel="conversation", reject_creating=False)
        # Reconcile before recovery, not after it: a dead pre-write turn
        # (PROCESSING + no anchor + no lease) must be cleaned up so that
        # frontend retries can start a fresh turn. Recovery derives its
        # eligibility from the same conversation_state, and refuses PROCESSING
        # — so a conversation whose box died mid-turn met that refusal on every
        # retry, as a non-retryable client error, until the periodic reconciler
        # settled the turn seconds later. Settle first, then recover.
        snapshot = await self._get_kernel_session_snapshot(session_id)
        if isinstance(snapshot, dict):
            await self._reconcile_stuck_turn(
                session_id=session_id,
                session=session,
                snapshot=snapshot,
            )
        # A conversation whose runtime subject is unavailable re-enters the
        # common startup path before dispatch. RuntimeSubjectCoordinator decides
        # whether that means recreating a Session allocation or recovering and
        # reattaching a longer-lived owner such as an Assistant workspace.
        #
        # Two triggers, both resolved before dispatch:
        #   1. runtime_unavailable: durable "compute is gone" signal, set by a prior
        #      reclaim/terminate or by a SANDBOX_GONE turn that hit a killed sandbox.
        #   2. a lapsed stored lease (expires_at <= now): recovery checks supplier
        #      liveness before choosing attachment or replacement.
        #   3. an Assistant conversation whose box is confirmed gone
        #      (_bound_subject_sandbox_gone).
        # None keys on a terminal session state: reclaim/expiry keep the
        # conversation wakeable (state READY). DELETED stays final (handled by the
        # delete guard below).
        if str(session.get("state") or "") != SessionState.DELETED.value and (
            bool(session.get("runtime_unavailable"))
            or self._bound_runtime_lease_expired(session)
            or await self._bound_subject_sandbox_gone(session)
        ):
            await self.recover_session(user, session_id)
            # recover_session recreates the sandbox asynchronously (it spawns
            # the startup worker as a background task and returns while the
            # session is still CREATING); wait for the rebuilt sandbox to reach
            # READY before starting the turn, otherwise the reject_creating
            # guard below fails the turn with SESSION_BUSY.
            session = await self._await_runtime_subject_rebuild_ready(user, session_id)
        self._require_turn_eligible(session, channel="conversation")
        if interaction_response:
            # Engine-client interactions are answered through the
            # interaction-answer command (answer_pending_interaction →
            # engine_client.submit_interaction_response): a live side-channel into the
            # original turn's worker. Carrying the answer on a new turn
            # dispatch is refused loudly rather than silently starting one.
            raise APIError(
                code="ENGINE_ANSWER_VIA_ANSWER_COMMAND",
                message=(
                    "interaction answers go through the interaction-answer "
                    f"command, not a turn dispatch (session={session_id})"
                ),
                status_code=409,
            )
        return await self._dispatch_active_input_queue(
            user=user,
            session=session,
            session_id=session_id,
            content=content,
            content_blocks=content_blocks,
            permission_mode=permission_mode,
            client_message_id=client_message_id,
        )

    async def _bound_subject_sandbox_gone(self, session: dict[str, Any]) -> bool:
        """Whether a subject-owned box this conversation names is confirmed gone.

        An Agent conversation that finds its box gone while its input is being
        delivered borrows a replacement inside that delivery. An Assistant's box
        is its workspace's, and only the workspace's startup authority replaces
        it: that authority re-materializes the box for every conversation of
        the Assistant and resets this conversation to CREATING, which it may do
        only while no turn is accepted. So an Assistant conversation learns a
        lost box here, before admission, and the recovery below replaces it and
        this message is answered on the replacement. Learnt during delivery
        instead, the message was refused as SANDBOX_GONE and the user had to
        send it again.
        """

        if (
            self._runtime_subjects.recovery_action_for(session)
            != "restart_session_on_subject"
        ):
            return False
        return await self._turn_service.bound_sandbox_confirmed_gone(session)

    async def stream_ai_stream(
        self,
        user: UserContext,
        session_id: str,
        content: str,
        *,
        interaction_response: dict[str, Any] | None = None,
        permission_mode: str | None = None,
        client_message_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Dispatch a turn and carry it on this response — the coupled path.

        Kept for callers that still submit and receive on one request. The
        console uses :meth:`dispatch_turn_input` plus terminal-bounded session
        subscription responses from :meth:`follow_session_stream`.
        """
        receipt = await self.dispatch_turn_input(
            user,
            session_id,
            content,
            interaction_response=interaction_response,
            permission_mode=permission_mode,
            client_message_id=client_message_id,
        )
        command_id = str(receipt["command_id"])
        async for frame in self.resume_command_stream(session_id, command_id=command_id):
            yield frame

    async def resume_command_stream(
        self, session_id: str, *, command_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Follow an accepted command without submitting or delivering input again."""
        command_event = await self._session_events_repo.get_command_event(
            session_id,
            command_id=command_id,
        )
        turn_id = str((command_event or {}).get("turn_id") or "").strip()
        if not turn_id:
            raise RuntimeError(
                f"accepted command has no journal turn_id command_id={command_id!r}"
            )
        async for frame in self._tail_frames(
            session_id,
            command_id=command_id,
            producer_task=self._accepted_turn_producers.get(command_id),
            include_terminal_resume_cursor=False,
        ):
            yield frame
        # One chokepoint covers every stream-tail exit: durable finish, live
        # terminal drain, or a bridge ending before terminal. Keep the body open
        # until the snapshot makes the turn slot admissible for the next send.
        await self._tail_await_turn_slot_settled(session_id, turn_id=turn_id)

    @staticmethod
    def _new_turn_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _input_command_id(session_id: str, client_message_id: str) -> str:
        normalized = str(client_message_id or "").strip()
        if not normalized:
            raise APIError(
                code="INVALID_REQUEST",
                message="client_message_id is required",
                status_code=400,
            )
        return f"{session_id}:{normalized}"

    @staticmethod
    def _input_id(session_id: str, client_message_id: str) -> str:
        normalized = str(client_message_id or "").strip()
        if not normalized:
            raise APIError(
                code="INVALID_REQUEST",
                message="client_message_id is required",
                status_code=400,
            )
        try:
            return str(uuid.UUID(normalized))
        except ValueError:
            try:
                session_namespace = uuid.UUID(str(session_id or "").strip())
            except ValueError as exc:
                raise RuntimeError(
                    "session_id must be a valid UUID"
                ) from exc
            return str(uuid.uuid5(session_namespace, normalized))

    async def _dispatch_active_input_queue(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
        content: str,
        content_blocks: list[dict[str, Any]] | None,
        permission_mode: str | None,
        client_message_id: str | None,
    ) -> dict[str, Any]:
        if content_blocks:
            manifest = session.get("engine_capabilities")
            declared_types = (
                manifest.get("input_content_types")
                if isinstance(manifest, dict)
                else None
            )
            if not isinstance(declared_types, list) or any(
                not isinstance(block_type, str) or not block_type.strip()
                for block_type in declared_types
            ):
                raise APIError(
                    code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                    message=(
                        "session has no verified engine input-content declaration "
                        f"(session={session_id})"
                    ),
                    status_code=502,
                )
            requested_types = {
                str(block.get("type") or "").strip()
                for block in content_blocks
                if isinstance(block, dict)
            }
            unsupported_types = sorted(requested_types - set(declared_types))
            if unsupported_types:
                engine_kind = str(session.get("engine_kind") or "").strip()
                raise APIError(
                    code="ENGINE_CAPABILITY_UNAVAILABLE",
                    message=(
                        f"engine {engine_kind!r} does not accept turn content "
                        f"types {unsupported_types!r}"
                    ),
                    status_code=409,
                )

        # Validate the requested permission mode against the engine's verified
        # capability before the command is journaled. The journal write below
        # is durable and the FIFO delivers strictly in order, so an input that
        # is accepted first and refused later squats the FIFO head forever —
        # every subsequent valid message then fails the consumption-boundary
        # identity check. A client sending a mode name another engine defines
        # would otherwise brick the conversation with a single 400.
        if permission_mode is not None:
            from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
                PermissionLifecycle,
            )

            PermissionLifecycle.validate_mode_for_session(
                permission_mode,
                session=session,
            )

        normalized_client_message_id = str(client_message_id or "").strip()
        command_id = self._input_command_id(
            session_id,
            normalized_client_message_id,
        )
        input_id = self._input_id(
            session_id,
            normalized_client_message_id,
        )

        existing = await self._session_events_repo.get_command_event(
            session_id,
            command_id=command_id,
        )
        created = existing is None
        if existing is None:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            conversation_state = str(
                (snapshot or {}).get("conversation_state") or ""
            ).strip()
            if conversation_state == "WAITING_FOR_INTERACTION":
                raise APIError(
                    code="INVALID_REQUEST",
                    message="there is a pending interaction to answer",
                    status_code=400,
                )
            if derive_turn_recovery_phase(snapshot) is not None:
                raise APIError(
                    code="SESSION_BUSY",
                    message="session turn recovery is still pending",
                    status_code=409,
                )
            if conversation_state == "INTERRUPTING":
                # An input arriving while the turn is stopping belongs to the
                # next turn. The dying turn's FIFO verdict has already been
                # taken (the runner reports continues_fifo when the interrupt
                # lands), so submitting into its stream strands the reply: the
                # input is consumed after turn.completed and nothing answers
                # it. Hand the stopping state over — wait out the settle, then
                # dispatch against the settled conversation.
                snapshot = await self._await_interrupt_settled(session_id)
                conversation_state = str(
                    (snapshot or {}).get("conversation_state") or ""
                ).strip()
            # An engine-owned response holds the slot without a worker; a
            # new input does not join its FIFO, it starts its own turn and the
            # engine queues it behind the response it is already producing.
            active = (
                conversation_state in {"PROCESSING", "STREAMING"}
                and resident_engine_turn_id(snapshot) is None
            )
            if active:
                turn_id = str((snapshot or {}).get("current_turn_id") or "").strip()
                if not turn_id:
                    raise RuntimeError(
                        "active engine input FIFO has no current platform worker"
                    )
                command_type = "SubmitInput"
            else:
                await self._ensure_start_turn_allowed(session_id, session=session)
                turn_id = self._new_turn_id()
                command_type = "StartTurn"

            from astrabox.seams.admission import (
                ADMISSION_KIND_TURN,
                AdmissionRequest,
                enforce_admission,
            )

            await enforce_admission(
                AdmissionRequest(
                    kind=ADMISSION_KIND_TURN,
                    user_id=str(getattr(user, "user_id", "") or ""),
                    session_id=session_id,
                    agent_id=str(session.get("agent_id") or "").strip() or None,
                )
            )
            command_payload = {
                "interaction_id": None,
                "content": content,
                "interaction_response": None,
                "permission_mode": permission_mode,
                "client_message_id": normalized_client_message_id,
                "input_id": input_id,
            }
            if content_blocks:
                # Only text-and-more inputs carry a second representation;
                # ``content`` stays the projection every other reader uses.
                command_payload["content_blocks"] = content_blocks
            command_event, created = await self._session_events_repo.try_claim_event(
                {
                    "session_id": session_id,
                    "channel": "command",
                    "turn_id": turn_id,
                    "event_type": "command.accepted",
                    "causation_id": command_id,
                    "correlation_id": command_id,
                    "payload": {
                        "command_type": command_type,
                        "author_user_id": user.user_id,
                        **command_payload,
                    },
                }
            )
            existing = command_event
            if command_type == "StartTurn" and created:
                await self._project_command_to_snapshot(
                    session_id=session_id,
                    turn_id=turn_id,
                    command_seq=int(command_event.get("event_seq") or 0),
                    command_id=command_id,
                )

        payload = existing.get("payload") if isinstance(existing, dict) else None
        if (
            not isinstance(payload, dict)
            or str(payload.get("input_id") or "").strip() != input_id
            or payload.get("content") != content
            or (payload.get("content_blocks") or None) != (content_blocks or None)
            or str(payload.get("client_message_id") or "").strip()
            != normalized_client_message_id
        ):
            raise RuntimeError(
                "delivery command id collision with different session or payload "
                f"command_id={command_id!r}"
            )
        turn_id = str(existing.get("turn_id") or "").strip()
        command_type = str(payload.get("command_type") or "").strip()
        if command_type == "SubmitInput":
            live = await self._session_snapshots_repo.get_snapshot(session_id)
            live_state = str((live or {}).get("conversation_state") or "").strip()
            live_turn = str((live or {}).get("current_turn_id") or "").strip()
            if live_turn != turn_id or live_state not in {"PROCESSING", "STREAMING"}:
                # The carrier this input was classified under is stopping or
                # already settled: delivering now would push the input into
                # the resident FIFO after the runner's continues_fifo verdict
                # was taken, where its reply streams to nothing (consumed
                # after turn.completed, answered by no reader). Leave it
                # undelivered — every turn start replays the journal outbox in
                # order — and drive the next turn that will carry it.
                await self._await_interrupt_settled(session_id)
                self._drive_handoff_turn(
                    session_id, command_id=command_id, input_id=input_id
                )
                await self._sessions_repo.mark_interaction(session_id, utcnow_iso())
                return {
                    "session_id": session_id,
                    "command_id": command_id,
                    "client_message_id": normalized_client_message_id,
                    "input_id": input_id,
                    "status": "delivered",
                }
        should_spawn_producer = command_type == "StartTurn" and created
        if (
            command_type == "StartTurn"
            and not created
            and command_id not in self._accepted_turn_producers
        ):
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            should_spawn_producer = (
                str((snapshot or {}).get("conversation_state") or "").strip()
                in {"PROCESSING", "STREAMING", "INTERRUPTING"}
                and str((snapshot or {}).get("current_turn_id") or "").strip()
                == turn_id
            )
        if should_spawn_producer:
            # The producer owns the turn heartbeat during runtime preparation.
            # Start it before delivery can block on replacing a lost sandbox;
            # otherwise the reconciler sees PROCESSING without a live owner.
            self._spawn_accepted_turn_producer(
                session_id, command_id=command_id, turn_id=turn_id
            )
        delivery_status = "delivered"

        async def _settle_refused_turn(refused: InputDeliveryRefused) -> None:
            await self._settle_refused_start_turn(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
                refused=refused,
            )

        try:
            await self._turn_service.deliver_pending_inputs(
                user=user,
                session=session,
                session_id=session_id,
                requested_command_id=command_id,
                permission_mode=permission_mode,
                on_refused=(
                    _settle_refused_turn if command_type == "StartTurn" else None
                ),
            )
        except EngineStreamDetached as exc:
            # Admission is durable; a missing transport receipt cannot reject
            # it. The existing owner/recovery lane settles the original turn.
            delivery_status = "accepted"
            logger.warning(
                "accepted input lost its delivery link; leaving delivery to "
                "the turn owner/recovery session=%s command=%s detail=%s",
                session_id,
                command_id,
                exc,
            )
            self._wakeup_turn_coordinator(session_id)
        await self._sessions_repo.mark_interaction(session_id, utcnow_iso())
        return {
            "session_id": session_id,
            "command_id": command_id,
            "client_message_id": normalized_client_message_id,
            "input_id": input_id,
            "status": delivery_status,
        }

    async def _settle_refused_start_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
        refused: InputDeliveryRefused,
    ) -> None:
        """Make a delivery refusal this message's only outcome.

        The request that accepted the turn is answered with ``refused``; the
        turn is failed before that answer leaves, with the refusal recorded, so
        its worker stands down instead of delivering the input later and the
        same message sent again is answered the same way. Nothing is written
        when the turn is not this conversation's current one: whoever
        settled it wrote its outcome.
        """

        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        conversation_state = str((snapshot or {}).get("conversation_state") or "").strip()
        if (
            str((snapshot or {}).get("current_turn_id") or "").strip() != turn_id
            or conversation_state not in _ACTIVE_CONVERSATION_SNAPSHOT_STATES
        ):
            return
        settled = await self._settle_turn_not_received(
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
            conversation_state=conversation_state,
            error_text=refused.message,
            refusal=refusal_record(refused),
        )
        logger.warning(
            "delivery refused and turn settled session=%s turn=%s code=%s settled=%s",
            session_id,
            turn_id,
            refused.code,
            isinstance(settled, dict),
        )

    def _spawn_accepted_turn_producer(
        self,
        session_id: str,
        *,
        command_id: str,
        turn_id: str,
    ) -> None:
        if command_id in self._accepted_turn_producers:
            return
        producer_task = self._spawn_background_task(
            self._build_turn_worker().run(
                WorkerWakeup(
                    session_id=session_id,
                    channel="conversation",
                    command_id=command_id,
                    turn_id=turn_id,
                )
            ),
            name=f"session-kernel-turn-worker-{session_id}",
        )
        self._accepted_turn_producers[command_id] = producer_task
        producer_task.add_done_callback(
            lambda _t, key=command_id: self._accepted_turn_producers.pop(
                key, None
            )
        )

    def _drive_handoff_turn(
        self,
        session_id: str,
        *,
        command_id: str,
        input_id: str,
    ) -> None:
        """Carry an already-claimed input root under a fresh turn.

        A second command for the same input would be a second FIFO root: the
        outbox replays unconsumed roots, the duplicate can never be consumed
        (the engine consumes an input once, under its original command), and
        the phantom root re-renders the queue chip on every authoritative
        refresh. The original SubmitInput command is the root; the producer
        just runs it under a turn that is allowed to exist. The turn id
        derives from the input, so a retry or the settle sweep racing this
        spawns one producer, and a turn the user starts first fences it out —
        that turn's own start replays the outbox either way.
        """

        try:
            session_namespace = uuid.UUID(str(session_id or "").strip())
        except ValueError as exc:
            raise RuntimeError("session_id must be a valid UUID") from exc
        turn_id = str(uuid.uuid5(session_namespace, f"handoff-turn:{input_id}"))
        self._spawn_accepted_turn_producer(
            session_id, command_id=command_id, turn_id=turn_id
        )

    async def _redrive_stranded_inputs_after_settle(
        self,
        session_id: str,
        *,
        turn_id: str,
    ) -> None:
        """Re-drive inputs whose FIFO verdict predates them, after the settle.

        A dispatch-time state check cannot close the settle race — the live
        ledger shows the claim, the terminal, and the delivery landing inside
        one millisecond bucket — so the settled turn's own worker sweeps the
        ledger it just wrote. An input is stranded when its SubmitInput was
        accepted after the turn's last InterruptTurn and no frame ever
        answered it — ``input_answered_in_frames`` is the criterion, shared
        with the delivery outbox's carrier-loss reopening. Consumed without a
        carrier terminal stays stranded: the engine took the input and
        nothing delivered its reply. No durable field says "cancelled" — the
        settled terminal frame normalizes to finish/stop — so the ledger
        shape itself is the criterion. The handoff command id derives from
        the input, so racing the dispatch-side recheck double-claims nothing.
        """

        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        if str((snapshot or {}).get("current_turn_id") or "").strip():
            return
        if str((snapshot or {}).get("last_turn_id") or "").strip() != turn_id:
            return
        commands = await self._session_events_repo.list_events(
            session_id,
            channel="command",
            turn_id=turn_id,
            event_type="command.accepted",
        )
        interrupt_seq = max(
            (
                int(event.get("event_seq") or 0)
                for event in commands
                if str((event.get("payload") or {}).get("command_type") or "")
                == "InterruptTurn"
            ),
            default=0,
        )
        if interrupt_seq <= 0:
            return
        stranded = sorted(
            (
                event
                for event in commands
                if str((event.get("payload") or {}).get("command_type") or "")
                == "SubmitInput"
                and int(event.get("event_seq") or 0) > interrupt_seq
            ),
            key=lambda event: int(event.get("event_seq") or 0),
        )
        if not stranded:
            return
        frames = await self._session_events_repo.list_frames(session_id)
        for event in stranded:
            payload = event.get("payload") or {}
            input_id = str(payload.get("input_id") or "").strip()
            stranded_command_id = str(event.get("causation_id") or "").strip()
            if not input_id or not stranded_command_id:
                continue
            if input_answered_in_frames(
                frames, command_id=stranded_command_id, input_id=input_id
            ):
                continue
            logger.info(
                "re-driving stranded input after cancelled settle "
                "session=%s turn=%s input=%s",
                session_id,
                turn_id,
                input_id,
            )
            self._drive_handoff_turn(
                session_id, command_id=stranded_command_id, input_id=input_id
            )

    async def _redispatch_input_as_next_turn(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
        input_id: str,
        content: str,
        permission_mode: str | None,
        client_message_id: str,
    ) -> dict[str, Any]:
        """Drive the next turn for an input whose carrier settled under it.

        The input's SubmitInput command stays in the journal as the durable
        FIFO root; this claims the StartTurn that will replay it — every turn
        start delivers the pending outbox in order — and stream its reply.
        Every id derives from the input, so a retried POST lands on the same
        command, and a competing turn that starts first simply fences this
        producer out: its own start replays the outbox, so the input is
        carried either way.
        """

        _ = session
        command_id = f"{session_id}:handoff:{input_id}"
        try:
            session_namespace = uuid.UUID(str(session_id or "").strip())
        except ValueError as exc:
            raise RuntimeError("session_id must be a valid UUID") from exc
        turn_id = str(uuid.uuid5(session_namespace, f"handoff-turn:{input_id}"))
        command_event, created = await self._session_events_repo.try_claim_event(
            {
                "session_id": session_id,
                "channel": "command",
                "turn_id": turn_id,
                "event_type": "command.accepted",
                "causation_id": command_id,
                "correlation_id": command_id,
                "payload": {
                    "command_type": "StartTurn",
                    "author_user_id": user.user_id,
                    "interaction_id": None,
                    "content": content,
                    "interaction_response": None,
                    "permission_mode": permission_mode,
                    "client_message_id": client_message_id,
                    "input_id": input_id,
                },
            }
        )
        if created:
            await self._project_command_to_snapshot(
                session_id=session_id,
                turn_id=turn_id,
                command_seq=int(command_event.get("event_seq") or 0),
                command_id=command_id,
            )
        self._spawn_accepted_turn_producer(
            session_id, command_id=command_id, turn_id=turn_id
        )
        await self._sessions_repo.mark_interaction(session_id, utcnow_iso())
        return {
            "session_id": session_id,
            "command_id": command_id,
            "client_message_id": client_message_id,
            "input_id": input_id,
            "status": "delivered",
        }

    async def _tail_await_turn_slot_settled(
        self,
        session_id: str,
        *,
        turn_id: str | None,
        timeout_s: float = 5.0,
    ) -> None:
        """Hold the stream open until the turn slot is admissible again.

        The durable finish frame can precede the worker's snapshot CAS that
        frees the turn slot. Waiting only for that frame lets a no-gap next send
        race the CAS; waiting for the whole worker also includes unrelated
        post-settle drains and manifests. Poll the snapshot contract itself
        until the conversation leaves its mid-turn states or the slot changes
        hands. ``WAITING_FOR_INTERACTION`` is a settled stream boundary and
        passes immediately. On timeout, close and let the eligibility gate
        enforce the same contract.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        clean_turn_id = str(turn_id or "").strip()
        woke_coordinator = False
        while True:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            state = str((snapshot or {}).get("conversation_state") or "").strip()
            mid_turn = state in {"PROCESSING", "STREAMING", "INTERRUPTING"}
            # A bridge that ended before its terminal can settle this turn into
            # TRANSCRIPT_PENDING (mirror evidence still in flight). That phase
            # 409s the next send too, and its resolver runs on the reconcile
            # tick — up to a whole scan interval away — so kick the coordinator
            # here and keep the body open until the phase resolves.
            phase_pending = derive_turn_recovery_phase(snapshot) is not None and (
                not clean_turn_id
                or str((snapshot or {}).get("last_turn_id") or "").strip() == clean_turn_id
            )
            if not mid_turn and not phase_pending:
                return
            current = str((snapshot or {}).get("current_turn_id") or "").strip()
            if clean_turn_id and current and current != clean_turn_id:
                return
            if phase_pending and not woke_coordinator:
                wake = getattr(self, "_wakeup_turn_coordinator", None)
                if callable(wake):
                    wake(session_id)
                woke_coordinator = True
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "ai-stream close waited %.0fs for the turn slot to settle; "
                    "closing anyway session=%s turn=%s state=%s phase_pending=%s",
                    timeout_s,
                    session_id,
                    clean_turn_id or "?",
                    state,
                    phase_pending,
                )
                return
            await asyncio.sleep(0.025)

    async def follow_session_stream(
        self,
        user: UserContext,
        session_id: str,
        *,
        after_seq: int = -1,
    ) -> AsyncIterator[dict[str, Any]]:
        """Follow one session from a cursor through one assistant reply.

        The request may open while the session is idle and may span interaction
        segments inside one reply. A FIFO Result or terminal frame closes the
        response after its durable cursor; the client reopens from that cursor
        for the next reply, possibly in the same turn. This preserves idle delivery and the AI SDK's
        one-message-per-response parser boundary.
        """
        await self._must_get_projection_backed_session(
            user,
            session_id,
            reconcile_conversation=False,
        )
        async for frame in self.follow_session_output(session_id, after_seq=after_seq):
            yield frame

    async def resume_ai_stream(
        self,
        user: UserContext,
        session_id: str,
        *,
        after_seq: int = -1,
    ) -> AsyncIterator[dict[str, Any]] | None:
        session = await self._must_get_projection_backed_session(
            user,
            session_id,
            reconcile_conversation=False,
            internal_wiring=True,
        )
        self._require_turn_eligible(session, channel="conversation")

        state = str(session.get("state") or "")
        turn_id = str(session.get("current_turn_id") or "").strip()
        # pending comes from the projection-backed session (synthesized
        # from interaction_snapshots).
        pending = session.get("pending_interaction")
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        resume_recovered_terminal = False
        if isinstance(snapshot, dict) and self._is_unresolved_turn(snapshot):
            snapshot, resume_recovered_terminal = await self._resolve_pending_turn_for_resume(
                session_id=session_id,
                session=session,
                snapshot=snapshot,
            )
        snapshot_state = ""
        snapshot_last_turn_id = ""
        snapshot_last_turn_status = ""
        if isinstance(snapshot, dict):
            turn_id = str(snapshot.get("current_turn_id") or turn_id or "").strip()
            snapshot_state = str(snapshot.get("conversation_state") or "").strip()
            snapshot_last_turn_id = str(snapshot.get("last_turn_id") or "").strip()
            snapshot_last_turn_status = str(snapshot.get("last_turn_status") or "").strip()
            if state not in _ACTIVE_SESSION_STATES and snapshot_state in _ACTIVE_CONVERSATION_SNAPSHOT_STATES:
                state = "PROCESSING"
            if resume_recovered_terminal and snapshot_state == "IDLE" and snapshot_last_turn_id:
                turn_id = snapshot_last_turn_id
                state = "PROCESSING"

        if state not in _ACTIVE_SESSION_STATES:
            if (
                (after_seq >= 0 or resume_recovered_terminal)
                and snapshot_state == "IDLE"
                and snapshot_last_turn_id
                and snapshot_last_turn_status in {"COMPLETED", "FAILED"}
            ):
                turn_id = snapshot_last_turn_id
            else:
                return None
        if not turn_id and isinstance(pending, dict):
            turn_id = str(pending.get("turn_id") or "").strip()
        if not turn_id:
            interaction = await self._interaction_snapshots_repo.get_active_interaction(session_id)
            if isinstance(interaction, dict):
                turn_id = str(interaction.get("turn_id") or "").strip()
        if not turn_id:
            return None

        async def _resume_from_frame_store() -> AsyncIterator[dict[str, Any]]:
            safe_after_seq = await self._rewind_unsafe_resume_cursor(
                session_id,
                requested_after_seq=after_seq,
            )
            async for frame in self._tail_frames(
                session_id,
                turn_id=turn_id,
                after_seq=safe_after_seq,
                acknowledged_after_seq=after_seq,
                stop_on_segment_finish=False,
                response_messages=True,
            ):
                yield frame

        # Streaming resume always replays durable engine-frame events —
        # the byte-exact frames the turn worker persisted as it streamed. The Mongo
        # mirror stores the coarser claude JSONL transcript, which is the truth for
        # terminal block-recovery, not frame-accurate replay. The frame store serves
        # both active live-tail and terminal replay (after_seq>=0 and full replay);
        # terminal-incomplete turns are caught up separately by recovery
        # (mirror -> project_settled_transcript).
        return _resume_from_frame_store()

    async def answer_pending_interaction(
        self,
        user: UserContext,
        session_id: str,
        interaction_id: str,
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        session = await self._must_get_projection_backed_session(user, session_id, internal_wiring=True)
        self._require_turn_eligible(session, channel="interaction")
        requested_interaction_id = str(interaction_id or "").strip() or None
        # Read pending interaction from interaction_snapshots via
        # the already-synthesized session.pending_interaction projection.
        interaction = session.get("pending_interaction")
        active_interaction_id = (
            str((interaction or {}).get("interaction_id") or "").strip()
            if isinstance(interaction, dict)
            else ""
        )
        turn_id = (
            str(
                (interaction or {}).get("turn_id")
                or session.get("current_turn_id")
                or ""
            ).strip()
            if isinstance(interaction, dict)
            else ""
        )
        if (
            not isinstance(interaction, dict)
            or not active_interaction_id
            or (requested_interaction_id and requested_interaction_id != active_interaction_id)
            or not turn_id
        ):
            (
                interaction,
                active_interaction_id,
                turn_id,
            ) = await self._resolve_active_interaction_command_context(
                session_id=session_id,
                session=session,
                requested_interaction_id=requested_interaction_id,
            )
        # The answer is a live side-channel: submit_interaction_response resolves the
        # runner's pending PreToolUse wait and the original turn keeps
        # streaming in its own worker. No kernel command, no new BUSY cycle.
        return await self._answer_via_engine_client(
            session=session,
            session_id=session_id,
            interaction=interaction if isinstance(interaction, dict) else {},
            interaction_id=active_interaction_id,
            turn_id=turn_id,
            answer=dict(answer),
        )

    async def supersede_pending_interaction(
        self,
        user: UserContext,
        session_id: str,
        interaction_id: str,
    ) -> dict[str, Any]:
        """Close a pending interaction that a newer independent input replaces.

        While the engine that raised the question still holds its wait, the
        decline goes to it: the engine records the refusal as that tool's
        result and the parked turn resumes in its own worker. Once that
        engine is gone the wait went with it — nothing can write the tool
        result and nobody is left to produce the turn's terminal — so the
        platform settles the parked turn here instead. Both endings retire
        the interaction and free the conversation for the new input; only the
        live one reaches the engine, and only the live one produces a tool
        result.

        The terminal 409s below say there is no wait left to answer: either the
        engine runtime is absent, its sandbox is confirmed gone, or the wait
        already expired. Any other failure still propagates: an interaction
        that *could* have been declined must never be retired without one.
        """
        try:
            return await self.answer_pending_interaction(
                user,
                session_id,
                interaction_id,
                {"decline": True},
            )
        except APIError as exc:
            if exc.code not in _SUPERSEDE_ABANDON_CODES:
                raise
            abandon_reason = exc.code
        return await self._abandon_pending_interaction(
            session_id=session_id,
            interaction_id=interaction_id,
            reason=abandon_reason,
        )

    async def _abandon_pending_interaction(
        self,
        *,
        session_id: str,
        interaction_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Retire an interaction with no engine left to answer it."""

        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        turn_id = str((snapshot or {}).get("current_turn_id") or "").strip()
        # ``settle_parked_turn`` appends its terminal event before the CAS
        # that rejects a turn which is not parked, so a turn that is running
        # again must not be handed to it — the rejected snapshot update would
        # still leave a ``turn.failed`` in the journal.
        parked = (
            str((snapshot or {}).get("conversation_state") or "").strip()
            == "WAITING_FOR_INTERACTION"
        )
        logger.warning(
            "abandoning a pending interaction with no engine wait left: "
            "session=%s interaction=%s turn=%s parked=%s reason=%s",
            session_id,
            interaction_id,
            turn_id or "?",
            parked,
            reason,
        )
        if turn_id and parked:
            await settle_parked_turn(
                session_events_repo=self._session_events_repo,
                session_snapshots_repo=self._session_snapshots_repo,
                interaction_snapshots_repo=self._interaction_snapshots_repo,
                session_id=session_id,
                turn_id=turn_id,
                command_id=None,
                status="FAILED",
                failure_phase="interaction_engine_gone",
                error_text=(
                    "the engine holding this question exited before a newer "
                    f"input superseded it ({reason})"
                ),
                causation=f"supersede-abandon:{session_id}:{turn_id}",
                user_stop=False,
            )
        await self._sessions_repo.clear_pending_interaction(
            session_id,
            interaction_id=interaction_id,
        )
        return {
            "interaction_id": interaction_id,
            "answered": False,
            "abandoned": True,
            "reason": reason,
            "turn_id": turn_id or None,
        }

    async def _answer_via_engine_client(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
        interaction: dict[str, Any],
        interaction_id: str,
        turn_id: str,
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        """Deliver an interaction answer to the engine client, live.

        Requires the runtime (and its engine client) to be attached: the
        runner's PreToolUse wait is in-memory in the box, so once it expires
        there is nothing durable to answer — the defer has already fired and
        the user's next message resumes the session. Both absences are 409s
        that say exactly that, never a silent drop.
        """
        from astrabox.core.service.orchestrator.engine.base import EngineTurnReceipt

        runtime = await self._turn_service.acquire_engine_control_runtime(
            session,
            operation="answer interaction",
        )
        engine_client = runtime.engine_client
        # Structure first, vendor meaning second: the platform checks the
        # answer against the record's declared presentation, then the
        # adapter judges its vendor meaning and encodes the native reply.
        validate_interaction_response(interaction, answer)
        receipt = EngineTurnReceipt(
            engine_turn_id=str(interaction.get("engine_turn_id") or turn_id or ""),
            engine_session_key=(
                str(
                    interaction.get("engine_session_key")
                    or session.get("engine_session_key")
                    or getattr(runtime, "engine_session_key", "")
                    or ""
                ).strip()
                or None
            ),
            started_at_monotonic_ns=time.monotonic_ns(),
            # An engine cannot raise an interaction before consuming the root
            # input.  A replacement host may not know that input's id, but it
            # does know the boundary was crossed; the continuation must not
            # wait for a second consumption event that cannot exist.
            input_consumed=True,
        )
        try:
            manifest = bound_engine_client_manifest(runtime)
        except TypeError as exc:
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message=str(exc),
                status_code=502,
            ) from exc
        if not manifest.supports_interaction:
            raise APIError(
                code="ENGINE_CAPABILITY_UNAVAILABLE",
                message=(
                    f"engine_kind={manifest.engine_kind!r} cannot resolve "
                    "pending interactions"
                ),
                status_code=409,
            )
        assert isinstance(engine_client, EngineInteractions)
        accepted = await engine_client.submit_interaction_response(
            receipt,
            pending=interaction,
            response=answer,
        )
        if not accepted:
            raise APIError(
                code="INTERACTION_EXPIRED",
                message=(
                    "the approval wait already expired (deferred) — send a "
                    "message to resume and the engine will re-request it"
                ),
                status_code=409,
            )
        with contextlib.suppress(Exception):
            await self._sessions_repo.update_session(
                session_id, {"pending_interaction": None}
            )
        # The answer resolved the runner's PreToolUse wait, but the parked
        # turn's engine stream now has no consumer: the original bridge
        # segment closed at the interaction boundary (the SSE segment must
        # end). Dispatch the continuation segment: an AnswerInteraction
        # worker command whose bridge re-enters the same stream (turn_service
        # passes answer_continuation into the engine generator) and consumes
        # it to its terminal. Without this the turn completes inside the box
        # with nobody reading and the session sits WAITING_INPUT forever. The
        # continuation runs in the background because the client's answer POST
        # must return now; the client follows it over the resume cursor.
        command_id, _ = await self._append_command_accepted(
            user=UserContext(user_id=str(session.get("user_id") or "")),
            session_id=session_id,
            turn_id=turn_id,
            command_type="AnswerInteraction",
            payload={
                "interaction_id": interaction_id,
                # The resolve gate matches the response to the pending
                # interaction by id — the client's answer body does not carry
                # it (the URL/command context does), so it is folded in here.
                "interaction_response": {
                    "interaction_id": interaction_id,
                    **dict(answer),
                },
            },
        )
        continuation = asyncio.create_task(
            self._build_turn_worker().run(
                WorkerWakeup(
                    session_id=session_id,
                    channel="conversation",
                    command_id=command_id,
                    turn_id=turn_id,
                )
            ),
            name=f"answer-continuation-{session_id}",
        )
        _ANSWER_CONTINUATIONS.add(continuation)
        continuation.add_done_callback(_ANSWER_CONTINUATIONS.discard)
        return {
            "interaction_id": interaction_id,
            "answered": True,
            "turn_id": turn_id,
        }

    async def _resolve_active_interaction_command_context(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        requested_interaction_id: str | None,
    ) -> tuple[dict[str, Any], str, str]:
        snapshot = await self._get_kernel_session_snapshot(session_id)
        interaction = await self._get_pending_interaction(
            session_id,
            snapshot=snapshot,
        )
        if not isinstance(interaction, dict):
            raise APIError(
                code="INVALID_REQUEST",
                message="there is no pending interaction to answer",
                status_code=400,
            )
        active_interaction_id = str(interaction.get("interaction_id") or "").strip()
        requested_id = str(requested_interaction_id or "").strip()
        if requested_id and requested_id != active_interaction_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="the specified interaction is no longer pending",
                status_code=400,
            )
        turn_id = str(
            interaction.get("turn_id")
            or session.get("current_turn_id")
            or ""
        ).strip()
        if not turn_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="pending interaction is missing turn_id",
                status_code=409,
            )
        return interaction, active_interaction_id, turn_id

    async def interrupt(self, user: UserContext, session_id: str) -> dict[str, Any]:
        session = await self._must_get_projection_backed_session(
            user,
            session_id,
            reconcile_conversation=False,
            internal_wiring=True,
        )
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        terminal_state = str((snapshot or {}).get("terminal_state") or "").strip()
        if terminal_state in _ACTIVE_TERMINAL_SNAPSHOT_STATES:
            self._require_turn_eligible(
                session,
                channel="interrupt",
                reject_creating=True,
                reject_terminated=False,
                reject_deleted=True,
            )
            command_id, _ = await self._append_command_accepted(
                user=user,
                session_id=session_id,
                command_type="InterruptTerminal",
                payload={},
            )
            outcome = await self._build_terminal_worker().run(
                WorkerWakeup(
                    session_id=session_id,
                    channel="terminal",
                    command_id=command_id,
                )
            )
            result = outcome.metadata.get("result") if isinstance(outcome.metadata, dict) else None
            if isinstance(result, dict):
                return {
                    "session_id": str(result.get("session_id") or session_id),
                    "status": str(result.get("status") or "completed"),
                }
            return {"session_id": session_id, "status": "completed"}

        self._require_turn_eligible(session, channel="conversation")
        turn_id = str(
            (snapshot or {}).get("current_turn_id")
            or session.get("current_turn_id")
            or ""
        ).strip() or None
        command_id, _ = await self._append_command_accepted(
            user=user,
            session_id=session_id,
            turn_id=turn_id,
            command_type="InterruptTurn",
            payload={"turn_id": turn_id},
        )
        outcome = await self._build_turn_worker().run(
            WorkerWakeup(
                session_id=session_id,
                channel="conversation",
                command_id=command_id,
                turn_id=turn_id,
            )
        )
        result = outcome.metadata.get("result") if isinstance(outcome.metadata, dict) else None
        if isinstance(result, dict):
            return {
                "session_id": str(result.get("session_id") or session_id),
                "status": str(result.get("status") or "accepted"),
            }
        return {"session_id": session_id, "status": "accepted"}

    async def _await_interrupt_settled(
        self,
        session_id: str,
        *,
        budget_s: float = 15.0,
    ) -> dict[str, Any] | None:
        """Wait for a stopping turn to settle before classifying an input.

        The engine acknowledges an interrupt within seconds; a settle that
        outlives this budget is a stuck stop, and queueing more work behind
        it would hide that, so the input is refused as retryable instead.
        """

        deadline = time.monotonic() + budget_s
        while True:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            state = str((snapshot or {}).get("conversation_state") or "").strip()
            if state != "INTERRUPTING":
                return snapshot
            if time.monotonic() >= deadline:
                raise APIError(
                    code="SESSION_BUSY",
                    message="the current turn is still stopping; retry the input",
                    status_code=409,
                )
            await asyncio.sleep(0.1)

    async def _ensure_start_turn_allowed(
        self,
        session_id: str,
        *,
        session: dict[str, Any],
    ) -> None:
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        conversation_state = str((snapshot or {}).get("conversation_state") or "").strip()
        if (
            conversation_state in {"PROCESSING", "STREAMING"}
            and resident_engine_turn_id(snapshot) is None
        ):
            logger.warning(
                "start-turn gate busy: session=%s state=%s current_turn=%s "
                "watermark=%s last_turn=%s/%s",
                session_id,
                conversation_state,
                str((snapshot or {}).get("current_turn_id") or ""),
                (snapshot or {}).get("conversation_event_seq_applied"),
                str((snapshot or {}).get("last_turn_id") or ""),
                str((snapshot or {}).get("last_turn_status") or ""),
            )
            raise APIError(
                code="SESSION_BUSY",
                message="session already has an active turn",
                status_code=409,
            )
        if conversation_state == "INTERRUPTING":
            raise APIError(
                code="SESSION_BUSY",
                message="session already has an active turn",
                status_code=409,
            )
        if conversation_state == "WAITING_FOR_INTERACTION":
            raise APIError(
                code="INVALID_REQUEST",
                message="there is a pending interaction to answer",
                status_code=400,
            )
        if derive_turn_recovery_phase(snapshot) is not None:
            raise APIError(
                code="SESSION_BUSY",
                message="session turn recovery is still pending",
                status_code=409,
            )
        if conversation_state == "IDLE":
            last_turn_status = str((snapshot or {}).get("last_turn_status") or "").strip()
            last_turn_id = str((snapshot or {}).get("last_turn_id") or "").strip() or None
            last_turn_command_id = str((snapshot or {}).get("last_turn_command_id") or "").strip() or None
            if last_turn_status == "COMPLETED" and last_turn_id:
                if not turn_terminal_frame_matches(
                    (snapshot or {}).get("last_turn_terminal_frame"),
                    turn_id=last_turn_id,
                    command_id=last_turn_command_id,
                    frame_type="finish",
                    finish_reason=AI_SDK_FINISH_REASON_STOP,
                ):
                    raise APIError(
                        code="SESSION_BUSY",
                        message="previous turn terminal frame is not durable yet",
                        status_code=409,
                    )
        # Deliberately no session-document state check: the snapshot above is
        # the single authority for turn liveness, while the document is an
        # asynchronously written projection mirror. Interaction pendency is
        # likewise read from interaction_snapshots below rather than from the
        # document's WAITING_INPUT mirror.
        active_interaction = await self._interaction_snapshots_repo.get_active_interaction(
            session_id
        )
        if isinstance(active_interaction, dict):
            raise APIError(
                code="INVALID_REQUEST",
                message="there is a pending interaction to answer",
                status_code=400,
            )
        # Any transcript-pending failure was synchronously offered to the
        # coordinator by dispatch_turn_input before this gate. Reaching here
        # means the prior turn is terminal, not merely waiting for the periodic
        # reconcile scan.

        # Pending interaction authority is interaction_snapshots only.

    async def _append_command_accepted(
        self,
        *,
        user: UserContext,
        session_id: str,
        command_type: str,
        payload: dict[str, Any],
        turn_id: str | None = None,
    ) -> tuple[str, int]:
        command_id = str(uuid.uuid4())
        doc = await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "command",
                "turn_id": turn_id,
                "event_type": "command.accepted",
                "causation_id": command_id,
                "correlation_id": command_id,
                "payload": {
                    "command_type": command_type,
                    "author_user_id": user.user_id,
                    **dict(payload),
                },
            }
        )
        event_seq = int(doc.get("event_seq") or 0)
        if command_type == "StartTurn":
            await self._sessions_repo.mark_interaction(
                session_id,
                str(doc.get("occurred_at") or utcnow_iso()),
            )
        return command_id, event_seq

    async def _project_command_to_snapshot(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_seq: int,
        command_id: str | None = None,
    ) -> None:
        """Persist snapshot PROCESSING before spawning the turn worker.

        The user message is derived from the already-durable command event.
        This snapshot write makes the in-progress state visible immediately;
        the worker's same-watermark write remains idempotent.

        A new turn takes the slot with no engine anchor. The slot can be held
        by a response the engine started on its own (PlatformResidentOutputSink
        anchors it on the response id), and the new turn's bridge seeds its
        anchor from this snapshot: inheriting that anchor makes the turn refuse
        its own dispatch as a conflicting engine turn.
        """
        prev_snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        snapshot_updates: dict[str, Any] = build_turn_active_snapshot_updates(
            conversation_state="PROCESSING",
            turn_id=turn_id,
            worker_command_id=command_id,
            current_turn_remote_anchor=None,
            current_turn_engine_anchor=None,
            delivery_state="PENDING",
        )
        if command_id:
            snapshot_updates["worker_heartbeat_at"] = utcnow_iso()
        await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=command_seq,
            updates=snapshot_updates,
        )
