"""Persistence for what happens inside a session: inputs, runtime facts and output.

Four writers, each with its own statements:

* The send path accepts an input and enqueues its notice in one transaction
  (:meth:`SessionRuntimeRepository.accept_input`) and records what became of
  handing it to the runtime.
* The session's reader holds a lease (:meth:`SessionRuntimeRepository.claim_reader`)
  and records everything the runtime reported in one transaction per batch
  (:meth:`SessionRuntimeRepository.record`). The transaction first checks the
  lease generation, so a reader that lost its lease writes nothing.
* An operator imports another system's history before the conversation starts
  (:meth:`SessionRuntimeRepository.import_history`); it writes inputs and
  responses no runtime handles, and never the output log.
* Page reads (history, the follow stream, the session view) only read.

Rows are returned as dicts keyed by column name with times as ISO-8601 strings.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    union_all,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.config.settings import get_settings, validate_environment_name
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.interaction_contract import public_interaction_view
from astrabox.persistence.sql import (
    execute,
    fetch_all,
    fetch_one,
    fetch_value,
    from_iso,
    to_iso,
    transaction,
)
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.sql.statements import (
    TimestampAfter, insert_if_absent, insert_if_absent_primary_key,
)
from astrabox.seams.persistence_coordination import SessionNotice
from astrabox.persistence.sql.models.agents import Agent
from astrabox.persistence.sql.models.sessions import (
    Session,
    SessionControl,
    SessionInput,
    SessionInteraction,
    SessionOutput,
    SessionReader,
    SessionResponse,
    SessionResponseInput,
)

_SESSIONS = Session.__table__
_AGENTS = Agent.__table__
_INPUTS = SessionInput.__table__
_RESPONSES = SessionResponse.__table__
_RESPONSE_INPUTS = SessionResponseInput.__table__
_INTERACTIONS = SessionInteraction.__table__
#: An Assistant workspace's own Sessions, which prepare its box and take no
#: messages. They have nothing for a reader: one would only attach to the
#: workspace's box behind the workspace's lifecycle, as when a pause is
#: releasing it.
_WORKSPACE_OWN = and_(
    _SESSIONS.c.hidden.is_(True),
    _SESSIONS.c.owner_type.is_not_distinct_from("assistant_workspace"),
)
_OUTPUT = SessionOutput.__table__
_READERS = SessionReader.__table__
_CONTROLS = SessionControl.__table__

_OPEN_INPUT_STATES = ("pending", "delivered")

#: How a message imported from another system's history is marked: the
#: ``source`` and ``state`` of an imported user message and the ``origin`` of
#: an imported reply (see :meth:`SessionRuntimeRepository.import_history`).
IMPORTED_SOURCE = "import"
IMPORTED_STATE = "imported"
IMPORTED_ORIGIN = "import"


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: (to_iso(value) if isinstance(value, datetime) else value) for key, value in row.items()}


def _view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else _row(row)


def _views(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [view for view in map(_view, rows) if view is not None]


# --------------------------------------------------------------------------
# What a reader records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReaderLease:
    """The right to read one session's runtime and record its facts."""

    session_id: str
    owner: str
    generation: int


@dataclass(frozen=True, slots=True)
class OutputPart:
    seq: int
    response_id: str | None
    part: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResponseStart:
    response_id: str
    origin: str
    input_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MessagePosition:
    kind: Literal["input", "response"]
    message_id: str
    output_seq: int


@dataclass(frozen=True, slots=True)
class ResponseEnd:
    response_id: str
    outcome: str
    native_reason: str | None = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class InteractionOpen:
    response_id: str
    interaction_id: str
    contract: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InteractionClose:
    interaction_id: str
    status: Literal["answered", "withdrawn", "expired"]
    output_seq: int | None = None


@dataclass(frozen=True, slots=True)
class InputsAdmitted:
    input_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InputsFailed:
    """Inputs nothing will answer, with the reason the page shows."""

    input_ids: tuple[str, ...]
    failure: dict[str, Any]


Fact = ResponseStart | ResponseEnd | MessagePosition | InteractionOpen | InteractionClose | InputsAdmitted | InputsFailed


