from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    TurnTerminalAuthority,
    build_turn_terminal_snapshot_updates,
)

logger = get_logger(__name__)

TURN_TERMINAL_EVENT_APPEND = "append"
TURN_TERMINAL_EVENT_CLAIM = "claim"
TURN_TERMINAL_EVENT_EXISTING = "existing"

TURN_TERMINAL_DEACTIVATE_NONE = "none"
TURN_TERMINAL_DEACTIVATE_TURN = "turn"
TURN_TERMINAL_DEACTIVATE_ALL = "all"


@dataclass(frozen=True)
class TurnTerminalEventSpec:
    mode: str
    event_doc: dict[str, Any] | None = None
    existing_event: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnTerminalAssistantSpec:
    content: str = ""
    blocks: list[dict[str, Any]] | None = None
    prefer_event_payload: bool = False
    write_when_empty: bool = False
    message_id: str | None = None
    message_seq: int | None = None
    user_id: str | None = None
    synthetic: bool | None = None
    created_at: str | None = None
    extra_fields: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnTerminalSnapshotSpec:
    status: str | None
    error_text: str | None
    command_id: str | None = None
    recovery_anchor: dict[str, Any] | None = None
    recovery_engine_anchor: dict[str, Any] | None = None
    active_interaction_id: str | None = None
    last_turn_id: str | None = None
    delivery_state: str | None = None
    failure_phase: str | None = None
    terminal_frame: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnTerminalSideEffects:
    clear_interrupt_request: bool = False
    deactivate_interactions: str = TURN_TERMINAL_DEACTIVATE_NONE


@dataclass(frozen=True)
class TurnTerminalTransition:
    authority: TurnTerminalAuthority
    event: TurnTerminalEventSpec
    snapshot: TurnTerminalSnapshotSpec
    assistant: TurnTerminalAssistantSpec | None = None
    side_effects: TurnTerminalSideEffects = TurnTerminalSideEffects()


@dataclass(frozen=True)
class TurnTerminalSettleResult:
    allowed: bool
    applied: bool
    reason: str
    event: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    event_created: bool = False


def _clean_text(raw: Any) -> str:
    return str(raw or "").strip()


def _copy_blocks(raw: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in (raw or []) if isinstance(item, dict)]


