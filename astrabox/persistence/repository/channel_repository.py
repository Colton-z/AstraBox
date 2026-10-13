"""Channel spine persistence — inbound work items, conversation map, reply outbox.

Durable collections behind the channel ingress spine
(:mod:`astrabox.core.service.orchestrator.channel_ingress_service`), contract
in ``docs/channel-spine.md``:

* ``channel_inbound`` — one durable record per platform message. The document
  is both the at-least-once dedup identity (``_id`` is deterministic per
  (binding, dedup_key)) and the recoverable unit of work: it carries the full
  typed payload, so driving the turn never depends on the source
  redelivering. The ingress classifies an inbound before writing it, so a
  record is created in the state it belongs in::

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
                                      turn drove
                                          ▼
                                       SETTLED                      (terminal)

  OBSERVED and AUTHORIZED records are never driven or recovered: only the
  promotion to RECEIVED, at most once per record, gives one a turn. Every live
  item is held by ``owner_token`` under a monotonic ``generation`` fence with a
  renewable lease. Every transition CAS-matches ``(_id, state, owner_token,
  generation)``: a stale worker — one whose lease expired and was stolen —
  misses its predicate and changes nothing. Failure paths never delete: they
  CAS back to RECEIVED (re-drivable, attempts ``$inc``-ed, lease set to the
  retry watermark) or to DEAD with evidence. An admitted record's payload is
  never replaced: a redelivery that steals or revives it drives the input and
  execution account it was admitted with.
* ``channel_conversations`` — conversation continuity. One doc per
  (binding, configured execution account, conversation_key) mapping to the live session; replaced via CAS
  when the mapped session is terminated. Doubles as the per-conversation
  dispatch lock (owner + TTL + the same monotonic fence).
* ``channel_output_subscriptions`` — one destination and one journal cursor per
  (binding, Session). Delivery intents are durable before the cursor advances.
* ``channel_account_links`` — which AstraBox account a participant of one
  binding runs as when the binding runs as linked accounts. Platform
  administrators write them; the built-in account resolver reads them.
* ``channel_outbox`` — reply delivery, created idempotently per
  (binding, Session, response) and
  bound to the exact response it reports — the deliverer never reads
  "the session's last assistant message". States
  ``PENDING → SENDING → DELIVERED | DEAD`` under a fenced lease; platform
  aliases are persisted on the row before it is marked DELIVERED.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository._compat import DuplicateKeyError, ReturnDocument
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)

logger = get_logger(__name__)

INBOUND_COLLECTION = "channel_inbound"
CONVERSATIONS_COLLECTION = "channel_conversations"
OUTBOX_COLLECTION = "channel_outbox"
ALIASES_COLLECTION = "channel_aliases"
OUTPUT_SUBSCRIPTIONS_COLLECTION = "channel_output_subscriptions"
INTERACTIONS_COLLECTION = "channel_interactions"
ACCOUNT_LINKS_COLLECTION = "channel_account_links"

# A question a channel showed: OPEN while it can still be answered there;
# ANSWERED once the engine accepted the participant's answer; CLOSED when the
# interaction ended any other way (superseded, answered elsewhere, expired).
INTERACTION_OPEN = "OPEN"
INTERACTION_ANSWERED = "ANSWERED"
INTERACTION_CLOSED = "CLOSED"

# Inbound work item states (see module docstring for the machine).
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


#: The drive path's longest bounded stretch between lease writes: the
#: conversation-lock wait plus the session-ready wait (120s each) with real
#: margin. The lease TTL floor must exceed it, or a legitimately-waiting
#: worker's item could be stolen mid-wait — the renew fence before the
#: command append makes that safe but wasteful; the floor keeps it rare.
_INBOUND_LEASE_FLOOR_SECONDS = 300.0


def _inbound_lease_seconds() -> float:
    """Work-item lease TTL.

    Must exceed the max time a live worker goes between renewals — the worker
    renews at drive-start, before the command append, and periodically while
    draining — or a legitimately-running turn's item gets stolen and
    re-driven (safely, but wastefully).
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

OUTBOX_PENDING = "PENDING"
OUTBOX_SENDING = "SENDING"
OUTBOX_DELIVERED = "DELIVERED"
OUTBOX_DEAD = "DEAD"
# The deliverer holds its lease across inline retries (renewed on every failed
# attempt — see record_outbox_failure), so this TTL only has to cover one
# inter-attempt window: the longest single retry backoff plus a worst-case
# delivery attempt, with real margin. If it were <= the longest backoff, the
# lease would expire mid-sleep and a concurrent sweep could double-deliver —
# exactly the race the lease exists to prevent.
_OUTBOX_LEASE_SECONDS = 300.0

# A conversation dispatch lock is held for the duration of one turn; the TTL is
# above the realistic max turn length so a live turn never has its lock stolen,
# but a crashed holder's lock still expires.
_CONVERSATION_LOCK_SECONDS = 900.0


