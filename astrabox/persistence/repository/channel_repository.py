"""Channel spine persistence — inbound work items, conversation map, reply outbox.

Three collections behind the channel ingress spine
(:mod:`astrabox.core.service.orchestrator.channel_ingress_service`), contract
in ``docs/channel-spine.md``:

* ``channel_inbound`` — one durable work item per accepted inbound message.
  The document is both the at-least-once dedup identity (``_id`` is
  deterministic per (binding, dedup_key)) and the recoverable unit of work:
  it carries the full typed payload, so driving the turn never depends on the
  source redelivering. States::

      (absent) ─claim/steal─► RECEIVED ─policy─► IGNORED   (terminal)
                                 │
                          begin_dispatch
                                 ▼
                            DISPATCHING ─exhausted─► DEAD  (terminal)
                                 │
                             turn drove
                                 ▼
                              SETTLED                      (terminal)

  Every non-terminal item is held by ``owner_token`` under a monotonic
  ``generation`` fence with a renewable lease. Every transition CAS-matches
  ``(_id, state, owner_token, generation)``: a stale worker — one whose lease
  expired and was stolen — misses its predicate and changes nothing. Failure
  paths never delete: they CAS back to RECEIVED (re-drivable, attempts
  ``$inc``-ed, lease set to the retry watermark) or to DEAD with evidence.
* ``channel_conversations`` — conversation continuity. One doc per
  (binding, conversation_key) mapping to the live session; replaced via CAS
  when the mapped session is terminated. Doubles as the per-conversation
  dispatch lock (owner + TTL + the same monotonic fence).
* ``channel_outbox`` — reply delivery, one row per SETTLED turn, created
  idempotently under a deterministic ``_id`` per (work item, command) and
  bound to the exact command/turn it reports — the deliverer never reads
  "the session's last assistant message". States
  ``PENDING → SENDING → DELIVERED | DEAD`` under a fenced lease; platform
  aliases are persisted on the row before it is marked DELIVERED.
"""

from __future__ import annotations

import hashlib
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

# Inbound work item states (see module docstring for the machine).
INBOUND_RECEIVED = "RECEIVED"
INBOUND_DISPATCHING = "DISPATCHING"
INBOUND_SETTLED = "SETTLED"
INBOUND_IGNORED = "IGNORED"
INBOUND_DEAD = "DEAD"

