"""Durable-frame recovery helpers for :class:`SessionKernelService`.

Shared helpers for journal-terminal replay and anchor recovery: locating an
existing terminal frame for a turn, appending a synthesized recovery finish
frame, listing/normalizing a turn's durable engine-frame events, and
recovering the initiating user message. Consumed by the reconciliation
tiers and the assistant anchor-recovery mixin."""
from __future__ import annotations

import contextlib
import uuid
from astrabox.common.logger.logger_factory import get_logger
from typing import Any
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.chunk_processing import is_result_payload
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    build_turn_terminal_snapshot_updates,
    coerce_int as _coerce_int,
    find_recovery_finish_frame,
    normalize_current_turn_engine_anchor as _normalize_current_turn_engine_anchor,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
    normalize_turn_terminal_frame,
    turn_terminal_frame_matches,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _ACTIVE_SESSION_STATES,
    _MIRROR_SEQ_ENTRY_FIELD,
    _is_duplicate_frame_error,
    _payload_is_turn_terminal,
    _has_authoritative_terminal_conversation_state,
)


logger = get_logger(__name__)

from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_sandbox_io import (
    DurableRecoverySandboxIOMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_assistant import (
    DurableEngineRecoveryMixin,
)


class DurableRecoveryCheckpointMixin:
    """Durable-frame recovery helpers (journal-terminal replay support)."""

    @staticmethod
    def _terminal_frame_proof_from_doc(frame_doc: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(frame_doc, dict):
            return None
        payload = frame_doc.get("payload")
        if not isinstance(payload, dict):
            return None
        proof: dict[str, Any] = {
            "turn_id": str(frame_doc.get("turn_id") or "").strip(),
            "command_id": str(frame_doc.get("command_id") or "").strip(),
            "frame_seq": frame_doc.get("frame_seq"),
            "type": str(payload.get("type") or "").strip(),
        }
        finish_reason = str(
            payload.get("finishReason")
            or payload.get("finish_reason")
            or ""
        ).strip()
        if finish_reason:
            proof["finish_reason"] = finish_reason
        return normalize_turn_terminal_frame(proof)

    async def _find_existing_turn_terminal_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str | None = None,
        expected_type: str | None = None,
        expected_finish_reason: str | None = None,
        page_size: int = 500,
    ) -> dict[str, Any] | None:
        """Find an already durable terminal frame for a turn.

        Recovery paths may run after the live worker has persisted the real
        terminal frame but before snapshot/checkpoint projection has caught up.
        In that state the durable frame is authoritative; recovery must reuse
        it instead of appending a synthetic finish with a new command id.
        """

        requested_command_id = str(command_id or "").strip()
        after_seq = -1
        while True:
            frames = await self._session_events_repo.list_frames(
                session_id,
                command_id=requested_command_id or None,
                turn_id=turn_id,
                after_seq=after_seq,
                limit=page_size,
            )
            if not frames:
                return None
            max_seq = after_seq
            for frame in frames:
                frame_seq = _coerce_int(frame.get("frame_seq"))
                if frame_seq is not None:
                    max_seq = max(max_seq, frame_seq)
                proof = self._terminal_frame_proof_from_doc(frame)
                if not isinstance(proof, dict):
                    continue
                frame_type = str(proof.get("type") or "")
                if expected_type is not None and frame_type != expected_type:
                    continue
                if (
                    expected_finish_reason is not None
                    and proof.get("finish_reason") != expected_finish_reason
                ):
                    continue
                if frame_type == "finish" and not turn_terminal_frame_matches(
                    proof,
                    turn_id=turn_id,
                    command_id=proof.get("command_id"),
                    frame_type="finish",
                    finish_reason=str(proof.get("finish_reason") or "") or None,
                ):
                    continue
                return proof
            if len(frames) < page_size or max_seq <= after_seq:
                return None
            after_seq = max_seq

    async def _resolve_recovery_command_id(
        self,
        *,
        session_id: str,
        turn_id: str,
        snapshot: dict[str, Any],
    ) -> str:
        for value in (
            snapshot.get("last_turn_command_id"),
            snapshot.get("current_turn_worker_command_id"),
        ):
            command_id = str(value or "").strip()
            if command_id:
                return command_id
        existing_terminal = await self._find_existing_turn_terminal_frame(
            session_id=session_id,
            turn_id=turn_id,
            expected_type="finish",
            expected_finish_reason=AI_SDK_FINISH_REASON_STOP,
        )
        if isinstance(existing_terminal, dict):
            command_id = str(existing_terminal.get("command_id") or "").strip()
            if command_id:
                return command_id
        return f"recover:{session_id}:{turn_id}"

    async def _append_recovery_finish_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
    ) -> dict[str, Any]:
        existing_for_turn = await self._find_existing_turn_terminal_frame(
            session_id=session_id,
            turn_id=turn_id,
            expected_type="finish",
            expected_finish_reason=AI_SDK_FINISH_REASON_STOP,
        )
        if isinstance(existing_for_turn, dict):
            return existing_for_turn
        existing = await self._find_recovery_finish_frame(
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
        )
        if isinstance(existing, dict):
            return existing

        frame_seq = await self._session_events_repo.get_next_session_frame_seq(session_id)
        try:
            await self._session_events_repo.append_frame(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "command_id": command_id,
                    "source_kind": "turn_recovery",
                    "frame_seq": int(frame_seq),
                    "payload": {
                        "type": "finish",
                        "finishReason": AI_SDK_FINISH_REASON_STOP,
                    },
                    "created_at": utcnow_iso(),
                }
            )
        except Exception as exc:
            if not _is_duplicate_frame_error(exc):
                raise
            existing_after_race = await self._find_recovery_finish_frame(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
            )
            if isinstance(existing_after_race, dict):
                return existing_after_race
            raise
        proof = normalize_turn_terminal_frame(
            {
                "turn_id": turn_id,
                "command_id": command_id,
                "frame_seq": int(frame_seq),
                "type": "finish",
                "finish_reason": "stop",
            }
        )
        if not isinstance(proof, dict):
            raise RuntimeError("failed to build recovered finish frame proof")
        return proof

    async def _append_recovery_error_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
        error_text: str,
    ) -> dict[str, Any]:
        """Append the ERROR terminal frame for an unrecoverable turn.

        The failed-turn counterpart of ``_append_recovery_finish_frame``:
        the stream terminal for a failed turn is an ``error`` frame (see the
        bridge's ``_append_terminal_signal``), and recovery reuses any
        terminal the live worker already persisted rather than appending a
        second one.
        """
        existing_for_turn = await self._find_existing_turn_terminal_frame(
            session_id=session_id,
            turn_id=turn_id,
            expected_type="error",
        )
        if isinstance(existing_for_turn, dict):
            return existing_for_turn

        frame_seq = await self._session_events_repo.get_next_session_frame_seq(session_id)
        try:
            await self._session_events_repo.append_frame(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "command_id": command_id,
                    "source_kind": "turn_recovery",
                    "frame_seq": int(frame_seq),
                    "payload": {
                        "type": "error",
                        "errorText": error_text or "unknown error",
                    },
                    "created_at": utcnow_iso(),
                }
            )
        except Exception as exc:
            if not _is_duplicate_frame_error(exc):
                raise
            existing_after_race = await self._find_existing_turn_terminal_frame(
                session_id=session_id,
                turn_id=turn_id,
                expected_type="error",
            )
            if isinstance(existing_after_race, dict):
                return existing_after_race
            raise
        proof = normalize_turn_terminal_frame(
            {
                "turn_id": turn_id,
                "command_id": command_id,
                "frame_seq": int(frame_seq),
                "type": "error",
            }
        )
        if not isinstance(proof, dict):
            raise RuntimeError("failed to build recovered error frame proof")
        return proof

    async def _find_recovery_finish_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
    ) -> dict[str, Any] | None:
        return await find_recovery_finish_frame(
            self._session_events_repo,
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
        )

    async def _list_turn_frames(
        self,
        session_id: str,
        turn_id: str,
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        after_seq = -1
        while True:
            batch = await self._session_events_repo.list_frames(
                session_id,
                turn_id=turn_id,
                after_seq=after_seq,
                limit=500,
            )
            if not batch:
                return frames
            frames.extend(batch)
            next_seq = max(int(item.get("frame_seq") or after_seq) for item in batch)
            if len(batch) < 500 or next_seq <= after_seq:
                return frames
            after_seq = next_seq

    @staticmethod
    def _normalize_recovered_ai_sdk_frame(
        frame: dict[str, Any],
        *,
        turn_id: str,
        command_id: str,
    ) -> dict[str, Any]:
        normalized = dict(frame)
        frame_type = str(normalized.get("type") or "").strip()
        if frame_type == "data-result" and "id" not in normalized:
            result_id = str(turn_id or command_id or "").strip()
            if result_id:
                normalized["id"] = f"result:{result_id}"
        if frame_type == "data-raw-event" and "id" not in normalized:
            raw_id = str(turn_id or command_id or "").strip()
            frame_data = normalized.get("data")
            subtype = ""
            if isinstance(frame_data, dict):
                subtype = str(frame_data.get("subtype") or "").strip()
            if raw_id:
                normalized["id"] = f"raw-event:{raw_id}:{subtype or 'system'}"
        return normalized

class DurableRecoveryMaterializationMixin(
    DurableRecoverySandboxIOMixin,
    DurableRecoveryCheckpointMixin,
    DurableEngineRecoveryMixin,
):
    """Aggregate durable-recovery/materialization mixin, composed from
    the sandbox-IO, checkpoint, and assistant sub-mixins. The facade
    inherits this single class."""
