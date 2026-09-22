"""Turn execution — the engine-client generator.

``iter_engine_client_events`` drives every engine through the required durable
input seam: bind the native conversation, attach the turn to one accepted FIFO
command, then stream its output. The assistant (Hermes TUI Gateway) engine and
translation-shell Claude Code runner use this path
(``docs/design-translation-shell-2026-07.md``). Nothing in this generator
branches on the engine identity — the engine kind is read once from the runtime
and stamped into envelopes and anchors as data.

Dispatch is mutually exclusive with the sidecar path (selected once by the
capability profile in ``iter_sandbox_events``), and this generator shares no
closure/nonlocal state with it.

``TurnService._iter_engine_client_events`` stays reachable at its attribute
path (``iter_sandbox_events`` calls it by name at its capability fork) via a
thin delegator that re-yields this generator's events unchanged — see
turn_service.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from astrabox.persistence.repository import SessionRepository
from astrabox.persistence.repository.backend import is_mongo_transient_error
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.retry_utils import (
    build_retry_warning_before_sleep,
    retry_async_call,
)
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.engine.base import (
    EngineInputCommand,
    EngineStreamDetached,
    EngineTurnReceipt,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    BackgroundTasksOpened,
    ChildResourceFact,
    EngineEmission,
    InputConsumed,
    InteractionRequested,
    PrivateDiagnostic,
    PublicUIFrame,
    ResponseCompleted,
    TurnTerminal,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    mark_engine_public_ui_frame,
)
from astrabox.core.service.orchestrator.engine.input_content import (
    read_engine_content_blocks,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
)
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.event_broker import SessionEventBroker
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_pending_interaction_record,
    validate_interaction_contract,
)
from astrabox.core.service.orchestrator.stream_errors import (
    IncompleteStreamError as _IncompleteStreamError,
)
from astrabox.core.service.orchestrator.turn_preparation import prepared_sandbox_turn
from astrabox.seams.sandbox import TURN_PREPARATION_FAILED, SandboxTurnContext

logger = get_logger(__name__)


async def iter_engine_client_events(
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
    sessions_repo: SessionRepository,
    broker: SessionEventBroker,
    answer_continuation: bool = False,
    parked_engine_anchor: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Drive one durable FIFO command through an engine client.

    ``answer_continuation`` resumes the original turn's engine stream after
    an interaction answer instead of beginning a new turn: the previous
    bridge segment parked at the interaction boundary and exited (the client
    stream segment must close), the answer was delivered over the live side-channel
    (``submit_interaction_response``), and this segment re-enters the same stream on the
    engine client's still-active receipt — no new input is delivered, and
    the turn_id is the parked turn's own.

    Engine-translated AI SDK frames from the per-engine translator seam
    flow as the `ai_sdk_frame` envelope so the turn_worker can persist them
    through the live + durable frame writer without running the
    Anthropic-shaped canonical projector. The platform-lifecycle envelopes
    (ack, status BUSY, pending_interaction, assistant_message, result,
    status READY, error) match the sidecar path's shape exactly so the
    worker's terminal-frame projection, interaction state-machine and
    snapshot writers stay engine-agnostic.

    Cross-process turn-lock is held by the session-kernel turn-coordinator
    upstream of this generator; the in-process exclusion is runtime.lock.
    No engine on this path participates in the remote-agent dispatch-confirm
    IPC (that channel belongs to the sidecar path) — the engine turn id from
    ``begin_delivery`` is the engine-side confirmation.

    Conversation continuity is a platform guarantee. Before a process-local
    runtime can carry a turn, AstraBox supplies its opaque durable engine
    identity to ``bind_conversation``. The adapter must prove it resumed that
    native conversation; UI messages are never used as engine checkpoints.
    """
    engine_kind = str(getattr(runtime, "engine_kind", "") or "").strip().lower()
    if not engine_kind:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"engine-client runtime missing engine_kind "
                f"session={session_id} turn={turn_id}"
            ),
            status_code=500,
        )
    engine_client = getattr(runtime, "engine_client", None)
    if engine_client is None:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"engine-client runtime missing engine_client "
                f"session={session_id} turn={turn_id}"
            ),
            status_code=500,
        )
    if getattr(runtime, "conversation_bound", False) is not True:
        raise APIError(
            code="ENGINE_CONVERSATION_NOT_BOUND",
            message=(
                "runtime was published before binding its durable engine "
                f"conversation session={session_id} turn={turn_id}"
            ),
            status_code=500,
        )

    lock_wait_start = time.monotonic()
    async with runtime.lock:
        emit_timing(
            "turn_service.runtime_lock_acquired",
            wait_ms=round((time.monotonic() - lock_wait_start) * 1000, 3),
            engine_kind=engine_kind,
        )
        runtime.current_task = asyncio.current_task()
        receipt: Any = None
        expected_input_id: str | None = None
        expected_input_content: str | None = None
        input_consumption_observed = answer_continuation
        try:
            try:
                if not answer_continuation:
                    prepare_engine_input = getattr(
                        runtime, "prepare_engine_input", None
                    )
                    if prepare_engine_input is not None:
                        await prepare_engine_input()
                if answer_continuation:
                    # Re-enter the parked turn's stream. No sandbox turn
                    # preparation and no new FIFO delivery: the input was delivered
                    # by the original segment, and the answer already went
                    # over the live side-channel before this segment was
                    # dispatched.
                    receipt = getattr(engine_client, "active_receipt", None)
                    if receipt is None:
                        # A client that did not start this turn holds no
                        # receipt — which is every client after a platform
                        # restart. The turn is still running in the box and its
                        # frames are still arriving on the link this runtime
                        # just attached to, so refusing here would report this
                        # process's memory loss as the stream's absence and
                        # leave the answered turn parked in WAITING_INPUT
                        # forever.
                        #
                        # The receipt is pure data — engine ids and a start
                        # stamp, and `iter_turn_events` does not key the stream
                        # off it — so rebuild it. Whether the stream really
                        # exists is the runtime's question, and it was answered
                        # before this line by ensure_runtime.
                        # The engine's own turn id was minted when the turn
                        # began, in a process that is gone — and it is durable,
                        # on the snapshot, as this turn's anchor. Rebuild from
                        # that and the reattached client is indistinguishable
                        # from the original one.
                        #
                        # An invented id is not: it collides with the stored
                        # anchor, and `iter_turn_events` drops a ResultMessage
                        # whose command stamp does not match the receipt — so a
                        # placeholder can discard the turn's own terminal and
                        # leave the bridge waiting for a frame already consumed.
                        anchor = parked_engine_anchor or {}
                        engine_turn_id = str(anchor.get("engine_turn_id") or "").strip()
                        if not engine_turn_id:
                            raise APIError(
                                code="INTERACTION_RUNTIME_UNAVAILABLE",
                                message=(
                                    "the answered turn has no durable engine "
                                    "anchor to resume from — send a message to "
                                    "resume the conversation"
                                ),
                                status_code=409,
                            )
                        receipt = EngineTurnReceipt(
                            engine_turn_id=engine_turn_id,
                            engine_session_key=(
                                str(
                                    anchor.get("engine_session_key")
                                    or getattr(runtime, "engine_session_key", "")
                                    or ""
                                ).strip()
                                or None
                            ),
                            started_at_monotonic_ns=time.monotonic_ns(),
                            # A pending interaction could only have been
                            # emitted after this turn's root input. The
                            # replacement host has no input boundary to observe
                            # again, so carry that durable platform fact on the
                            # rebuilt receipt.
                            input_consumed=True,
                        )
                else:
                    principal_id = session.get("user_id")
                    async with prepared_sandbox_turn(
                        SandboxTurnContext(
                            sandbox_backend=str(
                                session.get("sandbox_backend") or ""
                            ).strip().lower(),
                            sandbox_id=(
                                str(getattr(runtime, "sandbox_id", "") or "").strip()
                                or str(session.get("sandbox_id") or "").strip()
                            ),
                            session_id=session_id,
                            principal_id=(
                                principal_id.strip()
                                if isinstance(principal_id, str)
                                else ""
                            ),
                            turn_id=turn_id,
                            engine_kind=engine_kind,
                            dispatch_attempt_id=str(uuid.uuid4()),
                            sandbox_handle=getattr(runtime, "sandbox", None),
                        ),
                        sessions_repo=sessions_repo,
                    ):
                        if delivery_command is not None:
                            command_sequence = delivery_command.get("sequence")
                            input_id = str(
                                delivery_command.get("input_id") or ""
                            ).strip()
                            command_content = delivery_command.get("content")
                            consumption_confirmed = delivery_command.get(
                                "consumption_confirmed", False
                            )
                            if (
                                isinstance(command_sequence, bool)
                                or not isinstance(command_sequence, int)
                                or command_sequence <= 0
                                or not input_id
                                or not isinstance(command_content, str)
                                or not isinstance(consumption_confirmed, bool)
                            ):
                                raise RuntimeError(
                                    "engine delivery command is malformed"
                                )
                            receipt = await engine_client.begin_delivery(
                                EngineInputCommand(
                                    command_id=str(
                                        delivery_command.get("command_id") or ""
                                    ),
                                    session_id=str(
                                        delivery_command.get("session_id") or ""
                                    ),
                                    sequence=command_sequence,
                                    input_id=input_id,
                                    content=command_content,
                                    client_message_id=(
                                        str(
                                            delivery_command.get(
                                                "client_message_id"
                                            )
                                            or ""
                                        ).strip()
                                        or None
                                    ),
                                    content_blocks=read_engine_content_blocks(
                                        delivery_command.get("content_blocks")
                                    ),
                                ),
                                consumption_confirmed=consumption_confirmed,
                            )
                            if receipt.input_id != input_id:
                                raise RuntimeError(
                                    "engine turn receipt does not identify the "
                                    "durable FIFO head exactly: "
                                    f"expected={input_id!r} actual={receipt.input_id!r}"
                                )
                            if receipt.input_consumed is not consumption_confirmed:
                                raise RuntimeError(
                                    "engine turn receipt consumption state disagrees "
                                    "with durable FIFO evidence: "
                                    f"expected={consumption_confirmed!r} "
                                    f"actual={receipt.input_consumed!r}"
                                )
                            expected_input_id = input_id
                            expected_input_content = command_content
                            input_consumption_observed = consumption_confirmed
                        else:
                            raise RuntimeError(
                                "engine turn has no durable FIFO delivery command"
                            )
            except asyncio.CancelledError:
                raise
            except EngineStreamDetached:
                # Input delivery may have reached the resident engine even if
                # its correlated acknowledgement did not reach this host. A
                # transport loss here is therefore the same unsettled-turn
                # fact as a loss while streaming: recovery owns the verdict.
                raise
            except BaseException as exc:
                if isinstance(exc, APIError) and exc.code == TURN_PREPARATION_FAILED:
                    logger.warning(
                        "engine turn preparation failed session=%s turn=%s "
                        "detail=%s",
                        session_id,
                        turn_id,
                        exc.debug_message,
                    )
                    error_code = TURN_PREPARATION_FAILED
                    error_message = exc.user_message
                else:
                    logger.exception(
                        "engine input delivery failed session=%s turn=%s",
                        session_id,
                        turn_id,
                    )
                    error_code = "ENGINE_INPUT_DELIVERY_FAILED"
                    error_message = str(exc)
                yield {
                    "type": "error",
                    "turn_id": turn_id,
                    "code": error_code,
                    "message": error_message,
                }
                yield {
                    "type": "status",
                    "state": SessionState.READY.value,
                    "at": utcnow_iso(),
                    "turn_id": turn_id,
                }
                return

            engine_turn_id_str = str(
                getattr(receipt, "engine_turn_id", "") or ""
            )
            engine_session_key_raw = getattr(receipt, "engine_session_key", "")
            engine_session_key = (
                str(engine_session_key_raw).strip() or None
                if engine_session_key_raw is not None
                else None
            )
            engine_anchor = {
                "engine_kind": engine_kind,
                "engine_turn_id": engine_turn_id_str,
                **(
                    {"engine_session_key": engine_session_key}
                    if engine_session_key
                    else {}
                ),
            }

            async def observe_engine_session_key() -> None:
                """Persist the engine's exact resume key before later output.

                The key is platform lifecycle state, not an AI SDK frame field.
                A client may learn it only after its first vendor message, so
                this runs at construction and before every yielded frame.
                """

                nonlocal engine_session_key
                raw_key = engine_client.engine_session_key
                if raw_key is None:
                    return
                if (
                    not isinstance(raw_key, str)
                    or not raw_key
                    or raw_key != raw_key.strip()
                ):
                    raise RuntimeError(
                        "engine reported a malformed native conversation key"
                    )
                if engine_session_key is not None and raw_key != engine_session_key:
                    raise RuntimeError(
                        "engine changed native conversation identity during a turn: "
                        f"expected={engine_session_key!r} actual={raw_key!r}"
                    )
                persisted_key = str(session.get("engine_session_key") or "").strip()
                if persisted_key and persisted_key != raw_key:
                    raise RuntimeError(
                        "engine resumed a different native conversation than the "
                        f"session owns: expected={persisted_key!r} actual={raw_key!r}"
                    )
                engine_session_key = raw_key
                engine_anchor["engine_session_key"] = raw_key
                runtime.engine_session_key = raw_key
                if not persisted_key:
                    await asyncio.wait_for(
                        sessions_repo.update_session(
                            session_id,
                            {"engine_session_key": raw_key},
                        ),
                        timeout=2.0,
                    )
                    session["engine_session_key"] = raw_key

            await observe_engine_session_key()
            emit_timing(
                "turn_service.engine_input_delivery_started",
                engine_kind=engine_kind,
                engine_turn_id=engine_turn_id_str,
            )

            if on_query_committed is not None:
                await on_query_committed(
                    {
                        "engine_kind": engine_kind,
                        "engine_turn_id": engine_turn_id_str,
                        "engine_session_key": engine_session_key,
                        "engine_anchor": dict(engine_anchor),
                        # Cross-engine compatibility for the worker's
                        # remote-anchor bookkeeping (the sidecar path sets
                        # sandbox_turn_id/last_sandbox_seq here); engine-client
                        # engines have no remote-agent turn counter, so these
                        # are absent and the worker's `is not None` guards
                        # cover the difference.
                        "sandbox_turn_id": None,
                        "last_sandbox_seq": None,
                    }
                )

            yield {
                "type": "ack",
                "session_id": session_id,
                "turn_id": turn_id,
                "user_content": effective_content,
                "client_message_id": client_message_id,
                "at": utcnow_iso(),
                "engine_kind": engine_kind,
                "engine_turn_id": engine_turn_id_str,
                "engine_session_key": engine_session_key,
                "engine_anchor": dict(engine_anchor),
                "sandbox_turn_id": None,
                "last_sandbox_seq": None,
            }
            yield {
                "type": "status",
                "state": SessionState.BUSY.value,
                "at": utcnow_iso(),
                "turn_id": turn_id,
                "permission_mode": interaction_permission_mode,
            }

            assistant_text_chunks: list[str] = []
            saw_terminal_frame = False
            terminal_finish_reason: str | None = None
            terminal_outcome: str | None = None
            terminal_native_reason: str | None = None
            terminal_usage: dict[str, Any] | None = None
            terminal_error_payload: dict[str, Any] | None = None
            terminal_engine_sequence_number: int | None = None
            terminal_closes_interaction = False
            interaction_emitted = False
            first_engine_frame = True
            v5_frame_seq = 0

            async for emission in engine_client.iter_turn_events(receipt):
                if not isinstance(emission, EngineEmission):
                    raise TypeError(
                        "engine adapter crossed the seam with an untyped output: "
                        f"{type(emission).__name__}"
                    )
                v5_frame = emission.as_frame()
                if isinstance(emission, PublicUIFrame):
                    v5_frame = mark_engine_public_ui_frame(v5_frame)
                await observe_engine_session_key()
                frame_type = str(v5_frame.get("type") or "").strip()
                if not input_consumption_observed:
                    if not isinstance(emission, InputConsumed):
                        raise RuntimeError(
                            "engine emitted output before consuming the durable "
                            f"FIFO head: frame={frame_type!r} "
                            f"input_id={expected_input_id!r}"
                        )
                    expected_response_id = input_response_message_id(
                        str(expected_input_id or "")
                    )
                    if (
                        emission.input_id != expected_input_id
                        or emission.response_message_id != expected_response_id
                    ):
                        raise RuntimeError(
                            "engine consumption boundary does not identify the "
                            "durable FIFO head exactly"
                        )
                    if emission.content != expected_input_content:
                        # Identity is the boundary judgement; content is only
                        # evidence. A vendor may legally rewrite the input it
                        # consumed (slash expansion, attachment wrapping), so a
                        # mismatch here is logged with both sides rather than
                        # failing a turn whose identity already matched.
                        logger.warning(
                            "consumed input content differs from the durable "
                            "command session=%s input_id=%s delivered=%r "
                            "consumed=%r",
                            session_id,
                            expected_input_id,
                            expected_input_content,
                            emission.content,
                        )
                    input_consumption_observed = True
                if first_engine_frame:
                    first_engine_frame = False
                    emit_timing(
                        "turn_service.first_engine_frame",
                        engine_kind=engine_kind,
                        frame_type=frame_type,
                    )

                if isinstance(emission, BackgroundTasksOpened):
                    yield {
                        "type": "background_tasks_opened",
                        "engine_kind": engine_kind,
                        "manifest": dict(emission.manifest),
                    }
                    continue

                if isinstance(emission, InteractionRequested):
                    interaction_id = emission.interaction_id or str(uuid.uuid4())
                    contract = dict(emission.contract)
                    gate_tool_use_id = str(contract.pop("tool_use_id", "") or "").strip()
                    # The adapter's declared contract is the only reading of
                    # this interaction the platform will ever hold, so a
                    # malformed declaration fails the turn here — before the
                    # record exists — rather than degrading to a shape no
                    # validator can answer.
                    validate_interaction_contract(contract)
                    tool_name = str(contract.get("tool_name") or "")
                    pending = build_pending_interaction_record(
                        contract=contract,
                        session_id=session_id,
                        turn_id=turn_id,
                        interaction_id=interaction_id,
                        # Only the engine can declare a tool binding. Native
                        # dialogs may be independent of any model tool call.
                        tool_call_id=gate_tool_use_id or None,
                    )
                    # Store the engine handles on the pending record so the
                    # AnswerInteraction continuation can route the choice to
                    # the engine's run id through its approve-run call.
                    pending_with_engine = dict(pending)
                    pending_with_engine["engine_kind"] = engine_kind
                    pending_with_engine["engine_turn_id"] = engine_turn_id_str
                    if engine_session_key:
                        pending_with_engine["engine_session_key"] = engine_session_key

                    # Persist the pending interaction under the house retry
                    # engine: retry only genuine transient mongo faults (a
                    # non-transient error — bad data, a permanent write reject —
                    # fails fast on the first attempt rather than burning three).
                    # Swallow-on-exhaustion is preserved: any failure to persist
                    # logs an error and breaks the frame loop, leaving the worker
                    # to keep the turn BUSY without a persisted pending record.
                    try:
                        await retry_async_call(
                            lambda: sessions_repo.update_session(
                                session_id,
                                {
                                    "pending_interaction": pending_with_engine,
                                    "last_error": None,
                                    "runtime_unavailable": False,
                                },
                            ),
                            should_retry_exception=is_mongo_transient_error,
                            max_attempts=3,
                            wait_seconds=1.0,
                            before_sleep=build_retry_warning_before_sleep(
                                logger,
                                lambda retry_state, exc: (
                                    "persist engine interaction attempt %d "
                                    "failed session=%s turn=%s tool=%s "
                                    "engine_turn_id=%s: %s"
                                    % (
                                        retry_state.attempt_number,
                                        session_id,
                                        turn_id,
                                        tool_name,
                                        engine_turn_id_str,
                                        exc,
                                    )
                                ),
                            ),
                        )
                    except Exception as exc:
                        logger.error(
                            "persist engine interaction exhausted retries "
                            "session=%s turn=%s tool=%s engine_turn_id=%s: %s",
                            session_id,
                            turn_id,
                            tool_name,
                            engine_turn_id_str,
                            exc,
                        )
                        break

                    with contextlib.suppress(Exception):
                        await broker.publish(
                            session_id,
                            {
                                "type": "pending_interaction",
                                "turn_id": turn_id,
                                "interaction_id": pending_with_engine["interaction_id"],
                                "tool_name": tool_name,
                                "pending_interaction": pending_with_engine,
                            },
                        )

                    yield {
                        "type": "pending_interaction",
                        "turn_id": turn_id,
                        "interaction_id": pending_with_engine["interaction_id"],
                        "tool_name": tool_name,
                        "pending_interaction": pending_with_engine,
                    }
                    interaction_emitted = True
                    # The engine keeps the turn open pending approval.
                    # The worker's pending_interaction handler closes
                    # this client-stream segment and keeps the session in busy
                    # waiting-for-interaction state until the
                    # AnswerInteraction command arrives.
                    break

                if isinstance(emission, ResponseCompleted):
                    yield {
                        "type": "response_result",
                        "turn_id": turn_id,
                        "engine_kind": engine_kind,
                        "engine_turn_id": engine_turn_id_str,
                        "data": dict(emission.public_data),
                    }
                    assistant_text_chunks.clear()
                    continue

                if isinstance(emission, TurnTerminal):
                    saw_terminal_frame = True
                    if not engine_session_key:
                        raise RuntimeError(
                            "engine settled a turn without exposing a durable "
                            "native conversation key"
                        )
                    terminal_engine_sequence_number = emission.engine_sequence_number
                    terminal_finish_reason = emission.finish_reason
                    terminal_outcome = emission.outcome
                    terminal_native_reason = emission.native_reason
                    terminal_usage = (
                        dict(emission.usage) if emission.usage is not None else None
                    )
                    terminal_error_payload = (
                        dict(emission.error) if emission.error is not None else None
                    )
                    terminal_closes_interaction = emission.closes_interaction
                    if emission.private_data:
                        yield {
                            "type": "engine_diagnostic",
                            "turn_id": turn_id,
                            "engine_kind": engine_kind,
                            "engine_turn_id": engine_turn_id_str,
                            "event_type": "engine.terminal",
                            "subtype": terminal_native_reason or emission.outcome,
                            "raw": dict(emission.private_data),
                        }
                    # The platform-lifecycle terminal envelopes (`result`
                    # and `status READY`/`error`) handle the journal's
                    # terminal-frame proof in an engine-agnostic way.
                    # Do not emit the engine's terminal AI SDK frame as an
                    # ai_sdk_frame envelope — the worker's `evt_type ==
                    # "status"` handler appends the canonical
                    # `type=finish` durable terminal signal which
                    # supersedes the engine's `type=result` shape.
                    break

                if isinstance(emission, PrivateDiagnostic):
                    yield {
                        "type": "engine_diagnostic",
                        "turn_id": turn_id,
                        "engine_kind": engine_kind,
                        "engine_turn_id": engine_turn_id_str,
                        "event_type": emission.event_type,
                        "subtype": emission.subtype,
                        "raw": emission.raw,
                    }
                    continue

                if not isinstance(
                    emission,
                    (PublicUIFrame, InputConsumed, ChildResourceFact),
                ):
                    raise TypeError(
                        "engine emission has no orchestration path: "
                        f"{type(emission).__name__}"
                    )

                if frame_type == "text-delta":
                    delta_text = str(v5_frame.get("delta") or "")
                    if delta_text:
                        assistant_text_chunks.append(delta_text)

                # Engine-translated AI SDK frame envelope. The
                # turn_worker's `evt_type == "ai_sdk_frame"` handler
                # routes this directly into the durable live-frame
                # writer (no canonical projector pass since the frame
                # is already in AI SDK wire shape from the per-engine translator).
                yield {
                    "type": "ai_sdk_frame",
                    "turn_id": turn_id,
                    "engine_kind": engine_kind,
                    "engine_turn_id": engine_turn_id_str,
                    "frame": dict(v5_frame),
                    "frame_index": v5_frame_seq,
                    **(
                        {"engine_sequence_number": emission.engine_sequence_number}
                        if emission.engine_sequence_number is not None
                        else {}
                    ),
                }
                v5_frame_seq += 1

            if interaction_emitted:
                emit_timing(
                    "turn_service.engine_stream_paused_for_interaction",
                    engine_kind=engine_kind,
                    frame_count=v5_frame_seq,
                )
                # Pending-interaction semantics: no terminal result/READY
                # is emitted; the worker keeps the turn in BUSY waiting
                # for the AnswerInteraction command.
                return

            if not saw_terminal_frame:
                raise _IncompleteStreamError(
                    f"engine stream ended without terminal frame "
                    f"session={session_id} turn={turn_id} "
                    f"engine_turn_id={engine_turn_id_str}"
                )

            emit_timing(
                "turn_service.engine_stream_complete",
                engine_kind=engine_kind,
                finish_reason=terminal_finish_reason,
                frame_count=v5_frame_seq,
            )

            if terminal_outcome == "failed":
                err_message = ""
                err_code = "ASSISTANT_RESPONSE_FAILED"
                if terminal_error_payload is not None:
                    err_message = str(terminal_error_payload.get("message") or "").strip()
                    code_str = str(terminal_error_payload.get("code") or "").strip()
                    if code_str:
                        err_code = code_str
                if not err_message:
                    err_message = (
                        "engine reported finishReason=error without message"
                    )
                yield {
                    "type": "error",
                    "turn_id": turn_id,
                    "code": err_code,
                    "message": err_message,
                    **(
                        {"terminal_reason": terminal_native_reason}
                        if terminal_native_reason
                        else {}
                    ),
                }
                yield {
                    "type": "status",
                    "state": SessionState.READY.value,
                    "at": utcnow_iso(),
                    "turn_id": turn_id,
                }
                return

            if terminal_outcome == "cancelled":
                # Engine-reported cancellation (the engine accepted a stop
                # request). Surface a READY with the cancellation reason
                # in the result-data so the snapshot records it.
                yield {
                    "type": "result",
                    "turn_id": turn_id,
                    "data": {
                        "finish_reason": "cancelled",
                        "engine_kind": engine_kind,
                        "engine_turn_id": engine_turn_id_str,
                        **(
                            {"engine_sequence_number": terminal_engine_sequence_number}
                            if terminal_engine_sequence_number is not None
                            else {}
                        ),
                        **(
                            {"session_id": engine_session_key}
                            if engine_session_key
                            else {}
                        ),
                        **(
                            {"usage": terminal_usage}
                            if terminal_usage
                            else {}
                        ),
                        **(
                            {"interaction_closed": True}
                            if terminal_closes_interaction
                            else {}
                        ),
                        **(
                            {"terminal_reason": terminal_native_reason}
                            if terminal_native_reason
                            else {}
                        ),
                    },
                }
                yield {
                    "type": "status",
                    "state": SessionState.READY.value,
                    "at": utcnow_iso(),
                    "turn_id": turn_id,
                }
                return

            assistant_text = "".join(assistant_text_chunks)
            if assistant_text:
                yield {
                    "type": "assistant_message",
                    "turn_id": turn_id,
                    "text": assistant_text,
                }

            result_data: dict[str, Any] = {
                "finish_reason": terminal_finish_reason or "stop",
                "engine_kind": engine_kind,
                "engine_turn_id": engine_turn_id_str,
            }
            if terminal_engine_sequence_number is not None:
                result_data["engine_sequence_number"] = terminal_engine_sequence_number
            if engine_session_key:
                result_data["session_id"] = engine_session_key
            if terminal_usage:
                result_data["usage"] = terminal_usage
            if terminal_closes_interaction:
                result_data["interaction_closed"] = True
            if terminal_native_reason:
                result_data["terminal_reason"] = terminal_native_reason
            yield {
                "type": "result",
                "turn_id": turn_id,
                "data": result_data,
            }
            yield {
                "type": "status",
                "state": SessionState.READY.value,
                "at": utcnow_iso(),
                "turn_id": turn_id,
            }
        except asyncio.CancelledError:
            if receipt is not None and bool(getattr(runtime, "interrupting", False)):
                with contextlib.suppress(BaseException):
                    await engine_client.cancel_turn(receipt)
            raise
        except APIError:
            if receipt is not None:
                with contextlib.suppress(BaseException):
                    await engine_client.cancel_turn(receipt)
            raise
        except _IncompleteStreamError as exc:
            logger.exception(
                "engine incomplete stream session=%s turn=%s",
                session_id,
                turn_id,
            )
            yield {
                "type": "error",
                "turn_id": turn_id,
                "code": "AGENT_RUNTIME_ERROR",
                "message": str(exc),
                "stream_unsettled": True,
                "active_state": "STREAMING",
                "engine_kind": engine_kind,
                "engine_turn_id": (
                    str(getattr(receipt, "engine_turn_id", "") or "")
                    if receipt is not None
                    else None
                ),
            }
        except EngineStreamDetached:
            # A transport statement, not a turn verdict — pass it through
            # untouched so the worker abandons without authoring a terminal
            # (the box may still be running the turn), and never cancel: an
            # interrupt here would aim at a link that is already gone, for a
            # turn recovery is about to settle from durable evidence.
            raise
        except Exception as exc:
            if receipt is not None:
                with contextlib.suppress(BaseException):
                    await engine_client.cancel_turn(receipt)
            logger.exception(
                "engine iter_turn_events failed session=%s turn=%s",
                session_id,
                turn_id,
            )
            yield {
                "type": "error",
                "turn_id": turn_id,
                "code": "ASSISTANT_STREAM_FAILED",
                "message": str(exc),
            }
            yield {
                "type": "status",
                "state": SessionState.READY.value,
                "at": utcnow_iso(),
                "turn_id": turn_id,
            }
        finally:
            runtime.current_task = None
