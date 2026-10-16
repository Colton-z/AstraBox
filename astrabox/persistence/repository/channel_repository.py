"""Channel spine persistence: inbound work items, conversations, subscriptions and deliveries.

The tables are declared in :mod:`astrabox.persistence.sql.models.channels`;
the contract they serve is ``docs/channel-spine.md``. Every record belongs to
one binding (a Deployment) and is deleted with it, and every compare-and-set
below is a single conditional statement.

* ``channel_inbound``: one record per platform message. It is the message's
  dedup identity, ``(deployment_id, dedup_key)``, and its recoverable unit of
  work: it holds the whole admitted input, so sending the message into its
  conversation never depends on the source redelivering. The ingress classifies an inbound before
  writing it, so a record is created in the state it belongs in::

      (absent) ─refused──────────────► IGNORED      (terminal)
      (absent) ─context only─────────► OBSERVED ─admitting signal─┐
      (absent) ─authorization────────► AUTHORIZED ─observation────┤
                     │                     └─wait expires─► DEAD │
      (absent) ─admitted message─────► RECEIVED ◄────────────────┘
                                          │
                                   begin_dispatch
                                          ▼
                                     DISPATCHING ─exhausted─► DEAD  (terminal)
                                          │
                              sent, or answered a question
                                          ▼
                                       SETTLED                      (terminal)

  OBSERVED and AUTHORIZED records are never driven or recovered: only the
  promotion to RECEIVED, at most once per record, sends one. A live
  item is held by ``owner_token`` under a monotonic ``generation`` with a
  renewable lease. The holder's writes match its owner token and generation,
  so a worker whose lease was taken changes nothing. Admission, promotion,
  steal, recovery and authorization expiry match the state and generation
  they read; they take or need no owner. Marking explicit attention and
  recording a recall are write-once marks with no fence. Failure paths never
  delete: they return the item to RECEIVED with a retry watermark or end it
  DEAD with evidence. An admitted message's input is never replaced.
* ``channel_conversations`` maps a binding's conversation, per execution
  account, to its Session; ``channel_conversation_locks`` holds the renewable
  lease that serializes the conversation's drives.
* ``channel_output_subscriptions``: one destination per binding and Session,
  with the Session output position through which its replies became
  deliveries. Delivery records are written before that position moves.
* ``channel_outbox``: one delivery per binding, Session and reply, bound to
  the exact reply it reports: ``PENDING → SENDING → DELIVERED | DEAD`` under a
  fenced lease. A final-text reply's row is not due until the reply is
  complete. A failed attempt waits out a persistent backoff and the row dies
  once its retries are spent.
* ``channel_aliases`` maps platform messages the spine delivered to their
  conversation; ``channel_interactions`` and ``channel_interaction_answers``
  keep the questions a binding showed and their answers so far.
* ``channel_account_links``: the account a participant of a linked binding
  runs as.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    and_,
    case,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from astrabox.persistence.sql import (
    execute,
    fetch_all,
    fetch_one,
    fetch_value,
    from_iso,
    to_iso,
    transaction,
)
from astrabox.persistence.sql.models.channels import (
    ChannelAccountLink,
    ChannelAlias,
    ChannelConversation,
    ChannelConversationLock,
    ChannelInbound,
    ChannelInteraction,
    ChannelInteractionAnswer,
    ChannelOutbox,
    ChannelOutputSubscription,
)
from astrabox.persistence.sql.models.deployments import Deployment
from astrabox.persistence.sql.models.sessions import Session, SessionOutput

_INBOUND = ChannelInbound.__table__
_CONVERSATIONS = ChannelConversation.__table__
_LOCKS = ChannelConversationLock.__table__
_SUBSCRIPTIONS = ChannelOutputSubscription.__table__
_OUTBOX = ChannelOutbox.__table__
_ALIASES = ChannelAlias.__table__
_INTERACTIONS = ChannelInteraction.__table__
_ANSWERS = ChannelInteractionAnswer.__table__
_LINKS = ChannelAccountLink.__table__
_DEPLOYMENTS = Deployment.__table__
_SESSIONS = Session.__table__
_SESSION_OUTPUT = SessionOutput.__table__

# A question a channel showed: OPEN while it can still be answered there;
# ANSWERED once the engine accepted the participant's answer; CLOSED when the
# interaction ended any other way (superseded, answered elsewhere, expired).
INTERACTION_OPEN = "OPEN"
INTERACTION_ANSWERED = "ANSWERED"
INTERACTION_CLOSED = "CLOSED"

# Inbound work item states (see the module docstring for the machine).
INBOUND_RECEIVED = "RECEIVED"
INBOUND_DISPATCHING = "DISPATCHING"
INBOUND_SETTLED = "SETTLED"
INBOUND_IGNORED = "IGNORED"
INBOUND_DEAD = "DEAD"
#: Recorded as retained context, not admitted to run: an observation, or a
#: message the binding's attention policy did not address.
INBOUND_OBSERVED = "OBSERVED"
#: Admitted to run by an authorization whose observation has not arrived.
INBOUND_AUTHORIZED = "AUTHORIZED"

_INBOUND_LIVE_STATES = (INBOUND_RECEIVED, INBOUND_DISPATCHING)
_INBOUND_UNADMITTED_STATES = (INBOUND_OBSERVED, INBOUND_AUTHORIZED)
_INBOUND_CLAIM_STATES = (
    INBOUND_RECEIVED, INBOUND_OBSERVED, INBOUND_AUTHORIZED, INBOUND_IGNORED,
)

#: How long an authorization waits for the observation that carries its
#: message before it dies with evidence. An authorization arrives on a
#: platform's trigger feed and its message on a separate context feed; one day
#: covers a context feed that is down for hours, and a message older than that
#: is answered by asking again rather than late.
_AUTHORIZATION_WAIT_SECONDS = 24 * 3600.0


#: The drive path's longest bounded stretch with nobody renewing its lease
#: when the renewal heartbeat stalls: the response judge (30s) and the
#: conversation-lock wait (120s), with margin. The lease TTL floor exceeds it,
#: so a slow but live worker's item is not taken over mid-wait; the renew
#: fence before the message is sent makes a takeover safe but wasteful.
_INBOUND_LEASE_FLOOR_SECONDS = 300.0

#: How often a drive renews its item lease and its conversation lock. Every
#: lease below outlives several renewals, so only a holder that stopped
#: running loses one.
LEASE_RENEW_SECONDS = 60.0


def _inbound_lease_seconds() -> float:
    """Work-item lease TTL.

    A live worker renews the lease every :data:`LEASE_RENEW_SECONDS` while it
    drives the item, and again at the fences before it sends the message; an
    item whose lease lapses is driven again by the reconciler.
    """
    try:
        return max(
            _INBOUND_LEASE_FLOOR_SECONDS,
            float(os.getenv("ASTRABOX_CHANNEL_CLAIM_STALE_SECONDS", "900")),
        )
    except ValueError:
        return 900.0


# Claim outcomes returned to the ingress spine.
CLAIM_OUTCOME_CLAIMED = "claimed"          # dispatch this message (caller owns it)
CLAIM_OUTCOME_DUPLICATE = "duplicate"      # already settled — ack the winner
CLAIM_OUTCOME_IN_PROGRESS = "in_progress"  # a live owner holds it — ack, no dispatch
CLAIM_OUTCOME_IGNORED = "ignored"          # terminally refused or classified away
CLAIM_OUTCOME_OBSERVED = "observed"        # held as context, not admitted to run
CLAIM_OUTCOME_AUTHORIZED = "authorized"    # admitted, waiting for its observation

#: The outcome of a claim whose statement wrote the record, by its new state.
_WRITTEN_OUTCOMES = {
    INBOUND_RECEIVED: CLAIM_OUTCOME_CLAIMED,
    INBOUND_OBSERVED: CLAIM_OUTCOME_OBSERVED,
    INBOUND_AUTHORIZED: CLAIM_OUTCOME_AUTHORIZED,
    INBOUND_IGNORED: CLAIM_OUTCOME_IGNORED,
}

OUTBOX_PENDING = "PENDING"
OUTBOX_SENDING = "SENDING"
OUTBOX_DELIVERED = "DELIVERED"
OUTBOX_DEAD = "DEAD"
#: A deliverer holds the lease for one attempt: one provider call for a
#: final-text reply, or a streaming session that renews it on every poll. A
#: lapsed lease therefore means the deliverer stopped, and the sweep delivers
#: the row again.
_OUTBOX_LEASE_SECONDS = 300.0

#: How long a conversation lock outlives its last renewal. The holding drive
#: renews it every :data:`LEASE_RENEW_SECONDS`, so it lapses only once its
#: holder stopped; the conversation's next drive then waits at most this long.
_CONVERSATION_LOCK_SECONDS = 300.0

#: How many retained messages a turn quotes as context: the most recent ones
#: no earlier turn consumed. Older ones are consumed with them, so a later turn
#: never quotes a message older than one already quoted.
CONTEXT_WINDOW_MESSAGES = 50

#: The admitted input's columns, by the payload keys services read and write.
_PAYLOAD_FIELDS = (
    "content", "reply_text", "action", "conversation_key", "conversation",
    "reply_context", "reference", "participant", "provider_ignore_reason",
    "creator_user_id", "execution_user_id", "execution_mode", "retain_context",
    "message_timestamp", "binding_revision", "prompt_prefix", "selection",
    "selection_reason", "participated",
)
_TEXT_PAYLOAD_FIELDS = ("content", "creator_user_id", "execution_user_id", "prompt_prefix")


def new_owner_token() -> str:
    return uuid.uuid4().hex


def _const(value: str) -> ColumnElement[str]:
    """A constant written into the SQL text rather than bound.

    A partial index applies only to a plan that can prove its predicate; a
    cached generic plan cannot see a bound value, so the constants that select
    a partial index are written into the statement.
    """
    return literal(value, literal_execute=True)


def _states(*names: str) -> list[ColumnElement[str]]:
    return [_const(name) for name in names]


def _instant(now: float | None) -> ColumnElement[datetime]:
    """``now`` in epoch seconds, or the database's clock when ``None``."""
    if now is None:
        return func.now()
    return literal(datetime.fromtimestamp(now, tz=timezone.utc), TIMESTAMP(timezone=True))


