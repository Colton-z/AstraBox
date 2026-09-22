"""Repository-driven snapshot and interaction projections for the turn worker.

The ``command-and-answer-projection`` methods (``_project_command_accepted`` /
``_project_answer_persisted``) and the ``turn-message-projection-closures`` are
gathered here as :class:`_TurnProjectionMixin`, mixed into ``TurnWorker``. The
turn-message methods take ``(self, state, ctx)`` plus their original
parameters, where ``self`` is the worker, ``state`` is the per-turn
:class:`_BridgeRunState`, and ``ctx`` bundles the ``_run_bridge_command``
locals the bodies need (``command_id``, ``correlation_id``, ``user``,
``command_event``, ``ordered_assistant_segments`` ...).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from astrabox.persistence.repository.backend import is_mongo_transient_error
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.message_blocks import (
    canonicalize_terminal_message_blocks,
    drop_unresolved_tool_use_blocks,
)
from astrabox.core.service.orchestrator.tool_result_semantics import (
    TOOL_RESULT_STATE_AVAILABLE,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    build_turn_active_snapshot_updates,
)
from astrabox.core.service.orchestrator.session_kernel.engine_emission_projection import (
    project_engine_interaction_opened,
    record_engine_background_tasks_opened,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn import bridge_terminal
from astrabox.core.service.orchestrator.session_kernel.workers.turn._fencing import (
    _TurnFencedOut,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._helpers import (
    _replace_plain_text_blocks,
)

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
        _BridgeRunState,
    )

logger = get_logger(__name__)


class _TurnProjectionMixin:
    """Projection methods mixed into :class:`TurnWorker`.

    All members read ``self._*_repo`` / ``self._get_max_frame_seq`` off the
    worker instance; the turn-message projections additionally take the
    per-turn ``state`` and the bridge ``ctx`` explicitly.
    """

    async def _project_command_accepted(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        command_event: dict[str, Any],
        command_type: str,
        readmit_after_settle: bool = False,
    ) -> None:
        if command_type == "InterruptTurn":
            return
        command_id = str(command_event.get("causation_id") or "").strip() or None
        conversation_state = "PROCESSING"
        active_interaction_id = None
        if command_type == "AnswerInteraction":
            active_interaction = await self._interaction_snapshots_repo.get_active_interaction(session_id)
            if isinstance(active_interaction, dict):
                active_interaction_id = str(active_interaction.get("interaction_id") or "").strip() or None
        command_seq = int(command_event.get("event_seq") or 0)
        active_update_kwargs: dict[str, Any] = {
            "worker_command_id": command_id,
        }
        snapshot_updates = build_turn_active_snapshot_updates(
            conversation_state=conversation_state,
            turn_id=turn_id,
            active_interaction_id=active_interaction_id,
            **active_update_kwargs,
        )
        if command_id:
            snapshot_updates["worker_heartbeat_at"] = utcnow_iso()
        if command_type == "StartTurn":
            snapshot_updates["current_turn_remote_anchor"] = None
        # Single-dispatch guard: a StartTurn only ever admits a turn into
        # the empty slot. Without this, a concurrently-accepted StartTurn
        # (higher event_seq, different turn_id) would win the watermark CAS
        # purely on ordering and silently re-project over a turn that is
        # still actively PROCESSING/STREAMING/WAITING_FOR_INTERACTION —
        # orphaning it. Every terminal transition (build_turn_terminal_
        # snapshot_updates) and the initial session bootstrap
        # (_initialize_create_snapshots) both set current_turn_id to None,
        # so gating on "no turn currently owns the slot" still admits the
        # legitimate sequential case (new turn after terminal) and the
        # reconcile path (which also clears current_turn_id on convergence).
        start_turn_extra_filter = (
            {"current_turn_id": None} if command_type == "StartTurn" else None
        )

        if readmit_after_settle:
            # A handoff continuation re-runs a command whose accepted event
            # predates the settle that stranded its input, so that event's seq
            # can never pass the conversation watermark. Admission authority is
            # the empty turn slot instead — the same CAS StartTurn uses above.
            # Losing the CAS means another turn took the session meanwhile; the
            # sweep after that turn's settle re-drives this input.
            admitted = await self._session_snapshots_repo.force_update_fields(
                session_id,
                snapshot_updates,
                extra_filter={"current_turn_id": None},
            )
            if not admitted:
                logger.warning(
                    "handoff admission lost the turn slot session=%s turn=%s command=%s",
                    session_id,
                    turn_id,
                    command_id,
                )
            return

        # The command event is already the durable user message. Only the
        # snapshot projection remains, guarded by its event watermark.
        async def apply_snapshot_update() -> None:
            applied_snapshot = await self._session_snapshots_repo.apply_channel_update(
                session_id,
                channel="conversation",
                event_seq=command_seq,
                updates=snapshot_updates,
                extra_filter=start_turn_extra_filter,
            )
            if applied_snapshot is None and turn_id and command_id:
                heartbeat_updates = {
                    "current_turn_worker_command_id": command_id,
                    "worker_heartbeat_at": utcnow_iso(),
                }
                refreshed = await self._session_snapshots_repo.force_update_fields(
                    session_id,
                    heartbeat_updates,
                    extra_filter={
                        "current_turn_id": turn_id,
                        "current_turn_worker_command_id": command_id,
                    },
                )
                if not refreshed:
                    await self._session_snapshots_repo.force_update_fields(
                        session_id,
                        heartbeat_updates,
                        extra_filter={
                            "current_turn_id": turn_id,
                            "current_turn_worker_command_id": None,
                        },
                    )

        await apply_snapshot_update()


    async def _project_answer_persisted(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        command_event: dict[str, Any],
        payload: dict[str, Any],
    ) -> int:
        interaction_id = str(payload.get("interaction_id") or "").strip()
        response_payload = payload.get("answer")
        interaction_response = payload.get("interaction_response")
        if isinstance(interaction_response, dict):
            interaction_id = str(interaction_response.get("interaction_id") or interaction_id or "").strip()
            response_payload = dict(interaction_response)
        if not interaction_id or not isinstance(response_payload, dict):
            return int(command_event.get("event_seq") or 0)
        command_id = str(command_event.get("causation_id") or "").strip() or None
        command_seq = int(command_event.get("event_seq") or 0)
        active_interaction = await self._interaction_snapshots_repo.get_interaction(
            session_id,
            interaction_id,
        )
        if not isinstance(active_interaction, dict):
            active_interaction = await self._interaction_snapshots_repo.get_active_interaction(
                session_id
            )
        if (
            isinstance(active_interaction, dict)
            and str(active_interaction.get("interaction_id") or "").strip() != interaction_id
        ):
            raise RuntimeError(
                f"active interaction does not match answer command: "
                f"expected={interaction_id} active={active_interaction.get('interaction_id')}"
            )
        existing_answer_command_id = (
            str((active_interaction or {}).get("answer_command_id") or "").strip()
            or None
        )
        if (
            isinstance(active_interaction, dict)
            and str(active_interaction.get("interaction_state") or "").strip() == "ANSWERED"
            and existing_answer_command_id
            and command_id
            and existing_answer_command_id != command_id
        ):
            logger.info(
                "session kernel ignored duplicate answer command session=%s interaction=%s owner=%s command=%s",
                session_id,
                interaction_id,
                existing_answer_command_id,
                command_id,
            )
            return int(command_event.get("event_seq") or 0)

        answer_projection = {
            "turn_id": turn_id,
            # Carried from the open record, never re-minted: the projection
            # has no vocabulary of its own to guess a presentation from.
            "presentation": str(
                (active_interaction or {}).get("presentation") or ""
            ).strip()
            or None,
            "tool_name": str((active_interaction or {}).get("tool_name") or "").strip() or None,
            "tool_call_id": str((active_interaction or {}).get("tool_call_id") or "").strip() or None,
            "raw_input": dict((active_interaction or {}).get("raw_input") or {}),
            "answer_command_id": command_id,
            "interaction_state": "ANSWERED",
            "response": dict(response_payload),
            "active": False,
        }
        projected_interaction = await self._interaction_snapshots_repo.try_answer_interaction(
            session_id=session_id,
            interaction_id=interaction_id,
            source_event_seq_applied=command_seq,
            answer=dict(response_payload),
            updates=answer_projection,
        )
        if not isinstance(projected_interaction, dict):
            # The CAS refused. Its precondition is interaction_state == OPEN,
            # so this is either a second answer or a row in a state nobody
            # expects — and until it says which, an interaction that stays
            # active looks identical to one nothing ever tried to answer.
            logger.info(
                "answer projection: CAS refused session=%s interaction=%s seq=%s",
                session_id,
                interaction_id,
                command_seq,
            )
            current_interaction = await self._interaction_snapshots_repo.get_interaction(
                session_id,
                interaction_id,
            )
            current_state = str((current_interaction or {}).get("interaction_state") or "").strip()
            current_answer_command_id = (
                str((current_interaction or {}).get("answer_command_id") or "").strip()
                or None
            )
            if (
                isinstance(current_interaction, dict)
                and current_state == "ANSWERED"
                and command_id
                and current_answer_command_id == command_id
            ):
                projected_interaction = current_interaction
            elif (
                isinstance(current_interaction, dict)
                and current_state == "ANSWERED"
                and current_answer_command_id
                and command_id
                and current_answer_command_id != command_id
            ):
                logger.info(
                    "session kernel answer command lost interaction claim session=%s interaction=%s owner=%s command=%s",
                    session_id,
                    interaction_id,
                    current_answer_command_id,
                    command_id,
                )
                return command_seq
            else:
                raise RuntimeError(
                    f"failed to claim interaction answer: "
                    f"session={session_id} interaction={interaction_id} "
                    f"state={current_state or '<missing>'} owner={current_answer_command_id}"
                )
        active_interaction = projected_interaction
        if turn_id and command_id:
            await self._session_snapshots_repo.force_update_fields(
                session_id,
                {
                    "current_turn_worker_command_id": command_id,
                    "worker_heartbeat_at": utcnow_iso(),
                },
                extra_filter={"current_turn_id": turn_id},
            )

        event_doc = {
            "session_id": session_id,
            "channel": "interaction",
            "turn_id": turn_id,
            "event_type": "interaction.answer_persisted",
            "causation_id": command_id,
            "correlation_id": str(command_event.get("correlation_id") or "").strip() or None,
            "payload": {
                "interaction_id": interaction_id,
                "response": dict(response_payload),
            },
        }
        if event_doc["causation_id"]:
            event, _created = await self._session_events_repo.try_claim_event(event_doc)
        else:
            event = await self._session_events_repo.append_event(event_doc)
        event_seq = int(event.get("event_seq") or command_seq)
        return event_seq


    async def _project_turn_requested(self, state: _BridgeRunState, ctx: Any) -> None:
        session_id = ctx.session_id
        command_id = ctx.command_id
        correlation_id = ctx.correlation_id
        client_message_id = ctx.client_message_id
        command_type = ctx.command_type
        event = await bridge_terminal._find_existing_conversation_event(self, state, ctx, "turn.requested")
        if event is None:
            event = await self._session_events_repo.append_event(
                {
                    "session_id": session_id,
                    "channel": "conversation",
                    "turn_id": state.effective_turn_id or None,
                    "event_type": "turn.requested",
                    "causation_id": command_id,
                    "correlation_id": correlation_id,
                    "payload": {
                        "command_id": command_id,
                        "client_message_id": client_message_id,
                    },
                }
            )
        state.last_event_seq = int(event.get("event_seq") or state.last_event_seq)
        if command_type == "StartTurn":
            state.current_turn_remote_anchor = None
            state.persisted_current_turn_remote_anchor = None
            if state.current_turn_engine_anchor is None:
                state.persisted_current_turn_engine_anchor = None
        requested_updates = build_turn_active_snapshot_updates(
            conversation_state="STREAMING",
            turn_id=state.effective_turn_id,
            worker_command_id=command_id,
            current_turn_remote_anchor=(
                None if command_type == "StartTurn" else state.current_turn_remote_anchor
            ),
            current_turn_engine_anchor=state.current_turn_engine_anchor,
        )
        if command_id:
            requested_updates["worker_heartbeat_at"] = utcnow_iso()
        result = await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=state.last_event_seq,
            updates=requested_updates,
            extra_filter={"current_turn_id": state.effective_turn_id} if state.effective_turn_id else None,
        )
        if result is None and state.effective_turn_id:
            raise _TurnFencedOut(state.effective_turn_id)


    async def _project_turn_requested_with_retry(self, state: _BridgeRunState, ctx: Any) -> None:
        session_id = ctx.session_id
        deadline = time.monotonic() + self._requested_projection_retry_window_s
        delay_s = self._requested_projection_retry_delay_s
        while True:
            try:
                await self._project_turn_requested(state, ctx)
                return
            except Exception as exc:
                if not is_mongo_transient_error(exc) or time.monotonic() >= deadline:
                    raise
                logger.warning(
                    "session kernel requested projection retry session=%s turn_id=%s err=%s",
                    session_id,
                    state.effective_turn_id or None,
                    exc,
                )
                await asyncio.sleep(delay_s)
                delay_s = min(delay_s * 2, 2.0)


    def _build_projected_tool_uses(self, state: _BridgeRunState, ctx: Any) -> dict[str, dict[str, Any]]:
        return {
            str(tc_id): {
                "name": str((tc_data or {}).get("name") or ""),
                "input": (
                    dict((tc_data or {}).get("input"))
                    if isinstance((tc_data or {}).get("input"), dict)
                    else {}
                ),
            }
            for tc_id, tc_data in state.accumulated_tool_uses.items()
            if str(tc_id).strip()
        }


    def _build_projected_assistant_blocks(self, state: _BridgeRunState, ctx: Any, *, include_result: bool) -> list[dict[str, Any]]:
        ordered_assistant_segments = ctx.ordered_assistant_segments
        projected_blocks: list[dict[str, Any]] = []
        if state.accumulated_thinking_parts:
            projected_blocks.append(
                {
                    "type": "thinking",
                    "thinking": "".join(state.accumulated_thinking_parts),
                }
            )
        # Replay AI SDK frame arrival order so the derived message blocks
        # interleave text and tool blocks the same way the live SSE stream
        # does.
        seen_tool_ids: set[str] = set()
        for segment in ordered_assistant_segments:
            seg_type = str(segment.get("type") or "")
            if seg_type == "text":
                text = str(segment.get("text") or "")
                if not text.strip():
                    continue
                projected_blocks.append({"type": "text", "text": text})
            elif seg_type == "tool":
                tc_id = str(segment.get("tc_id") or "").strip()
                if not tc_id:
                    continue
                seen_tool_ids.add(tc_id)
                projected_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc_id,
                        "name": str(segment.get("name") or ""),
                        "input": (
                            dict(segment.get("input"))
                            if isinstance(segment.get("input"), dict)
                            else {}
                        ),
                    }
                )
                result_block = segment.get("result")
                if isinstance(result_block, dict):
                    projected_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc_id,
                            "content": str(result_block.get("content") or ""),
                            "is_error": result_block.get("is_error") is True,
                            "tool_result_state": str(
                                result_block.get("tool_result_state")
                                or TOOL_RESULT_STATE_AVAILABLE
                            ),
                        }
                    )
        # Cover tool calls that landed in accumulated state before their
        # ordered segment was recorded. They are appended at the tail so their
        # data is preserved.
        for tc_id, tc_data in self._build_projected_tool_uses(state, ctx).items():
            if tc_id in seen_tool_ids or not tc_id:
                continue
            projected_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc_id,
                    "name": tc_data.get("name", ""),
                    "input": tc_data.get("input", {}),
                }
            )
            if tc_id in state.accumulated_tool_results:
                tool_result = state.accumulated_tool_results[tc_id]
                projected_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc_id,
                        "content": str(tool_result.get("content") or ""),
                        "is_error": tool_result.get("is_error") is True,
                        "tool_result_state": str(
                            tool_result.get("tool_result_state")
                            or TOOL_RESULT_STATE_AVAILABLE
                        ),
                    }
                )
        # Final fallback: if no segments and no buffered tools accumulated but
        # plain text did, render it as a single text block so the turn
        # doesn't collapse to empty content.
        if not projected_blocks and (state.last_assistant_text or "").strip():
            projected_blocks.append({"type": "text", "text": state.last_assistant_text})
        if include_result and state.last_result_data:
            projected_blocks.append({"type": "result", **state.last_result_data})
        return projected_blocks


    def _build_terminal_assistant_blocks(self, state: _BridgeRunState, ctx: Any) -> list[dict[str, Any]]:
        projected_blocks = self._build_projected_assistant_blocks(state, ctx, include_result=True)
        return canonicalize_terminal_message_blocks(
            _replace_plain_text_blocks(
                projected_blocks,
                authoritative_text=state.last_assistant_text,
            ),
        )


    async def _record_background_task_manifest_if_needed(self, state: _BridgeRunState, ctx: Any) -> None:
        session_id = ctx.session_id
        command_id = ctx.command_id
        correlation_id = ctx.correlation_id
        if not state.effective_turn_id or not command_id:
            return
        # The engine has already reduced its vendor events to this neutral id
        # manifest. Core persists the declaration but never parses the raw
        # vendor vocabulary that produced it.
        manifest = state.background_tasks_opened
        if not isinstance(manifest, dict):
            return
        engine_kind = str(manifest.get("engine_kind") or "").strip()
        await record_engine_background_tasks_opened(
            session_events_repo=self._session_events_repo,
            session_id=session_id,
            turn_id=state.effective_turn_id,
            command_id=command_id,
            correlation_id=correlation_id,
            engine_kind=engine_kind,
            manifest={
                key: value for key, value in manifest.items() if key != "engine_kind"
            },
        )


    async def _project_interaction_opened(self, state: _BridgeRunState, ctx: Any, pending: dict[str, Any], tool_name: str) -> None:
        session_id = ctx.session_id
        command_id = ctx.command_id
        correlation_id = ctx.correlation_id
        projection = await project_engine_interaction_opened(
            session_events_repo=self._session_events_repo,
            session_snapshots_repo=self._session_snapshots_repo,
            interaction_snapshots_repo=self._interaction_snapshots_repo,
            session_id=session_id,
            turn_id=state.effective_turn_id,
            command_id=command_id,
            correlation_id=correlation_id,
            pending=pending,
            tool_name=tool_name,
            source_event_seq_applied=state.last_event_seq,
            current_turn_remote_anchor=state.current_turn_remote_anchor,
            current_turn_engine_anchor=state.current_turn_engine_anchor,
        )
        state.last_event_seq = max(state.last_event_seq, projection.event_seq)
        if projection.fenced:
            raise _TurnFencedOut(state.effective_turn_id)
        if projection.waiting:
            state.waiting_for_interaction = True
            state.turn_settled = True