_INBOUND_LIVE_STATES = (INBOUND_RECEIVED, INBOUND_DISPATCHING)


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
CLAIM_OUTCOME_IGNORED = "ignored"          # attention policy classified it away

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
        agent_id: str,
        payload: dict[str, Any],
        now: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Durably claim one inbound message; return ``(doc, outcome)``.

        This is the ACK point of the spine (channel-spine.md invariant A):
        once the outcome is anything but an exception, the source may
        acknowledge — the payload is persisted, so recovery does not depend
        on redelivery.

        ``dedup_key=None`` (a platform without message ids) claims under a
        synthetic unique key: every callback is a distinct message, but it
        still gets a recoverable work item.

        Outcomes: ``claimed`` (caller owns the item — drive it),
        ``duplicate`` (SETTLED — ack the winner's session), ``ignored``
        (terminally classified), ``in_progress`` (a live owner holds it).
        A DEAD item explicitly redelivered by the platform is revived —
        stolen back to RECEIVED with the fresh payload; its prior evidence
        stays on the row.
        """
        collection = await get_async_collection(INBOUND_COLLECTION)
        now = now if now is not None else time.time()
        key = dedup_key if dedup_key else f"anon:{uuid.uuid4().hex}"
        doc_id = _scoped_id("inbound", deployment_id, key)
        owner = new_owner_token()
        doc = {
            "_id": doc_id,
            "deployment_id": deployment_id,
            "dedup_key": dedup_key,
            "channel_name": channel_name,
            "agent_id": agent_id,
            "payload": dict(payload),
            "state": INBOUND_RECEIVED,
            "owner_token": owner,
            "generation": 1,
            "lease_expires_epoch": now + _inbound_lease_seconds(),
            "session_id": None,
            "command_id": None,
            "turn_id": None,
            "attempts": 0,
            "last_error": None,
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        }
        try:
            await run_mongo_with_retry(
                "channel.claim_inbound", lambda: collection.insert_one(doc)
            )
            return doc, CLAIM_OUTCOME_CLAIMED
        except DuplicateKeyError:
            pass

        existing = await collection.find_one({"_id": doc_id})
        if existing is None:  # pragma: no cover - winner's insert just landed
            raise RuntimeError("inbound claim vanished after duplicate-key race")
        state = str(existing.get("state") or "")
        if state == INBOUND_SETTLED:
            return existing, CLAIM_OUTCOME_DUPLICATE
        if state == INBOUND_IGNORED:
            return existing, CLAIM_OUTCOME_IGNORED
        if state == INBOUND_DEAD:
            # The platform redelivered a message whose retries were exhausted:
            # that is a fresh, explicit signal — revive rather than dedupe the
            # message away forever. Prior evidence (attempts/last_error) stays.
            revived = await self._steal_inbound(
                collection,
                existing,
                owner=owner,
                payload=payload,
                now=now,
                expected_states=(INBOUND_DEAD,),
            )
            if revived is not None:
                return revived, CLAIM_OUTCOME_CLAIMED
            return existing, CLAIM_OUTCOME_IN_PROGRESS

        # Live states: a fresh lease means a live owner; an expired one means
        # a crashed/stalled owner whose item the redelivery may steal.
        if float(existing.get("lease_expires_epoch") or 0.0) > now:
            return existing, CLAIM_OUTCOME_IN_PROGRESS
        stolen = await self._steal_inbound(
            collection,
            existing,
            owner=owner,
            payload=payload,
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
        payload: dict[str, Any],
        now: float,
        expected_states: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """CAS-steal an expired/DEAD item: fence forward, refresh the payload.

        The predicate pins the exact ``generation`` that was read, so exactly
        one stealer wins; the prior owner's later writes miss their own
        generation and change nothing. The stolen item returns to RECEIVED
        with the redelivered payload — command/turn bindings are kept so an
        attach-not-append re-drive can find the prior dispatch.
        """
        prior_generation = int(existing.get("generation") or 0)
        retained_payload = dict(existing.get("payload") or {})
        if retained_payload.get("retain_context"):
            # The original context identity and input belong to the accepted
            # message, not to a later transport redelivery or binding revision.
            payload = retained_payload
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
                    "payload": dict(payload),
                    "updated_at": utcnow_iso(),
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        return updated if isinstance(updated, dict) else None

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
    ) -> bool:
        """Record dispatch bindings (session, then command/turn) on the owned item.

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
        self, *, item_id: str, owner_token: str, generation: int, reason: str
    ) -> bool:
        """CAS RECEIVED → IGNORED: a terminal, ACK-safe policy classification."""
        collection = await get_async_collection(INBOUND_COLLECTION)
        result = await collection.update_one(
            {
                "_id": item_id,
                "state": INBOUND_RECEIVED,
                "owner_token": owner_token,
                "generation": int(generation),
            },
            {
                "$set": {
                    "state": INBOUND_IGNORED,
                    "ignored_reason": str(reason)[:200],
                    "updated_at": utcnow_iso(),
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

    async def record_recall(
        self, *, deployment_id: str, conversation_key: str,
        event_id: str, message_id: str, timestamp: str,
    ) -> bool:
        """Record external deletion without rewriting accepted Agent input."""
        if not all((deployment_id, conversation_key, event_id, message_id, timestamp)):
            raise ValueError("recall event identity is incomplete")
        if event_id == message_id:
            raise ValueError("recall event and target must have different identities")
        collection = await get_async_collection(INBOUND_COLLECTION)
        target_id = _scoped_id("inbound", deployment_id, message_id)
        target = await collection.find_one({"_id": target_id})
        if target is None:
            return False
        if (target.get("payload") or {}).get("conversation_key") != conversation_key:
            raise RuntimeError("recall target conversation identity changed")
        recall = {"event_id": event_id, "timestamp": timestamp}
        existing = target.get("recall")
        if existing is not None:
            if existing != recall:
                raise RuntimeError(f"conflicting recall payload for message {message_id!r}")
            return True
        result = await run_mongo_with_retry(
            "channel.record_recall",
            lambda: collection.update_one(
                {"_id": target_id, "payload.conversation_key": conversation_key,
                 "recall": {"$exists": False}},
                {"$set": {"recall": recall}},
            ),
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
        """Read ordinary context in the current binding revision and time cutoff."""
        payload = item["payload"]
        if not payload.get("retain_context"):
            return []
        if not all(payload.get(key) for key in (
            "conversation_key", "binding_revision", "message_timestamp",
        )):
            raise RuntimeError("channel context ordering identity is incomplete")
        collection = await get_async_collection(INBOUND_COLLECTION)
        cursor = collection.find({
            "_id": {"$ne": item["_id"]},
            "deployment_id": item["deployment_id"],
            "payload.conversation_key": payload["conversation_key"],
            "payload.binding_revision": payload["binding_revision"],
            "payload.retain_context": True,
            "payload.provider_ignore_reason": None,
            "payload.message_timestamp": {"$lte": payload["message_timestamp"]},
            "context_submitted": {"$ne": True},
        })
        context = []
        async for record in cursor:
            retained = record["payload"]
            entry = {
                "item_id": record["_id"],
                "message_id": record["dedup_key"],
                "timestamp": retained["message_timestamp"],
                "participant": retained["participant"],
                "content": retained["content"],
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
        self, *, deployment_id: str, conversation_key: str
    ) -> dict[str, Any] | None:
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        return await collection.find_one(
            {"_id": _scoped_id("conv", deployment_id, conversation_key)}
        )

    async def upsert_conversation(
        self,
        *,
        deployment_id: str,
        conversation_key: str,
        session_id: str,
        agent_id: str,
        replaces_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Map the key to ``session_id``.

        First mapping inserts; a replacement (terminated session swapped for a
        fresh one) is CAS-guarded on the session it replaces so two racing
        callbacks cannot both replace and orphan a session.
        """
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        doc_id = _scoped_id("conv", deployment_id, conversation_key)
        now = utcnow_iso()
        if replaces_session_id is None:
            doc = {
                "_id": doc_id,
                "deployment_id": deployment_id,
                "conversation_key": conversation_key,
                "session_id": session_id,
                "agent_id": agent_id,
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
        self, *, deployment_id: str, conversation_key: str
    ) -> None:
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        await collection.update_one(
            {"_id": _scoped_id("conv", deployment_id, conversation_key)},
            {"$set": {"last_inbound_at": utcnow_iso()}},
        )

    # ── per-conversation dispatch lock ───────────────────────────────────

    async def acquire_conversation_lock(
        self,
        *,
        deployment_id: str,
        conversation_key: str,
        owner: str,
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
                "_id": _scoped_id("conv", deployment_id, conversation_key),
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
        generation: int,
    ) -> None:
        """Release iff still held by ``owner`` at ``generation`` (never steal)."""
        collection = await get_async_collection(CONVERSATIONS_COLLECTION)
        await collection.update_one(
            {
                "_id": _scoped_id("conv", deployment_id, conversation_key),
                "lock_owner": owner,
                "lock_generation": int(generation),
            },
            {"$set": {"lock_owner": None, "lock_expires_epoch": None}},
        )

    # ── reply outbox ─────────────────────────────────────────────────────

    async def create_outbox_entry(
        self,
        *,
        work_item_id: str,
        deployment_id: str,
        channel_name: str,
        session_id: str,
        command_id: str,
        turn_id: str,
        reply_context: dict[str, Any],
        conversation_key: str | None = None,
        streaming: bool = False,
    ) -> dict[str, Any]:
        """Idempotently create the turn's delivery row.

        The ``_id`` is deterministic per (work item, command): a re-driven
        settle after a crash re-creates the same row and finds the prior
        delivery state instead of double-sending. The row is born bound to
        the exact command/turn it reports (channel-spine.md invariant C).
        Simple deliveries create it at settle time; a streaming delivery
        creates it at dispatch-bind time (``streaming=True``) so the session
        can tail the turn's durable frames live, resuming from
        ``frame_cursor`` after a crash.
        """
        collection = await get_async_collection(OUTBOX_COLLECTION)
        doc = {
            "_id": _scoped_id("outbox", work_item_id, command_id),
            "work_item_id": work_item_id,
            "deployment_id": deployment_id,
            "channel_name": channel_name,
            "session_id": session_id,
            "command_id": command_id,
            "turn_id": turn_id,
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
            "linked_from": str(existing_alias),
            "created_at": utcnow_iso(),
        }
        await collection.update_one(
            {"_id": _scoped_id("alias", deployment_id, str(new_alias))},
            {"$set": doc},
            upsert=True,
        )
        return doc

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