@dataclass(slots=True)
class FactBatch:
    """What a reader read since its last commit, in the order it happened.

    ``output`` is the browser parts, already numbered. ``facts`` are the
    runtime's state changes, applied in order. ``cursor`` is where the runtime's
    event stream resumes after everything in the batch; ``status`` is the
    runtime's own status after it, when it changed.
    """

    output: list[OutputPart] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    cursor: dict[str, Any] | None = None
    status: str | None = None

    def __bool__(self) -> bool:
        return bool(self.output or self.facts or self.cursor is not None or self.status is not None)


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One message imported from another system's history, ready to store.

    ``record`` is stored as the row's ``import_record``. A user message
    carries ``content`` and ``content_blocks``; a reply carries ``outcome``
    (``None`` when the source recorded no ending) and its parts in ``record``.
    """

    source_id: str
    role: Literal["user", "assistant"]
    created_at: str
    record: dict[str, Any]
    content: str = ""
    content_blocks: list[dict[str, Any]] | None = None
    outcome: str | None = None
    parent_source_id: str | None = None


class SessionRuntimeRepository:
    def __init__(self, *, environment_name: str | None = None) -> None:
        self.environment_name = validate_environment_name(
            get_settings().environment_name if environment_name is None else environment_name
        )

    # -- inputs ------------------------------------------------------------

    async def accept_input(
        self,
        *,
        session_id: str,
        input_id: str,
        client_message_id: str,
        content: str,
        content_blocks: list[dict[str, Any]] | None,
        owner_user_id: str | None,
        author_user_id: str | None,
        source: str | None,
    ) -> tuple[dict[str, Any], bool] | None:
        """Accept one input and enqueue its notice in the same transaction.

        ``owner_user_id``, when given, must own the session. Returns the stored
        input and whether this call created it (``False`` when the same
        ``client_message_id`` was accepted before), or ``None`` when no live,
        owned session can take input.
        """
        async def accept(conn: AsyncConnection) -> dict[str, Any] | None:
            eligible = select(_SESSIONS.c.session_id).where(
                _SESSIONS.c.session_id == session_id, *_accepting(owner_user_id),
            ).exists()
            if not await insert_if_absent(
                conn, _INPUTS,
                {"input_id": input_id, "session_id": session_id,
                 "client_message_id": client_message_id, "author_user_id": author_user_id,
                 "source": source, "content": content, "content_blocks": content_blocks},
                index_columns=["session_id", "client_message_id"], where=eligible,
            ):
                return None
            await conn.execute(
                update(_SESSIONS).where(_SESSIONS.c.session_id == session_id)
                .values(updated_at=func.now())
            )
            row = (await conn.execute(
                select(_INPUTS).where(_INPUTS.c.input_id == input_id)
            )).mappings().one()
            await get_persistence().coordination.notify(
                conn, SessionNotice(session_id=session_id, kind="input", seq=0),
            )
            return dict(row)

        accepted = await transaction(accept)
        if accepted is not None:
            return _row(accepted), True
        existing = _view(
            await fetch_one(
                select(_INPUTS)
                .join(_SESSIONS, _SESSIONS.c.session_id == _INPUTS.c.session_id)
                .where(
                    _INPUTS.c.session_id == session_id,
                    _INPUTS.c.client_message_id == client_message_id,
                    *_accepting(owner_user_id),
                )
            )
        )
        return (existing, False) if existing is not None else None

    async def mark_delivered(self, input_ids: Sequence[str]) -> None:
        """The runtime received these inputs."""
        if not input_ids:
            return
        await execute(
            update(_INPUTS)
            .where(_INPUTS.c.input_id.in_(list(input_ids)), _INPUTS.c.state == "pending")
            .values(state="delivered", delivered_at=func.now())
        )

    async def mark_failed(self, input_id: str, failure: dict[str, Any]) -> None:
        """The runtime refused the input, or it could not be handed to one."""
        await execute(
            update(_INPUTS)
            .where(_INPUTS.c.input_id == input_id, _INPUTS.c.state.in_(_OPEN_INPUT_STATES))
            .values(state="failed", failure=failure)
        )

    async def open_input_ids(self, session_id: str) -> list[str]:
        """Inputs the platform holds that no runtime has admitted, in acceptance order."""
        rows = await fetch_all(
            select(_INPUTS.c.input_id)
            .where(_INPUTS.c.session_id == session_id, _INPUTS.c.state.in_(_OPEN_INPUT_STATES))
            .order_by(_INPUTS.c.seq)
        )
        return [str(row["input_id"]) for row in rows]

    async def undelivered_inputs(self, session_id: str) -> list[dict[str, Any]]:
        """Inputs the platform still holds for a runtime, in acceptance order."""
        return _views(
            await fetch_all(
                select(_INPUTS)
                .where(_INPUTS.c.session_id == session_id, _INPUTS.c.state == "pending")
                .order_by(_INPUTS.c.seq)
            )
        )

    async def reclaim_unadmitted(self, session_id: str) -> int:
        """Take back every input a lost runtime received but never durably held.

        The replacement runtime gets them again; inputs the lost runtime had
        admitted live in its transcript and are not sent twice.
        """
        return await execute(
            update(_INPUTS)
            .where(_INPUTS.c.session_id == session_id, _INPUTS.c.state == "delivered")
            .values(state="pending", delivered_at=None)
        )

    async def get_input(self, session_id: str, input_id: str) -> dict[str, Any] | None:
        return _view(
            await fetch_one(
                select(_INPUTS).where(_INPUTS.c.session_id == session_id, _INPUTS.c.input_id == input_id)
            )
        )

    async def input_for_client_message(self, session_id: str, client_message_id: str) -> dict[str, Any] | None:
        """The input accepted under the sender's own ``client_message_id``, if any."""
        return _view(
            await fetch_one(
                select(_INPUTS).where(
                    _INPUTS.c.session_id == session_id,
                    _INPUTS.c.client_message_id == client_message_id,
                )
            )
        )

    # -- answers and interrupts --------------------------------------------

    async def request_answer(
        self, session_id: str, interaction_id: str, *, answer: dict[str, Any], answered_by: str
    ) -> bool:
        """Store a person's answer to an open interaction for the reader to hand over.

        ``False`` when the interaction is not open or already has an answer.
        """
        statement = (
            update(_INTERACTIONS)
            .where(
                _INTERACTIONS.c.session_id == session_id,
                _INTERACTIONS.c.interaction_id == interaction_id,
                _INTERACTIONS.c.status == "open",
                _INTERACTIONS.c.answer.is_(None),
            )
            .values(answer=answer, answered_by=answered_by, answered_at=func.now())
        )

        async def write(conn: AsyncConnection) -> bool:
            if (await conn.execute(statement)).rowcount == 0:
                return False
            await get_persistence().coordination.notify(
                conn, SessionNotice(session_id=session_id, kind="answer", seq=0),
            )
            return True

        return await transaction(write)

    async def pending_answers(self, session_id: str) -> list[dict[str, Any]]:
        """Open interactions with an answer the runtime has not taken yet."""
        return _views(
            await fetch_all(
                select(_INTERACTIONS)
                .where(
                    _INTERACTIONS.c.session_id == session_id,
                    _INTERACTIONS.c.status == "open",
                    _INTERACTIONS.c.answer.isnot(None),
                )
                .order_by(_INTERACTIONS.c.answered_at)
            )
        )

    async def request_control(
        self, session_id: str, *, kind: str, target: str | None, requested_by: str | None
    ) -> str | None:
        """Record a control for the session's live reader; ``None`` when no reader is live.

        Without a live reader nothing is running, so there is nothing to steer.
        """
        control_id = str(uuid.uuid4())
        live = select(
            literal(control_id, type_=_CONTROLS.c.control_id.type).label("control_id"),
            _READERS.c.session_id,
            literal(kind, type_=_CONTROLS.c.kind.type).label("kind"),
            literal(target, type_=_CONTROLS.c.target.type).label("target"),
            _READERS.c.generation.label("reader_generation"),
            literal(requested_by, type_=_CONTROLS.c.requested_by.type).label("requested_by"),
        ).where(_READERS.c.session_id == session_id, _READERS.c.expires_at > func.now())
        statement = insert(_CONTROLS).from_select(
            ["control_id", "session_id", "kind", "target", "reader_generation", "requested_by"],
            live,
        )

        async def write(conn: AsyncConnection) -> str | None:
            if (await conn.execute(statement)).rowcount == 0:
                return None
            await get_persistence().coordination.notify(
                conn, SessionNotice(session_id=session_id, kind="control", seq=0),
            )
            return control_id

        return await transaction(write)

    async def pending_controls(self, lease: ReaderLease) -> list[dict[str, Any]]:
        """Controls the lease's reader generation has yet to apply, oldest first."""
        return _views(
            await fetch_all(
                select(_CONTROLS)
                .where(
                    _CONTROLS.c.session_id == lease.session_id,
                    _CONTROLS.c.state == "pending",
                    _CONTROLS.c.reader_generation == lease.generation,
                )
                .order_by(_CONTROLS.c.requested_at)
            )
        )

    async def settle_control(self, control_id: str, *, state: str, error: str | None = None) -> None:
        await execute(
            update(_CONTROLS)
            .where(_CONTROLS.c.control_id == control_id, _CONTROLS.c.state == "pending")
            .values(state=state, error=error, applied_at=func.now())
        )

    async def expire_controls(self, session_id: str, *, before_generation: int) -> None:
        """Expire what earlier reader generations never applied."""
        await execute(
            update(_CONTROLS)
            .where(
                _CONTROLS.c.session_id == session_id,
                _CONTROLS.c.state == "pending",
                _CONTROLS.c.reader_generation < before_generation,
            )
            .values(state="expired")
        )

    # -- the reader lease --------------------------------------------------

    async def claim_reader(self, session_id: str, *, owner: str, ttl_s: float) -> ReaderLease | None:
        """Take the session's reader lease when it is free, expired or held by ``owner``.

        Every claim starts a new generation, so commits by a previous holder
        that is still running are refused from this point on. A workspace's
        own Session gets no lease (``_WORKSPACE_OWN``).
        """
        async def claim(conn: AsyncConnection) -> ReaderLease | None:
            eligible = await conn.scalar(
                select(_SESSIONS.c.session_id).where(
                    _SESSIONS.c.session_id == session_id,
                    _SESSIONS.c.environment_name == self.environment_name,
                    ~_WORKSPACE_OWN,
                )
            )
            if eligible is None:
                return None
            # Seed the missing row without changing an existing incarnation.
            # The conditional update owns the row through the generation read.
            await conn.execute(insert_if_absent_primary_key(
                conn, _READERS,
                {"session_id": session_id, "owner": owner, "generation": 0,
                 "expires_at": TimestampAfter(func.now(), ttl_s)},
            ))
            result = await conn.execute(
                update(_READERS)
                .where(
                    _READERS.c.session_id == session_id,
                    or_(_READERS.c.expires_at < func.now(), _READERS.c.owner == owner),
                )
                .values(owner=owner, generation=_READERS.c.generation + 1,
                        expires_at=TimestampAfter(func.now(), ttl_s))
            )
            if result.rowcount == 0:
                return None
            generation = (await conn.execute(
                select(_READERS.c.generation).where(_READERS.c.session_id == session_id)
            )).scalar_one()
            return ReaderLease(session_id=session_id, owner=owner, generation=int(generation))

        return await transaction(claim)

    async def renew_reader(self, lease: ReaderLease, *, ttl_s: float) -> bool:
        """Extend the lease; ``False`` when another reader has taken it."""
        return (
            await execute(
                update(_READERS)
                .where(*_holds(lease))
                .values(expires_at=TimestampAfter(func.now(), ttl_s))
            )
            > 0
        )

    async def release_reader(self, lease: ReaderLease) -> None:
        """Give the lease up now, so another process may read the session at once."""
        await execute(update(_READERS).where(*_holds(lease)).values(expires_at=func.now()))

    async def reader_owner(self, session_id: str) -> str | None:
        """The process holding a live lease on the session's reader, if any."""
        value = await fetch_value(
            select(_READERS.c.owner).where(
                _READERS.c.session_id == session_id, _READERS.c.expires_at > func.now()
            )
        )
        return str(value) if value else None

    async def sessions_needing_readers(self, *, limit: int = 200) -> list[str]:
        """Sessions with no live reader lease that need one.

        A ready session needs one for a runtime to read (a bound, available,
        unparked box) or for inputs still in platform custody (including
        delivered inputs awaiting admission). A parked or lost box then wakes
        or is replaced. Any session needs one while responses are
        recorded open, so the reader can end what an ended runtime took with
        it, and an ended conversation needs one while it still holds inputs,
        so the reader can fail them. A workspace's own Sessions never need one
        (``_WORKSPACE_OWN``).
        """
        reader_live = (
            select(_READERS.c.session_id)
            .where(_READERS.c.session_id == _SESSIONS.c.session_id, _READERS.c.expires_at > func.now())
            .exists()
        )
        inputs_waiting = (
            select(_INPUTS.c.input_id)
            .where(_INPUTS.c.session_id == _SESSIONS.c.session_id, _INPUTS.c.state.in_(_OPEN_INPUT_STATES))
            .exists()
        )
        runtime_live = and_(
            func.coalesce(_SESSIONS.c.sandbox_id, "") != "",
            _SESSIONS.c.runtime_unavailable.is_(False),
            _SESSIONS.c.sandbox_parked_at.is_(None),
        )
        responses_open = (
            select(_RESPONSES.c.response_id)
            .where(_RESPONSES.c.session_id == _SESSIONS.c.session_id, _RESPONSES.c.status != "ended")
            .exists()
        )
        ready = and_(
            _SESSIONS.c.deleted.is_(False),
            _SESSIONS.c.state == SessionState.READY.value,
            or_(runtime_live, inputs_waiting),
        )
        # An ended conversation still holding inputs needs a reader to fail them.
        ended_holding = and_(
            _SESSIONS.c.state.in_((SessionState.TERMINATED.value, SessionState.DELETED.value)),
            select(_INPUTS.c.input_id)
            .where(_INPUTS.c.session_id == _SESSIONS.c.session_id, _INPUTS.c.state.in_(_OPEN_INPUT_STATES))
            .exists(),
        )
        rows = await fetch_all(
            select(_SESSIONS.c.session_id)
            .where(
                _SESSIONS.c.environment_name == self.environment_name,
                ~reader_live, ~_WORKSPACE_OWN, or_(ready, responses_open, ended_holding),
            )
            .limit(limit)
        )
        return [str(row["session_id"]) for row in rows]

    async def resume_point(self, session_id: str) -> tuple[int, dict[str, Any] | None]:
        """The last stored output ``seq`` (0 for none) and the cursor committed with it."""
        row = await fetch_one(
            select(
                select(func.coalesce(func.max(_OUTPUT.c.seq), 0))
                .where(_OUTPUT.c.session_id == session_id)
                .scalar_subquery()
                .label("seq"),
                _SESSIONS.c.reader_cursor,
            ).where(_SESSIONS.c.session_id == session_id)
        )
        if row is None:
            return 0, None
        return int(row["seq"]), row["reader_cursor"]

    # -- recording ---------------------------------------------------------

    async def record(self, lease: ReaderLease, batch: FactBatch, *, ttl_s: float) -> bool:
        """Commit one batch under the reader lease; ``False`` when the lease is lost.

        The lease check also renews it, and holds the lease row until commit,
        so a claim by another reader waits for this batch rather than reading
        a resume point it is about to change.
        """

        async def write(conn: AsyncConnection) -> bool:
            held = await conn.execute(
                update(_READERS)
                .where(*_holds(lease))
                .values(expires_at=TimestampAfter(func.now(), ttl_s))
            )
            if held.rowcount == 0:
                return False
            sid = lease.session_id
            for fact in batch.facts:
                await _apply(conn, sid, fact)
            output = {part.seq: part for part in batch.output}
            for fact in batch.facts:
                if isinstance(fact, MessagePosition):
                    table = _INPUTS if fact.kind == "input" else _RESPONSES
                    identity = table.c.input_id if fact.kind == "input" else table.c.response_id
                    order = (await conn.execute(
                        select(table.c.conversation_order).where(
                            table.c.session_id == sid, identity == fact.message_id,
                        )
                    )).scalar_one()
                    output[fact.output_seq] = OutputPart(
                        fact.output_seq, None,
                        {"type": "data-message-order", "transient": True, "data": {
                            "kind": fact.kind, "id": fact.message_id, "order": order,
                        }},
                    )
                    continue
                if not isinstance(fact, InteractionClose) or fact.output_seq is None:
                    continue
                row = (await conn.execute(select(_INTERACTIONS).where(
                    _INTERACTIONS.c.session_id == sid,
                    _INTERACTIONS.c.interaction_id == fact.interaction_id,
                ))).mappings().one()
                data = public_interaction_view({
                    **row["contract"], "status": row["status"], "answer": row["answer"],
                })
                output[fact.output_seq] = OutputPart(
                    fact.output_seq, str(row["response_id"]),
                    {"type": "data-interaction", "id": fact.interaction_id, "data": data},
                )
            batch.output[:] = sorted(output.values(), key=lambda part: part.seq)
            if batch.output:
                await conn.execute(
                    insert(_OUTPUT),
                    [
                        {"session_id": sid, "seq": part.seq, "response_id": part.response_id, "part": part.part}
                        for part in batch.output
                    ],
                )
            session_values: dict[str, Any] = {}
            if batch.cursor is not None:
                session_values["reader_cursor"] = batch.cursor
            if batch.status is not None:
                session_values["runtime_status"] = batch.status
                session_values["runtime_status_at"] = func.now()
            if batch.output:
                session_values["last_output_at"] = func.now()
            if any(isinstance(fact, ResponseEnd) for fact in batch.facts):
                session_values["updated_at"] = func.now()
            if session_values:
                statement = update(_SESSIONS).where(_SESSIONS.c.session_id == sid).values(**session_values)
                if batch.output or batch.facts or batch.status is not None:
                    # Subscribers in other processes re-read when this commits:
                    # followers read the log through ``seq``; a batch of facts
                    # alone (an interaction closed, a status changed) carries 0.
                    last = max((part.seq for part in batch.output), default=0)
                    if (await conn.execute(statement)).rowcount > 0:
                        await get_persistence().coordination.notify(
                            conn, SessionNotice(session_id=sid, kind="output", seq=last),
                        )
                else:
                    await conn.execute(statement)
            return True

        if not batch:
            return await self.renew_reader(lease, ttl_s=ttl_s)
        return await transaction(write)

    # -- reads -------------------------------------------------------------

    async def read_output(self, session_id: str, *, after_seq: int, limit: int = 500) -> list[dict[str, Any]]:
        """Output after ``after_seq``, in order: what a reconnecting follower missed."""
        return _views(
            await fetch_all(
                select(_OUTPUT.c.seq, _OUTPUT.c.response_id, _OUTPUT.c.part)
                .where(_OUTPUT.c.session_id == session_id, _OUTPUT.c.seq > after_seq)
                .order_by(_OUTPUT.c.seq)
                .limit(limit)
            )
        )

    async def output_for_responses(
        self, session_id: str, response_ids: Sequence[str], *, through_seq: int | None = None
    ) -> list[dict[str, Any]]:
        """Every part of these responses, in order, through ``through_seq`` when given."""
        if not response_ids:
            return []
        conditions: list[ColumnElement[bool]] = [
            _OUTPUT.c.session_id == session_id,
            _OUTPUT.c.response_id.in_(list(response_ids)),
        ]
        if through_seq is not None:
            conditions.append(_OUTPUT.c.seq <= through_seq)
        return _views(
            await fetch_all(
                select(_OUTPUT.c.seq, _OUTPUT.c.response_id, _OUTPUT.c.part).where(*conditions).order_by(_OUTPUT.c.seq)
            )
        )

    async def response_interactions(self, session_id: str, response_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Current interaction settlements for the responses on a history page."""
        if not response_ids:
            return []
        return _views(await fetch_all(
            select(_INTERACTIONS.c.interaction_id, _INTERACTIONS.c.status, _INTERACTIONS.c.answer).where(
                _INTERACTIONS.c.session_id == session_id,
                _INTERACTIONS.c.response_id.in_(list(response_ids)),
            )
        ))

    async def approval_answers(self, session_id: str, response_ids: Sequence[str]) -> list[dict[str, Any]]:
        """The answers people gave to these responses' interactions, by interaction.

        The output log holds what the runtime asked; what the person decided is
        recorded on the interaction. An answer to an interaction that expired
        before the runtime took it never took effect and is left out.
        """
        if not response_ids:
            return []
        return _views(
            await fetch_all(
                select(_INTERACTIONS.c.interaction_id, _INTERACTIONS.c.answer).where(
                    _INTERACTIONS.c.session_id == session_id,
                    _INTERACTIONS.c.response_id.in_(list(response_ids)),
                    _INTERACTIONS.c.answer.isnot(None),
                    _INTERACTIONS.c.status.in_(("open", "answered")),
                )
            )
        )

    async def response_output_tail(
        self, session_id: str, response_id: str, *, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        """A response's last ``limit`` parts, in order, and how many parts it has."""
        of_response = and_(_OUTPUT.c.session_id == session_id, _OUTPUT.c.response_id == response_id)
        total = await fetch_value(select(func.count()).select_from(_OUTPUT).where(of_response))
        newest = _views(
            await fetch_all(
                select(_OUTPUT.c.seq, _OUTPUT.c.response_id, _OUTPUT.c.part, _OUTPUT.c.created_at)
                .where(of_response)
                .order_by(_OUTPUT.c.seq.desc())
                .limit(limit)
            )
        )
        return list(reversed(newest)), int(total or 0)

    async def history_page(
        self, session_id: str, *, before: tuple[int, int, int, int] | None, limit: int,
        through_seq: int | None = None,
    ) -> dict[str, Any]:
        """One page in native conversation order, returned oldest first.

        Imported history keeps its declared source order before native output.
        Inputs not yet observed by the engine stay at the queued tail, ordered
        by acceptance sequence. Native positions share the output log's clock;
        receipt timestamps do not decide conversation order. ``through_seq``
        pins a paged read before later native observations.
        """
        def entry(table: Any, kind: int, imported: ColumnElement[bool]) -> Any:
            order = table.c.conversation_order
            observed = order.is_not(None)
            if through_seq is not None:
                observed = and_(observed, or_(imported, order <= through_seq * 2))
            return select(
                case((imported, 0), (observed, 1), else_=2).label("phase"),
                case((observed, order), else_=table.c.seq).label("position"),
                literal(kind).label("kind"),
                table.c.seq.label("seq"),
            ).where(table.c.session_id == session_id)

        entries = union_all(
            entry(_INPUTS, 0, _INPUTS.c.source == IMPORTED_SOURCE),
            entry(_RESPONSES, 1, _RESPONSES.c.origin == IMPORTED_ORIGIN),
        ).subquery("entries")
        position = tuple_(entries.c.phase, entries.c.position, entries.c.kind, entries.c.seq)
        query = select(entries)
        if before is not None:
            query = query.where(position < tuple_(*(literal(value) for value in before)))
        rows = await fetch_all(query.order_by(*(entries.c[key].desc() for key in ("phase", "position", "kind", "seq"))).limit(limit + 1))
        has_more = len(rows) > limit
        rows = list(reversed(rows[:limit]))
        input_seqs = [int(row["seq"]) for row in rows if row["kind"] == 0]
        response_seqs = [int(row["seq"]) for row in rows if row["kind"] == 1]
        inputs = (
            _views(
                await fetch_all(
                    select(_INPUTS)
                    .where(_INPUTS.c.session_id == session_id, _INPUTS.c.seq.in_(input_seqs))
                    .order_by(_INPUTS.c.seq)
                )
            )
            if input_seqs
            else []
        )
        responses = (
            _views(
                await fetch_all(
                    select(_RESPONSES)
                    .where(_RESPONSES.c.session_id == session_id, _RESPONSES.c.seq.in_(response_seqs))
                    .order_by(_RESPONSES.c.seq)
                )
            )
            if response_seqs
            else []
        )
        links = (
            await fetch_all(
                select(_RESPONSE_INPUTS.c.response_id, _RESPONSE_INPUTS.c.input_id).where(
                    _RESPONSE_INPUTS.c.session_id == session_id,
                    _RESPONSE_INPUTS.c.response_id.in_([r["response_id"] for r in responses]),
                )
            )
            if responses
            else []
        )
        oldest = rows[0] if rows else None
        return {
            "entries": [{"kind": "input" if r["kind"] == 0 else "response", "seq": int(r["seq"])} for r in rows],
            "inputs": inputs,
            "responses": responses,
            "links": links,
            "has_more": has_more,
            "before": tuple(int(oldest[key]) for key in ("phase", "position", "kind", "seq")) if has_more and oldest else None,
        }

    async def activity(self, session_id: str) -> dict[str, Any]:
        """What the session view shows about the runtime right now.

        The open responses, the open interactions, the last ended response, and
        the inputs the platform still holds or failed to hand over.
        """
        open_responses = _views(
            await fetch_all(
                select(_RESPONSES)
                .where(_RESPONSES.c.session_id == session_id, _RESPONSES.c.status != "ended")
                .order_by(_RESPONSES.c.seq)
            )
        )
        last_ended = _view(
            await fetch_one(
                select(_RESPONSES)
                .where(
                    _RESPONSES.c.session_id == session_id,
                    _RESPONSES.c.status == "ended",
                    _RESPONSES.c.origin != IMPORTED_ORIGIN,
                )
                .order_by(_RESPONSES.c.seq.desc())
                .limit(1)
            )
        )
        interactions = _views(
            await fetch_all(
                select(_INTERACTIONS)
                .where(_INTERACTIONS.c.session_id == session_id, _INTERACTIONS.c.status == "open")
                .order_by(_INTERACTIONS.c.opened_at)
            )
        )
        held_inputs = _views(
            await fetch_all(
                select(_INPUTS)
                .where(
                    _INPUTS.c.session_id == session_id,
                    _INPUTS.c.state.in_(_OPEN_INPUT_STATES),
                )
                .order_by(_INPUTS.c.seq)
            )
        )
        return {
            "open_responses": open_responses,
            "last_response": last_ended,
            "open_interactions": interactions,
            "held_inputs": held_inputs,
        }

    async def open_response_ids(self, session_id: str) -> list[str]:
        rows = await fetch_all(
            select(_RESPONSES.c.response_id).where(
                _RESPONSES.c.session_id == session_id, _RESPONSES.c.status != "ended"
            )
        )
        return [str(row["response_id"]) for row in rows]

    async def open_interaction_ids(self, session_id: str) -> list[str]:
        rows = await fetch_all(
            select(_INTERACTIONS.c.interaction_id).where(
                _INTERACTIONS.c.session_id == session_id, _INTERACTIONS.c.status == "open"
            )
        )
        return [str(row["interaction_id"]) for row in rows]

    async def get_interaction(self, session_id: str, interaction_id: str) -> dict[str, Any] | None:
        return _view(
            await fetch_one(
                select(_INTERACTIONS).where(
                    _INTERACTIONS.c.session_id == session_id,
                    _INTERACTIONS.c.interaction_id == interaction_id,
                )
            )
        )

    async def get_response(self, session_id: str, response_id: str) -> dict[str, Any] | None:
        return _view(
            await fetch_one(
                select(_RESPONSES).where(
                    _RESPONSES.c.session_id == session_id, _RESPONSES.c.response_id == response_id
                )
            )
        )

    async def response_for_input(self, session_id: str, input_id: str) -> dict[str, Any] | None:
        """The response that answers an input, once one has started."""
        return _view(
            await fetch_one(
                select(_RESPONSES)
                .join(
                    _RESPONSE_INPUTS,
                    and_(
                        _RESPONSE_INPUTS.c.session_id == _RESPONSES.c.session_id,
                        _RESPONSE_INPUTS.c.response_id == _RESPONSES.c.response_id,
                    ),
                )
                .where(_RESPONSE_INPUTS.c.session_id == session_id, _RESPONSE_INPUTS.c.input_id == input_id)
            )
        )

    async def input_by_client_message_id(
        self, session_id: str, client_message_id: str
    ) -> dict[str, Any] | None:
        """The input a caller sent under its own id (an A2A task, an artifact's run)."""
        return _view(
            await fetch_one(
                select(_INPUTS).where(
                    _INPUTS.c.session_id == session_id,
                    _INPUTS.c.client_message_id == client_message_id,
                )
            )
        )

    async def inputs_for_response(self, session_id: str, response_id: str) -> list[dict[str, Any]]:
        """The inputs a response answers, in acceptance order."""
        return _views(
            await fetch_all(
                select(_INPUTS)
                .join(
                    _RESPONSE_INPUTS,
                    and_(
                        _RESPONSE_INPUTS.c.session_id == _INPUTS.c.session_id,
                        _RESPONSE_INPUTS.c.input_id == _INPUTS.c.input_id,
                    ),
                )
                .where(
                    _RESPONSE_INPUTS.c.session_id == session_id,
                    _RESPONSE_INPUTS.c.response_id == response_id,
                )
                .order_by(_INPUTS.c.seq)
            )
        )

    async def response_ordinal(self, session_id: str, response_id: str) -> int | None:
        """Where a response stands among the conversation's own responses, 1 for the first.

        Responses count in the order they started; imported history is not the
        conversation's own and does not count. ``None`` when there is no such
        response.
        """
        started = (
            select(_RESPONSES.c.seq)
            .where(_RESPONSES.c.session_id == session_id, _RESPONSES.c.response_id == response_id)
            .scalar_subquery()
        )
        position = await fetch_value(
            select(func.count())
            .select_from(_RESPONSES)
            .where(
                _RESPONSES.c.session_id == session_id,
                _RESPONSES.c.origin != IMPORTED_ORIGIN,
                _RESPONSES.c.seq <= started,
            )
        )
        return int(position) if position else None

    async def open_interactions_for_response(
        self, session_id: str, response_id: str
    ) -> list[dict[str, Any]]:
        """The interactions a response is waiting on, oldest first."""
        return _views(
            await fetch_all(
                select(_INTERACTIONS)
                .where(
                    _INTERACTIONS.c.session_id == session_id,
                    _INTERACTIONS.c.response_id == response_id,
                    _INTERACTIONS.c.status == "open",
                )
                .order_by(_INTERACTIONS.c.opened_at)
            )
        )

    # -- imported history --------------------------------------------------

    async def imported_history(self, session_id: str) -> list[dict[str, Any]]:
        """The session's imported messages in the order they were imported.

        Each is ``{"role", "record"}``, ``record`` being what
        :meth:`import_history` stored.
        """
        inputs = await fetch_all(
            select(_INPUTS.c.import_record).where(
                _INPUTS.c.session_id == session_id, _INPUTS.c.source == IMPORTED_SOURCE
            )
        )
        replies = await fetch_all(
            select(_RESPONSES.c.import_record).where(
                _RESPONSES.c.session_id == session_id, _RESPONSES.c.origin == IMPORTED_ORIGIN
            )
        )
        rows = [{"role": "user", "record": row["import_record"]} for row in inputs]
        rows += [{"role": "assistant", "record": row["import_record"]} for row in replies]
        return sorted(rows, key=lambda row: int(row["record"]["position"]))

    async def import_history(self, session_id: str, entries: Sequence[HistoryEntry]) -> bool:
        """Store messages imported from another system's history, in one transaction.

        A user message becomes an ``imported`` input, never handed to a
        runtime; a reply becomes an ended response whose parts live in its
        ``import_record``. A reply answers the imported user message its
        ``parent_source_id`` names. Both keep the source's declared order and
        original timestamp. Returns ``False``, storing
        nothing, when the conversation already has a message of its own:
        history cannot be placed before messages it has ordered.
        """

        async def write(conn: AsyncConnection) -> bool:
            own_input = (
                select(_INPUTS.c.input_id)
                .where(
                    _INPUTS.c.session_id == session_id,
                    _INPUTS.c.source.is_distinct_from(IMPORTED_SOURCE),
                )
                .exists()
            )
            own_response = (
                select(_RESPONSES.c.response_id)
                .where(_RESPONSES.c.session_id == session_id, _RESPONSES.c.origin != IMPORTED_ORIGIN)
                .exists()
            )
            if (await conn.execute(select(or_(own_input, own_response)))).scalar():
                return False
            imported_inputs = {
                str(row.import_record["source_id"]): str(row.input_id)
                for row in await conn.execute(
                    select(_INPUTS.c.input_id, _INPUTS.c.import_record).where(
                        _INPUTS.c.session_id == session_id, _INPUTS.c.source == IMPORTED_SOURCE
                    )
                )
            }
            for entry in entries:
                at = from_iso(entry.created_at)
                if entry.role == "user":
                    input_id = str(uuid.uuid4())
                    await conn.execute(
                        insert(_INPUTS).values(
                            input_id=input_id,
                            session_id=session_id,
                            client_message_id=f"{IMPORTED_SOURCE}:{entry.source_id}",
                            source=IMPORTED_SOURCE,
                            state=IMPORTED_STATE,
                            content=entry.content,
                            content_blocks=entry.content_blocks,
                            import_record=entry.record,
                            conversation_order=int(entry.record["position"]),
                            created_at=at,
                        )
                    )
                    imported_inputs[entry.source_id] = input_id
                    continue
                response_id = f"{IMPORTED_ORIGIN}:{entry.source_id}"
                await conn.execute(
                    insert(_RESPONSES).values(
                        session_id=session_id,
                        response_id=response_id,
                        origin=IMPORTED_ORIGIN,
                        status="ended",
                        outcome=entry.outcome,
                        import_record=entry.record,
                        conversation_order=int(entry.record["position"]),
                        started_at=at,
                        ended_at=at,
                    )
                )
                parent_input = imported_inputs.get(str(entry.parent_source_id or ""))
                if parent_input is not None:
                    await conn.execute(
                        insert(_RESPONSE_INPUTS).values(
                            session_id=session_id, response_id=response_id, input_id=parent_input
                        )
                    )
                    await conn.execute(
                        update(_INPUTS)
                        .where(_INPUTS.c.input_id == parent_input)
                        .values(response_id=response_id)
                    )
            return True

        return await transaction(write)


def _accepting(owner_user_id: str | None) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        _SESSIONS.c.deleted.is_(False),
        _SESSIONS.c.state.notin_((SessionState.TERMINATED.value, SessionState.DELETED.value)),
        # A deleted Agent's conversations keep their history and take nothing new.
        ~exists().where(_AGENTS.c.agent_id == _SESSIONS.c.agent_id, _AGENTS.c.state != "ACTIVE"),
    ]
    if owner_user_id is not None:
        conditions.append(_SESSIONS.c.user_id == owner_user_id)
    return conditions