def _coerce_positive_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class TurnTerminalStateMachine:
    """Apply terminal turn transitions through one journal/projection path."""

    def __init__(
        self,
        *,
        session_events_repo: Any,
        session_snapshots_repo: Any,
        sessions_repo: Any | None = None,
        interaction_snapshots_repo: Any | None = None,
    ) -> None:
        self._journal = session_events_repo
        self._snapshots = session_snapshots_repo
        self._sessions = sessions_repo
        self._interaction_snapshots = interaction_snapshots_repo

    async def settle(
        self,
        *,
        session_id: str,
        session: dict[str, Any] | None,
        transition: TurnTerminalTransition,
    ) -> TurnTerminalSettleResult:
        authority = transition.authority
        if not authority.allowed:
            return TurnTerminalSettleResult(
                allowed=False,
                applied=False,
                reason=authority.reason,
            )
        turn_id = _clean_text(authority.turn_id)
        if not turn_id:
            return TurnTerminalSettleResult(
                allowed=False,
                applied=False,
                reason="missing_authority_turn_id",
            )

        event_spec = self._event_with_assistant(
            transition.event,
            transition.assistant,
        )
        event, created = await self._materialize_event(event_spec)
        event_seq = _coerce_positive_int((event or {}).get("event_seq"))
        if event_seq is None:
            raise RuntimeError(
                f"terminal transition missing journal event_seq session={session_id} turn={turn_id}"
            )

        snapshot_spec = transition.snapshot
        result = await self._snapshots.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=event_seq,
            updates=build_turn_terminal_snapshot_updates(
                turn_id=turn_id,
                status=snapshot_spec.status,
                error_text=snapshot_spec.error_text,
                command_id=snapshot_spec.command_id,
                recovery_anchor=snapshot_spec.recovery_anchor,
                recovery_engine_anchor=snapshot_spec.recovery_engine_anchor,
                active_interaction_id=snapshot_spec.active_interaction_id,
                last_turn_id=snapshot_spec.last_turn_id,
                delivery_state=snapshot_spec.delivery_state,
                failure_phase=snapshot_spec.failure_phase,
                terminal_frame=snapshot_spec.terminal_frame,
            ),
            **authority.snapshot_update_kwargs(),
        )
        if not isinstance(result, dict):
            if created:
                # This call appended the terminal event and then lost the
                # snapshot CAS, so the journal says the turn ended and the
                # projection says it is still running. A reader of the
                # projection alone sees a live turn no worker owns; the
                # journal is the authority.
                logger.warning(
                    "turn terminal: journaled %s but the snapshot CAS missed "
                    "session=%s turn=%s — journal and projection now disagree",
                    _clean_text((event or {}).get("event_type")),
                    session_id,
                    turn_id,
                )
            return TurnTerminalSettleResult(
                allowed=True,
                applied=False,
                reason="snapshot_cas_miss",
                event=event,
                snapshot=None,
                event_created=created,
            )

        await self._apply_side_effects(
            session_id=session_id,
            turn_id=turn_id,
            side_effects=transition.side_effects,
        )
        return TurnTerminalSettleResult(
            allowed=True,
            applied=True,
            reason="applied",
            event=event,
            snapshot=result,
            event_created=created,
        )

    async def _materialize_event(
        self,
        spec: TurnTerminalEventSpec,
    ) -> tuple[dict[str, Any], bool]:
        mode = _clean_text(spec.mode)
        if mode == TURN_TERMINAL_EVENT_EXISTING:
            if not isinstance(spec.existing_event, dict):
                raise RuntimeError("terminal transition existing event missing")
            return dict(spec.existing_event), False
        if not isinstance(spec.event_doc, dict):
            raise RuntimeError("terminal transition event_doc missing")
        event_doc = dict(spec.event_doc)
        if mode == TURN_TERMINAL_EVENT_APPEND:
            event = await self._journal.append_event(event_doc)
            return dict(event), True
        if mode == TURN_TERMINAL_EVENT_CLAIM:
            event, created = await self._journal.try_claim_event(event_doc)
            return dict(event), bool(created)
        raise RuntimeError(f"unsupported terminal event mode: {mode}")

    def _event_with_assistant(
        self,
        spec: TurnTerminalEventSpec,
        assistant: TurnTerminalAssistantSpec | None,
    ) -> TurnTerminalEventSpec:
        if _clean_text(spec.mode) == TURN_TERMINAL_EVENT_EXISTING:
            return spec
        if not isinstance(spec.event_doc, dict):
            return spec
        resolved = self._assistant_from_event_payload(assistant, spec.event_doc)
        if resolved is None:
            return spec
        event_doc = dict(spec.event_doc)
        payload = event_doc.get("payload")
        payload = dict(payload) if isinstance(payload, dict) else {}
        payload["assistant_text"] = str(resolved.content or "") or None
        payload["blocks"] = _copy_blocks(resolved.blocks)
        event_doc["payload"] = payload
        return TurnTerminalEventSpec(mode=spec.mode, event_doc=event_doc)

    def _assistant_from_event_payload(
        self,
        assistant: TurnTerminalAssistantSpec | None,
        event: dict[str, Any],
    ) -> TurnTerminalAssistantSpec | None:
        if assistant is None:
            return None
        content = str(assistant.content or "")
        blocks = _copy_blocks(assistant.blocks)
        if assistant.prefer_event_payload:
            payload = event.get("payload") if isinstance(event, dict) else None
            if isinstance(payload, dict):
                event_blocks = _copy_blocks(payload.get("blocks"))
                if event_blocks:
                    blocks = event_blocks
                event_text = str(payload.get("assistant_text") or "")
                if event_text:
                    content = event_text
        if not assistant.write_when_empty and not blocks and not content:
            return None
        return TurnTerminalAssistantSpec(
            content=content,
            blocks=blocks,
            prefer_event_payload=False,
            write_when_empty=assistant.write_when_empty,
            message_id=assistant.message_id,
            message_seq=assistant.message_seq,
            user_id=assistant.user_id,
            synthetic=assistant.synthetic,
            created_at=assistant.created_at,
            extra_fields=dict(assistant.extra_fields or {}),
        )

    async def _apply_side_effects(
        self,
        *,
        session_id: str,
        turn_id: str,
        side_effects: TurnTerminalSideEffects,
    ) -> None:
        if side_effects.deactivate_interactions != TURN_TERMINAL_DEACTIVATE_NONE:
            await self._deactivate_interactions(
                session_id=session_id,
                turn_id=turn_id,
                mode=side_effects.deactivate_interactions,
            )
        if side_effects.clear_interrupt_request:
            await self._clear_interrupt_request(session_id)

    async def _clear_interrupt_request(self, session_id: str) -> None:
        if self._sessions is None:
            raise RuntimeError("terminal transition interrupt clear has no sessions repo")
        await self._sessions.update_session(
            session_id,
            {"interrupt_requested": False},
        )

    async def _deactivate_interactions(
        self,
        *,
        session_id: str,
        turn_id: str,
        mode: str,
    ) -> None:
        if self._interaction_snapshots is None:
            return
        clean_mode = _clean_text(mode)
        if clean_mode == TURN_TERMINAL_DEACTIVATE_TURN:
            deactivate = getattr(
                self._interaction_snapshots,
                "deactivate_active_for_turn",
                None,
            )
            args = (session_id, turn_id)
        elif clean_mode == TURN_TERMINAL_DEACTIVATE_ALL:
            deactivate = getattr(
                self._interaction_snapshots,
                "deactivate_all_active",
                None,
            )
            args = (session_id,)
        else:
            raise RuntimeError(f"unsupported interaction deactivation mode: {clean_mode}")
        if not callable(deactivate):
            return
        try:
            await deactivate(*args)
        except Exception as exc:
            logger.warning(
                "terminal transition interaction deactivation failed session=%s turn=%s mode=%s err=%s",
                session_id,
                turn_id,
                clean_mode,
                exc,
            )
