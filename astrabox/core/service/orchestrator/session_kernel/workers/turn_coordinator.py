"""TurnCoordinator: per-turn durable recovery with lease fence and retry budget.

Resolves unresolved turns by:
1. Claiming a per-turn durable lease via ``try_claim_event``
2. Reading the settled projection from the durable mirror, sliced by the
   accepted command's SDK input
3. Writing ``turn.recovered`` as durable truth to the journal
4. Settling the snapshot projection

A turn only dies on evidence that it cannot make further progress: the session's
durable ``runtime_unavailable`` flag, a terminal lifecycle probe of the box the
turn was dispatched to, or no such box on record at all. Uncertainty is not
evidence — it retries on a later pass.

The coordinator never writes directly to the message view or snapshots during
the claim/fetch phase — only after ``turn.recovered`` is durably journaled
does it update the projection layer.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from astrabox.persistence.repository._compat import DuplicateKeyError, WriteError

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import parse_iso_utc, utcnow_iso
from astrabox.core.service.orchestrator.engine.base import EngineTranscriptRecovery
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    command_input_id,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    USER_STOP_FAILURE_TEXT,
    find_recovery_finish_frame,
    journal_terminal_for_turn,
    normalize_turn_terminal_frame,
    resolve_transcript_recovery_terminal_authority,
    turn_terminal_frame_matches,
)
from astrabox.core.service.orchestrator.session_kernel.turn_terminal_state_machine import (
    TURN_TERMINAL_DEACTIVATE_TURN,
    TURN_TERMINAL_EVENT_CLAIM,
    TURN_TERMINAL_EVENT_EXISTING,
    TurnTerminalAssistantSpec,
    TurnTerminalEventSpec,
    TurnTerminalSideEffects,
    TurnTerminalSnapshotSpec,
    TurnTerminalStateMachine,
    TurnTerminalTransition,
)

logger = get_logger(__name__)

MAX_RECOVERY_ATTEMPTS = 10
MAX_RECOVERY_WINDOW_S = 3600  # 1 hour

_UNRESOLVED_SNAPSHOT_STATES = frozenset({"IDLE"})
_MIRROR_SEQ_ENTRY_FIELD = "__astrabox_mirror_seq"
# These frames establish and resume the transport but carry no assistant
# presentation. Unknown or content-bearing frames fail closed so recovery never
# duplicates output the live writer already materialized.
_RECOVERY_MATERIALIZATION_PREAMBLE_TYPES = frozenset(
    {
        "start",
        "data-resume-cursor",
        "data-input-consumed",
        "data-session-store-reload",
        "data-turn-accepted",
    }
)


def _session_transcript_config_dir(session: dict[str, Any] | None) -> str | None:
    if not isinstance(session, dict):
        return None
    candidates: list[Any] = [session.get("runtime_identity")]
    workspace_ref = session.get("workspace_ref")
    if isinstance(workspace_ref, dict):
        candidates.append(workspace_ref.get("runtime_identity"))
    for identity in candidates:
        if not isinstance(identity, dict):
            continue
        config_dir = str(identity.get("config_dir") or "").strip()
        if config_dir:
            return config_dir
    return None


def _is_duplicate_frame_error(exc: Exception) -> bool:
    if isinstance(exc, DuplicateKeyError):
        return True
    if isinstance(exc, WriteError):
        return "Duplicate entry" in str(exc) or exc.code in (1, 11000)
    return False


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


class TurnCoordinator:
    """Resolves unresolved turns using the durable transcript mirror.

    Each ``try_resolve_turn`` call is atomic at the per-turn level:
    only one coordinator across all processes can claim a given turn.
    """

    def __init__(
        self,
        *,
        session_events_repo: Any,
        session_snapshots_repo: Any,
        sessions_repo: Any,
        message_view: Any,
        runtime_manager: Any,
        interaction_snapshots_repo: Any | None = None,
        transcript_entries_repo: Any | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._journal = session_events_repo
        self._snapshots = session_snapshots_repo
        self._sessions = sessions_repo
        self._message_view = message_view
        self._session_events = session_events_repo
        self._interaction_snapshots = interaction_snapshots_repo
        self._transcript_entries = transcript_entries_repo
        self._runtime_manager = runtime_manager
        self._worker_id = worker_id or f"coordinator-{uuid.uuid4().hex[:8]}"
        self._terminal_state_machine = TurnTerminalStateMachine(
            session_events_repo=self._journal,
            session_snapshots_repo=self._snapshots,
            sessions_repo=self._sessions,
            interaction_snapshots_repo=self._interaction_snapshots,
        )

    async def _initiating_user_prompt_text(
        self,
        session_id: str,
        turn_id: str,
    ) -> str:
        user_row = await self._message_view.get_message(
            session_id,
            f"{turn_id}:user",
        )
        if isinstance(user_row, dict):
            content = str(user_row.get("content") or "")
            if content.strip():
                return content

        events = await self._journal.list_events(
            session_id,
            after_seq=0,
            channel="command",
            turn_id=turn_id,
            event_type="command.accepted",
            limit=20,
        )
        for event in reversed(events):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("command_type") or "").strip() != "StartTurn":
                continue
            input_id = command_input_id(payload)
            if input_id is not None:
                sdk_user_row = await self._message_view.get_message(
                    session_id,
                    f"{input_id}:user",
                )
                if isinstance(sdk_user_row, dict):
                    content = str(sdk_user_row.get("content") or "")
                    if content.strip():
                        return content
            content = str(payload.get("content") or "")
            if content.strip():
                return content
        return ""

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

    async def _find_existing_turn_finish_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        page_size: int = 500,
    ) -> dict[str, Any] | None:
        after_seq = -1
        while True:
            frames = await self._session_events.list_frames(
                session_id,
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
                if not turn_terminal_frame_matches(
                    proof,
                    turn_id=turn_id,
                    command_id=(proof or {}).get("command_id"),
                    frame_type="finish",
                    finish_reason=AI_SDK_FINISH_REASON_STOP,
                ):
                    continue
                return proof
            if len(frames) < page_size or max_seq <= after_seq:
                return None
            after_seq = max_seq

    async def _append_recovery_finish_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
    ) -> dict[str, Any]:
        existing_for_turn = await self._find_existing_turn_finish_frame(
            session_id=session_id,
            turn_id=turn_id,
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

        frame_seq = await self._session_events.get_next_session_frame_seq(session_id)
        existing_after_seq_read = await self._find_existing_turn_finish_frame(
            session_id=session_id,
            turn_id=turn_id,
        )
        if isinstance(existing_after_seq_read, dict):
            return existing_after_seq_read
        doc = {
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
        try:
            await self._session_events.append_frame(doc)
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

    @staticmethod
    def _assistant_text_from_blocks(
        blocks: list[dict[str, Any]],
        assistant_text: str,
    ) -> str:
        text = str(assistant_text or "")
        if text.strip():
            return text
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type == "text":
                parts.append(str(block.get("text") or ""))
            elif block_type == "result":
                parts.append(str(block.get("result") or ""))
        return "".join(parts)

    async def _materialize_recovered_assistant_frames(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
        assistant_text: str,
        blocks: list[dict[str, Any]],
        source_mirror_seq: int | None,
    ) -> None:
        after_seq = -1
        page_size = 500
        while True:
            page = await self._session_events.list_frames(
                session_id,
                turn_id=turn_id,
                after_seq=after_seq,
                limit=page_size,
            )
            if not page:
                break
            page_end = after_seq
            for frame in page:
                frame_seq = _coerce_int(frame.get("frame_seq"))
                if frame_seq is not None:
                    page_end = max(page_end, frame_seq)
                payload = frame.get("payload")
                frame_type = (
                    str(payload.get("type") or "").strip()
                    if isinstance(payload, dict)
                    else ""
                )
                if (
                    str(frame.get("source_kind") or "").strip()
                    in {"turn_recovery", "transcript_mirror"}
                    or frame_type not in _RECOVERY_MATERIALIZATION_PREAMBLE_TYPES
                ):
                    return
            if len(page) < page_size or page_end <= after_seq:
                break
            after_seq = page_end

        text = self._assistant_text_from_blocks(blocks, assistant_text)
        if not text.strip():
            return

        text_id = "recovered-text-0"
        payloads = [
            {"type": "start-step"},
            {"type": "text-start", "id": text_id},
            {"type": "text-delta", "id": text_id, "delta": text},
            {"type": "text-end", "id": text_id},
            {"type": "finish-step"},
        ]
        frame_seq = await self._session_events.allocate_session_frame_seq(
            session_id,
            count=len(payloads),
        )
        docs: list[dict[str, Any]] = []
        for source_frame_index, payload in enumerate(payloads):
            doc: dict[str, Any] = {
                "session_id": session_id,
                "turn_id": turn_id,
                "command_id": command_id,
                "source_kind": "transcript_mirror",
                "frame_seq": int(frame_seq),
                "payload": payload,
                "created_at": utcnow_iso(),
            }
            if source_mirror_seq is not None:
                doc.update(
                    {
                        "source_kind": "transcript_mirror",
                        "source_mirror_seq": int(source_mirror_seq),
                        "source_frame_index": int(source_frame_index),
                    }
                )
            docs.append(doc)
            frame_seq += 1

        append_frames = getattr(self._session_events, "append_frames", None)
        if callable(append_frames):
            await append_frames(docs)
            return
        for doc in docs:
            await self._session_events.append_frame(doc)

    async def _find_recovery_finish_frame(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str,
    ) -> dict[str, Any] | None:
        return await find_recovery_finish_frame(
            self._session_events,
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
        )

    async def try_resolve_turn(
        self,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        owner_dead: bool = False,
    ) -> dict[str, Any] | None:
        """Attempt to resolve an unresolved turn.

        Returns the updated snapshot on success, or None if no action was taken.

        Cross-process safety: multiple coordinators may race through the
        read/fetch phase (harmless — projecting the turn from the mirror
        writes nothing). Exactly-once semantics come from ``try_claim_event``
        on the terminal write (``turn.recovered`` / ``turn.failed``).

        ``owner_dead`` is the reconcile lane's explicit verdict that the
        active writer is gone (stale worker heartbeat). Only then may the
        coordinator take a PROCESSING/STREAMING turn: the single-owner rule —
        never synthesize recovery while a live writer can still append — is
        preserved because the caller, not this method, is the authority on
        owner death, and the terminal write is still claim-fenced. This is
        what lets a turn abandoned by a dying process settle COMPLETED from
        the mirror with no interim FAILED verdict ever surfacing.
        """
        turn_id = self._get_unresolved_turn_id(snapshot)
        if not turn_id and owner_dead:
            conv_state = str(snapshot.get("conversation_state") or "").strip()
            if conv_state in {"PROCESSING", "STREAMING"}:
                turn_id = str(snapshot.get("current_turn_id") or "").strip() or None
        if not turn_id:
            return None

        # ── Terminal proof ───────────────────────────────────────────────────
        if self._snapshot_has_completed_terminal_proof(snapshot, turn_id):
            return None

        # ── Durable mirror read ──────────────────────────────────────────────
        # Recovery is sandbox-independent: the per-session sandbox may be
        # terminated/hibernated when recovery runs, so this method projects
        # the turn from the externalized mirror. The turn's slice is found by
        # the CLI's own boundary: the prompt user entry matching the turn's
        # durable accepted input. The mirror carries no platform turn stamp.
        prompt_text = await self._initiating_user_prompt_text(session_id, turn_id)
        engine_kind = resolve_session_engine_kind(session)
        adapter = get_engine_adapter(engine_kind)
        if not isinstance(adapter, EngineTranscriptRecovery):
            # This collaborator owns only the durable-transcript lane. A
            # resident engine's continuation is handled from its durable turn
            # anchor by DurableEngineRecoveryMixin; absence here is therefore
            # a structural route choice, not a malformed adapter declaration.
            return None
        raw_items = adapter.slice_recovery_turn(
            await self._transcript_entries.load_recovery_entries_by_platform_session(
                session_id
            ),
            prompt_text=prompt_text,
        )
        done = adapter.has_transcript_terminal_evidence(raw_items)
        settled = adapter.project_settled_transcript(raw_items, done=done)

        if settled.completed:
            source_mirror_seq = max(
                [
                    seq
                    for seq in (
                        _coerce_int(raw.get(_MIRROR_SEQ_ENTRY_FIELD))
                        for raw in raw_items
                        if isinstance(raw, dict)
                    )
                    if seq is not None
                ],
                default=None,
            )
            projection = {
                "done": True,
                "blocks": settled.blocks,
                "assistant_text": settled.assistant_text,
                "interrupted": bool(getattr(settled, "interrupted", False)),
                "source_mirror_seq": source_mirror_seq,
            }
            return await self._complete_recovery(
                session_id, turn_id, session, snapshot, projection,
                owner_dead=owner_dead,
            )

        # Mirror has no terminal proof yet. If the sandbox is gone the turn can never
        # complete from it — settle unrecoverable from durable data. Otherwise it is
        # genuinely still in progress; retry on a later pass.
        #
        # Source-of-truth order matters. The session owns its sandbox lifecycle, so
        # its durable runtime_unavailable flag — set whenever the platform
        # reclaims/terminates/expires the box, backend-agnostic — is the
        # authoritative "the sandbox is gone" verdict. The lifecycle probe is only
        # the fallback for a still-attached sandbox that may have died out-of-band,
        # and it is backend-coupled: the terminal-state set reflects one backend's
        # state vocabulary; some backends destroy their boxes (no 'terminated'
        # tombstone, lagging get_info) so the probe never sees it terminal. Trust
        # the owner first; probe only when the session still claims a live sandbox.
        sandbox_id = await self._turn_local_sandbox_id(session_id, turn_id)
        if not sandbox_id:
            # Nothing ever carried this turn, so nothing can ever finish it. This is
            # terminal for a turn that failed before dispatch — and if a dispatch did
            # happen, the missing journal row is a durability defect that must stay
            # visible, not be masked by waiting for a box the record never named.
            return await self._settle_unrecoverable(
                session_id, turn_id, session, snapshot,
                reason="no_turn_local_sandbox",
                owner_dead=owner_dead,
            )
        sandbox_reclaimed = bool(session.get("runtime_unavailable"))
        if sandbox_reclaimed or await self._sandbox_is_terminal(sandbox_id):
            return await self._settle_unrecoverable(
                session_id, turn_id, session, snapshot,
                reason="sandbox_terminated_incomplete_mirror",
                owner_dead=owner_dead,
            )
        logger.info(
            "turn coordinator: turn still in progress (mirror incomplete) session=%s turn=%s",
            session_id, turn_id,
        )
        return None

    async def _sandbox_is_terminal(self, sandbox_id: str) -> bool:
        """Whether the sandbox is in a terminal lifecycle state (state query only)."""
        sid = str(sandbox_id or "").strip()
        if not sid:
            return False
        try:
            probe = await self._runtime_manager.get_sandbox_lifecycle_probe(sid)
        except Exception:
            # Probe uncertainty must not fabricate a FAILED turn: treat as non-terminal
            # so recovery retries on a later pass.
            logger.warning(
                "turn coordinator: sandbox lifecycle probe failed sandbox=%s", sid,
                exc_info=True,
            )
            return False
        return self._runtime_manager._is_terminal_turn_sandbox_lifecycle_probe(probe)

    @staticmethod
    def _snapshot_has_completed_terminal_proof(snapshot: dict[str, Any], turn_id: str) -> bool:
        if str(snapshot.get("conversation_state") or "").strip() != "IDLE":
            return False
        if str(snapshot.get("last_turn_id") or "").strip() != turn_id:
            return False
        if str(snapshot.get("last_turn_status") or "").strip() != "COMPLETED":
            return False
        command_id = str(snapshot.get("last_turn_command_id") or "").strip()
        if not command_id:
            return False
        return turn_terminal_frame_matches(
            snapshot.get("last_turn_terminal_frame"),
            turn_id=turn_id,
            command_id=command_id,
            frame_type="finish",
            finish_reason=AI_SDK_FINISH_REASON_STOP,
        )

    @staticmethod
    def _conversation_watermark(snapshot: dict[str, Any] | None) -> int:
        if not isinstance(snapshot, dict):
            return 0
        return _coerce_int(snapshot.get("conversation_event_seq_applied")) or 0

    # ── Turn-local sandbox ───────────────────────────────────────────────

    async def _turn_local_sandbox_id(self, session_id: str, turn_id: str) -> str | None:
        """The box this turn was dispatched to, from durable journal data only.

        Recovery projects the turn out of the mirror, sliced by the accepted
        input, so a sandbox id answers exactly one question: can this turn still
        make progress? The session's current sandbox_id cannot answer it — a
        re-boxed session would have recovery probe a box that never carried the
        turn.

        The durable mirror is the transcript authority, so this lookup does not
        derive an unused remote ``sandbox_turn_id`` anchor. Absence of such an
        anchor is not terminal evidence and must not settle a producer that may
        still be streaming.
        """
        context = await self._derive_dispatch_recovery_context(session_id, turn_id)
        return str((context or {}).get("sandbox_id") or "").strip() or None

    async def _derive_dispatch_recovery_context(
        self,
        session_id: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        dispatch_events = await self._journal.list_events(
            session_id,
            turn_id=turn_id,
            event_type="dispatch.confirmed",
            limit=1,
        )
        if not dispatch_events:
            return None
        payload = dispatch_events[0].get("payload") or {}
        rc = payload.get("recovery_context") or {}
        return dict(rc) if isinstance(rc, dict) else None

    # ── Recovery completion ──────────────────────────────────────────────
    # Recovery is sandbox-independent: try_resolve_turn projects from the durable
    # Mongo mirror, never the sandbox WAL (see the read at the top of that method).

    async def _claim_recovered_projection_event(
        self,
        *,
        session_id: str,
        turn_id: str,
        base_event: dict[str, Any],
        latest_snapshot: dict[str, Any],
        assistant_text: str,
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        watermark = self._conversation_watermark(latest_snapshot)
        base_seq = int(base_event.get("event_seq") or 0)
        if watermark < base_seq:
            return None
        event, _created = await self._journal.try_claim_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.recovered",
                "causation_id": f"recover-projection:{session_id}:{turn_id}:{watermark}",
                "correlation_id": f"recover:{session_id}:{turn_id}",
                "payload": {
                    "assistant_text": assistant_text or None,
                    "block_count": len(blocks),
                    "blocks": blocks,
                    "source": "turn_coordinator_projection_retry",
                    "supersedes_event_seq": base_seq,
                    "snapshot_conversation_event_seq_applied": watermark,
                },
            }
        )
        return event if isinstance(event, dict) else None

    @staticmethod
    def _command_id_candidate(value: Any) -> str | None:
        candidate = str(value or "").strip()
        return candidate or None

    @classmethod
    def _event_command_id(cls, event: dict[str, Any] | None) -> str | None:
        if not isinstance(event, dict):
            return None
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        return (
            cls._command_id_candidate(payload.get("command_id"))
            or cls._command_id_candidate(event.get("causation_id"))
        )

    async def _resolve_recovery_command_id(
        self,
        *,
        session_id: str,
        turn_id: str,
        snapshot: dict[str, Any],
        recovery_event: dict[str, Any] | None = None,
    ) -> str:
        """Resolve the command owner for a recovered platform turn."""
        event_command_id = self._event_command_id(recovery_event)
        if event_command_id and not event_command_id.startswith("recover:"):
            return event_command_id

        dispatch_events = await self._journal.list_events(
            session_id,
            after_seq=0,
            channel="conversation",
            turn_id=turn_id,
            event_type="dispatch.confirmed",
            limit=50,
        )
        for event in reversed(dispatch_events):
            command_id = self._event_command_id(event)
            if command_id:
                return command_id

        command_events = await self._journal.list_events(
            session_id,
            after_seq=0,
            channel="command",
            turn_id=turn_id,
            event_type="command.accepted",
            limit=50,
        )
        for event in reversed(command_events):
            command_id = self._event_command_id(event)
            if command_id:
                return command_id

        if str(snapshot.get("current_turn_id") or "").strip() == turn_id:
            command_id = self._command_id_candidate(
                snapshot.get("current_turn_worker_command_id")
            )
            if command_id:
                return command_id

        if event_command_id:
            return event_command_id
        return f"recover:{session_id}:{turn_id}"

    async def _complete_recovery(
        self,
        session_id: str,
        turn_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        projection: dict[str, Any],
        *,
        owner_dead: bool = False,
    ) -> dict[str, Any] | None:
        """Write recovered content as durable truth and refresh projection."""
        blocks = [
            dict(b) for b in (projection.get("blocks") or [])
            if isinstance(b, dict)
        ]
        assistant_text = str(projection.get("assistant_text") or "")
        source_mirror_seq = _coerce_int(projection.get("source_mirror_seq"))
        interrupted = bool(projection.get("interrupted"))

        if not blocks and not assistant_text:
            return await self._settle_unrecoverable(
                session_id, turn_id, session, snapshot,
                reason="empty_projection",
            )

        if interrupted:
            return await self._settle_interrupted_projection(
                session_id,
                turn_id,
                session,
                snapshot,
                assistant_text=assistant_text,
                blocks=blocks,
            )

        if not blocks and assistant_text:
            blocks = [{"type": "text", "text": assistant_text}]

        terminal_authority = resolve_transcript_recovery_terminal_authority(
            snapshot,
            turn_id=turn_id,
            owner_dead=owner_dead,
        )
        if not terminal_authority.allowed:
            logger.info(
                "turn coordinator: skip complete recovery without authority "
                "session=%s turn=%s reason=%s",
                session_id,
                turn_id,
                terminal_authority.reason,
            )
            return None

        terminal_event = await journal_terminal_for_turn(
            self._journal,
            session_id=session_id,
            turn_id=turn_id,
        )
        if (
            isinstance(terminal_event, dict)
            and str(terminal_event.get("event_type") or "").strip() == "turn.completed"
        ):
            payload = terminal_event.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            completed_seq = int(terminal_event.get("event_seq") or 0)
            completed_command_id = await self._resolve_recovery_command_id(
                session_id=session_id,
                turn_id=turn_id,
                snapshot=snapshot,
                recovery_event=terminal_event,
            )
            stored_text = str(payload.get("assistant_text") or "")
            if stored_text:
                assistant_text = stored_text
            await self._materialize_recovered_assistant_frames(
                session_id=session_id,
                turn_id=turn_id,
                command_id=completed_command_id,
                assistant_text=assistant_text,
                blocks=blocks,
                source_mirror_seq=source_mirror_seq,
            )
            terminal_frame = await self._append_recovery_finish_frame(
                session_id=session_id,
                turn_id=turn_id,
                command_id=completed_command_id,
            )
            terminal_command_id = str(terminal_frame.get("command_id") or "").strip()
            if terminal_command_id:
                completed_command_id = terminal_command_id

            settle_result = await self._terminal_state_machine.settle(
                session_id=session_id,
                session=session,
                transition=TurnTerminalTransition(
                    authority=terminal_authority,
                    event=TurnTerminalEventSpec(
                        mode=TURN_TERMINAL_EVENT_EXISTING,
                        existing_event=terminal_event,
                    ),
                    assistant=TurnTerminalAssistantSpec(
                        content=assistant_text,
                        blocks=blocks,
                        prefer_event_payload=True,
                    ),
                    snapshot=TurnTerminalSnapshotSpec(
                        status="COMPLETED",
                        error_text=None,
                        command_id=completed_command_id,
                        terminal_frame=terminal_frame,
                    ),
                    side_effects=TurnTerminalSideEffects(
                        clear_interrupt_request=True,
                        deactivate_interactions=TURN_TERMINAL_DEACTIVATE_TURN,
                    ),
                ),
            )
            if settle_result.applied and isinstance(settle_result.snapshot, dict):
                logger.info(
                    "turn coordinator: reused existing turn.completed session=%s turn=%s",
                    session_id,
                    turn_id,
                )
                return settle_result.snapshot
            latest = await self._snapshots.get_snapshot(session_id)
            if (
                isinstance(latest, dict)
                and self._conversation_watermark(latest) < completed_seq
            ):
                latest_authority = resolve_transcript_recovery_terminal_authority(
                    latest,
                    turn_id=turn_id,
                )
                if not latest_authority.allowed:
                    return latest
                settle_result = await self._terminal_state_machine.settle(
                    session_id=session_id,
                    session=session,
                    transition=TurnTerminalTransition(
                        authority=latest_authority,
                        event=TurnTerminalEventSpec(
                            mode=TURN_TERMINAL_EVENT_EXISTING,
                            existing_event=terminal_event,
                        ),
                        assistant=TurnTerminalAssistantSpec(
                            content=assistant_text,
                            blocks=blocks,
                            prefer_event_payload=True,
                        ),
                        snapshot=TurnTerminalSnapshotSpec(
                            status="COMPLETED",
                            error_text=None,
                            command_id=completed_command_id,
                            terminal_frame=terminal_frame,
                        ),
                        side_effects=TurnTerminalSideEffects(
                            clear_interrupt_request=True,
                            deactivate_interactions=TURN_TERMINAL_DEACTIVATE_TURN,
                            ),
                    ),
                )
                if settle_result.applied and isinstance(settle_result.snapshot, dict):
                    logger.info(
                        "turn coordinator: reused existing turn.completed after latest snapshot session=%s turn=%s",
                        session_id,
                        turn_id,
                    )
                    return settle_result.snapshot
            return latest if isinstance(latest, dict) else snapshot

        # Write turn.recovered to journal (durable truth).  The journal event is
        # only one step in the transaction; if a prior coordinator crashed after
        # this claim, this run must still finish the projections and proof.
        resolved_command_id = await self._resolve_recovery_command_id(
            session_id=session_id,
            turn_id=turn_id,
            snapshot=snapshot,
        )
        await self._materialize_recovered_assistant_frames(
            session_id=session_id,
            turn_id=turn_id,
            command_id=resolved_command_id,
            assistant_text=assistant_text,
            blocks=blocks,
            source_mirror_seq=source_mirror_seq,
        )
        terminal_frame = await self._append_recovery_finish_frame(
            session_id=session_id,
            turn_id=turn_id,
            command_id=resolved_command_id,
        )
        terminal_command_id = str(terminal_frame.get("command_id") or "").strip()
        if terminal_command_id:
            resolved_command_id = terminal_command_id

        event_spec = TurnTerminalEventSpec(
            mode=TURN_TERMINAL_EVENT_CLAIM,
            event_doc={
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.recovered",
                "causation_id": f"recover:{session_id}:{turn_id}",
                "correlation_id": f"recover:{session_id}:{turn_id}",
                "payload": {
                    "assistant_text": assistant_text or None,
                    "block_count": len(blocks),
                    "blocks": blocks,
                },
            },
        )
        settle_result = await self._terminal_state_machine.settle(
            session_id=session_id,
            session=session,
            transition=TurnTerminalTransition(
                authority=terminal_authority,
                event=event_spec,
                assistant=TurnTerminalAssistantSpec(
                    content=assistant_text,
                    blocks=blocks,
                    prefer_event_payload=True,
                ),
                snapshot=TurnTerminalSnapshotSpec(
                    status="COMPLETED",
                    error_text=None,
                    command_id=resolved_command_id,
                    terminal_frame=terminal_frame,
                ),
                side_effects=TurnTerminalSideEffects(
                    clear_interrupt_request=True,
                    deactivate_interactions=TURN_TERMINAL_DEACTIVATE_TURN,
                ),
            ),
        )
        event = settle_result.event or {}
        seq = int(event.get("event_seq") or 0)
        result = settle_result.snapshot if settle_result.applied else None
        if result is None:
            latest = await self._snapshots.get_snapshot(session_id)
            if self._snapshot_has_completed_terminal_proof(latest or {}, turn_id):
                result = latest
            elif isinstance(latest, dict):
                latest_authority = resolve_transcript_recovery_terminal_authority(
                    latest,
                    turn_id=turn_id,
                )
                if result is None and latest_authority.allowed:
                    projection_event = await self._claim_recovered_projection_event(
                        session_id=session_id,
                        turn_id=turn_id,
                        base_event=event,
                        latest_snapshot=latest,
                        assistant_text=assistant_text,
                        blocks=blocks,
                    )
                    projection_seq = int((projection_event or {}).get("event_seq") or 0)
                    if (
                        projection_seq > 0
                        and isinstance(projection_event, dict)
                    ):
                        settle_result = await self._terminal_state_machine.settle(
                            session_id=session_id,
                            session=session,
                            transition=TurnTerminalTransition(
                                authority=latest_authority,
                                event=TurnTerminalEventSpec(
                                    mode=TURN_TERMINAL_EVENT_EXISTING,
                                    existing_event=projection_event,
                                ),
                                assistant=TurnTerminalAssistantSpec(
                                    content=assistant_text,
                                    blocks=blocks,
                                    prefer_event_payload=True,
                                ),
                                snapshot=TurnTerminalSnapshotSpec(
                                    status="COMPLETED",
                                    error_text=None,
                                    command_id=resolved_command_id,
                                    terminal_frame=terminal_frame,
                                ),
                                side_effects=TurnTerminalSideEffects(
                                    clear_interrupt_request=True,
                                    deactivate_interactions=TURN_TERMINAL_DEACTIVATE_TURN,
                                            ),
                            ),
                        )
                        result = settle_result.snapshot if settle_result.applied else None

        if isinstance(result, dict):
            logger.info(
                "turn coordinator: recovery completed session=%s turn=%s blocks=%d",
                session_id, turn_id, len(blocks),
            )
            return result

        latest = await self._snapshots.get_snapshot(session_id)
        return latest if isinstance(latest, dict) else None

    async def _settle_interrupted_projection(
        self,
        session_id: str,
        turn_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        assistant_text: str,
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        failure_text = USER_STOP_FAILURE_TEXT
        terminal_authority = resolve_transcript_recovery_terminal_authority(
            snapshot,
            turn_id=turn_id,
        )
        if not terminal_authority.allowed:
            logger.info(
                "turn coordinator: skip interrupted settlement without authority "
                "session=%s turn=%s reason=%s",
                session_id,
                turn_id,
                terminal_authority.reason,
            )
            return None

        failure_block: dict[str, Any] = {
            "type": "turn_failure",
            "error": failure_text,
            "failure_phase": "post_dispatch",
        }
        merged_blocks = [dict(block) for block in blocks]
        merged_blocks.append(failure_block)

        settle_result = await self._terminal_state_machine.settle(
            session_id=session_id,
            session=session,
            transition=TurnTerminalTransition(
                authority=terminal_authority,
                event=TurnTerminalEventSpec(
                    mode=TURN_TERMINAL_EVENT_CLAIM,
                    event_doc={
                        "session_id": session_id,
                        "channel": "conversation",
                        "turn_id": turn_id,
                        "event_type": "turn.failed",
                        "causation_id": f"recover-interrupted:{session_id}:{turn_id}",
                        "correlation_id": f"recover-interrupted:{session_id}:{turn_id}",
                        "payload": {
                            "reason": "interrupted",
                            "error_text": failure_text,
                            "assistant_text": assistant_text or None,
                            "block_count": len(blocks),
                            "blocks": blocks,
                            "settled_by": "turn_coordinator",
                        },
                    },
                ),
                assistant=TurnTerminalAssistantSpec(
                    content=assistant_text,
                    blocks=merged_blocks,
                    prefer_event_payload=False,
                ),
                snapshot=TurnTerminalSnapshotSpec(
                    status="FAILED",
                    error_text=failure_text,
                    command_id=str(snapshot.get("last_turn_command_id") or "").strip() or None,
                    delivery_state="RECEIVED",
                    failure_phase="post_dispatch",
                ),
                side_effects=TurnTerminalSideEffects(clear_interrupt_request=True),
            ),
        )
        if settle_result.applied and isinstance(settle_result.snapshot, dict):
            logger.info(
                "turn coordinator: settled interrupted projection session=%s turn=%s blocks=%d",
                session_id, turn_id, len(blocks),
            )
            return settle_result.snapshot

        latest = await self._snapshots.get_snapshot(session_id)
        return latest if isinstance(latest, dict) else None

    # ── Failure handling ─────────────────────────────────────────────────

    async def _handle_fetch_failure(
        self,
        session_id: str,
        turn_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Handle transient sandbox fetch failure with retry budget."""
        await self._record_attempt_failed(session_id, turn_id, "sandbox_unreachable")

        attempts = await self._count_recovery_attempts(session_id, turn_id)
        if attempts >= MAX_RECOVERY_ATTEMPTS:
            first_ts = await self._get_first_attempt_time(session_id, turn_id)
            if first_ts is not None:
                now = datetime.now(timezone.utc)
                elapsed = (now - first_ts).total_seconds()
                if elapsed > MAX_RECOVERY_WINDOW_S:
                    return await self._settle_unrecoverable(
                        session_id, turn_id, session, snapshot,
                        reason="retry_budget_exhausted",
                    )

        return None

    async def _record_attempt_failed(
        self,
        session_id: str,
        turn_id: str,
        reason: str,
    ) -> None:
        try:
            await self._journal.append_event(
                {
                    "session_id": session_id,
                    "channel": "conversation",
                    "turn_id": turn_id,
                    "event_type": "turn.recovery_attempt_failed",
                    "causation_id": f"recovery-fail:{session_id}:{turn_id}:{uuid.uuid4().hex[:8]}",
                    "payload": {
                        "reason": reason,
                        "attempted_at": utcnow_iso(),
                    },
                }
            )
        except Exception:
            logger.warning(
                "turn coordinator: failed to record attempt session=%s turn=%s",
                session_id, turn_id,
                exc_info=True,
            )

    async def _count_recovery_attempts(
        self,
        session_id: str,
        turn_id: str,
    ) -> int:
        events = await self._journal.list_events(
            session_id,
            turn_id=turn_id,
            event_type="turn.recovery_attempt_failed",
            limit=MAX_RECOVERY_ATTEMPTS + 1,
        )
        return len(events)

    async def _get_first_attempt_time(
        self,
        session_id: str,
        turn_id: str,
    ) -> datetime | None:
        events = await self._journal.list_events(
            session_id,
            turn_id=turn_id,
            event_type="turn.recovery_attempt_failed",
            limit=1,
        )
        if not events:
            return None
        ts_str = (events[0].get("payload") or {}).get("attempted_at")
        if ts_str:
            try:
                return parse_iso_utc(str(ts_str))
            except (ValueError, TypeError):
                pass
        return None

    async def _settle_unrecoverable(
        self,
        session_id: str,
        turn_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        reason: str,
        owner_dead: bool = False,
    ) -> dict[str, Any] | None:
        """Permanently settle a turn as FAILED when recovery is impossible."""
        terminal_authority = resolve_transcript_recovery_terminal_authority(
            snapshot,
            turn_id=turn_id,
            owner_dead=owner_dead,
        )
        if not terminal_authority.allowed:
            logger.info(
                "turn coordinator: skip unrecoverable settlement without authority "
                "session=%s turn=%s reason=%s",
                session_id,
                turn_id,
                terminal_authority.reason,
            )
            return None

        failure_block: dict[str, Any] = {
            "type": "turn_failure",
            "error": f"Recovery failed: {reason}",
            "failure_phase": "recovery",
        }
        existing_msg = await self._message_view.get_assistant_message_for_turn(
            session_id,
            turn_id=turn_id,
        )
        base_blocks = list((existing_msg or {}).get("blocks") or [])
        base_content = str((existing_msg or {}).get("content") or "")

        command_id = await self._resolve_recovery_command_id(
            session_id=session_id,
            turn_id=turn_id,
            snapshot=snapshot,
        )
        terminal_frame = await self._append_recovery_finish_frame(
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
        )
        command_id = str(terminal_frame.get("command_id") or command_id).strip()

        settle_result = await self._terminal_state_machine.settle(
            session_id=session_id,
            session=session,
            transition=TurnTerminalTransition(
                authority=terminal_authority,
                event=TurnTerminalEventSpec(
                    mode=TURN_TERMINAL_EVENT_CLAIM,
                    event_doc={
                        "session_id": session_id,
                        "channel": "conversation",
                        "turn_id": turn_id,
                        "event_type": "turn.failed",
                        "causation_id": f"unrecoverable:{session_id}:{turn_id}",
                        "correlation_id": f"unrecoverable:{session_id}:{turn_id}",
                        "payload": {
                            "reason": reason,
                            "settled_by": "turn_coordinator",
                        },
                    },
                ),
                assistant=TurnTerminalAssistantSpec(
                    content=base_content,
                    blocks=base_blocks + [failure_block],
                    created_at=str((existing_msg or {}).get("created_at") or utcnow_iso()),
                ),
                snapshot=TurnTerminalSnapshotSpec(
                    status="FAILED",
                    error_text=f"Recovery failed: {reason}",
                    command_id=command_id,
                    delivery_state="RECEIVED",
                    failure_phase="recovery",
                    terminal_frame=terminal_frame,
                ),
                side_effects=TurnTerminalSideEffects(clear_interrupt_request=True),
            ),
        )
        if settle_result.applied and isinstance(settle_result.snapshot, dict):
            logger.info(
                "turn coordinator: settled unrecoverable session=%s turn=%s reason=%s",
                session_id, turn_id, reason,
            )
            return settle_result.snapshot

        latest = await self._snapshots.get_snapshot(session_id)
        return latest if isinstance(latest, dict) else None

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _get_unresolved_turn_id(snapshot: dict[str, Any]) -> str | None:
        """Identify an unresolved turn from snapshot state.

        A turn is unresolved when:
        - conversation_state is IDLE
        - last_turn_status is FAILED
        - turn_recovery_phase is TRANSCRIPT_PENDING
        - last_turn_id is set

        Active PROCESSING/STREAMING snapshots are still owned by the live writer
        and its checkpoint lease.  The coordinator must not synthesize
        turn.recovered from mirror data while that writer can still append
        durable AI SDK frames for the same turn.
        """
        conv_state = str(snapshot.get("conversation_state") or "").strip()
        status = str(snapshot.get("last_turn_status") or "").strip()
        if status != "FAILED":
            return None
        recovery_phase = str(snapshot.get("turn_recovery_phase") or "").strip()
        if recovery_phase != "TRANSCRIPT_PENDING":
            return None
        turn_id = str(snapshot.get("last_turn_id") or "").strip()
        current_turn_id = str(snapshot.get("current_turn_id") or "").strip()
        if conv_state in _UNRESOLVED_SNAPSHOT_STATES:
            return turn_id or None
        if conv_state in {"PROCESSING", "STREAMING", "WAITING_FOR_INTERACTION"}:
            logger.info(
                "turn coordinator: refusing active recovery snapshot state=%s last_turn=%s current_turn=%s",
                conv_state,
                turn_id,
                current_turn_id,
            )
        return None