def _holds(lease: ReaderLease) -> list[ColumnElement[bool]]:
    return [
        _READERS.c.session_id == lease.session_id,
        _READERS.c.owner == lease.owner,
        _READERS.c.generation == lease.generation,
    ]


async def _apply(conn: AsyncConnection, session_id: str, fact: Fact) -> None:
    if isinstance(fact, ResponseStart):
        await conn.execute(insert_if_absent_primary_key(
            conn, _RESPONSES,
            {"session_id": session_id, "response_id": fact.response_id,
             "origin": fact.origin, "status": "running"},
        ))
        if fact.input_ids:
            await conn.execute(insert_if_absent_primary_key(
                conn, _RESPONSE_INPUTS,
                [{"session_id": session_id, "response_id": fact.response_id, "input_id": input_id}
                 for input_id in fact.input_ids],
            ))
            await conn.execute(
                update(_INPUTS)
                .where(_INPUTS.c.session_id == session_id, _INPUTS.c.input_id.in_(list(fact.input_ids)))
                .values(response_id=fact.response_id)
            )
    elif isinstance(fact, MessagePosition):
        table = _INPUTS if fact.kind == "input" else _RESPONSES
        identity = table.c.input_id if fact.kind == "input" else table.c.response_id
        await conn.execute(
            update(table)
            .where(
                table.c.session_id == session_id,
                identity == fact.message_id,
                table.c.conversation_order.is_(None),
            )
            .values(conversation_order=fact.output_seq * 2)
        )
    elif isinstance(fact, ResponseEnd):
        await conn.execute(
            update(_RESPONSES)
            .where(
                _RESPONSES.c.session_id == session_id,
                _RESPONSES.c.response_id == fact.response_id,
                _RESPONSES.c.status != "ended",
            )
            .values(
                status="ended",
                outcome=fact.outcome,
                native_reason=fact.native_reason,
                usage=fact.usage,
                error=fact.error,
                ended_at=func.now(),
            )
        )
        await conn.execute(
            update(_INTERACTIONS)
            .where(
                _INTERACTIONS.c.session_id == session_id,
                _INTERACTIONS.c.response_id == fact.response_id,
                _INTERACTIONS.c.status == "open",
            )
            .values(status="withdrawn", closed_at=func.now())
        )
    elif isinstance(fact, InteractionOpen):
        await conn.execute(insert_if_absent_primary_key(
            conn, _INTERACTIONS,
            {"session_id": session_id, "interaction_id": fact.interaction_id,
             "response_id": fact.response_id, "contract": fact.contract, "status": "open"},
        ))
        await conn.execute(
            update(_RESPONSES)
            .where(
                _RESPONSES.c.session_id == session_id,
                _RESPONSES.c.response_id == fact.response_id,
                _RESPONSES.c.status == "running",
            )
            .values(status="waiting")
        )
    elif isinstance(fact, InteractionClose):
        closing = [
            _INTERACTIONS.c.session_id == session_id,
            _INTERACTIONS.c.interaction_id == fact.interaction_id,
            _INTERACTIONS.c.status == "open",
        ]
        closed = (await conn.execute(
            select(_INTERACTIONS.c.response_id).where(*closing).with_for_update()
        )).first()
        if closed is not None:
            await conn.execute(
                update(_INTERACTIONS).where(*closing)
                .values(status=fact.status, closed_at=func.now())
            )
            still_open = (
                select(_INTERACTIONS.c.interaction_id)
                .where(
                    _INTERACTIONS.c.session_id == session_id,
                    _INTERACTIONS.c.response_id == closed.response_id,
                    _INTERACTIONS.c.status == "open",
                )
                .exists()
            )
            await conn.execute(
                update(_RESPONSES)
                .where(
                    _RESPONSES.c.session_id == session_id,
                    _RESPONSES.c.response_id == closed.response_id,
                    _RESPONSES.c.status == "waiting",
                    ~still_open,
                )
                .values(status="running")
            )
    elif isinstance(fact, InputsAdmitted):
        if fact.input_ids:
            await conn.execute(
                update(_INPUTS)
                .where(
                    _INPUTS.c.session_id == session_id,
                    _INPUTS.c.input_id.in_(list(fact.input_ids)),
                    _INPUTS.c.state.in_(_OPEN_INPUT_STATES),
                )
                .values(state="admitted", admitted_at=func.now())
            )
    elif isinstance(fact, InputsFailed):
        if fact.input_ids:
            await conn.execute(
                update(_INPUTS)
                .where(
                    _INPUTS.c.session_id == session_id,
                    _INPUTS.c.input_id.in_(list(fact.input_ids)),
                    _INPUTS.c.state.in_(_OPEN_INPUT_STATES),
                )
                .values(state="failed", failure=fact.failure)
            )
    else:  # pragma: no cover - the union is closed
        raise TypeError(f"unknown fact {fact!r}")