def _after(now: float | None, seconds: float) -> ColumnElement[datetime]:
    return _instant(now) + timedelta(seconds=seconds)


def _times(row: dict[str, Any], *names: str) -> dict[str, Any]:
    return {**row, **{name: to_iso(row[name]) for name in names}}


# ── row views ────────────────────────────────────────────────────────────────


def _payload_columns(payload: dict[str, Any]) -> dict[str, Any]:
    """Column values for an admitted input; a key no column holds is refused."""
    unknown = set(payload) - set(_PAYLOAD_FIELDS) - {"response_policy"}
    if unknown:
        raise ValueError(f"not an inbound payload field: {sorted(unknown)}")
    values = {field: payload.get(field) for field in _PAYLOAD_FIELDS}
    for field in _TEXT_PAYLOAD_FIELDS:
        values[field] = str(values[field] or "")
    values["retain_context"] = bool(values["retain_context"])
    policy = payload.get("response_policy") or {}
    values["response_policy_mode"] = policy.get("mode")
    values["response_instruction"] = policy.get("instruction")
    return values


def _inbound_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """An inbound record as the spine reads it; the admitted input is ``payload``."""
    if row is None:
        return None
    payload = {field: row[field] for field in _PAYLOAD_FIELDS}
    payload["response_policy"] = (
        {"mode": row["response_policy_mode"], "instruction": row["response_instruction"] or ""}
        if row["response_policy_mode"]
        else None
    )
    return {
        "item_id": row["item_id"],
        "deployment_id": row["deployment_id"],
        "dedup_key": row["dedup_key"],
        "channel_name": row["channel_name"],
        **(
            {"assistant_id": row["assistant_id"]}
            if row["assistant_id"]
            else {"agent_id": row["agent_id"]}
        ),
        "payload": payload,
        "state": row["state"],
        "owner_token": row["owner_token"],
        "generation": int(row["generation"]),
        "lease_expires_at": to_iso(row["lease_expires_at"]),
        "authorization_expires_at": to_iso(row["authorization_expires_at"]),
        "attempts": int(row["attempts"]),
        "last_error": row["last_error"],
        "ignored_reason": row["ignored_reason"],
        "session_id": row["session_id"],
        "resolved_conversation_key": row["resolved_conversation_key"],
        "judgment": (
            {
                "respond": row["judgment_respond"],
                "reason": row["judgment_reason"],
                "judge": row["judgment_judge"],
                "judged_at": to_iso(row["judged_at"]),
            }
            if row["judged_at"] is not None
            else None
        ),
        "explicit_attention": bool(row["explicit_attention"]),
        "answer_ref": row["answer_ref"],
        "answer_question_index": row["answer_question_index"],
        "recall": (
            {"event_id": row["recall_event_id"], "timestamp": row["recall_timestamp"]}
            if row["recall_event_id"]
            else None
        ),
        "frozen_input": (
            {
                "content": row["frozen_content"],
                "context_item_ids": list(row["frozen_context_ids"] or []),
            }
            if row["frozen_content"] is not None
            else None
        ),
        "context_excluded": bool(row["context_excluded"]),
        "context_submitted": bool(row["context_submitted"]),
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }


