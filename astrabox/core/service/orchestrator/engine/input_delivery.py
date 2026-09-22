"""Deliver durable input rows through the mandatory engine input FIFO."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from astrabox.core.service.orchestrator.engine.base import EngineInputCommand
from astrabox.core.service.orchestrator.engine.input_content import (
    read_engine_content_blocks,
)


def input_response_message_id(input_id: str) -> str:
    """Derive the stable UI response identity for one platform FIFO input."""

    try:
        normalized = uuid.UUID(str(input_id or "").strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("engine input_id must be a valid UUID") from exc
    return str(uuid.uuid5(normalized, "astrabox-sdk-response"))


def consumption_carrier(
    sandbox_id: str | None,
    isolated_session_id: str | None = None,
) -> str | None:
    """Name the runtime generation an engine-side consumption belongs to.

    A consumption receipt promises "this input reached an engine that will
    answer it" only while that engine's process survives. The process is
    pinned by the box AND, under the shared-sandbox tenancy, by the isolated
    session inside it — many conversations share one box, and a re-placement
    into the same box is still a new engine process. One formula, used by
    every writer and the reader, or the comparison silently never matches.
    """

    box = str(sandbox_id or "").strip()
    if not box:
        return None
    isolated = str(isolated_session_id or "").strip()
    return f"{box}#{isolated}" if isolated else box


_TERMINAL_FRAME_TYPES = frozenset({"finish", "data-result"})


def input_answered_in_frames(
    frames: list[dict[str, Any]],
    *,
    command_id: str,
    input_id: str,
) -> bool:
    """Judge from the frame ledger whether an input's answer ever streamed.

    Answered has two shapes, because a carried input's frames never name its
    own command: a finish/data-result frame on the input's command, or the
    engine's input-consumed marker followed by a terminal frame on the same
    carrier turn. Order is part of the criterion — a terminal that precedes
    the consumption belongs to the response an interrupt ended, so consumed
    after turn-completed still reads as unanswered.
    """

    normalized_command_id = str(command_id or "").strip()
    normalized_input_id = str(input_id or "").strip()
    # Every marker counts, not just the first: a redelivered input leaves an
    # earlier marker on the carrier that died under it, and its real answer
    # follows the LATER marker on the turn that redrove it.
    consumed_markers: list[tuple[str, int]] = []
    for frame in frames:
        payload = frame.get("payload") or {}
        frame_type = str(payload.get("type") or "")
        if (
            frame_type in _TERMINAL_FRAME_TYPES
            and str(frame.get("command_id") or "").strip() == normalized_command_id
        ):
            return True
        if (
            frame_type == "data-input-consumed"
            and str(payload.get("id") or "")
            == f"input-consumed:{normalized_input_id}"
        ):
            consumed_markers.append(
                (
                    str(frame.get("turn_id") or "").strip(),
                    int(frame.get("frame_seq") or 0),
                )
            )
    return any(
        str(frame.get("turn_id") or "").strip() == carrier_turn_id
        and int(frame.get("frame_seq") or 0) > consumed_seq
        and str((frame.get("payload") or {}).get("type") or "")
        in _TERMINAL_FRAME_TYPES
        for carrier_turn_id, consumed_seq in consumed_markers
        for frame in frames
    )


class _Outbox(Protocol):
    async def list_pending(self, session_id: str) -> list[dict[str, Any]]: ...

    async def mark_delivered(self, command_id: str) -> dict[str, Any]: ...


class _DeliveryGateway(Protocol):
    async def deliver(self, command: EngineInputCommand) -> None: ...


def command_input_id(payload: dict[str, Any]) -> str | None:
    """Return the platform input identity carried by an accepted command."""

    input_id = str(payload.get("input_id") or "").strip()
    return input_id or None


class DeliveryCoordinator:
    def __init__(self, outbox: _Outbox) -> None:
        self._outbox = outbox

    @staticmethod
    def _command(row: dict[str, Any], session_id: str) -> EngineInputCommand:
        command_id = str(row.get("command_id") or "").strip()
        row_session_id = str(row.get("session_id") or "").strip()
        sequence = row.get("sequence")
        input_id = str(row.get("input_id") or "").strip()
        content = row.get("content")
        client_message_id = str(row.get("client_message_id") or "").strip()
        if (
            not command_id
            or row_session_id != session_id
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or not input_id
            or not isinstance(content, str)
        ):
            raise RuntimeError(
                f"malformed delivery outbox row command_id={command_id!r}"
            )
        return EngineInputCommand(
            command_id=command_id,
            session_id=row_session_id,
            sequence=sequence,
            input_id=input_id,
            content=content,
            client_message_id=client_message_id or None,
            content_blocks=read_engine_content_blocks(row.get("content_blocks")),
        )

    async def deliver_pending(
        self,
        session_id: str,
        gateway: _DeliveryGateway,
    ) -> list[str]:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise ValueError("session_id is required")
        rows = await self._outbox.list_pending(normalized_session_id)
        commands = [
            self._command(row, normalized_session_id)
            for row in rows
        ]
        if any(
            left.sequence >= right.sequence
            for left, right in zip(commands, commands[1:])
        ):
            raise RuntimeError("delivery outbox rows are not strict FIFO")
        delivered: list[str] = []
        for command in commands:
            await gateway.deliver(command)
            await self._outbox.mark_delivered(command.command_id)
            delivered.append(command.command_id)
        return delivered


class JournalDeliveryOutbox:
    """Journal-backed mapping for the engine input outbox.

    ``command.accepted`` is enqueue and ``input.delivered`` is the adapter
    receipt. Only the engine's later consumption boundary writes
    ``input.consumed``, and that receipt is honoured only while its consumer
    carrier survives: a consumption taken by a runtime generation that died
    before the answer streamed reopens the row (see ``_journal_input_rows``).
    A platform process cannot infer that an in-box process retained volatile
    input, so every open row remains replayable; command ids make replay
    idempotent at every engine client and at the runner.
    """

    def __init__(
        self,
        journal_repo: Any,
        *,
        session_id: str,
        current_carrier: str | None = None,
    ) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise ValueError("session_id is required")
        self._journal_repo = journal_repo
        self._session_id = normalized_session_id
        self._current_carrier = current_carrier
        self._rows_by_command_id: dict[str, dict[str, Any]] = {}

    async def list_pending(self, session_id: str) -> list[dict[str, Any]]:
        normalized_session_id = str(session_id or "").strip()
        if normalized_session_id != self._session_id:
            raise ValueError(
                "delivery outbox belongs to another session "
                f"expected={self._session_id!r} actual={normalized_session_id!r}"
            )
        rows = await _journal_input_rows(
            self._journal_repo,
            self._session_id,
            current_carrier=self._current_carrier,
        )
        self._rows_by_command_id = {
            str(row["command_id"]): row
            for row in rows
        }
        return rows

    async def mark_delivered(self, command_id: str) -> dict[str, Any]:
        normalized_command_id = str(command_id or "").strip()
        row = self._rows_by_command_id.get(normalized_command_id)
        if row is None:
            raise KeyError(f"unknown delivery command {normalized_command_id!r}")
        delivered, _created = await self._journal_repo.try_claim_event(
            {
                "session_id": self._session_id,
                "channel": "delivery",
                "turn_id": row.get("turn_id"),
                "event_type": "input.delivered",
                "causation_id": normalized_command_id,
                "correlation_id": normalized_command_id,
                "payload": {
                    "input_id": str(row.get("input_id") or "").strip(),
                    "sequence": row["sequence"],
                },
            }
        )
        return delivered

async def _journal_input_rows(
    journal_repo: Any,
    session_id: str,
    *,
    current_carrier: str | None = None,
) -> list[dict[str, Any]]:
    after_seq = 0
    accepted: list[dict[str, Any]] = []
    delivered: set[str] = set()
    consumed: dict[str, dict[str, Any]] = {}
    abandoned: set[str] = set()
    while True:
        rows = await journal_repo.list_events(
            session_id,
            after_seq=after_seq,
            limit=500,
        )
        if not rows:
            break
        for row in rows:
            event_type = str(row.get("event_type") or "").strip()
            if event_type == "command.accepted":
                payload = row.get("payload")
                if isinstance(payload, dict) and command_input_id(payload) is not None:
                    accepted.append(row)
            elif event_type == "input.delivered":
                delivered.add(str(row.get("causation_id") or "").strip())
            elif event_type == "input.consumed":
                consumed[str(row.get("causation_id") or "").strip()] = row
            elif event_type == "turn.failed":
                # An input is owed only while the turn that accepted it can
                # still consume it. A turn that failed before dispatch never
                # reached the engine and is settled, so nothing will ever
                # submit its input — and because a turn start requires the FIFO
                # head to be its own command, leaving it queued does not delay
                # that input, it refuses every later turn in the conversation.
                # The other phases are not this: they dispatched, so a consumed
                # input is already excluded above and an unconsumed one is a
                # delivery still in question.
                payload = row.get("payload")
                phase = (
                    str(payload.get("failure_phase") or "").strip()
                    if isinstance(payload, dict)
                    else ""
                )
                if phase == "pre_dispatch":
                    abandoned.add(str(row.get("causation_id") or "").strip())
        next_after_seq = max(int(row.get("event_seq") or 0) for row in rows)
        if next_after_seq <= after_seq:
            raise RuntimeError("session journal input scan did not advance")
        after_seq = next_after_seq
        if len(rows) < 500:
            break

    frames: list[dict[str, Any]] | None = None

    async def _answered(
        command_id: str, input_id: str, accepted_seq: int
    ) -> bool:
        nonlocal frames
        if frames is None:
            # Any frame answering this input strictly postdates its
            # acceptance, so the suffix after the accepted event bounds the
            # scan. Paged: list_frames caps each read.
            collected: list[dict[str, Any]] = []
            frame_after_seq = accepted_seq
            while True:
                page = await journal_repo.list_frames(
                    session_id, after_seq=frame_after_seq, limit=500
                )
                if not page:
                    break
                collected.extend(page)
                next_seq = max(int(f.get("frame_seq") or 0) for f in page)
                if next_seq <= frame_after_seq:
                    raise RuntimeError("session frame scan did not advance")
                frame_after_seq = next_seq
                if len(page) < 500:
                    break
            frames = collected
        return input_answered_in_frames(
            frames, command_id=command_id, input_id=input_id
        )

    pending: list[dict[str, Any]] = []
    for row in accepted:
        command_id = str(row.get("causation_id") or "").strip()
        if command_id in abandoned:
            continue
        consumption = consumed.get(command_id)
        if consumption is not None:
            # A consumption receipt closes the row only while the engine
            # process that took the input can still answer it. When the
            # current carrier is a different runtime generation and no answer
            # ever streamed, the receipt is orphaned evidence — the input is
            # pending again, and redelivery is safe because every engine
            # client and the runner accept a replayed command id
            # idempotently. An unknown carrier (reads that have no runtime in
            # hand, or evidence written before carriers were recorded) keeps
            # the closed reading: reopening is the delivery path's judgement.
            consumer = str(
                (consumption.get("payload") or {}).get("consumer_carrier") or ""
            ).strip()
            row_input_id = command_input_id(row.get("payload") or {})
            if (
                not current_carrier
                or not consumer
                or consumer == current_carrier
                or await _answered(
                    command_id,
                    str(row_input_id or ""),
                    int(row.get("event_seq") or 0),
                )
            ):
                continue
        payload = row.get("payload")
        input_id = command_input_id(payload) if isinstance(payload, dict) else None
        content = payload.get("content") if isinstance(payload, dict) else None
        sequence = row.get("event_seq")
        if (
            not command_id
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or input_id is None
            or not isinstance(content, str)
        ):
            raise RuntimeError(
                f"malformed journal delivery command command_id={command_id!r}"
            )
        pending.append(
            {
                "command_id": command_id,
                "session_id": session_id,
                "turn_id": row.get("turn_id"),
                "input_id": input_id,
                "client_message_id": str(
                    (payload or {}).get("client_message_id") or ""
                ).strip(),
                "content": content,
                "content_blocks": (payload or {}).get("content_blocks") or None,
                "sequence": sequence,
                "state": "DELIVERED" if command_id in delivered else "PENDING",
                "payload": dict(payload),
            }
        )
    if any(
        int(left["sequence"]) >= int(right["sequence"])
        for left, right in zip(pending, pending[1:])
    ):
        raise RuntimeError("journal delivery commands are not strict FIFO")
    return pending


async def journal_input_rows(
    journal_repo: Any,
    session_id: str,
    *,
    current_carrier: str | None = None,
) -> list[dict[str, Any]]:
    """Return the open FIFO projection for reads/replay.

    With ``current_carrier`` the projection also reopens rows whose
    consumption was taken by a different, dead runtime generation and never
    answered; without it, a consumption receipt closes its row.
    """

    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise ValueError("session_id is required")
    return await _journal_input_rows(
        journal_repo, normalized_session_id, current_carrier=current_carrier
    )


async def confirm_engine_input_consumed(
    journal_repo: Any,
    *,
    session_id: str,
    input_id: str,
    response_message_id: str | None,
    content: str,
    consumer_carrier: str | None = None,
) -> dict[str, Any]:
    """Commit the hook's dequeue before its root boundary advances any cursor.

    ``consumer_carrier`` names the runtime generation that took the input
    (``consumption_carrier``). It is evidence, not identity: a later
    confirmation of the same input from a replacement carrier returns the
    original receipt unchanged — the redelivered input's answer re-closes the
    row through the frame ledger, not through a second consumption event.
    """

    normalized_session_id = str(session_id or "").strip()
    normalized_input_id = str(input_id or "").strip()
    if not normalized_session_id or not normalized_input_id or not isinstance(content, str):
        raise RuntimeError("engine input consumption boundary is incomplete")
    expected_response_message_id = input_response_message_id(normalized_input_id)
    normalized_response_message_id = str(response_message_id or "").strip()
    if (
        normalized_response_message_id
        and normalized_response_message_id != expected_response_message_id
    ):
        raise RuntimeError("engine input consumption response identity is malformed")

    command = await journal_repo.find_input_command_by_input_id(
        normalized_session_id,
        input_id=normalized_input_id,
    )
    if not isinstance(command, dict):
        raise RuntimeError(
            f"engine consumed unknown external input {normalized_input_id!r}"
        )
    command_id = str(command.get("causation_id") or "").strip()
    payload = command.get("payload")
    expected_content = payload.get("content") if isinstance(payload, dict) else None
    if not command_id or not isinstance(expected_content, str):
        raise RuntimeError(
            f"engine input consumption does not match delivery {command_id!r}"
        )

    consumed_payload = {
        "input_id": normalized_input_id,
        "response_message_id": expected_response_message_id,
        "client_message_id": str(
            (payload or {}).get("client_message_id") or ""
        ).strip(),
        "content": expected_content,
    }
    expected_blocks = (payload or {}).get("content_blocks") or None
    if expected_blocks:
        # The consumed event is what a reload rebuilds this user message from,
        # so an image the turn was sent has to be readable here too — the
        # engine's prompt boundary only reports the text.
        consumed_payload["content_blocks"] = expected_blocks
    if content != expected_content:
        consumed_payload["sdk_content"] = content
    normalized_carrier = str(consumer_carrier or "").strip()
    if normalized_carrier:
        consumed_payload["consumer_carrier"] = normalized_carrier

    def _consumption_identity(candidate: Any) -> dict[str, Any]:
        # The carrier is evidence about WHERE consumption happened, not part
        # of WHAT was consumed — a redelivered input legitimately confirms
        # again from its replacement carrier.
        identity = dict(candidate) if isinstance(candidate, dict) else {}
        identity.pop("consumer_carrier", None)
        return identity

    existing = await journal_repo.list_events(
        normalized_session_id,
        event_type="input.consumed",
        causation_id=command_id,
        limit=1,
    )
    if existing:
        consumed = existing[0]
        if _consumption_identity(consumed.get("payload")) != _consumption_identity(
            consumed_payload
        ):
            raise RuntimeError(
                f"engine input consumption identity collided for {command_id!r}"
            )
        return consumed

    pending = await _journal_input_rows(journal_repo, normalized_session_id)
    head_input_id = str(
        pending[0].get("input_id") if pending else ""
    ).strip()
    if head_input_id != normalized_input_id:
        raise RuntimeError(
            "engine input consumption is not the durable FIFO head "
            f"expected={head_input_id!r} actual={normalized_input_id!r}"
        )

    consumed, _created = await journal_repo.try_claim_event(
        {
            "session_id": normalized_session_id,
            "channel": "delivery",
            "turn_id": command.get("turn_id"),
            "event_type": "input.consumed",
            "causation_id": command_id,
            "correlation_id": command_id,
            "payload": consumed_payload,
        }
    )
    if _consumption_identity(consumed.get("payload")) != _consumption_identity(
        consumed_payload
    ):
        raise RuntimeError(
            f"engine input consumption identity collided for {command_id!r}"
        )
    return consumed
