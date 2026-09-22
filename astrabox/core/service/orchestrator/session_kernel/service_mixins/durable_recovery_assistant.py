"""Engine-anchor recovery for :class:`SessionKernelService`."""
from __future__ import annotations

import asyncio
import contextlib
from astrabox.common.logger.logger_factory import get_logger
from typing import Any
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.engine.base import (
    EngineOutputCheckpoint,
    EngineTranscriptRecovery,
    EngineLiveTurnReconnect,
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
    pop_engine_frame_scope,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_pending_interaction_record,
    validate_interaction_contract,
)
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_TOOL_CALLS,
    SDK_RESPONSE_RESULT_BOUNDARY,
    build_turn_terminal_snapshot_updates,
    coerce_int as _coerce_int,
    normalize_current_turn_engine_anchor as _normalize_current_turn_engine_anchor,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
)
from astrabox.core.service.orchestrator.session_kernel.engine_emission_projection import (
    project_engine_interaction_opened,
    record_engine_background_tasks_opened,
)
from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
    DeadSandboxRecoverySettlement,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.bridge_terminal import (
    _commit_turn_terminal_snapshot,
)


logger = get_logger(__name__)


def build_engine_output_checkpoint(
    frames: list[dict[str, Any]],
    *,
    after_sequence: int | None,
) -> EngineOutputCheckpoint:
    """Return committed output without interpreting adapter-owned grammar."""

    return EngineOutputCheckpoint(
        after_sequence=after_sequence,
        committed_frames=tuple(dict(frame) for frame in frames),
    )