def _views(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [view for view in map(_inbound_view, rows) if view is not None]


def _outbox_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    view = _times(row, "lease_expires_at", "next_attempt_at", "created_at", "updated_at")
    view["delivery_aliases"] = list(row["delivery_aliases"] or [])
    return view


def _alias_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else _times(row, "created_at")


def _owned(item_id: str, owner_token: str, generation: int, *states: str) -> ColumnElement[bool]:
    """The fence of the worker holding a live item at ``generation``."""
    c = _INBOUND.c
    return and_(
        c.item_id == item_id,
        c.state.in_(_states(*(states or _INBOUND_LIVE_STATES))),
        c.owner_token == owner_token,
        c.generation == int(generation),
    )


def _leased(outbox_id: str, owner: str, generation: int) -> ColumnElement[bool]:
    """The fence of the deliverer holding an outbox row's lease at ``generation``."""
    c = _OUTBOX.c
    return and_(
        c.outbox_id == outbox_id,
        c.state == _const(OUTBOX_SENDING),
        c.lease_owner == owner,
        c.lease_generation == int(generation),
    )


def _outbox_due(now: float | None) -> ColumnElement[bool]:
    """A row that is due: PENDING past its backoff, or SENDING with a lapsed lease."""
    c = _OUTBOX.c
    return or_(
        and_(c.state == _const(OUTBOX_PENDING), c.next_attempt_at <= _instant(now)),
        and_(c.state == _const(OUTBOX_SENDING), c.lease_expires_at <= _instant(now)),
    )


def _outbox_head() -> ColumnElement[bool]:
    """No earlier reply of the row's Session to the same binding is undelivered.

    A Session's replies reach a binding in order, so a later reply waits for
    an earlier one to be delivered or to die.
    """
    earlier = _OUTBOX.alias("earlier")
    return ~exists().where(
        earlier.c.deployment_id == _OUTBOX.c.deployment_id,
        earlier.c.session_id == _OUTBOX.c.session_id,
        earlier.c.state.in_(_states(OUTBOX_PENDING, OUTBOX_SENDING)),
        earlier.c.response_seq < _OUTBOX.c.response_seq,
    )


async def _end_subscription(conn: AsyncConnection, deployment_id: str, session_id: str) -> None:
    """Remove a subscription and the rows of its replies that have not completed.

    A final-text reply's row waits, with no due time, for its reply's
    ``finish``, which only the subscription's projection records. Once the
    subscription is gone nothing would complete or remove that row, and it
    would hold back every later reply of the Session to the binding.
    """
    await conn.execute(delete(_SUBSCRIPTIONS).where(
        _SUBSCRIPTIONS.c.deployment_id == deployment_id,
        _SUBSCRIPTIONS.c.session_id == session_id,
    ))
    o = _OUTBOX.c
    await conn.execute(delete(_OUTBOX).where(
        o.deployment_id == deployment_id,
        o.session_id == session_id,
        o.state == _const(OUTBOX_PENDING),
        o.next_attempt_at.is_(None),
    ))


def _context_candidates(
    deployment_id: str, conversation: str, binding_revision: str,
) -> list[ColumnElement[bool]]:
    """Retained messages of one conversation and binding revision not yet consumed."""
    c = _INBOUND.c
    return [
        c.deployment_id == deployment_id,
        c.conversation == conversation,
        c.binding_revision == binding_revision,
        c.retain_context,
        ~c.context_submitted,
        ~c.context_excluded,
        c.provider_ignore_reason.is_(None),
    ]


class ChannelRepository:
    """Inbound work items, conversations, subscriptions, deliveries and their marks."""

    # ── inbound work items ───────────────────────────────────────────────

    async def claim_inbound(
        self,
        *,
        deployment_id: str,
        dedup_key: str | None,
        channel_name: str,
        agent_id: str = "",
        assistant_id: str = "",
        payload: dict[str, Any],
        now: float | None = None,
        state: str = INBOUND_RECEIVED,
        ignored_reason: str | None = None,
        context_excluded: bool = False,
    ) -> tuple[dict[str, Any], str]:
        """Durably record one inbound message; return ``(record, outcome)``.

        This is the ACK point of the spine (channel-spine.md invariant A):
        once the outcome is anything but an exception, the source may
        acknowledge — the payload is persisted, so recovery does not depend
        on redelivery.

        ``state`` is the ingress classification, written with the record so no
        process loss can leave an unaddressed message driveable: RECEIVED (an
        admitted message), AUTHORIZED (an admitted authorization, waiting for
        its observation), OBSERVED (context only), or IGNORED (refused, with
        ``ignored_reason``). ``dedup_key=None`` (a platform without message
        ids) always records a new message, which still gets a recoverable work
        item.

        A message that already has a record keeps it. Outcomes:

        * ``claimed`` — the caller owns a RECEIVED item and drives it;
        * ``duplicate`` — the message settled (or died, for a signal that
          cannot revive it);
        * ``in_progress`` — a live owner holds it;
        * ``ignored`` — it was refused;
        * ``observed`` / ``authorized`` — it is OBSERVED or AUTHORIZED. The
          caller decides whether this inbound completes it (see
          :meth:`promote_inbound`).

        Only RECEIVED and AUTHORIZED requests admit work, so only they take a
        live item whose lease expired or revive a DEAD one. The insert and
        that takeover are one statement, its condition re-checking the lease,
        so of several concurrent redeliveries exactly one owns the item. A
        taken item keeps the input it was admitted with and its prior evidence.
        """
        if state not in _INBOUND_CLAIM_STATES:
            raise ValueError(f"not an inbound claim state: {state!r}")
        c = _INBOUND.c
        owner = new_owner_token()
        live = state == INBOUND_RECEIVED
        values: dict[str, Any] = {
            "item_id": uuid.uuid4().hex,
            "deployment_id": deployment_id,
            "dedup_key": dedup_key or None,
            "channel_name": channel_name,
            **({"assistant_id": assistant_id} if assistant_id else {"agent_id": agent_id}),
            **_payload_columns(payload),
            "state": state,
            "owner_token": owner if live else None,
            "lease_expires_at": _after(now, _inbound_lease_seconds()) if live else None,
        }
        if state == INBOUND_IGNORED:
            values["ignored_reason"] = str(ignored_reason or "")[:200]
            values["context_excluded"] = bool(context_excluded)
        if state == INBOUND_AUTHORIZED:
            values["authorization_expires_at"] = _after(now, _AUTHORIZATION_WAIT_SECONDS)
        admits = state in (INBOUND_RECEIVED, INBOUND_AUTHORIZED)
        identity = [c.deployment_id, c.dedup_key]
        statement = pg_insert(_INBOUND).values(**values)
        if admits:
            statement = statement.on_conflict_do_update(
                index_elements=identity,
                set_={
                    "state": INBOUND_RECEIVED,
                    "owner_token": owner,
                    "generation": c.generation + 1,
                    "lease_expires_at": _after(now, _inbound_lease_seconds()),
                    "updated_at": func.now(),
                },
                where=or_(
                    and_(
                        c.state.in_(_states(*_INBOUND_LIVE_STATES)),
                        c.lease_expires_at <= _instant(now),
                    ),
                    c.state == _const(INBOUND_DEAD),
                ),
            )
        else:
            statement = statement.on_conflict_do_nothing(index_elements=identity)
        written = _inbound_view(await fetch_one(statement.returning(_INBOUND)))
        if written is not None:
            return written, _WRITTEN_OUTCOMES[written["state"]]

        existing = await self.get_message_record(
            deployment_id=deployment_id, dedup_key=str(dedup_key)
        )
        if existing is None:
            raise RuntimeError("inbound record vanished after its conflicting insert")
        current = existing["state"]
        if current == INBOUND_SETTLED:
            return existing, CLAIM_OUTCOME_DUPLICATE
        if current in (INBOUND_IGNORED, INBOUND_OBSERVED, INBOUND_AUTHORIZED):
            return existing, _WRITTEN_OUTCOMES[current]
        if current == INBOUND_DEAD and not admits:
            return existing, CLAIM_OUTCOME_DUPLICATE
        # A live owner holds it, or another admission revived it first.
        return existing, CLAIM_OUTCOME_IN_PROGRESS

    async def promote_inbound(
        self,
        *,
        item_id: str,
        expected_state: str,
        expected_generation: int,
        payload: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Admit one OBSERVED or AUTHORIZED record: take it as an owned RECEIVED item.

        ``payload`` is the admitted input, assembled by the ingress from the
        message's first recorded content and the admitting signal. The
        condition pins the state and generation that were read, so of two
        racing admissions exactly one wins and the record is sent at most
        once; a record carrying a recall is never admitted. Returns the owned
        item, or ``None`` when the record changed first.
        """
        if expected_state not in _INBOUND_UNADMITTED_STATES:
            raise ValueError(f"not an unadmitted inbound state: {expected_state!r}")
        c = _INBOUND.c
        return _inbound_view(await fetch_one(
            update(_INBOUND)
            .where(
                c.item_id == item_id,
                c.state == expected_state,
                c.generation == int(expected_generation),
                c.recall_event_id.is_(None),
            )
            .values(
                state=INBOUND_RECEIVED,
                owner_token=new_owner_token(),
                generation=c.generation + 1,
                lease_expires_at=_after(now, _inbound_lease_seconds()),
                updated_at=func.now(),
                **_payload_columns(payload),
            )
            .returning(_INBOUND)
        ))

    async def observe_authorized_inbound(
        self,
        *,
        item_id: str,
        expected_generation: int,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Record the content of an AUTHORIZED message that will not run.

        An authorization's message can be recalled before its observation
        arrives. The observation still supplies the recalled message's place
        in the conversation's context, so the record becomes OBSERVED with
        that content, never RECEIVED.
        """
        c = _INBOUND.c
        return _inbound_view(await fetch_one(
            update(_INBOUND)
            .where(
                c.item_id == item_id,
                c.state == INBOUND_AUTHORIZED,
                c.generation == int(expected_generation),
            )
            .values(
                state=INBOUND_OBSERVED,
                generation=c.generation + 1,
                updated_at=func.now(),
                **_payload_columns(payload),
            )
            .returning(_INBOUND)
        ))

    async def refuse_unadmitted_inbound(
        self,
        *,
        item_id: str,
        expected_state: str,
        expected_generation: int,
        reason: str,
    ) -> bool:
        """Move one OBSERVED or AUTHORIZED record to IGNORED, out of context.

        Used when a later signal for the message contradicts the identity the
        record holds. A refused message never joins retained context.
        """
        if expected_state not in _INBOUND_UNADMITTED_STATES:
            raise ValueError(f"not an unadmitted inbound state: {expected_state!r}")
        c = _INBOUND.c
        changed = await execute(
            update(_INBOUND)
            .where(
                c.item_id == item_id,
                c.state == expected_state,
                c.generation == int(expected_generation),
            )
            .values(
                state=INBOUND_IGNORED,
                ignored_reason=str(reason)[:200],
                context_excluded=True,
                updated_at=func.now(),
            )
        )
        return changed == 1

    async def expire_authorizations(
        self, *, limit: int = 50, now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Move AUTHORIZED records whose wait has expired to DEAD, with evidence.

        One statement takes the due records it can lock and skips the ones
        another replica is expiring, so replicas sweeping together expire each
        record once. Returns the expired records for the caller to report.
        """
        c = _INBOUND.c
        due = (
            select(c.item_id)
            .where(
                c.state == _const(INBOUND_AUTHORIZED),
                c.authorization_expires_at <= _instant(now),
            )
            .order_by(c.authorization_expires_at)
            .limit(int(limit))
            .with_for_update(skip_locked=True)
        )
        return _views(await fetch_all(
            update(_INBOUND)
            .where(c.item_id.in_(due), c.state == _const(INBOUND_AUTHORIZED))
            .values(
                state=INBOUND_DEAD,
                last_error="the observation of this authorized message never arrived",
                updated_at=func.now(),
            )
            .returning(_INBOUND)
        ))

    async def _update_owned(
        self,
        item_id: str,
        owner_token: str,
        generation: int,
        values: dict[str, Any],
        *states: str,
    ) -> bool:
        changed = await execute(
            update(_INBOUND)
            .where(_owned(item_id, owner_token, generation, *states))
            .values(**values, updated_at=func.now())
        )
        return changed == 1

    async def begin_inbound_dispatch(
        self, *, item_id: str, owner_token: str, generation: int,
        now: float | None = None,
    ) -> bool:
        """Move the owned item into DISPATCHING with a renewed lease."""
        return await self._update_owned(
            item_id, owner_token, generation,
            {
                "state": INBOUND_DISPATCHING,
                "lease_expires_at": _after(now, _inbound_lease_seconds()),
            },
        )

    async def renew_inbound_lease(
        self, *, item_id: str, owner_token: str, generation: int,
        now: float | None = None,
    ) -> bool:
        """Renew the owned item's lease; ``False`` when another worker owns the item."""
        return await self._update_owned(
            item_id, owner_token, generation,
            {"lease_expires_at": _after(now, _inbound_lease_seconds())},
        )

    async def bind_inbound_dispatch(
        self,
        *,
        item_id: str,
        owner_token: str,
        generation: int,
        session_id: str | None = None,
        conversation_key: str | None = None,
    ) -> bool:
        """Record the owned item's Session and the conversation it was resolved for.

        A re-drive reuses the bound Session without resolving it again, so it
        reads ``conversation_key`` from here to take the same conversation
        lock. Whether the message already reached the Session is the
        Session's own record: its input carries the item's id as
        ``client_message_id`` (channel-spine.md, "one input per message").
        """
        values: dict[str, Any] = {}
        if session_id is not None:
            values["session_id"] = session_id
        if conversation_key is not None:
            values["resolved_conversation_key"] = conversation_key
        return await self._update_owned(item_id, owner_token, generation, values)

    async def capture_inbound_account(
        self, *, item_id: str, owner_token: str, generation: int, user_id: str,
    ) -> dict[str, Any] | None:
        """Keep the account a linked binding's message runs as; return the payload.

        Written once, under the owner fence and only while no account is
        kept, so recovery, a steal or a redelivery runs the message as the
        account its first drive resolved. ``None`` when this worker does not
        own the item.
        """
        captured = _inbound_view(await fetch_one(
            update(_INBOUND)
            .where(
                _owned(item_id, owner_token, generation),
                _INBOUND.c.execution_user_id == "",
            )
            .values(creator_user_id=user_id, execution_user_id=user_id, updated_at=func.now())
            .returning(_INBOUND)
        ))
        return None if captured is None else dict(captured["payload"])

    async def mark_inbound_answer(
        self,
        *,
        item_id: str,
        owner_token: str,
        generation: int,
        interaction_ref: str,
        question_index: int,
    ) -> bool:
        """Record which shown question this item answers, before it is used.

        A re-drive reads the mark instead of re-reading the message against
        the question shown *now*, which may already be the next one.
        """
        return await self._update_owned(
            item_id, owner_token, generation,
            {"answer_ref": interaction_ref, "answer_question_index": int(question_index)},
        )

    async def topic_has_participation(
        self, *, deployment_id: str, conversation: str, binding_revision: str,
    ) -> bool:
        """Whether the agent already answered a message of this topic and revision.

        A settled message is one the agent took part in. Participation, like
        retained context, belongs to one binding revision: editing the binding
        starts the topic afresh.
        """
        c = _INBOUND.c
        return bool(await fetch_value(select(exists().where(
            c.deployment_id == deployment_id,
            c.conversation == conversation,
            c.binding_revision == binding_revision,
            c.state == _const(INBOUND_SETTLED),
        ))))

    async def record_inbound_judgment(
        self, *, item_id: str, owner_token: str, generation: int,
        judgment: dict[str, Any],
    ) -> bool:
        """Keep the owned item's response judgment, once, before anything acts on it.

        A re-drive after a host failure reads the recorded judgment instead of
        asking the judge again, so one message gets one decision.
        """
        judged_at = from_iso(judgment.get("judged_at"))
        changed = await execute(
            update(_INBOUND)
            .where(
                _owned(item_id, owner_token, generation, INBOUND_DISPATCHING),
                _INBOUND.c.judged_at.is_(None),
            )
            .values(
                judgment_respond=judgment.get("respond") is True,
                judgment_reason=str(judgment.get("reason") or ""),
                judgment_judge=judgment.get("judge"),
                judged_at=judged_at if judged_at is not None else func.now(),
                updated_at=func.now(),
            )
        )
        return changed == 1

    async def suppress_judged_inbound(
        self, *, item_id: str, owner_token: str, generation: int,
    ) -> dict[str, Any] | None:
        """Return a message the judge declined from DISPATCHING to OBSERVED.

        The message stays retained context. A copy of it that addressed the
        agent while it was being judged marked it explicitly attended; such a
        record is never suppressed, so the explicit signal is not lost.
        """
        c = _INBOUND.c
        return _inbound_view(await fetch_one(
            update(_INBOUND)
            .where(
                _owned(item_id, owner_token, generation, INBOUND_DISPATCHING),
                c.explicit_attention.is_(False),
            )
            .values(
                state=INBOUND_OBSERVED,
                owner_token=None,
                lease_expires_at=None,
                generation=c.generation + 1,
                updated_at=func.now(),
            )
            .returning(_INBOUND)
        ))

    async def note_explicit_attention(self, *, item_id: str) -> bool:
        """Mark a message under judgment as addressed to the agent by another copy.

        Unfenced: the mark is the same whoever writes it, and the judge's
        owner reads it before suppressing the message.
        """
        c = _INBOUND.c
        changed = await execute(
            update(_INBOUND)
            .where(
                c.item_id == item_id,
                c.state.in_(_states(*_INBOUND_LIVE_STATES)),
                c.selection == "judge",
            )
            .values(explicit_attention=True, updated_at=func.now())
        )
        return changed == 1

    async def settle_inbound(
        self,
        *,
        item_id: str,
        owner_token: str,
        generation: int,
        session_id: str,
        submit_context: bool = False,
    ) -> bool:
        """Move the owned DISPATCHING item to SETTLED: the Session holds its input,
        or it answered a question.

        With ``submit_context`` the item and the retained messages its frozen
        input quotes are consumed as context in the same transaction, so the
        context a sent message quoted is never quoted again and an unsent one's
        is.
        Returns ``False`` when another worker owns the item; that successor
        settles it.
        """
        c = _INBOUND.c

        async def settle(conn: AsyncConnection) -> bool:
            settled = (await conn.execute(
                update(_INBOUND)
                .where(_owned(item_id, owner_token, generation, INBOUND_DISPATCHING))
                .values(
                    state=INBOUND_SETTLED,
                    session_id=session_id,
                    context_submitted=c.context_submitted | bool(submit_context),
                    updated_at=func.now(),
                )
                .returning(c.deployment_id, c.frozen_content, c.frozen_context_ids)
            )).first()
            if settled is None:
                return False
            if not submit_context:
                return True
            if settled.frozen_content is None:
                raise RuntimeError("retained channel input has no frozen context")
            quoted = list(settled.frozen_context_ids or [])
            if quoted:
                marked = (await conn.execute(
                    update(_INBOUND)
                    .where(c.deployment_id == settled.deployment_id, c.item_id.in_(quoted))
                    .values(context_submitted=True)
                )).rowcount
                if marked != len(quoted):
                    raise RuntimeError("channel context quoted by this message is missing")
            return True

        return await transaction(settle)

    async def mark_inbound_ignored(
        self, *, item_id: str, owner_token: str, generation: int, reason: str,
        exclude_from_context: bool = False,
    ) -> bool:
        """Move a live item to IGNORED: a terminal, ACK-safe refusal.

        A refusal can be decided at admission (RECEIVED) or when a recovered
        message is driven again (DISPATCHING), always before it is sent.
        """
        return await self._update_owned(
            item_id, owner_token, generation,
            {
                "state": INBOUND_IGNORED,
                "ignored_reason": str(reason)[:200],
                **({"context_excluded": True} if exclude_from_context else {}),
            },
        )

    async def fail_inbound(
        self,
        *,
        item_id: str,
        owner_token: str,
        generation: int,
        error: str,
        backoff: Sequence[float],
        now: float | None = None,
    ) -> str | None:
        """Record a failed drive on the owned item; return the state it is left in.

        ``backoff`` is the wait before each retry. While retries remain the item
        returns to RECEIVED with its lease expiring after the next wait — the
        reconciler's watermark, so nobody hot-loops a failing item; after the
        last it is DEAD with evidence, and only an explicit platform
        redelivery revives it. The count of recorded failures and the choice
        are made in the statement, from the stored count. ``None`` when another
        worker owns the item.
        """
        c = _INBOUND.c
        retries = len(backoff)
        values: dict[str, Any] = {
            "attempts": c.attempts + 1,
            "last_error": str(error)[:500],
            "updated_at": func.now(),
        }
        if retries:
            values["state"] = case((c.attempts < retries, INBOUND_RECEIVED), else_=INBOUND_DEAD)
            values["lease_expires_at"] = case(
                *((c.attempts == index, _after(now, wait)) for index, wait in enumerate(backoff)),
                else_=c.lease_expires_at,
            )
        else:
            values["state"] = INBOUND_DEAD
        failed = await fetch_one(
            update(_INBOUND)
            .where(_owned(item_id, owner_token, generation))
            .values(**values)
            .returning(c.state)
        )
        return None if failed is None else str(failed["state"])

    async def release_inbound_lease(
        self, *, item_id: str, owner_token: str, generation: int
    ) -> bool:
        """Hand back owned work without failing it or forgetting its dispatch.

        The lease expires now, so the reconciler takes the item through the
        normal generation fence. The attempt count is kept: it is part of the
        command identity.
        """
        return await self._update_owned(
            item_id, owner_token, generation, {"lease_expires_at": func.now()},
        )

    async def list_recoverable_inbound(
        self, *, limit: int = 50, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Reconciler read: live items whose lease has expired, longest expired first.

        The ownership gate is :meth:`claim_inbound_for_recovery`; this read
        never transfers ownership.
        """
        c = _INBOUND.c
        return _views(await fetch_all(
            select(_INBOUND)
            .where(
                c.state.in_(_states(*_INBOUND_LIVE_STATES)),
                c.lease_expires_at <= _instant(now),
            )
            .order_by(c.lease_expires_at, c.item_id)
            .limit(int(limit))
        ))

    async def claim_inbound_for_recovery(
        self, *, item_id: str, expected_generation: int, now: float | None = None
    ) -> dict[str, Any] | None:
        """Take ownership of an expired live item (reconciler re-drive).

        Fences forward (``generation + 1``) atop the exact generation the
        sweep read and re-verifies expiry in the same condition, so a live
        renewal between read and claim wins and this returns ``None``. The
        state is preserved: DISPATCHING tells the new owner a command may
        already exist (attach path); RECEIVED means dispatch never began.
        """
        c = _INBOUND.c
        return _inbound_view(await fetch_one(
            update(_INBOUND)
            .where(
                c.item_id == item_id,
                c.state.in_(_states(*_INBOUND_LIVE_STATES)),
                c.generation == int(expected_generation),
                c.lease_expires_at <= _instant(now),
            )
            .values(
                owner_token=new_owner_token(),
                generation=c.generation + 1,
                lease_expires_at=_after(now, _inbound_lease_seconds()),
                updated_at=func.now(),
            )
            .returning(_INBOUND)
        ))

    async def get_inbound(self, item_id: str) -> dict[str, Any] | None:
        return _inbound_view(
            await fetch_one(select(_INBOUND).where(_INBOUND.c.item_id == item_id))
        )

    async def get_message_record(
        self, *, deployment_id: str, dedup_key: str,
    ) -> dict[str, Any] | None:
        """The record of one platform message, by the provider's dedup key."""
        c = _INBOUND.c
        return _inbound_view(await fetch_one(
            select(_INBOUND).where(c.deployment_id == deployment_id, c.dedup_key == dedup_key)
        ))

    async def record_recall(
        self, *, deployment_id: str, conversation_key: str,
        event_id: str, message_id: str, timestamp: str,
    ) -> bool:
        """Record external deletion without rewriting accepted Agent input.

        A recalled message that has not been admitted never runs: an admitting
        signal that arrives after the recall is refused (see
        :meth:`promote_inbound`). An AUTHORIZED record may not know its
        conversation yet — its observation carries that — so only a record
        that holds a conversation key is checked against the recall's. The
        recall is written once; the same recall again is a no-op and a
        different one raises. Returns ``False`` for a message with no record.
        """
        if not all((deployment_id, conversation_key, event_id, message_id, timestamp)):
            raise ValueError("recall event identity is incomplete")
        if event_id == message_id:
            raise ValueError("recall event and target must have different identities")
        c = _INBOUND.c
        awaiting_content = and_(
            c.state == INBOUND_AUTHORIZED, func.coalesce(c.conversation_key, "") == "",
        )
        recorded = await fetch_one(
            update(_INBOUND)
            .where(
                c.deployment_id == deployment_id,
                c.dedup_key == message_id,
                c.recall_event_id.is_(None),
                or_(c.conversation_key == conversation_key, awaiting_content),
            )
            .values(recall_event_id=event_id, recall_timestamp=timestamp, updated_at=func.now())
            .returning(c.item_id)
        )
        if recorded is not None:
            return True
        target = await self.get_message_record(deployment_id=deployment_id, dedup_key=message_id)
        if target is None:
            return False
        target_key = target["payload"]["conversation_key"]
        if not (target["state"] == INBOUND_AUTHORIZED and not target_key) and (
            target_key != conversation_key
        ):
            raise RuntimeError("recall target conversation identity changed")
        if target["recall"] != {"event_id": event_id, "timestamp": timestamp}:
            raise RuntimeError(f"conflicting recall payload for message {message_id!r}")
        return True

    async def collect_unsubmitted_context(
        self, item: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The retained messages a turn of ``item`` quotes, oldest first.

        Context is the conversation's not-yet-consumed messages in the item's
        binding revision, no later than the item's own message, at most
        :data:`CONTEXT_WINDOW_MESSAGES` of the most recent. It does not depend
        on the account a message runs as: what was said in a room is context
        for whoever the agent answers there.
        """
        payload = item["payload"]
        if not payload.get("retain_context"):
            return []
        if not all(payload.get(key) for key in (
            "conversation", "binding_revision", "message_timestamp",
        )):
            raise RuntimeError("channel context ordering identity is incomplete")
        c = _INBOUND.c
        window = (
            select(
                c.item_id, c.dedup_key, c.message_timestamp, c.participant,
                c.reply_text, c.recall_event_id, c.recall_timestamp,
            )
            .where(
                *_context_candidates(
                    item["deployment_id"], payload["conversation"], payload["binding_revision"],
                ),
                c.message_timestamp <= payload["message_timestamp"],
                c.item_id != item["item_id"],
            )
            .order_by(c.message_timestamp.desc(), c.dedup_key.desc())
            .limit(CONTEXT_WINDOW_MESSAGES)
            .subquery()
        )
        rows = await fetch_all(
            select(window).order_by(window.c.message_timestamp, window.c.dedup_key)
        )
        context = []
        for row in rows:
            entry: dict[str, Any] = {
                "item_id": row["item_id"],
                "message_id": row["dedup_key"],
                "timestamp": row["message_timestamp"],
                "participant": row["participant"],
                # What the participant wrote: the binding's prompt prefix
                # belongs to the turn's own input, once, not to every message
                # quoted as context.
                "content": row["reply_text"],
            }
            if row["recall_event_id"] is not None:
                entry["recall"] = {
                    "event_id": row["recall_event_id"], "timestamp": row["recall_timestamp"],
                }
                # The accepted task must not retain the recalled text through
                # a secondary context field after rendering its marker.
                entry["content"] = f"[该消息已于 {row['recall_timestamp']} 被撤回]"
            context.append(entry)
        return context

    async def freeze_inbound_input(
        self, *, item_id: str, owner_token: str, generation: int,
        content: str, context: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Freeze one accepted input under the dispatch owner fence; return it.

        Written once: a re-drive submits the input the first drive froze. When
        ``context`` filled the window, the older retained messages it left out
        are consumed in the same transaction, so a later turn never quotes a
        message older than one this turn quoted. ``None`` when another worker
        owns the item.
        """
        c = _INBOUND.c
        quoted = [str(entry["item_id"]) for entry in context]

        async def freeze(conn: AsyncConnection) -> dict[str, Any] | None:
            frozen = (await conn.execute(
                update(_INBOUND)
                .where(
                    _owned(item_id, owner_token, generation, INBOUND_DISPATCHING),
                    c.frozen_content.is_(None),
                )
                .values(frozen_content=content, frozen_context_ids=quoted, updated_at=func.now())
                .returning(
                    c.deployment_id, c.conversation, c.binding_revision,
                    c.frozen_content, c.frozen_context_ids,
                )
            )).first()
            if frozen is None:
                frozen = (await conn.execute(
                    select(c.frozen_content, c.frozen_context_ids)
                    .where(_owned(item_id, owner_token, generation, INBOUND_DISPATCHING))
                )).first()
                if frozen is None or frozen.frozen_content is None:
                    return None
            elif len(context) >= CONTEXT_WINDOW_MESSAGES:
                oldest = context[0]
                await conn.execute(
                    update(_INBOUND)
                    .where(
                        *_context_candidates(
                            frozen.deployment_id, frozen.conversation, frozen.binding_revision,
                        ),
                        c.item_id != item_id,
                        tuple_(c.message_timestamp, c.dedup_key)
                        < tuple_(str(oldest["timestamp"]), str(oldest["message_id"])),
                    )
                    .values(context_submitted=True)
                )
            return {
                "content": frozen.frozen_content,
                "context_item_ids": list(frozen.frozen_context_ids or []),
            }

        return await transaction(freeze)

    async def exclude_inbound_from_context(self, item: dict[str, Any]) -> None:
        """Keep an answer to a question out of later turns' conversation context.

        Only this item moves: earlier retained messages it did not consume stay
        pending for the next turn.
        """
        if not await self._update_owned(
            item["item_id"], item["owner_token"], int(item["generation"]),
            {"context_excluded": True},
        ):
            raise RuntimeError("channel answer lost dispatch ownership before leaving the context")

    # ── conversation continuity ──────────────────────────────────────────

    @staticmethod
    def _conversation(
        table: Any, deployment_id: str, conversation_key: str, execution_user_id: str,
    ) -> ColumnElement[bool]:
        return and_(
            table.c.deployment_id == deployment_id,
            table.c.execution_user_id == execution_user_id,
            table.c.conversation_key == conversation_key,
        )

    async def get_conversation(
        self, *, deployment_id: str, conversation_key: str, execution_user_id: str = "",
    ) -> dict[str, Any] | None:
        row = await fetch_one(select(_CONVERSATIONS).where(self._conversation(
            _CONVERSATIONS, deployment_id, conversation_key, execution_user_id,
        )))
        return None if row is None else _times(row, "created_at", "updated_at")

    async def upsert_conversation(
        self,
        *,
        deployment_id: str,
        conversation_key: str,
        session_id: str,
        replaces_session_id: str | None = None,
        execution_user_id: str = "",
    ) -> dict[str, Any] | None:
        """Map the key to ``session_id``; return the mapping that holds.

        A first mapping inserts, and of racing first mappings one wins and the
        others read it back. A replacement (a terminated Session swapped for a
        fresh one) matches the Session it replaces, so two racing callbacks
        cannot both replace and orphan a Session; it returns ``None`` when it
        lost. The replaced Session's output stops reaching this binding: the
        conversation continues in the new one.
        """
        if replaces_session_id is None:
            inserted = await fetch_one(
                pg_insert(_CONVERSATIONS)
                .values(
                    deployment_id=deployment_id,
                    execution_user_id=execution_user_id,
                    conversation_key=conversation_key,
                    session_id=session_id,
                )
                .on_conflict_do_nothing()
                .returning(_CONVERSATIONS)
            )
            if inserted is not None:
                return _times(inserted, "created_at", "updated_at")
            return await self.get_conversation(
                deployment_id=deployment_id,
                conversation_key=conversation_key,
                execution_user_id=execution_user_id,
            )

        async def replace(conn: AsyncConnection) -> dict[str, Any] | None:
            replaced = (await conn.execute(
                update(_CONVERSATIONS)
                .where(
                    self._conversation(
                        _CONVERSATIONS, deployment_id, conversation_key, execution_user_id,
                    ),
                    _CONVERSATIONS.c.session_id == replaces_session_id,
                )
                .values(session_id=session_id, updated_at=func.now())
                .returning(_CONVERSATIONS)
            )).first()
            if replaced is None:
                return None
            await _end_subscription(conn, deployment_id, replaces_session_id)
            return _times(dict(replaced._mapping), "created_at", "updated_at")

        return await transaction(replace)

    # ── per-conversation dispatch lock ───────────────────────────────────

    async def acquire_conversation_lock(
        self,
        *,
        deployment_id: str,
        conversation_key: str,
        owner: str,
        execution_user_id: str = "",
        now: float | None = None,
    ) -> int | None:
        """Take the conversation's dispatch lock; its generation, or ``None`` on contention.

        Serializes drives within one conversation, so two inbound messages
        never answer, stop or quote around each other in the same Session.
        Acquires when the lock
        is free or its lease lapsed (a stopped holder); every acquisition
        raises the generation, and renewal and release match ``(owner,
        generation)``, so a holder whose lease lapsed cannot renew or release
        its successor's lock.
        """
        statement = pg_insert(_LOCKS).values(
            deployment_id=deployment_id,
            execution_user_id=execution_user_id,
            conversation_key=conversation_key,
            owner=owner,
            generation=1,
            expires_at=_after(now, _CONVERSATION_LOCK_SECONDS),
        )
        acquired = await fetch_one(
            statement.on_conflict_do_update(
                index_elements=[
                    _LOCKS.c.deployment_id, _LOCKS.c.execution_user_id, _LOCKS.c.conversation_key,
                ],
                set_={
                    "owner": owner,
                    "expires_at": _after(now, _CONVERSATION_LOCK_SECONDS),
                    "generation": _LOCKS.c.generation + 1,
                },
                where=or_(_LOCKS.c.owner.is_(None), _LOCKS.c.expires_at <= _instant(now)),
            )
            .returning(_LOCKS.c.generation)
        )
        return None if acquired is None else int(acquired["generation"])

    async def renew_conversation_lock(
        self,
        *,
        deployment_id: str,
        conversation_key: str,
        owner: str,
        generation: int,
        execution_user_id: str = "",
        now: float | None = None,
    ) -> bool:
        """Extend the lock held by ``owner`` at ``generation``; ``False`` if it was taken."""
        changed = await execute(
            update(_LOCKS)
            .where(
                self._conversation(_LOCKS, deployment_id, conversation_key, execution_user_id),
                _LOCKS.c.owner == owner,
                _LOCKS.c.generation == int(generation),
            )
            .values(expires_at=_after(now, _CONVERSATION_LOCK_SECONDS))
        )
        return changed == 1

    async def release_conversation_lock(
        self,
        *,
        deployment_id: str,
        conversation_key: str,
        owner: str,
        execution_user_id: str = "",
        generation: int,
    ) -> None:
        """Release iff still held by ``owner`` at ``generation`` (never steal)."""
        await execute(
            update(_LOCKS)
            .where(
                self._conversation(_LOCKS, deployment_id, conversation_key, execution_user_id),
                _LOCKS.c.owner == owner,
                _LOCKS.c.generation == int(generation),
            )
            .values(owner=None, expires_at=None)
        )

    # ── output subscriptions ─────────────────────────────────────────────

    async def subscribe_to_output(
        self, *, session_id: str, deployment_id: str, channel_name: str,
        reply_context: dict[str, Any], conversation_key: str | None,
        after_seq: int, streaming: bool,
    ) -> None:
        """Bind a destination for the Session lifetime without replaying history.

        ``after_seq`` is the Session's output position now. The first input's
        destination and position stay: a later input in the Session neither
        replaces the destination nor moves the position.
        """
        await execute(
            pg_insert(_SUBSCRIPTIONS)
            .values(
                deployment_id=deployment_id,
                session_id=session_id,
                channel_name=channel_name,
                reply_context=dict(reply_context),
                conversation_key=conversation_key,
                streaming=bool(streaming),
                after_seq=int(after_seq),
            )
            .on_conflict_do_nothing()
        )

    async def session_output_subscriptions(self, session_id: str) -> list[dict[str, Any]]:
        """The subscriptions of one Session whose binding is enabled."""
        s = _SUBSCRIPTIONS.c
        rows = await fetch_all(
            select(_SUBSCRIPTIONS)
            .join(_DEPLOYMENTS, _DEPLOYMENTS.c.deployment_id == s.deployment_id)
            .where(s.session_id == session_id, _DEPLOYMENTS.c.enabled)
            .order_by(s.deployment_id)
        )
        return [_times(row, "created_at") for row in rows]

    async def list_output_subscriptions(
        self, *, after: tuple[str, str] | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """One page, in key order after ``after``, of the subscriptions with work.

        A subscription has work when its Session recorded output past its
        position, its binding is disabled (``binding_enabled`` false), or its
        Session was deleted (``session_deleted``).
        """
        s = _SUBSCRIPTIONS.c
        output_past = (
            select(_SESSION_OUTPUT.c.seq)
            .where(
                _SESSION_OUTPUT.c.session_id == s.session_id,
                _SESSION_OUTPUT.c.seq > s.after_seq,
            )
            .exists()
        )
        statement = (
            select(
                _SUBSCRIPTIONS,
                _DEPLOYMENTS.c.enabled.label("binding_enabled"),
                _SESSIONS.c.deleted.label("session_deleted"),
            )
            .join(_DEPLOYMENTS, _DEPLOYMENTS.c.deployment_id == s.deployment_id)
            .join(_SESSIONS, _SESSIONS.c.session_id == s.session_id)
            .where(or_(~_DEPLOYMENTS.c.enabled, _SESSIONS.c.deleted, output_past))
        )
        if after is not None:
            statement = statement.where(tuple_(s.deployment_id, s.session_id) > tuple_(*after))
        rows = await fetch_all(
            statement.order_by(s.deployment_id, s.session_id).limit(int(limit))
        )
        return [_times(row, "created_at") for row in rows]

    async def advance_output_subscription(
        self, *, deployment_id: str, session_id: str, expected_seq: int, sequence: int,
    ) -> bool:
        """Move the cursor from ``expected_seq``; concurrent projectors move it once."""
        s = _SUBSCRIPTIONS.c
        changed = await execute(
            update(_SUBSCRIPTIONS)
            .where(
                s.deployment_id == deployment_id,
                s.session_id == session_id,
                s.after_seq == int(expected_seq),
            )
            .values(after_seq=int(sequence))
        )
        return changed == 1

    async def remove_output_subscription(self, *, deployment_id: str, session_id: str) -> None:
        """End a binding's subscription to a Session, with the replies still waiting to complete."""

        async def end(conn: AsyncConnection) -> None:
            await _end_subscription(conn, deployment_id, session_id)

        await transaction(end)

    # ── reply outbox ─────────────────────────────────────────────────────

    async def create_outbox_entry(
        self,
        *,
        deployment_id: str,
        channel_name: str,
        session_id: str,
        response_seq: int,
        response_id: str,
        reply_context: dict[str, Any],
        conversation_key: str | None = None,
        streaming: bool = False,
        due: bool = True,
    ) -> dict[str, Any]:
        """Idempotently create a reply's delivery row; return it.

        Identity is the destination binding, Session and reply, whatever made
        the runtime reply. A row created with ``due`` false has no
        ``next_attempt_at`` and waits for :meth:`complete_reply`. Projecting
        the same output again finds the same row and keeps its delivery state.
        """
        o = _OUTBOX.c
        created = await fetch_one(
            pg_insert(_OUTBOX)
            .values(
                outbox_id=uuid.uuid4().hex,
                deployment_id=deployment_id,
                session_id=session_id,
                response_id=response_id,
                response_seq=int(response_seq),
                channel_name=channel_name,
                conversation_key=conversation_key,
                reply_context=dict(reply_context),
                streaming=bool(streaming),
                next_attempt_at=func.now() if due else None,
            )
            .on_conflict_do_nothing(index_elements=[o.deployment_id, o.session_id, o.response_id])
            .returning(_OUTBOX)
        )
        if created is None:
            created = await fetch_one(select(_OUTBOX).where(
                o.deployment_id == deployment_id,
                o.session_id == session_id,
                o.response_id == response_id,
            ))
            if created is None:
                raise RuntimeError("outbox row vanished after its conflicting insert")
        view = _outbox_view(created)
        assert view is not None
        return view

    async def complete_reply(
        self, *, deployment_id: str, session_id: str, response_id: str,
    ) -> dict[str, Any] | None:
        """Make a reply's waiting row due now that the reply is complete; the row, or ``None``.

        Only a ``PENDING`` row with no due time moves, so completing a reply
        twice, or one whose row a deliverer already holds, changes nothing.
        """
        o = _OUTBOX.c
        return _outbox_view(await fetch_one(
            update(_OUTBOX)
            .where(
                o.deployment_id == deployment_id,
                o.session_id == session_id,
                o.response_id == response_id,
                o.state == _const(OUTBOX_PENDING),
                o.next_attempt_at.is_(None),
            )
            .values(next_attempt_at=func.now(), updated_at=func.now())
            .returning(_OUTBOX)
        ))

    async def discard_reply(
        self, *, deployment_id: str, session_id: str, response_id: str,
    ) -> bool:
        """Settle an empty reply while retaining its delivery identity.

        Only a waiting ``PENDING`` row moves. A delayed projector of its
        ``start`` then finds this terminal row instead of recreating a
        waiting row after the subscription passed the reply's ``finish``.
        """
        o = _OUTBOX.c
        changed = await execute(
            update(_OUTBOX).where(
                o.deployment_id == deployment_id,
                o.session_id == session_id,
                o.response_id == response_id,
                o.state == _const(OUTBOX_PENDING),
                o.next_attempt_at.is_(None),
            )
            .values(state=OUTBOX_DELIVERED, updated_at=func.now())
        )
        return changed == 1

    async def claim_outbox_for_delivery(
        self, outbox_id: str, *, owner: str, now: float | None = None,
    ) -> dict[str, Any] | None:
        """Lease a due row for delivery; return the leased row, or ``None``.

        Due means PENDING past its backoff, or SENDING with a lapsed lease (a
        deliverer that stopped). The same statement checks that no earlier
        reply of the Session to this binding is undelivered, so replies reach
        the binding in order however many deliverers sweep. Two replicas
        claiming one row: only one leases it. Every acquisition raises
        ``lease_generation``, and every settlement write matches ``(owner,
        generation)``, so a deliverer whose lease lapsed mid-flight cannot
        complete or fail the row once a successor owns it.
        """
        o = _OUTBOX.c
        return _outbox_view(await fetch_one(
            update(_OUTBOX)
            .where(o.outbox_id == outbox_id, _outbox_due(now), _outbox_head())
            .values(
                state=OUTBOX_SENDING,
                lease_owner=owner,
                lease_expires_at=_after(now, _OUTBOX_LEASE_SECONDS),
                lease_generation=o.lease_generation + 1,
                next_attempt_at=None,
                updated_at=func.now(),
            )
            .returning(_OUTBOX)
        ))

    async def renew_outbox_lease(
        self, outbox_id: str, *, owner: str, generation: int, now: float | None = None,
    ) -> bool:
        """Heartbeat for a long-lived (streaming) delivery session."""
        changed = await execute(
            update(_OUTBOX)
            .where(_leased(outbox_id, owner, generation))
            .values(lease_expires_at=_after(now, _OUTBOX_LEASE_SECONDS))
        )
        return changed == 1

    async def mark_stream_started(self, outbox_id: str, *, owner: str, generation: int) -> bool:
        """Record that a streaming delivery opened the reply on the platform.

        A delivery that resumes after this continues the reply instead of
        opening a second one.
        """
        changed = await execute(
            update(_OUTBOX)
            .where(_leased(outbox_id, owner, generation), _OUTBOX.c.stream_started.is_(False))
            .values(stream_started=True, updated_at=func.now())
        )
        return changed == 1

    async def record_delivery_aliases(
        self,
        outbox_id: str,
        *,
        owner: str,
        generation: int,
        aliases: list[str],
        interaction: dict[str, Any] | None = None,
    ) -> bool:
        """Persist what a successful delivery created, before the row is marked DELIVERED.

        In one transaction under the delivery lease: the platform message
        aliases on the row, each alias as a reply target of the row's
        conversation (``channel_aliases``), and, when the delivery showed a
        question (``interaction``: ``ref``, ``interaction_id``, ``view``), the
        record of that question. A deliverer that dies afterwards leaves all
        of them for its successor, which sees the aliases as idempotency
        evidence (update-instead-of-create); a deliverer that dies before
        leaves none, and the question was never recorded as shown. Returns
        ``False`` when another deliverer holds the lease.
        """
        recorded = list(dict.fromkeys(str(alias) for alias in aliases))
        o = _OUTBOX.c

        async def record(conn: AsyncConnection) -> bool:
            leased = (await conn.execute(
                update(_OUTBOX)
                .where(_leased(outbox_id, owner, generation))
                .values(delivery_aliases=recorded, updated_at=func.now())
                .returning(
                    o.deployment_id, o.session_id, o.conversation_key, o.channel_name,
                    o.reply_context,
                )
            )).first()
            if leased is None:
                return False
            ref: str | None = None
            if interaction is not None:
                ref = str(interaction["ref"])
                await conn.execute(
                    pg_insert(_INTERACTIONS)
                    .values(
                        deployment_id=leased.deployment_id,
                        ref=ref,
                        session_id=leased.session_id,
                        interaction_id=str(interaction["interaction_id"]),
                        conversation_key=leased.conversation_key,
                        channel_name=leased.channel_name,
                        reply_context=leased.reply_context,
                        view=dict(interaction["view"]),
                    )
                    .on_conflict_do_nothing()
                )
            if recorded:
                statement = pg_insert(_ALIASES).values([
                    {
                        "deployment_id": leased.deployment_id,
                        "alias": alias,
                        "conversation_key": leased.conversation_key,
                        "session_id": leased.session_id,
                        "interaction_ref": ref,
                        "question_index": 0 if ref is not None else None,
                    }
                    for alias in recorded
                ])
                await conn.execute(statement.on_conflict_do_update(
                    index_elements=[_ALIASES.c.deployment_id, _ALIASES.c.alias],
                    set_={
                        "conversation_key": statement.excluded.conversation_key,
                        "session_id": statement.excluded.session_id,
                        # A later receipt for the same message without a
                        # question keeps the question it showed.
                        "interaction_ref": func.coalesce(
                            statement.excluded.interaction_ref, _ALIASES.c.interaction_ref,
                        ),
                        "question_index": func.coalesce(
                            statement.excluded.question_index, _ALIASES.c.question_index,
                        ),
                    },
                ))
            return True

        return await transaction(record)

    async def mark_outbox_delivered(
        self, outbox_id: str, *, owner: str, generation: int
    ) -> bool:
        """Move SENDING → DELIVERED for the exact lease holder."""
        changed = await execute(
            update(_OUTBOX)
            .where(_leased(outbox_id, owner, generation))
            .values(
                state=OUTBOX_DELIVERED,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=None,
                updated_at=func.now(),
            )
        )
        return changed == 1

    async def record_outbox_failure(
        self,
        outbox_id: str,
        *,
        owner: str,
        generation: int,
        error: str,
        backoff: Sequence[float],
        retry: bool = True,
        now: float | None = None,
    ) -> str | None:
        """Record a failed attempt for the exact lease holder; return the row's new state.

        ``backoff`` is the wait before each retry. While retries remain the row
        returns to PENDING, due after the next wait; afterwards, or at once
        when ``retry`` is false, it is DEAD. The count of failed attempts and
        the choice are made in the statement, from the stored count, so the
        budget holds across deliverers and restarts. The lease is released
        either way: no deliverer holds a row while it waits. ``None`` when
        another deliverer holds the lease.
        """
        o = _OUTBOX.c
        retries = len(backoff) if retry else 0
        values: dict[str, Any] = {
            "attempts": o.attempts + 1,
            "last_error": str(error)[:500],
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": func.now(),
        }
        if retries:
            values["state"] = case((o.attempts < retries, OUTBOX_PENDING), else_=OUTBOX_DEAD)
            values["next_attempt_at"] = case(
                *((o.attempts == index, _after(now, wait)) for index, wait in enumerate(backoff)),
                else_=None,
            )
        else:
            values["state"] = OUTBOX_DEAD
            values["next_attempt_at"] = None
        failed = await fetch_one(
            update(_OUTBOX)
            .where(_leased(outbox_id, owner, generation))
            .values(**values)
            .returning(o.state)
        )
        return None if failed is None else str(failed["state"])

    async def release_outbox_lease(self, outbox_id: str, *, owner: str, generation: int) -> bool:
        """Hand a delivery back without counting a failure; it is due again at once."""
        changed = await execute(
            update(_OUTBOX)
            .where(_leased(outbox_id, owner, generation))
            .values(
                state=OUTBOX_PENDING,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=func.now(),
                updated_at=func.now(),
            )
        )
        return changed == 1

    async def list_pending_outbox(
        self,
        *,
        limit: int = 50,
        now: float | None = None,
        deployment_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Due rows that are first in their Session's order, longest due first.

        ``deployment_id`` and ``session_id`` narrow the read to one binding's
        deliveries of one Session. A row waiting behind an earlier undelivered
        reply is not listed, so it cannot take the place of a row that can be
        delivered. The caller must lease each row through
        :meth:`claim_outbox_for_delivery` before delivering it.
        """
        o = _OUTBOX.c
        due_at = case(
            (o.state == _const(OUTBOX_PENDING), o.next_attempt_at), else_=o.lease_expires_at,
        )
        statement = select(_OUTBOX).where(_outbox_due(now), _outbox_head())
        if deployment_id is not None:
            statement = statement.where(o.deployment_id == deployment_id)
        if session_id is not None:
            statement = statement.where(o.session_id == session_id)
        rows = await fetch_all(
            statement.order_by(due_at, o.outbox_id).limit(int(limit))
        )
        return [view for view in map(_outbox_view, rows) if view is not None]


    # ── reply-chain aliases ──────────────────────────────────────────────

    async def link_alias(
        self, *, deployment_id: str, existing_alias: str, new_alias: str
    ) -> dict[str, Any] | None:
        """Attach a late-materialized platform id to an existing chain.

        Post-delivery alias enrichment (docs/channel-spine.md): some
        platforms mint the quotable message id only after the delivery ack;
        the provider observes the correlation and the spine records it here.
        The new alias copies the chain of ``existing_alias`` in one statement,
        so an enrichment can only attach to a chain a delivery receipt already
        anchored, never create one, and redeliveries and replica races
        converge on the same row. ``None`` when the anchor is not recorded.
        """
        a = _ALIASES.c
        anchor = select(
            a.deployment_id,
            literal(str(new_alias)).label("alias"),
            a.conversation_key,
            a.session_id,
            a.interaction_ref,
            a.question_index,
        ).where(a.deployment_id == deployment_id, a.alias == str(existing_alias))
        statement = pg_insert(_ALIASES).from_select(
            ["deployment_id", "alias", "conversation_key", "session_id",
             "interaction_ref", "question_index"],
            anchor,
        )
        return _alias_view(await fetch_one(
            statement.on_conflict_do_update(
                index_elements=[a.deployment_id, a.alias],
                set_={
                    "conversation_key": statement.excluded.conversation_key,
                    "session_id": statement.excluded.session_id,
                    "interaction_ref": func.coalesce(
                        statement.excluded.interaction_ref, a.interaction_ref,
                    ),
                    "question_index": func.coalesce(
                        statement.excluded.question_index, a.question_index,
                    ),
                },
            )
            .returning(_ALIASES)
        ))

    async def record_question_aliases(
        self,
        *,
        deployment_id: str,
        aliases: list[str],
        conversation_key: str | None,
        session_id: str,
        interaction_ref: str,
        question_index: int,
    ) -> None:
        """Make a follow-up question's message a reply target for that question."""
        recorded = list(dict.fromkeys(str(alias) for alias in aliases))
        if not recorded:
            return
        statement = pg_insert(_ALIASES).values([
            {
                "deployment_id": deployment_id,
                "alias": alias,
                "conversation_key": conversation_key,
                "session_id": session_id,
                "interaction_ref": interaction_ref,
                "question_index": int(question_index),
            }
            for alias in recorded
        ])
        await execute(statement.on_conflict_do_update(
            index_elements=[_ALIASES.c.deployment_id, _ALIASES.c.alias],
            set_={
                "conversation_key": statement.excluded.conversation_key,
                "session_id": statement.excluded.session_id,
                "interaction_ref": statement.excluded.interaction_ref,
                "question_index": statement.excluded.question_index,
            },
        ))

    async def resolve_alias(
        self, *, deployment_id: str, alias: str
    ) -> dict[str, Any] | None:
        """The conversation chain a delivered platform alias belongs to."""
        return _alias_view(await fetch_one(select(_ALIASES).where(
            _ALIASES.c.deployment_id == deployment_id, _ALIASES.c.alias == str(alias),
        )))

    # ── questions a channel showed ───────────────────────────────────────

    async def get_channel_interaction(
        self, *, deployment_id: str, ref: str
    ) -> dict[str, Any] | None:
        """One shown question with its answers so far (``answers``, by question)."""
        i, a = _INTERACTIONS.c, _ANSWERS.c
        answers = (
            select(func.coalesce(
                func.jsonb_agg(aggregate_order_by(
                    func.jsonb_build_object(
                        "row", a.answer_row, "choice", a.choice,
                        "participant", a.participant, "question_index", a.question_index,
                    ),
                    a.question_index,
                )),
                text("'[]'::jsonb"),
                type_=JSONB,
            ))
            .where(a.deployment_id == i.deployment_id, a.ref == i.ref)
            .scalar_subquery()
        )
        row = await fetch_one(
            select(_INTERACTIONS, answers.label("answers"))
            .where(i.deployment_id == deployment_id, i.ref == ref)
        )
        if row is None:
            return None
        return {**_times(row, "created_at", "updated_at"), "answers": list(row["answers"] or [])}

    @staticmethod
    def _answer_values(
        deployment_id: str, ref: str, question_index: int, answer: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "deployment_id": deployment_id,
            "ref": ref,
            "question_index": int(question_index),
            "answer_row": dict(answer.get("row") or {}),
            "choice": answer.get("choice"),
            "participant": answer.get("participant"),
        }

    async def record_channel_answer(
        self,
        *,
        deployment_id: str,
        ref: str,
        question_index: int,
        answer: dict[str, Any],
    ) -> bool:
        """Keep one question's answer and move to the next question.

        The move matches the question number, so of two people answering the
        same question one answer is kept and the other returns ``False``.
        """
        i = _INTERACTIONS.c

        async def keep(conn: AsyncConnection) -> bool:
            moved = (await conn.execute(
                update(_INTERACTIONS)
                .where(
                    i.deployment_id == deployment_id,
                    i.ref == ref,
                    i.state == INTERACTION_OPEN,
                    i.question_index == int(question_index),
                )
                .values(question_index=i.question_index + 1, updated_at=func.now())
                .returning(i.ref)
            )).first()
            if moved is None:
                return False
            await conn.execute(pg_insert(_ANSWERS).values(
                **self._answer_values(deployment_id, ref, question_index, answer)
            ))
            return True

        return await transaction(keep)

    async def stage_channel_answer(
        self,
        *,
        deployment_id: str,
        ref: str,
        item_id: str,
        question_index: int,
        answer: dict[str, Any],
    ) -> bool:
        """Claim the last question's answer for one inbound item, before the engine sees it.

        One item wins; a re-drive of that same item finds its own claim
        (``answering_item``).
        """
        i = _INTERACTIONS.c

        async def stage(conn: AsyncConnection) -> bool:
            claimed = (await conn.execute(
                update(_INTERACTIONS)
                .where(
                    i.deployment_id == deployment_id,
                    i.ref == ref,
                    i.state == INTERACTION_OPEN,
                    i.question_index == int(question_index),
                    i.answering_item.is_(None),
                )
                .values(answering_item=item_id, updated_at=func.now())
                .returning(i.ref)
            )).first()
            if claimed is None:
                return False
            await conn.execute(pg_insert(_ANSWERS).values(
                **self._answer_values(deployment_id, ref, question_index, answer)
            ))
            return True

        return await transaction(stage)

    async def settle_channel_interaction(
        self, *, deployment_id: str, ref: str, state: str,
    ) -> bool:
        """End a shown question; only an OPEN one moves."""
        if state not in {INTERACTION_ANSWERED, INTERACTION_CLOSED}:
            raise ValueError(f"not a settled channel interaction state: {state!r}")
        i = _INTERACTIONS.c
        changed = await execute(
            update(_INTERACTIONS)
            .where(i.deployment_id == deployment_id, i.ref == ref, i.state == INTERACTION_OPEN)
            .values(state=state, updated_at=func.now())
        )
        return changed == 1

    # ── participant account links ────────────────────────────────────────

    async def list_account_links(self, deployment_id: str) -> list[dict[str, Any]]:
        """One binding's links, by participant."""
        rows = await fetch_all(
            select(_LINKS)
            .where(_LINKS.c.deployment_id == deployment_id)
            .order_by(_LINKS.c.participant)
        )
        return [_times(row, "linked_at") for row in rows]

    async def get_account_link(
        self, *, deployment_id: str, participant: str,
    ) -> dict[str, Any] | None:
        row = await fetch_one(select(_LINKS).where(
            _LINKS.c.deployment_id == deployment_id, _LINKS.c.participant == participant,
        ))
        return None if row is None else _times(row, "linked_at")

    async def put_account_link(
        self, *, deployment_id: str, participant: str, user_id: str, linked_by: str,
    ) -> dict[str, Any]:
        """Link a participant to an account, replacing any earlier link.

        Messages already admitted keep the account they captured; the link
        decides only messages whose first drive is still ahead.
        """
        statement = pg_insert(_LINKS).values(
            deployment_id=deployment_id,
            participant=participant,
            user_id=user_id,
            linked_by=linked_by,
        )
        row = await fetch_one(
            statement.on_conflict_do_update(
                index_elements=[_LINKS.c.deployment_id, _LINKS.c.participant],
                set_={"user_id": user_id, "linked_by": linked_by, "linked_at": func.now()},
            )
            .returning(_LINKS)
        )
        assert row is not None
        return _times(row, "linked_at")

    async def delete_account_link(self, *, deployment_id: str, participant: str) -> bool:
        removed = await execute(delete(_LINKS).where(
            _LINKS.c.deployment_id == deployment_id, _LINKS.c.participant == participant,
        ))
        return removed == 1