def _scoped_id(prefix: str, deployment_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{deployment_id}::{key}".encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _conversation_id(deployment_id: str, key: str, execution_user_id: str) -> str:
    if not execution_user_id:
        return _scoped_id("conv", deployment_id, key)
    return _scoped_id(
        "conv-executor", deployment_id,
        json.dumps([execution_user_id, key], ensure_ascii=False, separators=(",", ":")),
    )


def new_owner_token() -> str:
    return uuid.uuid4().hex


class ChannelRepository:
    """Work items + conversation map + outbox, all through the collection seam."""

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
        """Durably record one inbound message; return ``(doc, outcome)``.

        This is the ACK point of the spine (channel-spine.md invariant A):
        once the outcome is anything but an exception, the source may
        acknowledge — the payload is persisted, so recovery does not depend
        on redelivery.

        ``state`` is the ingress classification, written with the record so no
        process loss can leave an unaddressed message driveable: RECEIVED (an
        admitted message), AUTHORIZED (an admitted authorization, waiting for
        its observation), OBSERVED (context only), or IGNORED (refused, with
        ``ignored_reason``). ``dedup_key=None`` (a platform without message
        ids) records under a synthetic unique key: every callback is a
        distinct message, but it still gets a recoverable work item.

        A message that already has a record keeps it. Outcomes:

        * ``claimed`` — the caller owns a RECEIVED item and drives it;
        * ``duplicate`` — the message settled (or died, for a signal that
          cannot revive it);
        * ``in_progress`` — a live owner holds it;
        * ``ignored`` — it was refused;
        * ``observed`` / ``authorized`` — it is OBSERVED or AUTHORIZED. The
          caller decides whether this inbound completes it (see
          :meth:`promote_inbound`).

        Only RECEIVED and AUTHORIZED requests admit work, so only they steal an
        expired live item or revive a DEAD one; both keep the payload the
        message was admitted with and the prior evidence on the row.
        """
        if state not in _INBOUND_CLAIM_STATES:
            raise ValueError(f"not an inbound claim state: {state!r}")
        collection = await get_async_collection(INBOUND_COLLECTION)
        now = now if now is not None else time.time()
        key = dedup_key if dedup_key else f"anon:{uuid.uuid4().hex}"
        doc_id = _scoped_id("inbound", deployment_id, key)
        owner = new_owner_token()
        live = state == INBOUND_RECEIVED
        doc = {
            "_id": doc_id,
            "deployment_id": deployment_id,
            "dedup_key": dedup_key,
            "channel_name": channel_name,
            **({"assistant_id": assistant_id} if assistant_id else {"agent_id": agent_id}),
            "payload": dict(payload),
            "state": state,
            "owner_token": owner if live else None,
            "generation": 1,
            "lease_expires_epoch": now + _inbound_lease_seconds() if live else None,
            "session_id": None,
            "command_id": None,
            "turn_id": None,
            "attempts": 0,
            "last_error": None,
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        }
        if state == INBOUND_IGNORED:
            doc["ignored_reason"] = str(ignored_reason or "")[:200]
            if context_excluded:
                doc["context_excluded"] = True
        if state == INBOUND_AUTHORIZED:
            doc["authorization_expires_epoch"] = now + _AUTHORIZATION_WAIT_SECONDS
        try:
            await run_mongo_with_retry(
                "channel.claim_inbound", lambda: collection.insert_one(doc)
            )
            return doc, {
                INBOUND_RECEIVED: CLAIM_OUTCOME_CLAIMED,
                INBOUND_OBSERVED: CLAIM_OUTCOME_OBSERVED,
                INBOUND_AUTHORIZED: CLAIM_OUTCOME_AUTHORIZED,
                INBOUND_IGNORED: CLAIM_OUTCOME_IGNORED,
            }[state]
        except DuplicateKeyError:
            pass

        existing = await collection.find_one({"_id": doc_id})
        if existing is None:  # pragma: no cover - winner's insert just landed
            raise RuntimeError("inbound claim vanished after duplicate-key race")
        current = str(existing.get("state") or "")
        if current == INBOUND_SETTLED:
            return existing, CLAIM_OUTCOME_DUPLICATE
        if current == INBOUND_IGNORED:
            return existing, CLAIM_OUTCOME_IGNORED
        if current == INBOUND_OBSERVED:
            return existing, CLAIM_OUTCOME_OBSERVED
        if current == INBOUND_AUTHORIZED:
            return existing, CLAIM_OUTCOME_AUTHORIZED
        admits = state in (INBOUND_RECEIVED, INBOUND_AUTHORIZED)
        if current == INBOUND_DEAD:
            if not admits:
                return existing, CLAIM_OUTCOME_DUPLICATE
            # The platform redelivered a message whose retries were exhausted:
            # that is a fresh, explicit signal — revive rather than dedupe the
            # message away forever. Prior evidence (attempts/last_error) stays.
            revived = await self._steal_inbound(
                collection,
                existing,
                owner=owner,
                now=now,
                expected_states=(INBOUND_DEAD,),
            )
            if revived is not None:
                return revived, CLAIM_OUTCOME_CLAIMED
            return existing, CLAIM_OUTCOME_IN_PROGRESS

        # Live states: a fresh lease means a live owner; an expired one means
        # a crashed/stalled owner whose item an admitting redelivery may steal.
        if not admits or float(existing.get("lease_expires_epoch") or 0.0) > now:
            return existing, CLAIM_OUTCOME_IN_PROGRESS
        stolen = await self._steal_inbound(
            collection,
            existing,
            owner=owner,
            now=now,
            expected_states=_INBOUND_LIVE_STATES,
        )
        if stolen is not None:
            return stolen, CLAIM_OUTCOME_CLAIMED
        return existing, CLAIM_OUTCOME_IN_PROGRESS

    @staticmethod
    async def _steal_inbound(
        collection: Any,
        existing: dict[str, Any],
        *,
        owner: str,
        now: float,
        expected_states: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """CAS-steal an expired/DEAD item: fence forward, keep the payload.

        The predicate pins the exact ``generation`` that was read, so exactly
        one stealer wins; the prior owner's later writes miss their own
        generation and change nothing. The stolen item returns to RECEIVED
        with the payload it was admitted with: a redelivery is a signal to run
        the accepted message again, and it never replaces that input, its
        context identity, or the execution account captured at admission.
        Command/turn bindings are kept so an attach-not-append re-drive can
        find the prior dispatch.
        """
        prior_generation = int(existing.get("generation") or 0)
        updated = await collection.find_one_and_update(
            {
                "_id": existing["_id"],
                "state": {"$in": list(expected_states)},
                "generation": prior_generation,
            },
            {
                "$set": {
                    "state": INBOUND_RECEIVED,
                    "owner_token": owner,
                    "generation": prior_generation + 1,
                    "lease_expires_epoch": now + _inbound_lease_seconds(),
                    "updated_at": utcnow_iso(),
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        return updated if isinstance(updated, dict) else None

    async def promote_inbound(
        self,
        *,
        item_id: str,
        expected_state: str,
        expected_generation: int,
        payload: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Admit one OBSERVED or AUTHORIZED record: CAS it to an owned RECEIVED.

        ``payload`` is the admitted input, assembled by the ingress from the
        message's first recorded content and the admitting signal. The
        predicate pins the state and generation that were read, so of two
        racing admissions exactly one wins and the record gets at most one
        turn; a record carrying a recall is never admitted. Returns the owned
        item, or ``None`` when the record changed first.
        """
        if expected_state not in _INBOUND_UNADMITTED_STATES:
            raise ValueError(f"not an unadmitted inbound state: {expected_state!r}")
        collection = await get_async_collection(INBOUND_COLLECTION)
        now = now if now is not None else time.time()
        updated = await collection.find_one_and_update(
            {
                "_id": item_id,
                "state": expected_state,
                "generation": int(expected_generation),
                "recall": {"$exists": False},
            },
            {
                "$set": {
                    "state": INBOUND_RECEIVED,
                    "owner_token": new_owner_token(),
                    "generation": int(expected_generation) + 1,
                    "lease_expires_epoch": now + _inbound_lease_seconds(),
                    "payload": dict(payload),
                    "admitted_at": utcnow_iso(),
                    "updated_at": utcnow_iso(),
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        return updated if isinstance(updated, dict) else None

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
        collection = await get_async_collection(INBOUND_COLLECTION)
        updated = await collection.find_one_and_update(
            {
                "_id": item_id,
                "state": INBOUND_AUTHORIZED,
                "generation": int(expected_generation),
            },
            {
                "$set": {
                    "state": INBOUND_OBSERVED,
                    "generation": int(expected_generation) + 1,
                    "payload": dict(payload),
                    "updated_at": utcnow_iso(),
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        return updated if isinstance(updated, dict) else None

    async def refuse_unadmitted_inbound(
        self,
        *,
        item_id: str,
        expected_state: str,
        expected_generation: int,
        reason: str,
    ) -> bool:
        """CAS one OBSERVED or AUTHORIZED record to IGNORED, out of context.

        Used when a later signal for the message contradicts the identity the
        record holds. A refused message never joins retained context.
        """
        if expected_state not in _INBOUND_UNADMITTED_STATES:
            raise ValueError(f"not an unadmitted inbound state: {expected_state!r}")
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": expected_state,
                "generation": int(expected_generation),
            },
            {
                "$set": {
                    "state": INBOUND_IGNORED,
                    "ignored_reason": str(reason)[:200],
                    "context_excluded": True,
                    "updated_at": utcnow_iso(),
                },
            },
        )
        return bool(getattr(result, "modified_count", 0))

    async def expire_authorizations(
        self, *, limit: int = 50, now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Move AUTHORIZED records whose wait has expired to DEAD, with evidence.

        Each transition CAS-matches the generation it read, so replicas
        sweeping together expire a record once. Returns the expired records
        for the caller to report.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        now = now if now is not None else time.time()
        cursor = collection.find({
            "state": INBOUND_AUTHORIZED,
            "authorization_expires_epoch": {"$lte": now},
        }).limit(int(limit))
        expired: list[dict[str, Any]] = []
        for record in [doc async for doc in cursor]:
            updated = await collection.find_one_and_update(
                {
                    "_id": record["_id"],
                    "state": INBOUND_AUTHORIZED,
                    "generation": int(record.get("generation") or 0),
                },
                {
                    "$set": {
                        "state": INBOUND_DEAD,
                        "last_error": "the observation of this authorized message never arrived",
                        "updated_at": utcnow_iso(),
                    },
                },
                return_document=ReturnDocument.AFTER,
            )
            if isinstance(updated, dict):
                expired.append(updated)
        return expired

    async def begin_inbound_dispatch(
        self, *, item_id: str, owner_token: str, generation: int,
        now: float | None = None,
    ) -> bool:
        """CAS the owned item into DISPATCHING with a renewed lease."""
        collection = await get_async_collection(INBOUND_COLLECTION)
        now = now if now is not None else time.time()
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {
                "$set": {
                    "state": INBOUND_DISPATCHING,
                    "lease_expires_epoch": now + _inbound_lease_seconds(),
                    "updated_at": utcnow_iso(),
                }
            },
        )
        return bool(getattr(result, "modified_count", 0))

    async def renew_inbound_lease(
        self, *, item_id: str, owner_token: str, generation: int,
        now: float | None = None,
    ) -> bool:
        """Renew the owned item's lease (drive-start, long-turn heartbeat)."""
        collection = await get_async_collection(INBOUND_COLLECTION)
        now = now if now is not None else time.time()
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {
                "$set": {
                    "lease_expires_epoch": now + _inbound_lease_seconds(),
                    "updated_at": utcnow_iso(),
                }
            },
        )
        return bool(getattr(result, "modified_count", 0))

    async def bind_inbound_dispatch(
        self,
        *,
        item_id: str,
        owner_token: str,
        generation: int,
        session_id: str | None = None,
        command_id: str | None = None,
        turn_id: str | None = None,
        conversation_key: str | None = None,
    ) -> bool:
        """Record dispatch bindings (session, then command/turn) on the owned item.

        ``conversation_key`` is the conversation the session was resolved
        for. A re-drive reuses the bound session without resolving it again,
        so it reads the key from here to take the same conversation lock.

        The command/turn binding is evidence, not the idempotency mechanism —
        the kernel's command journal keyed by ``client_message_id`` is the
        ledger a re-drive attaches through (channel-spine.md, "attach, never
        re-append"). Binding here lets the outbox and operators see the exact
        turn without a journal query.
        """
        updates: dict[str, Any] = {"updated_at": utcnow_iso()}
        if session_id is not None:
            updates["session_id"] = session_id
        if command_id is not None:
            updates["command_id"] = command_id
        if turn_id is not None:
            updates["turn_id"] = turn_id
        if conversation_key is not None:
            updates["resolved_conversation_key"] = conversation_key
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {"$set": updates},
        )
        return bool(getattr(result, "modified_count", 0))

    async def capture_inbound_account(
        self, *, item_id: str, owner_token: str, generation: int, user_id: str,
    ) -> dict[str, Any] | None:
        """Keep the account a linked binding's message runs as; return the payload.

        Written once, under the owner fence and only while no account is
        kept, so recovery, a steal or a redelivery runs the message as the
        account its first drive resolved. ``None`` when this worker does not
        own the item.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        updated = await collection.find_one_and_update(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": owner_token,
                "generation": int(generation),
                "payload.execution_user_id": "",
            },
            {
                "$set": {
                    "payload.creator_user_id": user_id,
                    "payload.execution_user_id": user_id,
                    "updated_at": utcnow_iso(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return dict(updated["payload"]) if isinstance(updated, dict) else None

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
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {"$set": {
                "answer_ref": interaction_ref,
                "answer_question_index": int(question_index),
                "updated_at": utcnow_iso(),
            }},
        )
        return bool(getattr(result, "modified_count", 0))

    async def topic_has_participation(
        self, *, deployment_id: str, conversation: str, binding_revision: str,
    ) -> bool:
        """Whether the agent already answered a message of this topic and revision.

        A settled message is one the agent took part in. Participation, like
        retained context, belongs to one binding revision: editing the binding
        starts the topic afresh.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        found = await collection.find_one({
            "deployment_id": deployment_id,
            "payload.conversation": conversation,
            "payload.binding_revision": binding_revision,
            "state": INBOUND_SETTLED,
        })
        return found is not None

    async def record_inbound_judgment(
        self, *, item_id: str, owner_token: str, generation: int,
        judgment: dict[str, Any],
    ) -> bool:
        """CAS the owned item's response judgment, before anything acts on it.

        A re-drive after a host failure reads the recorded judgment instead of
        asking the judge again, so one message gets one decision.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": INBOUND_DISPATCHING,
                "owner_token": owner_token,
                "generation": int(generation),
                "judgment": {"$exists": False},
            },
            {"$set": {"judgment": dict(judgment), "updated_at": utcnow_iso()}},
        )
        return bool(getattr(result, "modified_count", 0))

    async def suppress_judged_inbound(
        self, *, item_id: str, owner_token: str, generation: int,
    ) -> dict[str, Any] | None:
        """CAS DISPATCHING → OBSERVED for a message the judge declined.

        The message stays retained context. A copy of it that addressed the
        agent while it was being judged marked it explicitly attended; such a
        record is never suppressed, so the explicit signal is not lost.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        updated = await collection.find_one_and_update(
            {
                "_id": item_id,
                "state": INBOUND_DISPATCHING,
                "owner_token": owner_token,
                "generation": int(generation),
                "explicit_attention": {"$ne": True},
            },
            {"$set": {
                "state": INBOUND_OBSERVED,
                "owner_token": None,
                "lease_expires_epoch": None,
                "generation": int(generation) + 1,
                "updated_at": utcnow_iso(),
            }},
            return_document=ReturnDocument.AFTER,
        )
        return updated if isinstance(updated, dict) else None

    async def note_explicit_attention(self, *, item_id: str) -> bool:
        """Mark a message under judgment as addressed to the agent by another copy."""
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "payload.selection": "judge",
            },
            {"$set": {"explicit_attention": True, "updated_at": utcnow_iso()}},
        )
        return bool(getattr(result, "modified_count", 0))

    async def settle_inbound(
        self, *, item_id: str, owner_token: str, generation: int, session_id: str
    ) -> bool:
        """CAS DISPATCHING → SETTLED (the turn drove to a terminal frame)."""
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": INBOUND_DISPATCHING,
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {
                "$set": {
                    "state": INBOUND_SETTLED,
                    "session_id": session_id,
                    "settled_at": utcnow_iso(),
                    "updated_at": utcnow_iso(),
                }
            },
        )
        return bool(getattr(result, "modified_count", 0))

    async def mark_inbound_ignored(
        self, *, item_id: str, owner_token: str, generation: int, reason: str,
        exclude_from_context: bool = False,
    ) -> bool:
        """CAS a live item → IGNORED: a terminal, ACK-safe refusal.

        A refusal can be decided at admission (RECEIVED) or when a recovered
        message is driven again (DISPATCHING), always before its turn starts.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {
                "$set": {
                    "state": INBOUND_IGNORED,
                    "ignored_reason": str(reason)[:200],
                    "updated_at": utcnow_iso(),
                    **({"context_excluded": True} if exclude_from_context else {}),
                }
            },
        )
        return bool(getattr(result, "modified_count", 0))

    async def fail_inbound(
        self,
        *,
        item_id: str,
        owner_token: str,
        generation: int,
        error: str,
        retry_at_epoch: float | None,
    ) -> bool:
        """Record a failed drive on the owned item.

        ``retry_at_epoch`` set → back to RECEIVED with the lease expiring at
        that instant (the reconciler's backoff watermark — nobody hot-loops a
        failing item). ``None`` → DEAD with evidence; only an explicit
        platform redelivery revives it. Attempts bump atomically (``$inc``) —
        never read-modify-write.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        updates: dict[str, Any] = {
            "last_error": str(error)[:500],
            "updated_at": utcnow_iso(),
        }
        if retry_at_epoch is None:
            updates["state"] = INBOUND_DEAD
        else:
            updates["state"] = INBOUND_RECEIVED
            updates["lease_expires_epoch"] = float(retry_at_epoch)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {"$set": updates, "$inc": {"attempts": 1}},
        )
        return bool(getattr(result, "modified_count", 0))

    async def release_inbound_lease(
        self, *, item_id: str, owner_token: str, generation: int
    ) -> bool:
        """Hand back cancelled work without failing or forgetting its dispatch.

        The successor claims the expired lease through the normal generation
        fence. Keep the attempt counter: it is part of the command identity.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {"$set": {"lease_expires_epoch": time.time(), "updated_at": utcnow_iso()}},
        )
        return bool(getattr(result, "modified_count", 0))

    async def list_recoverable_inbound(
        self, *, limit: int = 50, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Reconciler read: live items whose lease has expired.

        The CAS gate is :meth:`claim_inbound_for_recovery`; this read never
        transfers ownership.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        now = now if now is not None else time.time()
        cursor = collection.find(
            {
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "lease_expires_epoch": {"$lte": now},
            }
        ).limit(int(limit))
        return [doc async for doc in cursor]

    async def claim_inbound_for_recovery(
        self, *, item_id: str, expected_generation: int, now: float | None = None
    ) -> dict[str, Any] | None:
        """CAS-take ownership of an expired live item (reconciler re-drive).

        Fences forward (``generation + 1``) atop the exact generation the
        sweep read and re-verifies expiry inside the predicate, so a live
        renewal between read and claim wins and this returns ``None``. The
        state is preserved: DISPATCHING tells the new owner a command may
        already exist (attach path); RECEIVED means dispatch never began.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        now = now if now is not None else time.time()
        updated = await collection.find_one_and_update(
            {
                "_id": item_id,
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "generation": int(expected_generation),
                "lease_expires_epoch": {"$lte": now},
            },
            {
                "$set": {
                    "owner_token": new_owner_token(),
                    "generation": int(expected_generation) + 1,
                    "lease_expires_epoch": now + _inbound_lease_seconds(),
                    "updated_at": utcnow_iso(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return updated if isinstance(updated, dict) else None

    async def get_inbound(self, item_id: str) -> dict[str, Any] | None:
        collection = await get_async_collection(INBOUND_COLLECTION)
        return await collection.find_one({"_id": item_id})

    async def get_message_record(
        self, *, deployment_id: str, dedup_key: str,
    ) -> dict[str, Any] | None:
        """The record of one platform message, by the provider's dedup key."""
        return await self.get_inbound(_scoped_id("inbound", deployment_id, dedup_key))

    async def record_recall(
        self, *, deployment_id: str, conversation_key: str,
        event_id: str, message_id: str, timestamp: str,
    ) -> bool:
        """Record external deletion without rewriting accepted Agent input.

        A recalled message that has not been admitted never runs: an admitting
        signal that arrives after the recall is refused (see
        :meth:`promote_inbound`). An AUTHORIZED record may not know its
        conversation yet — its observation carries that — so only a record
        that holds a conversation key is checked against the recall's.
        """
        if not all((deployment_id, conversation_key, event_id, message_id, timestamp)):
            raise ValueError("recall event identity is incomplete")
        if event_id == message_id:
            raise ValueError("recall event and target must have different identities")
        collection = await get_async_collection(INBOUND_COLLECTION)
        target_id = _scoped_id("inbound", deployment_id, message_id)
        target = await collection.find_one({"_id": target_id})
        if target is None:
            return False
        target_key = (target.get("payload") or {}).get("conversation_key")
        awaiting_content = target.get("state") == INBOUND_AUTHORIZED and not target_key
        if not awaiting_content and target_key != conversation_key:
            raise RuntimeError("recall target conversation identity changed")
        recall = {"event_id": event_id, "timestamp": timestamp}
        existing = target.get("recall")
        if existing is not None:
            if existing != recall:
                raise RuntimeError(f"conflicting recall payload for message {message_id!r}")
            return True
        predicate: dict[str, Any] = {"_id": target_id, "recall": {"$exists": False}}
        if not awaiting_content:
            predicate["payload.conversation_key"] = conversation_key
        result = await run_mongo_with_retry(
            "channel.record_recall",
            lambda: collection.update_one(predicate, {"$set": {"recall": recall}}),
        )
        if result.matched_count:
            return True
        raced = await collection.find_one({"_id": target_id})
        if (raced or {}).get("recall") != recall:
            raise RuntimeError(f"conflicting raced recall payload for message {message_id!r}")
        return True

    async def collect_unsubmitted_context(
        self, item: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Read ordinary context in the current binding revision and time cutoff.

        Context is the unsubmitted messages of the conversation the item
        continues, no later than the item's own message. It does not depend on
        the account a message runs as: what was said in a room is context for
        whoever the agent answers there.
        """
        payload = item["payload"]
        if not payload.get("retain_context"):
            return []
        if not all(payload.get(key) for key in (
            "conversation", "binding_revision", "message_timestamp",
        )):
            raise RuntimeError("channel context ordering identity is incomplete")
        collection = await get_async_collection(INBOUND_COLLECTION)
        cursor = collection.find({
            "_id": {"$ne": item["_id"]},
            "deployment_id": item["deployment_id"],
            # The conversation the message continues: its conversation_key, or
            # the conversation of the message it replies to when its provider
            # groups messages by reply chain.
            "payload.conversation": payload["conversation"],
            "payload.binding_revision": payload["binding_revision"],
            "payload.retain_context": True,
            "payload.provider_ignore_reason": None,
            "payload.message_timestamp": {"$lte": payload["message_timestamp"]},
            "context_submitted": {"$ne": True},
            "context_excluded": {"$ne": True},
        })
        context = []
        async for record in cursor:
            retained = record["payload"]
            entry = {
                "item_id": record["_id"],
                "message_id": record["dedup_key"],
                "timestamp": retained["message_timestamp"],
                "participant": retained["participant"],
                # What the participant wrote: the binding's prompt prefix
                # belongs to the turn's own input, once, not to every message
                # quoted as context.
                "content": retained["reply_text"],
            }
            if isinstance(record.get("recall"), dict):
                entry["recall"] = dict(record["recall"])
                # The accepted task must not retain the recalled text through
                # a secondary context field after rendering its marker.
                entry["content"] = f"[该消息已于 {record['recall']['timestamp']} 被撤回]"
            context.append(entry)
        context.sort(key=lambda entry: (entry["timestamp"], entry["message_id"]))
        return context

    async def freeze_inbound_input(
        self, *, item_id: str, owner_token: str, generation: int,
        content: str, context: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Freeze one accepted input under the existing dispatch owner fence."""
        collection = await get_async_collection(INBOUND_COLLECTION)
        query = {"_id": item_id, "state": INBOUND_DISPATCHING,
                 "owner_token": owner_token, "generation": generation}
        frozen = {"content": content, "context": context}
        updated = await collection.find_one_and_update(
            {**query, "frozen_input": {"$exists": False}},
            {"$set": {"frozen_input": frozen}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            updated = await collection.find_one(query)
        return updated.get("frozen_input") if isinstance(updated, dict) else None

    async def exclude_inbound_from_context(self, item: dict[str, Any]) -> None:
        """Keep an answer to a question out of later turns' conversation context.

        Only this item moves: earlier retained messages it did not consume stay
        pending for the next turn.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item["_id"],
                "state": {"$in": list(_INBOUND_LIVE_STATES)},
                "owner_token": item["owner_token"],
                "generation": int(item["generation"]),
            },
            {"$set": {"context_excluded": True, "updated_at": utcnow_iso()}},
        )
        if not result.matched_count:
            raise RuntimeError("channel answer lost dispatch ownership before leaving the context")

    async def mark_context_submitted(self, item: dict[str, Any]) -> None:
        """Mark exactly the frozen message set after its original turn settles."""
        if not await self.renew_inbound_lease(
            item_id=item["_id"], owner_token=item["owner_token"],
            generation=item["generation"],
        ):
            raise RuntimeError("channel context submission lost dispatch ownership")
        frozen = item.get("frozen_input")
        if not isinstance(frozen, dict) or not isinstance(frozen.get("context"), list):
            raise RuntimeError("retained channel input has no frozen context")
        context = frozen["context"]
        ids = [entry["item_id"] for entry in context] + [item["_id"]]
        collection = await get_async_collection(INBOUND_COLLECTION)
        for item_id in dict.fromkeys(ids):
            result = await collection.update_one(
                {"_id": item_id, "deployment_id": item["deployment_id"]},
                {"$set": {"context_submitted": True}},
            )
            if not result.matched_count:
                raise RuntimeError(f"cannot mark missing channel context {item_id!r} submitted")

    # ── conversation continuity ──────────────────────────────────────────

    async def get_conversation(
        self, *, deployment_id: str, conversation_key: str, execution_user_id: str = "",
    ) -> dict[str, Any] | None:
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        return await collection.find_one(
            {"_id": _conversation_id(deployment_id, conversation_key, execution_user_id)}
        )

    async def upsert_conversation(
        self,
        *,
        deployment_id: str,
        conversation_key: str,
        session_id: str,
        agent_id: str = "",
        assistant_id: str = "",
        replaces_session_id: str | None = None,
        execution_user_id: str = "",
    ) -> dict[str, Any] | None:
        """Map the key to ``session_id``.

        First mapping inserts; a replacement (terminated session swapped for a
        fresh one) is CAS-guarded on the session it replaces so two racing
        callbacks cannot both replace and orphan a session.
        """
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        doc_id = _conversation_id(deployment_id, conversation_key, execution_user_id)
        now = utcnow_iso()
        if replaces_session_id is None:
            doc = {
                "_id": doc_id,
                "deployment_id": deployment_id,
                "conversation_key": conversation_key,
                "session_id": session_id,
                **({"assistant_id": assistant_id} if assistant_id else {"agent_id": agent_id}),
                "execution_user_id": execution_user_id,
                "created_at": now,
                "last_inbound_at": now,
            }
            try:
                await run_mongo_with_retry(
                    "channel.insert_conversation", lambda: collection.insert_one(doc)
                )
                return doc
            except DuplicateKeyError:
                return await collection.find_one({"_id": doc_id})
        return await collection.find_one_and_update(
            {"_id": doc_id, "session_id": replaces_session_id},
            {"$set": {"session_id": session_id, "last_inbound_at": now}},
            return_document=ReturnDocument.AFTER,
        )

    async def touch_conversation(
        self, *, deployment_id: str, conversation_key: str, execution_user_id: str = "",
    ) -> None:
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        await collection.update_one(
            {"_id": _conversation_id(deployment_id, conversation_key, execution_user_id)},
            {"$set": {"last_inbound_at": utcnow_iso()}},
        )

    # ── per-conversation dispatch lock ───────────────────────────────────

    async def acquire_conversation_lock(
        self,
        *,
        deployment_id: str,
        conversation_key: str,
        owner: str,
        execution_user_id: str = "",
        now: float | None = None,
        ttl_seconds: float = _CONVERSATION_LOCK_SECONDS,
    ) -> int | None:
        """CAS-acquire the conversation's dispatch lock.

        Returns the lock's monotonic generation on success, ``None`` on
        contention. Serializes turns within one conversation so two
        concurrent inbound messages never dispatch overlapping turns on the
        same session. Acquires when the lock is free or its TTL has expired
        (a crashed holder); every acquisition ``$inc``s ``lock_generation``,
        and release CAS-matches ``(owner, generation)`` — a crashed-then-
        revived holder cannot release the successor's lock.
        """
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        now = now if now is not None else time.time()
        result = await collection.find_one_and_update(
            {
                "_id": _conversation_id(deployment_id, conversation_key, execution_user_id),
                "$or": [
                    {"lock_owner": None},
                    {"lock_owner": {"$exists": False}},
                    {"lock_expires_epoch": {"$lte": now}},
                ],
            },
            {
                "$set": {"lock_owner": owner, "lock_expires_epoch": now + ttl_seconds},
                "$inc": {"lock_generation": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if isinstance(result, dict) and result.get("lock_owner") == owner:
            return int(result.get("lock_generation") or 0)
        return None

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
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        await collection.update_one(
            {
                "_id": _conversation_id(deployment_id, conversation_key, execution_user_id),
                "lock_owner": owner,
                "lock_generation": int(generation),
            },
            {"$set": {"lock_owner": None, "lock_expires_epoch": None}},
        )

    # ── reply outbox ─────────────────────────────────────────────────────

    async def subscribe_to_output(
        self, *, session_id: str, deployment_id: str, channel_name: str,
        reply_context: dict[str, Any], conversation_key: str | None,
        after_seq: int, streaming: bool,
    ) -> None:
        """Bind a destination for the Session lifetime without replaying history."""
        collection = await get_async_collection(OUTPUT_SUBSCRIPTIONS_COLLECTION)
        await collection.update_one(
            {"_id": _scoped_id("output", deployment_id, session_id)},
            {"$setOnInsert": {
                "session_id": session_id, "deployment_id": deployment_id,
                "channel_name": channel_name, "reply_context": dict(reply_context),
                "conversation_key": conversation_key, "streaming": streaming,
                "after_seq": after_seq,
            }}, upsert=True,
        )

    async def list_output_subscriptions(
        self, *, after_id: str = "", limit: int = 50,
    ) -> list[dict[str, Any]]:
        collection = await get_async_collection(OUTPUT_SUBSCRIPTIONS_COLLECTION)
        return [row async for row in collection.find({"_id": {"$gt": after_id}}).sort("_id", 1).limit(limit)]

    async def advance_output_subscription(
        self, subscription_id: str, *, expected_seq: int, sequence: int,
    ) -> bool:
        collection = await get_async_collection(OUTPUT_SUBSCRIPTIONS_COLLECTION)
        result = await collection.update_one(
            {"_id": subscription_id, "after_seq": expected_seq}, {"$set": {"after_seq": sequence}},
        )
        return bool(result.modified_count)

    async def remove_output_subscription(self, subscription_id: str) -> None:
        collection = await get_async_collection(OUTPUT_SUBSCRIPTIONS_COLLECTION)
        await collection.delete_one({"_id": subscription_id})

    async def create_outbox_entry(
        self,
        *,
        deployment_id: str,
        channel_name: str,
        session_id: str,
        turn_id: str,
        response_seq: int,
        response_id: str,
        start_after_seq: int,
        reply_context: dict[str, Any],
        conversation_key: str | None = None,
        streaming: bool = False,
    ) -> dict[str, Any]:
        """Idempotently create the turn's delivery row.

        Identity is the destination binding, Session and response. Input
        dispatch and engine-owned output share this delivery path. Replaying
        the Session cursor finds the same row and retains its delivery state.
        """
        collection = await get_async_collection(OUTBOX_COLLECTION)
        doc = {
            "_id": _scoped_id("outbox", deployment_id, json.dumps([session_id, response_id])),
            "deployment_id": deployment_id,
            "channel_name": channel_name,
            "session_id": session_id,
            "turn_id": turn_id,
            "response_seq": response_seq,
            "response_id": response_id,
            "start_after_seq": start_after_seq,
            "conversation_key": conversation_key,
            "reply_context": dict(reply_context),
            "streaming": bool(streaming),
            "frame_cursor": -1,
            "state": OUTBOX_PENDING,
            "lease_owner": None,
            "lease_generation": 0,
            "lease_expires_epoch": None,
            "attempts": 0,
            "last_error": None,
            "delivery_aliases": [],
            "next_attempt_at": utcnow_iso(),
            "created_at": utcnow_iso(),
        }
        try:
            await run_mongo_with_retry(
                "channel.create_outbox", lambda: collection.insert_one(doc)
            )
            return doc
        except DuplicateKeyError:
            existing = await collection.find_one({"_id": doc["_id"]})
            if existing is None:  # pragma: no cover - concurrent delete only
                raise RuntimeError("outbox row vanished after duplicate-key race")
            return existing

    async def renew_outbox_lease(
        self, outbox_id: str, *, owner: str, generation: int,
        now: float | None = None, ttl_seconds: float = _OUTBOX_LEASE_SECONDS,
    ) -> bool:
        """Heartbeat for a long-lived (streaming) delivery session."""
        collection = await get_async_collection(OUTBOX_COLLECTION)
        now = now if now is not None else time.time()
        result = await collection.update_one(
            {
                "_id": outbox_id,
                "state": OUTBOX_SENDING,
                "lease_owner": owner,
                "lease_generation": int(generation),
            },
            {"$set": {"lease_expires_epoch": now + ttl_seconds}},
        )
        return bool(getattr(result, "modified_count", 0))

    async def advance_outbox_cursor(
        self, outbox_id: str, *, owner: str, generation: int, frame_seq: int
    ) -> bool:
        """Record durable projection progress (monotonic, fenced).

        The ``$lt`` predicate keeps the cursor monotonic without ``$max``
        (which the portable operator subset does not include): a stale or
        out-of-order write simply misses.
        """
        collection = await get_async_collection(OUTBOX_COLLECTION)
        result = await collection.update_one(
            {
                "_id": outbox_id,
                "state": OUTBOX_SENDING,
                "lease_owner": owner,
                "lease_generation": int(generation),
                "frame_cursor": {"$lt": int(frame_seq)},
            },
            {"$set": {"frame_cursor": int(frame_seq), "updated_at": utcnow_iso()}},
        )
        return bool(getattr(result, "modified_count", 0))

    async def claim_outbox_for_delivery(
        self, outbox_id: str, *, owner: str, now: float | None = None,
        ttl_seconds: float = _OUTBOX_LEASE_SECONDS,
    ) -> int | None:
        """CAS-lease a PENDING (or lease-expired SENDING) row for delivery.

        Returns the fenced lease generation when this caller now owns the
        lease, ``None`` otherwise. Two replicas sweeping the same row: only
        one leases it. Every acquisition ``$inc``s ``lease_generation``; all
        settlement writes CAS on ``(owner, generation)`` so a deliverer whose
        lease expired mid-flight cannot complete or fail the row once a
        successor owns it.
        """
        collection = await get_async_collection(OUTBOX_COLLECTION)
        now = now if now is not None else time.time()
        entry = await collection.find_one({"_id": outbox_id})
        if entry is None:
            return None
        earlier = await collection.find_one({
            "deployment_id": entry["deployment_id"], "session_id": entry["session_id"],
            "response_seq": {"$lt": entry["response_seq"]},
            "state": {"$in": [OUTBOX_PENDING, OUTBOX_SENDING]},
        })
        if earlier is not None:
            return None
        result = await collection.find_one_and_update(
            {
                "_id": outbox_id,
                "$or": [
                    {"state": OUTBOX_PENDING},
                    {"state": OUTBOX_SENDING, "lease_expires_epoch": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "state": OUTBOX_SENDING,
                    "lease_owner": owner,
                    "lease_expires_epoch": now + ttl_seconds,
                },
                "$inc": {"lease_generation": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if isinstance(result, dict) and result.get("lease_owner") == owner:
            return int(result.get("lease_generation") or 0)
        return None

    async def record_delivery_aliases(
        self,
        outbox_id: str,
        *,
        owner: str,
        generation: int,
        aliases: list[str],
        deployment_id: str | None = None,
        conversation_key: str | None = None,
        session_id: str | None = None,
        interaction_ref: str | None = None,
    ) -> bool:
        """Persist platform message aliases before the row is marked DELIVERED.

        The crash-after-create window: a deliverer that created the platform
        message but died before settling leaves its aliases here; the
        successor sees them as idempotency evidence (update-instead-of-create).

        When the chain fields are supplied, each alias also gets a resolvable
        document in ``channel_aliases`` — the reply-chain identity target: an
        inbound ``reference`` to a delivered alias resumes that alias's
        conversation (docs/channel-spine.md).
        """
        collection = await get_async_collection(OUTBOX_COLLECTION)
        result = await collection.update_one(
            {
                "_id": outbox_id,
                "state": OUTBOX_SENDING,
                "lease_owner": owner,
                "lease_generation": int(generation),
            },
            {
                "$set": {
                    "delivery_aliases": [str(a) for a in aliases],
                    "updated_at": utcnow_iso(),
                }
            },
        )
        recorded = bool(getattr(result, "modified_count", 0))
        if recorded and deployment_id:
            alias_collection = await get_async_collection(ALIASES_COLLECTION)
            for alias in aliases:
                alias_doc = {
                    "deployment_id": deployment_id,
                    "alias": str(alias),
                    "conversation_key": conversation_key,
                    "session_id": session_id,
                    "outbox_id": outbox_id,
                    "created_at": utcnow_iso(),
                }
                if interaction_ref:
                    # A reply to this message answers the question it shows.
                    alias_doc["interaction_ref"] = interaction_ref
                    alias_doc["question_index"] = 0
                await alias_collection.update_one(
                    {"_id": _scoped_id("alias", deployment_id, str(alias))},
                    {"$set": alias_doc},
                    upsert=True,
                )
        return recorded

    async def link_alias(
        self, *, deployment_id: str, existing_alias: str, new_alias: str
    ) -> dict[str, Any] | None:
        """Attach a late-materialized platform id to an existing chain.

        Post-delivery alias enrichment (docs/channel-spine.md): some
        platforms mint the quotable message id only after the delivery ack;
        the provider observes the correlation and the spine records it here.
        Resolves ``existing_alias`` first — an enrichment can only attach to
        a chain a delivery receipt already anchored, never create one. The
        write is a deterministic-id upsert: redeliveries and replica races
        converge on the same document.
        """
        anchor = await self.resolve_alias(
            deployment_id=deployment_id, alias=existing_alias
        )
        if anchor is None:
            return None
        collection = await get_async_collection(ALIASES_COLLECTION)
        doc = {
            "deployment_id": deployment_id,
            "alias": str(new_alias),
            "conversation_key": anchor.get("conversation_key"),
            "session_id": anchor.get("session_id"),
            "outbox_id": anchor.get("outbox_id"),
            **{
                key: anchor[key]
                for key in ("interaction_ref", "question_index")
                if key in anchor
            },
            "linked_from": str(existing_alias),
            "created_at": utcnow_iso(),
        }
        await collection.update_one(
            {"_id": _scoped_id("alias", deployment_id, str(new_alias))},
            {"$set": doc},
            upsert=True,
        )
        return doc

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
        collection = await get_async_collection(ALIASES_COLLECTION)
        for alias in aliases:
            await collection.update_one(
                {"_id": _scoped_id("alias", deployment_id, str(alias))},
                {"$set": {
                    "deployment_id": deployment_id,
                    "alias": str(alias),
                    "conversation_key": conversation_key,
                    "session_id": session_id,
                    "outbox_id": None,
                    "interaction_ref": interaction_ref,
                    "question_index": int(question_index),
                    "created_at": utcnow_iso(),
                }},
                upsert=True,
            )

    async def open_channel_interaction(
        self,
        *,
        deployment_id: str,
        ref: str,
        session_id: str,
        interaction_id: str,
        conversation_key: str | None,
        channel_name: str,
        reply_context: dict[str, Any],
        view: dict[str, Any],
    ) -> dict[str, Any]:
        """Record that a channel showed a question; idempotent per question.

        Keyed by the question's ref, so a redelivered message converges on the
        record its first delivery created, answers included.
        """
        collection = await get_async_collection(INTERACTIONS_COLLECTION)
        now = utcnow_iso()
        await collection.update_one(
            {"_id": _scoped_id("interaction", deployment_id, ref)},
            {"$setOnInsert": {
                "deployment_id": deployment_id,
                "ref": ref,
                "session_id": session_id,
                "interaction_id": interaction_id,
                "conversation_key": conversation_key,
                "channel_name": channel_name,
                "reply_context": dict(reply_context),
                # The question as it was shown, so a reply that arrives after
                # it closed can still be recognised as an answer to it.
                "view": dict(view),
                "state": INTERACTION_OPEN,
                "question_index": 0,
                "answers": [],
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )
        record = await self.get_channel_interaction(deployment_id=deployment_id, ref=ref)
        if record is None:
            raise RuntimeError(f"channel interaction {ref} vanished after it was recorded")
        return record

    async def get_channel_interaction(
        self, *, deployment_id: str, ref: str
    ) -> dict[str, Any] | None:
        collection = await get_async_collection(INTERACTIONS_COLLECTION)
        return await collection.find_one({"_id": _scoped_id("interaction", deployment_id, ref)})

    async def record_channel_answer(
        self,
        *,
        deployment_id: str,
        ref: str,
        question_index: int,
        answers: list[dict[str, Any]],
    ) -> bool:
        """Keep the answers so far and move to the next question.

        ``answers`` is every answer up to and including this question's; the
        compare on the question number makes the whole-list write safe.
        """
        collection = await get_async_collection(INTERACTIONS_COLLECTION)
        result = await collection.update_one(
            {
                "_id": _scoped_id("interaction", deployment_id, ref),
                "state": INTERACTION_OPEN,
                "question_index": int(question_index),
            },
            {
                "$inc": {"question_index": 1},
                "$set": {"answers": list(answers), "updated_at": utcnow_iso()},
            },
        )
        return bool(getattr(result, "modified_count", 0))

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

        One item wins; a re-drive of that same item finds its own claim.
        """
        collection = await get_async_collection(INTERACTIONS_COLLECTION)
        result = await collection.update_one(
            {
                "_id": _scoped_id("interaction", deployment_id, ref),
                "state": INTERACTION_OPEN,
                "question_index": int(question_index),
                "answering_item": {"$exists": False},
            },
            {"$set": {
                "answering_item": item_id,
                "staged_answer": answer,
                "updated_at": utcnow_iso(),
            }},
        )
        return bool(getattr(result, "modified_count", 0))

    async def settle_channel_interaction(
        self,
        *,
        deployment_id: str,
        ref: str,
        state: str,
        participant: str | None = None,
    ) -> bool:
        """End a shown question; only an OPEN one moves."""
        if state not in {INTERACTION_ANSWERED, INTERACTION_CLOSED}:
            raise ValueError(f"not a settled channel interaction state: {state!r}")
        collection = await get_async_collection(INTERACTIONS_COLLECTION)
        result = await collection.update_one(
            {"_id": _scoped_id("interaction", deployment_id, ref), "state": INTERACTION_OPEN},
            {"$set": {"state": state, "settled_by": participant, "updated_at": utcnow_iso()}},
        )
        return bool(getattr(result, "modified_count", 0))

    async def resolve_alias(
        self, *, deployment_id: str, alias: str
    ) -> dict[str, Any] | None:
        """The conversation chain a delivered platform alias belongs to."""
        collection = await get_async_collection(ALIASES_COLLECTION)
        return await collection.find_one(
            {"_id": _scoped_id("alias", deployment_id, str(alias))}
        )

    async def mark_outbox_delivered(
        self, outbox_id: str, *, owner: str, generation: int
    ) -> bool:
        """CAS SENDING → DELIVERED for the exact fenced lease holder."""
        collection = await get_async_collection(OUTBOX_COLLECTION)
        result = await collection.update_one(
            {
                "_id": outbox_id,
                "state": OUTBOX_SENDING,
                "lease_owner": owner,
                "lease_generation": int(generation),
            },
            {
                "$set": {
                    "state": OUTBOX_DELIVERED,
                    "delivered_at": utcnow_iso(),
                    "lease_owner": None,
                    "lease_expires_epoch": None,
                }
            },
        )
        return bool(getattr(result, "modified_count", 0))

    async def record_outbox_failure(
        self, outbox_id: str, *, owner: str, generation: int,
        error: str, next_attempt_at: str | None, now: float | None = None,
    ) -> bool:
        """Bump attempts atomically for the exact fenced lease holder.

        ``next_attempt_at=None`` marks DEAD (lease cleared); otherwise the row
        stays SENDING with the lease renewed so the inline retry loop keeps
        exclusive ownership across attempts — a second replica cannot
        re-deliver between attempts (it would have to wait out the renewed
        lease, which only expires if this deliverer crashes, at which point
        the sweep legitimately reclaims it).
        """
        collection = await get_async_collection(OUTBOX_COLLECTION)
        now = now if now is not None else time.time()
        updates: dict[str, Any] = {"last_error": str(error)[:500]}
        if next_attempt_at is None:
            updates["state"] = OUTBOX_DEAD
            updates["next_attempt_at"] = None
            updates["lease_owner"] = None
            updates["lease_expires_epoch"] = None
        else:
            # Retry: hold the lease, renew its expiry, stay SENDING.
            updates["state"] = OUTBOX_SENDING
            updates["next_attempt_at"] = next_attempt_at
            updates["lease_expires_epoch"] = now + _OUTBOX_LEASE_SECONDS
        result = await collection.update_one(
            {
                "_id": outbox_id,
                "state": OUTBOX_SENDING,
                "lease_owner": owner,
                "lease_generation": int(generation),
            },
            {"$set": updates, "$inc": {"attempts": 1}},
        )
        return bool(getattr(result, "modified_count", 0))

    async def list_pending_outbox(
        self, *, limit: int = 50, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Sweep read: PENDING rows plus SENDING rows whose lease has expired
        (a crashed deliverer), bounded. The sweep must CAS-lease each row via
        :meth:`claim_outbox_for_delivery` before delivering it."""
        collection = await get_async_collection(OUTBOX_COLLECTION)
        now = now if now is not None else time.time()
        cursor = collection.find(
            {
                "$or": [
                    {"state": OUTBOX_PENDING},
                    {"state": OUTBOX_SENDING, "lease_expires_epoch": {"$lte": now}},
                ]
            }
        ).limit(int(limit))
        return [doc async for doc in cursor]

    async def get_outbox_entry(self, outbox_id: str) -> dict[str, Any] | None:
        collection = await get_async_collection(OUTBOX_COLLECTION)
        return await collection.find_one({"_id": outbox_id})

    # ── participant account links ────────────────────────────────────────

    async def list_account_links(self, deployment_id: str) -> list[dict[str, Any]]:
        """One binding's links, by participant."""
        collection = await get_async_collection(ACCOUNT_LINKS_COLLECTION)
        rows = [row async for row in collection.find({"deployment_id": deployment_id})]
        return sorted(rows, key=lambda row: str(row.get("participant") or ""))

    async def get_account_link(
        self, *, deployment_id: str, participant: str,
    ) -> dict[str, Any] | None:
        collection = await get_async_collection(ACCOUNT_LINKS_COLLECTION)
        return await collection.find_one(
            {"_id": _scoped_id("link", deployment_id, participant)}
        )

    async def put_account_link(
        self, *, deployment_id: str, participant: str, user_id: str, linked_by: str,
    ) -> dict[str, Any]:
        """Link a participant to an account, replacing any earlier link.

        Messages already admitted keep the account they captured; the link
        decides only messages whose first drive is still ahead.
        """
        collection = await get_async_collection(ACCOUNT_LINKS_COLLECTION)
        link_id = _scoped_id("link", deployment_id, participant)
        fields = {
            "deployment_id": deployment_id,
            "participant": participant,
            "user_id": user_id,
            "linked_by": linked_by,
            "linked_at": utcnow_iso(),
        }
        await run_mongo_with_retry(
            "channel.put_account_link",
            lambda: collection.update_one({"_id": link_id}, {"$set": fields}, upsert=True),
        )
        return {"_id": link_id, **fields}

    async def delete_account_link(self, *, deployment_id: str, participant: str) -> bool:
        collection = await get_async_collection(ACCOUNT_LINKS_COLLECTION)
        result = await collection.delete_one(
            {"_id": _scoped_id("link", deployment_id, participant)}
        )
        return bool(getattr(result, "deleted_count", 0))

    async def delete_account_links(self, deployment_id: str) -> None:
        """Remove every link of a deleted binding."""
        collection = await get_async_collection(ACCOUNT_LINKS_COLLECTION)
        await collection.delete_many({"deployment_id": deployment_id})