class DurableEngineRecoveryMixin:
    """Recover unfinished turns through the protocols an engine implements."""

    async def _recover_engine_via_anchor(
        self,
        *,
        session: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any] | DeadSandboxRecoverySettlement | None:
        """Recover from the adapter's transcript or its live-turn anchor.

        Platform output replay is always served from ``session_events``. This
        path handles the separate unfinished-turn question: use an adapter's
        durable transcript when it has one, otherwise continue a resident
        engine when its client implements that protocol, otherwise settle with
        a named unrecoverable outcome. Competing attempts are prevented by:

          - The session_events command try_claim (causation_id is unique)
          - The snapshot CAS extra_filter on
            current_turn_id / current_turn_worker_command_id
          - The runtime's single active engine client and execd PTY takeover
            semantics

        Returns the updated snapshot on an ordinary terminal commit. A terminal
        caused by a confirmed-dead sandbox is tagged for the worker's named
        outcome counter. Returns ``None`` when more events are still expected —
        the caller will scan again on the next reconcile tick.
        """
        session_id = str(session.get("session_id") or "").strip()
        turn_id = str(snapshot.get("current_turn_id") or "").strip()
        if not session_id or not turn_id:
            return None

        command_id = str(snapshot.get("current_turn_worker_command_id") or "").strip()
        if not command_id:
            logger.info(
                "engine anchor recovery skipped: missing current_turn_worker_command_id "
                "session=%s turn=%s",
                session_id,
                turn_id,
            )
            return None

        anchor = _normalize_current_turn_engine_anchor(
            snapshot.get("current_turn_engine_anchor")
        )
        try:
            session_engine_kind = resolve_session_engine_kind(session)
        except ValueError as exc:
            return await self._fail_engine_turn_unrecoverable(
                session_id=session_id,
                session=session,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind="",
                reason=f"session engine identity is unavailable: {exc}",
            )
        engine_kind = session_engine_kind
        try:
            adapter = get_engine_adapter(engine_kind)
        except (KeyError, TypeError, ValueError) as exc:
            return await self._fail_engine_turn_unrecoverable(
                session_id=session_id,
                session=session,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=engine_kind,
                reason=f"engine adapter is unavailable for recovery: {exc}",
            )
        transcript_recovery = isinstance(adapter, EngineTranscriptRecovery)
        # A lost delivery receipt can leave no live-turn anchor even though
        # the adapter's authoritative transcript already holds the answer.
        if not transcript_recovery and (
            not isinstance(anchor, dict)
            or not str(anchor.get("engine_turn_id") or "").strip()
        ):
            return await self._fail_engine_turn_unrecoverable(
                session_id=session_id,
                session=session,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=session_engine_kind,
                reason="engine anchor missing or malformed on snapshot",
            )

        if isinstance(anchor, dict) and anchor.get("engine_kind") != engine_kind:
            return await self._fail_engine_turn_unrecoverable(
                session_id=session_id,
                session=session,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=session_engine_kind,
                reason=(
                    "engine anchor identity does not match the session: "
                    f"anchor={anchor.get('engine_kind')!r} session={engine_kind!r}"
                ),
            )

        runtime = self._runtime_manager.get_runtime(
            session_id,
            sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
        )
        if runtime is None:
            ensure_runtime = getattr(
                self._turn_service,
                "_ensure_runtime_lightweight_for_session",
                None,
            )
            if callable(ensure_runtime):
                try:
                    runtime = await ensure_runtime(session)
                except Exception:
                    logger.warning(
                        "engine anchor recovery: lightweight runtime attach failed session=%s",
                        session_id,
                        exc_info=True,
                    )
        if runtime is None:
            dead_sandbox_reason = await self._confirmed_dead_sandbox_reason(session)
            if dead_sandbox_reason:
                if transcript_recovery:
                    return await self._settle_engine_turn_transcript_pending(
                        session_id=session_id,
                        session=session,
                        snapshot=snapshot,
                        turn_id=turn_id,
                        command_id=command_id,
                        engine_kind=engine_kind,
                        remote_anchor=_normalize_current_turn_remote_anchor(
                            snapshot.get("current_turn_remote_anchor")
                        ),
                    )
                settled = await self._fail_engine_turn_unrecoverable(
                    session_id=session_id,
                    session=session,
                    turn_id=turn_id,
                    command_id=command_id,
                    engine_kind=engine_kind,
                    reason=dead_sandbox_reason,
                )
                if isinstance(settled, dict):
                    return DeadSandboxRecoverySettlement(snapshot=settled)
                return None
        if runtime is None and not str(session.get("sandbox_id") or "").strip():
            # No sandbox is a final fact, not a transient attach failure. An
            # adapter-owned durable transcript can still settle without it;
            # resident-engine continuation cannot.
            if transcript_recovery:
                return await self._settle_engine_turn_transcript_pending(
                    session_id=session_id,
                    session=session,
                    snapshot=snapshot,
                    turn_id=turn_id,
                    command_id=command_id,
                    engine_kind=engine_kind,
                    remote_anchor=_normalize_current_turn_remote_anchor(
                        snapshot.get("current_turn_remote_anchor")
                    ),
                )
            return await self._fail_engine_turn_unrecoverable(
                session_id=session_id,
                session=session,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=engine_kind,
                reason="session carries no sandbox_id; no runtime can be attached",
            )
        if transcript_recovery:
            return await self._settle_engine_turn_transcript_pending(
                session_id=session_id,
                session=session,
                snapshot=snapshot,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=engine_kind,
                remote_anchor=_normalize_current_turn_remote_anchor(
                    snapshot.get("current_turn_remote_anchor")
                ),
            )
        if runtime is None:
            # A sandbox is still named and its lifecycle probe did not prove
            # death. Resident continuation needs that runtime, so this remains a
            # retryable reconnect observation rather than a terminal verdict.
            return None
        engine_client = (
            getattr(runtime, "engine_client", None) if runtime is not None else None
        )
        if not isinstance(engine_client, EngineLiveTurnReconnect):
            return await self._fail_engine_turn_unrecoverable(
                session_id=session_id,
                session=session,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=engine_kind,
                reason=(
                    "the live engine client does not implement output reconnect"
                ),
            )

        assert isinstance(anchor, dict)
        engine_turn_id = str(anchor["engine_turn_id"]).strip()
        starting_after = _coerce_int(anchor.get("engine_sequence_number"))
        existing_frames = await self._list_turn_frames(session_id, turn_id)
        output_checkpoint = build_engine_output_checkpoint(
            existing_frames,
            after_sequence=starting_after,
        )

        recovered_frames: list[
            PublicUIFrame | InputConsumed | ChildResourceFact | ResponseCompleted
        ] = []
        terminal: TurnTerminal | None = None
        interaction: InteractionRequested | None = None
        background_manifest: dict[str, Any] | None = None
        last_sequence_number = starting_after
        interaction_sequence_number: int | None = None
        emission_index = 0
        stream = engine_client.iter_reconnected_turn_events(
            engine_turn_id=engine_turn_id,
            output_checkpoint=output_checkpoint,
        )
        iterator = stream.__aiter__()
        try:
            while True:
                try:
                    emission = await asyncio.wait_for(
                        iterator.__anext__(), timeout=15.0
                    )
                except StopAsyncIteration:
                    break
                if not isinstance(emission, EngineEmission):
                    raise TypeError(
                        "engine reconnect crossed the seam with an untyped output: "
                        f"{type(emission).__name__}"
                    )
                emission_index += 1
                seq = emission.engine_sequence_number
                if isinstance(emission, TurnTerminal):
                    terminal = emission
                    break
                if isinstance(emission, InteractionRequested):
                    interaction = emission
                    interaction_sequence_number = seq
                    break
                if seq is not None:
                    last_sequence_number = (
                        seq
                        if last_sequence_number is None
                        else max(int(last_sequence_number), seq)
                    )
                if isinstance(emission, PrivateDiagnostic):
                    await self._append_engine_recovery_diagnostic(
                        session_id=session_id,
                        turn_id=turn_id,
                        command_id=command_id,
                        engine_kind=engine_kind,
                        engine_turn_id=engine_turn_id,
                        event_type=emission.event_type,
                        subtype=emission.subtype,
                        raw=emission.raw,
                        identity=str(seq) if seq is not None else str(emission_index),
                    )
                    continue
                if isinstance(emission, ResponseCompleted):
                    recovered_frames.append(emission)
                    continue
                if isinstance(emission, BackgroundTasksOpened):
                    manifest = dict(emission.manifest)
                    if background_manifest is not None and manifest != background_manifest:
                        raise RuntimeError(
                            "engine reconnect declared conflicting background-task manifests"
                        )
                    background_manifest = manifest
                    continue
                if not isinstance(
                    emission,
                    (PublicUIFrame, InputConsumed, ChildResourceFact),
                ):
                    raise TypeError(
                        "engine reconnect emission has no recovery path: "
                        f"{type(emission).__name__}"
                    )
                recovered_frames.append(emission)
        except asyncio.TimeoutError:
            logger.info(
                "engine anchor recovery waiting for replayed events "
                "session=%s turn=%s engine_turn_id=%s starting_after=%s",
                session_id,
                turn_id,
                engine_turn_id,
                starting_after,
            )
            return None
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(BaseException):
                    await aclose()

        if recovered_frames:
            await self._append_engine_anchor_recovered_frames(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=engine_kind,
                engine_turn_id=engine_turn_id,
                frames=recovered_frames,
                starting_after=starting_after,
            )
        updated_anchor = dict(anchor)
        if (
            last_sequence_number is not None
            and last_sequence_number != anchor.get("engine_sequence_number")
        ):
            updated_anchor["engine_sequence_number"] = int(last_sequence_number)
            await self._session_snapshots_repo.force_update_fields(
                session_id,
                {"current_turn_engine_anchor": updated_anchor},
            )

        if interaction is not None:
            interaction_anchor = dict(updated_anchor)
            if interaction_sequence_number is not None:
                interaction_anchor["engine_sequence_number"] = int(
                    interaction_sequence_number
                )
            return await self._commit_engine_anchor_interaction(
                session_id=session_id,
                session=session,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=engine_kind,
                engine_turn_id=engine_turn_id,
                engine_anchor=interaction_anchor,
                interaction=interaction,
            )

        if terminal is None:
            logger.info(
                "engine anchor recovery waiting for terminal event "
                "session=%s turn=%s engine_turn_id=%s",
                session_id,
                turn_id,
                engine_turn_id,
            )
            return None

        return await self._commit_engine_anchor_terminal(
            session_id=session_id,
            session=session,
            snapshot=snapshot,
            turn_id=turn_id,
            command_id=command_id,
            engine_kind=engine_kind,
            engine_turn_id=engine_turn_id,
            terminal=terminal,
            background_manifest=background_manifest,
        )

    @staticmethod
    def _assistant_text_from_engine_events(frames: list[dict[str, Any]]) -> str:
        """Return the last FIFO response's visible text from durable frames."""

        chunks: list[str] = []
        for row in sorted(frames, key=lambda item: int(item.get("frame_seq") or 0)):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if (
                str(payload.get("type") or "").strip() == "data-result"
                and payload.get(SDK_RESPONSE_RESULT_BOUNDARY) is True
            ):
                chunks.clear()
                continue
            if str(payload.get("type") or "").strip() == "text-delta":
                chunks.append(str(payload.get("delta") or ""))
        return "".join(chunks)

    async def _append_engine_recovery_diagnostic(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
        engine_kind: str,
        engine_turn_id: str,
        event_type: str,
        subtype: str,
        raw: Any,
        identity: str,
    ) -> None:
        await self._session_events_repo.try_claim_event(
            {
                "session_id": session_id,
                "channel": "engine",
                "turn_id": turn_id,
                "event_type": "engine.diagnostic",
                "causation_id": (
                    f"recover:{session_id}:{turn_id}:diagnostic:{identity}"
                ),
                "correlation_id": command_id,
                "payload": {
                    "engine_kind": engine_kind,
                    "engine_turn_id": engine_turn_id,
                    "event_type": event_type,
                    "subtype": subtype,
                    "raw": raw,
                },
            }
        )

    async def _commit_engine_anchor_interaction(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        turn_id: str,
        command_id: str,
        engine_kind: str,
        engine_turn_id: str,
        engine_anchor: dict[str, Any],
        interaction: InteractionRequested,
    ) -> dict[str, Any] | None:
        """Commit a recovered interaction through the durable authority stores."""

        interaction_id = str(interaction.interaction_id or "").strip()
        if not interaction_id:
            raise RuntimeError("recovered engine interaction has no interaction id")
        contract = dict(interaction.contract)
        gate_tool_call_id = str(contract.pop("tool_use_id", "") or "").strip()
        validate_interaction_contract(contract)
        pending = build_pending_interaction_record(
            contract=contract,
            session_id=session_id,
            turn_id=turn_id,
            interaction_id=interaction_id,
            tool_call_id=gate_tool_call_id or None,
        )
        pending.update(
            {
                "engine_kind": engine_kind,
                "engine_turn_id": engine_turn_id,
            }
        )
        engine_session_key = str(session.get("engine_session_key") or "").strip()
        if engine_session_key:
            pending["engine_session_key"] = engine_session_key

        projection = await project_engine_interaction_opened(
            session_events_repo=self._session_events_repo,
            session_snapshots_repo=self._session_snapshots_repo,
            interaction_snapshots_repo=self._interaction_snapshots_repo,
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
            correlation_id=command_id,
            pending=pending,
            tool_name=str(pending.get("tool_name") or "").strip(),
            source_event_seq_applied=0,
            current_turn_engine_anchor=engine_anchor,
        )
        if projection.fenced:
            latest = await self._session_snapshots_repo.get_snapshot(session_id)
            if str(latest.get("current_turn_id") or "").strip() == turn_id:
                raise RuntimeError(
                    "recovered interaction did not enter WAITING_FOR_INTERACTION"
                )
            return latest
        if not projection.waiting:
            return await self._session_snapshots_repo.get_snapshot(session_id)
        result = projection.snapshot
        if not isinstance(result, dict):
            result = await self._session_snapshots_repo.get_snapshot(session_id)

        await self._sessions_repo.update_session(
            session_id,
            {
                "pending_interaction": pending,
                "last_error": None,
                "runtime_unavailable": False,
            },
        )
        await self._append_engine_anchor_recovered_frames(
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
            engine_kind=engine_kind,
            engine_turn_id=engine_turn_id,
            frames=[
                PublicUIFrame(
                    {
                        "type": "data-interaction",
                        "data": dict(pending),
                    }
                ),
                PublicUIFrame(
                    {
                        "type": "finish",
                        "finishReason": AI_SDK_FINISH_REASON_TOOL_CALLS,
                    }
                ),
            ],
            starting_after=None,
        )
        return result

    async def _confirmed_dead_sandbox_reason(
        self,
        session: dict[str, Any],
    ) -> str:
        """Return a reason only when durable or control-plane evidence proves death.

        No runtime and a failed attach are local observations, not a verdict.
        An unavailable probe or any non-terminal answer remains recoverable.
        """

        if bool(session.get("runtime_unavailable")):
            return (
                "session runtime_unavailable confirms that no sandbox runtime "
                "can be reattached"
            )

        sandbox_id = str(session.get("sandbox_id") or "").strip()
        if not sandbox_id:
            return ""
        probe_fn = getattr(self._runtime_manager, "get_sandbox_lifecycle_probe", None)
        terminal_fn = getattr(
            self._runtime_manager,
            "_is_terminal_sandbox_lifecycle_probe",
            None,
        )
        if not callable(probe_fn) or not callable(terminal_fn):
            return ""
        try:
            probe = await probe_fn(sandbox_id)
            terminal = bool(terminal_fn(probe))
        except Exception:
            logger.warning(
                "engine anchor recovery: sandbox lifecycle probe unconfirmed "
                "session=%s sandbox=%s",
                str(session.get("session_id") or "").strip(),
                sandbox_id,
                exc_info=True,
            )
            return ""
        if not terminal:
            return ""

        probe_status = str(getattr(probe, "probe_status", "") or "").strip()
        sandbox_state = str(getattr(probe, "sandbox_state", "") or "").strip()
        evidence = probe_status or "terminal_state"
        if sandbox_state:
            evidence = f"{evidence}:{sandbox_state}"
        return f"sandbox {sandbox_id} is control-plane-confirmed gone ({evidence})"

    async def _append_engine_anchor_recovered_frames(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
        engine_kind: str,
        engine_turn_id: str,
        frames: list[
            PublicUIFrame | InputConsumed | ChildResourceFact | ResponseCompleted
        ],
        starting_after: int | None,
    ) -> None:
        """Append continued engine frames to the stored UI frame stream.

        The engine anchor lives on ``snapshot.current_turn_engine_anchor``; the
        caller advances its replay cursor after this method returns.
        """
        if not frames:
            return
        selected_frames: list[tuple[dict[str, Any], int | None, str]] = []
        for emission in frames:
            if isinstance(emission, ResponseCompleted):
                payload = {
                    "type": "data-result",
                    SDK_RESPONSE_RESULT_BOUNDARY: True,
                    "data": dict(emission.public_data),
                }
            else:
                payload = emission.as_frame()
                if isinstance(emission, PublicUIFrame):
                    payload = mark_engine_public_ui_frame(payload)
            engine_sequence_number = emission.engine_sequence_number
            payload.pop("__engine_sequence_number", None)
            if (
                starting_after is not None
                and engine_sequence_number is not None
                and engine_sequence_number <= starting_after
            ):
                continue
            scope = pop_engine_frame_scope(payload)
            if isinstance(emission, ChildResourceFact):
                if scope != "session":
                    raise RuntimeError("child resource fact is not Session scoped")
            elif scope != "turn":
                raise RuntimeError(
                    f"{type(emission).__name__} cannot be Session scoped"
                )
            selected_frames.append((payload, engine_sequence_number, scope))
        if not selected_frames:
            return
        frame_seq = await self._session_events_repo.allocate_session_frame_seq(
            session_id,
            count=len(selected_frames),
        )
        docs: list[dict[str, Any]] = []
        for payload, engine_sequence_number, scope in selected_frames:
            normalized = (
                self._normalize_recovered_ai_sdk_frame(
                    payload,
                    turn_id=turn_id,
                    command_id=command_id,
                )
                if scope == "turn"
                else dict(payload)
            )
            doc = {
                "session_id": session_id,
                "turn_id": turn_id if scope == "turn" else None,
                "scope": scope,
                "command_id": command_id,
                "source_kind": "engine_live_reconnect",
                "frame_seq": int(frame_seq),
                "payload": normalized,
                "engine_kind": engine_kind,
                "engine_turn_id": engine_turn_id,
                "created_at": utcnow_iso(),
            }
            if engine_sequence_number is not None:
                doc["engine_sequence_number"] = int(engine_sequence_number)
            docs.append(doc)
            frame_seq += 1
        if not docs:
            return
        append_frames = getattr(self._session_events_repo, "append_frames", None)
        if callable(append_frames):
            await append_frames(docs)
        else:
            for doc in docs:
                await self._session_events_repo.append_frame(doc)

    async def _commit_engine_anchor_terminal(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        turn_id: str,
        command_id: str,
        engine_kind: str,
        engine_turn_id: str,
        terminal: TurnTerminal,
        background_manifest: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Persist an engine turn terminal and clear the platform busy state.

        Settles through the terminal state machine directly — there is no
        platform checkpoint row to commit.
        """
        outcome = terminal.outcome
        error_text: str | None = None
        if outcome == "failed":
            error_payload = terminal.error
            if error_payload is not None:
                error_text = str(error_payload.get("message") or "").strip() or None
            error_text = error_text or "engine response failed during recovery"

        await self._append_engine_anchor_recovered_frames(
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
            engine_kind=engine_kind,
            engine_turn_id=engine_turn_id,
            frames=[
                PublicUIFrame(
                    {
                        "type": "data-result",
                        "data": (
                            {"usage": dict(terminal.usage)}
                            if terminal.usage is not None
                            else {}
                        ),
                    }
                )
            ],
            starting_after=None,
        )

        if terminal.private_data:
            await self._append_engine_recovery_diagnostic(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
                engine_kind=engine_kind,
                engine_turn_id=engine_turn_id,
                event_type="engine.terminal",
                subtype=terminal.native_reason or outcome,
                raw=dict(terminal.private_data),
                identity="terminal",
            )
        if terminal.closes_interaction:
            await self._interaction_snapshots_repo.deactivate_active_for_turn(
                session_id,
                turn_id,
            )

        terminal_frame = (
            await self._append_recovery_finish_frame(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
            )
            if outcome != "failed"
            else await self._append_recovery_error_frame(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
                error_text=error_text
                or f"engine response finished with {terminal.finish_reason}",
            )
        )

        frames = await self._list_turn_frames(session_id, turn_id)
        assistant_text = self._assistant_text_from_engine_events(frames)
        blocks = (
            [{"type": "text", "text": assistant_text}] if assistant_text else []
        )
        event_type = "turn.failed" if outcome == "failed" else "turn.completed"
        event_payload: dict[str, Any] = {
            "assistant_text": assistant_text or None,
            "block_count": len(blocks),
            "blocks": blocks,
            "source": "engine_anchor_resume",
            "engine_kind": engine_kind,
            "engine_turn_id": engine_turn_id,
            "engine_outcome": outcome,
        }
        if terminal.native_reason:
            event_payload["terminal_reason"] = terminal.native_reason
        if terminal.usage is not None:
            event_payload["usage"] = dict(terminal.usage)
        if error_text:
            event_payload["error_text"] = error_text
        event, _created = await self._session_events_repo.try_claim_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": event_type,
                "causation_id": f"recover:{session_id}:{turn_id}",
                "correlation_id": f"recover:{session_id}:{turn_id}",
                "payload": event_payload,
            }
        )
        event_seq = int(event.get("event_seq") or 0)

        result = await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=event_seq,
            updates=build_turn_terminal_snapshot_updates(
                turn_id=turn_id,
                status="FAILED" if outcome == "failed" else "COMPLETED",
                error_text=error_text,
                command_id=command_id,
                terminal_reason=terminal.native_reason,
                terminal_frame=terminal_frame,
            ),
            expected_conversation_state=(
                str(snapshot.get("conversation_state") or "").strip() or None
            ),
        )
        if not isinstance(result, dict):
            result = await self._session_snapshots_repo.get_snapshot(session_id)
        if isinstance(result, dict):
            await self._sessions_repo.update_session(
                session_id,
                {"interrupt_requested": False},
            )
            logger.info(
                "engine anchor recovery completed session=%s turn=%s "
                "engine_turn_id=%s outcome=%s native_reason=%s",
                session_id,
                turn_id,
                engine_turn_id,
                outcome,
                terminal.native_reason,
            )
            if outcome != "failed" and background_manifest is not None:
                await record_engine_background_tasks_opened(
                    session_events_repo=self._session_events_repo,
                    session_id=session_id,
                    turn_id=turn_id,
                    command_id=command_id,
                    correlation_id=command_id,
                    engine_kind=engine_kind,
                    manifest=background_manifest,
                )
        return result

    async def _settle_engine_turn_transcript_pending(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        turn_id: str,
        command_id: str,
        engine_kind: str,
        remote_anchor: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Settle a detached turn into the mirror-fed recovery handoff.

        The bridge died mid-stream but the in-box runner keeps executing and
        the durable mirror keeps receiving the turn. Settling FAILED into
        turn_recovery_phase=TRANSCRIPT_PENDING (anchor preserved when one was
        observed; forced for an anchor-less death — a bridge that died before
        the first mirror row carries none) is the turn coordinator's pickup
        signal: it projects the turn from the mirror and replaces this
        provisional failure with the recovered content — or settles it
        unrecoverable itself when the box is truly gone. No error frame is
        appended here: the coordinator owns the terminal frame it recovers,
        and a fabricated one would pollute the stream it completes.
        """
        error_text = "turn detached mid-stream; transcript recovery pending"
        logger.info(
            "engine anchor recovery handing off to transcript lane "
            "session=%s turn=%s",
            session_id,
            turn_id,
        )
        event_payload = {
            "assistant_text": None,
            "block_count": 0,
            "blocks": [],
            "source": "engine_anchor_transcript_pending",
            "engine_kind": engine_kind,
            "error_text": error_text,
        }
        event, _created = await self._session_events_repo.try_claim_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.failed",
                "causation_id": f"recover:{session_id}:{turn_id}",
                "correlation_id": f"recover:{session_id}:{turn_id}",
                "payload": event_payload,
            }
        )
        event_seq = int(event.get("event_seq") or 0)
        result = await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=event_seq,
            updates=build_turn_terminal_snapshot_updates(
                turn_id=turn_id,
                status="FAILED",
                error_text=error_text,
                command_id=command_id,
                recovery_anchor=remote_anchor,
                force_transcript_pending=True,
            ),
            expected_conversation_state=(
                str(snapshot.get("conversation_state") or "").strip() or None
            ),
        )
        if not isinstance(result, dict):
            result = await self._session_snapshots_repo.get_snapshot(session_id)
        if isinstance(result, dict):
            await self._sessions_repo.update_session(
                session_id,
                {"interrupt_requested": False},
            )
        return result

    async def _fail_engine_turn_unrecoverable(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        turn_id: str,
        command_id: str,
        engine_kind: str,
        reason: str,
    ) -> dict[str, Any] | None:
        """One-shot mark an engine turn as unrecoverable and clear busy.

        Used when the snapshot lacks a usable engine turn anchor or its sandbox
        is confirmed gone. Without this short-circuit, reconcile_worker would
        revisit the same hopeless turn every tick, looping forever.
        """
        error_text = f"engine recovery unrecoverable: {reason}"
        logger.warning(
            "engine anchor recovery giving up session=%s turn=%s reason=%s",
            session_id,
            turn_id,
            reason,
        )
        terminal_frame = await self._append_recovery_error_frame(
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
            error_text=error_text,
        )
        event_payload = {
            "assistant_text": None,
            "block_count": 0,
            "blocks": [],
            "source": "engine_anchor_unrecoverable",
            "engine_kind": engine_kind,
            "error_text": error_text,
        }
        event, _created = await self._session_events_repo.try_claim_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.failed",
                "causation_id": f"recover:{session_id}:{turn_id}",
                "correlation_id": f"recover:{session_id}:{turn_id}",
                "payload": event_payload,
            }
        )
        event_seq = int(event.get("event_seq") or 0)
        result = await _commit_turn_terminal_snapshot(
            self,
            session_id=session_id,
            turn_id=turn_id,
            event_seq=event_seq,
            updates=build_turn_terminal_snapshot_updates(
                turn_id=turn_id,
                status="FAILED",
                error_text=error_text,
                command_id=command_id,
                terminal_frame=terminal_frame,
            ),
        )
        if isinstance(result, dict):
            await self._sessions_repo.update_session(
                session_id,
                {"interrupt_requested": False},
            )
        return result
