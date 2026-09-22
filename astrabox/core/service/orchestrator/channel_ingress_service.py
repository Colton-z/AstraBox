"""Channel ingress spine — one typed boundary from inbound message to reply.

Contract: ``docs/channel-spine.md``. This service owns everything between a
provider-authenticated inbound message and the delivered reply:

* :meth:`ChannelIngressService.ingest` — the single post-auth ingress
  (invariant E). The HTTP webhook trigger and any trusted broker source both
  call it with a resolved binding + :class:`~astrabox.seams.channel.ChannelInbound`;
  nobody fabricates HTTP and no provider touches spine internals.
* The durable inbound work item (invariant A): the full typed payload is
  persisted before the caller may acknowledge the source. Driving the turn
  never depends on the platform redelivering.
* Fenced ownership (invariant B): items, conversation locks, and delivery
  leases are held by ``owner_token`` + monotonic ``generation``; a stale
  worker's writes miss their CAS predicates and change nothing.
* Attach-not-append dispatch: the kernel's command journal — keyed by a
  per-drive-attempt ``client_message_id`` token — is the idempotency ledger.
  A re-drive (crash recovery, steal) first looks for its token's command and
  attaches to it instead of appending a second one: at-least-once ingress,
  at-most-once turns.
* Turn-bound delivery (invariant C): the outbox row is created at settle
  time, bound to the exact ``command_id``/``turn_id``, and the deliverer
  reads the assistant text of that exact turn — never the session's latest.

The channel reconciler (:mod:`channel_spine_reconciler`) re-drives expired
items and re-delivers abandoned outbox rows through the same methods.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.channel_credentials import (
    ChannelCredentialService,
)
from astrabox.core.service.orchestrator.channel_context import channel_turn_content
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_FORM,
)
from astrabox.persistence.repository.channel_repository import (
    CLAIM_OUTCOME_CLAIMED,
    CLAIM_OUTCOME_DUPLICATE,
    CLAIM_OUTCOME_IGNORED,
    INBOUND_DEAD,
    INBOUND_RECEIVED,
    INBOUND_SETTLED,
    ChannelRepository,
    new_owner_token,
)
from astrabox.seams.channel import (
    EVENT_FAILED,
    EVENT_PROGRESS,
    EVENT_SETTLED,
    EVENT_TURN_STARTED,
    ChannelDeliveryReceipt,
    ChannelEvent,
    ChannelInbound,
    channel_scene_name,
    get_channel,
)

logger = get_logger(__name__)

# How long to wait for a freshly-created session's sandbox to become READY
# before sending the message. Channel ingress is non-interactive: there is no
# external client to retry, so the drive worker polls. For a continued
# conversation the same poll doubles as the per-conversation serial queue.
_SESSION_READY_TIMEOUT_SECONDS = 120
_SESSION_READY_POLL_SECONDS = 2.0

#: Re-drive backoff per recorded failure; after the last entry the item is
#: DEAD (durable evidence, revivable only by an explicit redelivery).
_INBOUND_RETRY_BACKOFF_SECONDS = (10.0, 60.0, 300.0, 900.0)

#: Capped inline retry schedule for reply delivery (seconds between attempts).
#: After the last failure the outbox row is marked DEAD (visible to
#: operators) rather than retried forever.
_OUTBOX_RETRY_BACKOFF_SECONDS = (5.0, 30.0, 120.0)

#: Streaming delivery session pacing: how often the deliverer reads new
#: durable frames / re-checks the work item, and its overall budget. A hung
#: provider stalls only its own session (invariant D); the budget bounds a
#: turn that never terminates before the outbox retry policy takes over.
_STREAMING_POLL_SECONDS = 1.0
_STREAMING_MAX_SECONDS = 1800.0

#: Reply-chain resolution window: an inbound referencing a platform alias that
#: is still being delivered (alias write racing) gets this many short waits
#: before it legitimately starts a new chain.
_REFERENCE_RESOLVE_ATTEMPTS = 3
_REFERENCE_RESOLVE_WAIT_SECONDS = 0.5

#: Binding attention policies (docs/channel-spine.md). ``all`` responds to
#: every inbound; ``mentions`` requires a typed attention signal.
ATTENTION_POLICY_ALL = "all"
ATTENTION_POLICY_MENTIONS = "mentions"
VALID_ATTENTION_POLICIES = (ATTENTION_POLICY_ALL, ATTENTION_POLICY_MENTIONS)


@dataclass
class ChannelIngressReceipt:
    """What ``ingest`` durably promised the source.

    ``accepted`` — the work item is owned and will drive a turn;
    ``duplicate`` — this message already settled (idempotent redelivery ack);
    ``in_progress`` — a live owner is driving it;
    ``ignored`` — the provider or attention policy terminally classified it.
    Whatever the status, the source may acknowledge upstream: the outcome is
    durable (invariant A).
    """

    status: str
    item_id: str
    session_id: str | None = None
    ack_extra: dict[str, Any] = field(default_factory=dict)


def _dispatch_token(item: dict[str, Any]) -> str:
    """The turn-idempotency token for one drive attempt.

    Versioned by ``attempts``: a crash-recovered re-drive of the same attempt
    finds the same command (attach, no double turn), while a retry after a
    recorded failure uses a fresh token and legitimately starts a new turn.
    """
    return f"{item['_id']}::a{int(item.get('attempts') or 0)}"


class ChannelIngressService:
    """The channel spine between provider auth and platform reply."""

    def __init__(
        self,
        *,
        deployment_repo: Any,
        agent_repo: Any,
        agent_service_getter: Any,
        stream_message_events_ds: Any,
        resume_command_stream: Any,
        sessions_repo: Any,
        session_events_repo: Any,
        message_view: Any,
        session_detail_getter: Any,
        supersede_pending_interaction: Any,
        spawn_background_task: Any,
        channel_repo: ChannelRepository | None = None,
        channel_credentials: ChannelCredentialService | None = None,
    ) -> None:
        self._deployment_repo = deployment_repo
        self._agent_repo = agent_repo
        self._agent_service_getter = agent_service_getter
        self._stream_message_events_ds = stream_message_events_ds
        self._resume_command_stream = resume_command_stream
        self._sessions_repo = sessions_repo
        self._session_events_repo = session_events_repo
        self._message_view = message_view
        self._session_detail_getter = session_detail_getter
        self._supersede_pending_interaction = supersede_pending_interaction
        self._spawn_background_task = spawn_background_task
        self._channel_repo = channel_repo or ChannelRepository()
        self._channel_credentials = channel_credentials or ChannelCredentialService()

    # ── ingress (invariants A + E) ───────────────────────────────────────

    async def ingest(
        self, deployment_id: str, inbound: ChannelInbound
    ) -> ChannelIngressReceipt:
        """The single post-auth typed ingress for every channel source."""
        binding = await self._deployment_repo.get_by_id(deployment_id)
        if not binding or binding.get("deleted") or binding.get("enabled") is False:
            raise APIError(
                code="NOT_FOUND",
                message="channel binding not found or disabled",
                status_code=404,
            )
        return await self.ingest_resolved(binding, inbound)

    async def ingest_resolved(
        self, binding: dict[str, Any], inbound: ChannelInbound
    ) -> ChannelIngressReceipt:
        """`ingest` for a caller that already loaded the binding row."""
        deployment_id = str(binding.get("deployment_id") or "")
        channel_name = channel_scene_name(str(binding.get("scene") or ""))
        if channel_name is None:
            raise APIError(
                code="NOT_A_CHANNEL",
                message="binding is not a channel scene",
                status_code=409,
            )
        if inbound.recall is not None:
            applied = await self._channel_repo.record_recall(
                deployment_id=deployment_id,
                conversation_key=str(inbound.conversation_key or ""),
                event_id=str(inbound.dedup_key or ""),
                message_id=inbound.recall.message_id,
                timestamp=inbound.recall.timestamp,
            )
            return ChannelIngressReceipt(
                status="recalled" if applied else "ignored",
                item_id="",
                ack_extra=dict(inbound.ack_extra),
            )
        if inbound.alias_link is not None:
            # Post-delivery alias enrichment (docs/channel-spine.md): not a
            # message — no work item, no turn. The echo can race the delivery
            # receipt's alias write, so an unmatched anchor gets the same
            # bounded wait as reference resolution; a source host nacks
            # ``enrich_unmatched`` so the broker redelivers once the anchor
            # is durable.
            anchor_alias = str(inbound.alias_link.existing_alias or "")
            new_alias = str(inbound.alias_link.new_alias or "")
            linked: dict[str, Any] | None = None
            if anchor_alias and new_alias:
                for attempt in range(_REFERENCE_RESOLVE_ATTEMPTS):
                    linked = await self._channel_repo.link_alias(
                        deployment_id=deployment_id,
                        existing_alias=anchor_alias,
                        new_alias=new_alias,
                    )
                    if linked is not None:
                        break
                    if attempt + 1 < _REFERENCE_RESOLVE_ATTEMPTS:
                        await asyncio.sleep(_REFERENCE_RESOLVE_WAIT_SECONDS)
            return ChannelIngressReceipt(
                status="enriched" if linked is not None else "enrich_unmatched",
                item_id="",
                session_id=str((linked or {}).get("session_id") or "") or None,
                ack_extra=dict(inbound.ack_extra),
            )
        agent_id = str(binding.get("agent_id") or "").strip()
        agent = await self._agent_repo.get_agent(agent_id)
        if not agent:
            raise APIError(
                code="NOT_FOUND", message="bound agent not found", status_code=404
            )
        creator_user_id = str(agent.get("user_id") or "").strip()
        if not creator_user_id:
            raise APIError(
                code="DEPLOYMENT_NO_OWNER",
                message="bound agent has no creator to run as",
                status_code=409,
            )

        content = self._apply_prompt_prefix(binding, inbound.content)
        if inbound.retain_context and not all((
            inbound.dedup_key, inbound.conversation_key,
            inbound.message_timestamp, inbound.participant, binding.get("updated_at"),
        )):
            raise RuntimeError("retained channel message lacks ordering identity")
        payload = {
            "content": content,
            "conversation_key": inbound.conversation_key,
            "reply_context": dict(inbound.reply_context or {}) or None,
            "attention": inbound.attention.to_dict() if inbound.attention else None,
            "reference": inbound.reference,
            "participant": inbound.participant,
            "provider_ignore_reason": inbound.ignore_reason,
            "creator_user_id": creator_user_id,
            "retain_context": inbound.retain_context,
            "message_timestamp": inbound.message_timestamp,
            "binding_revision": binding.get("updated_at"),
        }
        item, outcome = await self._channel_repo.claim_inbound(
            deployment_id=deployment_id,
            dedup_key=inbound.dedup_key,
            channel_name=channel_name,
            agent_id=agent_id,
            payload=payload,
        )
        if outcome != CLAIM_OUTCOME_CLAIMED:
            logger.info(
                "channel ingest suppressed: binding=%s outcome=%s dedup_key=%s",
                deployment_id, outcome, inbound.dedup_key,
            )
            return ChannelIngressReceipt(
                status=outcome,
                item_id=str(item["_id"]),
                session_id=str(item.get("session_id") or "") or None,
                ack_extra=dict(inbound.ack_extra),
            )

        # Attention policy (core-owned, deterministic): an ignore is a durable
        # terminal classification, not a dropped message.
        ignore_reason = self._attention_ignore_reason(binding, inbound)
        if ignore_reason is not None:
            await self._channel_repo.mark_inbound_ignored(
                item_id=str(item["_id"]),
                owner_token=str(item["owner_token"]),
                generation=int(item["generation"]),
                reason=ignore_reason,
            )
            return ChannelIngressReceipt(
                status=CLAIM_OUTCOME_IGNORED,
                item_id=str(item["_id"]),
                ack_extra=dict(inbound.ack_extra),
            )

        # Resolve the target session inline so the ack carries it (and so
        # redeliveries get a session in their in_progress/duplicate acks).
        # Idempotent under crash: the session binds onto the item via CAS and
        # a re-drive reuses it.
        session_id = await self._resolve_item_session(item)
        self._spawn_background_task(
            self._drive_work_item(item),
            name=f"channel-drive-{item['_id']}",
        )
        logger.warning(
            "channel inbound accepted: binding=%s agent=%s session=%s item=%s",
            deployment_id, agent_id, session_id, item["_id"],
        )
        return ChannelIngressReceipt(
            status="accepted",
            item_id=str(item["_id"]),
            session_id=session_id or None,
            ack_extra=dict(inbound.ack_extra),
        )

    @staticmethod
    def _apply_prompt_prefix(binding: dict[str, Any], content: str) -> str:
        prefix = str(binding.get("prompt_prefix") or "").strip()
        if prefix:
            return f"{prefix}\n\n{content}"
        return content

    @staticmethod
    def _attention_ignore_reason(
        binding: dict[str, Any], inbound: ChannelInbound
    ) -> str | None:
        """Deterministic respond/ignore policy from typed signals.

        ``all`` (default) responds to everything. ``mentions`` requires the
        provider to have reported at least one attention signal — the Claude
        Tag "a mention starts the thread" behavior. Providers signal; core
        decides.
        """
        provider_reason = str(inbound.ignore_reason or "").strip()
        if provider_reason:
            return provider_reason
        policy = str(binding.get("attention_policy") or ATTENTION_POLICY_ALL).strip().lower()
        if policy != ATTENTION_POLICY_MENTIONS:
            return None
        if inbound.attention is not None and inbound.attention.any_signal:
            return None
        return "attention_policy=mentions with no attention signal"

    # ── dispatch (invariant B + attach-not-append) ───────────────────────

    async def _resolve_item_session(self, item: dict[str, Any]) -> str:
        """Get-or-create the item's target session and CAS-bind it.

        A bound session is reused verbatim on re-drive — a crash between
        session creation and binding at worst orphans one empty session,
        never runs a second turn. Reply-chain identity: an inbound that only
        carries a ``reference`` to a platform alias the spine delivered
        resumes that alias's conversation; an unknown reference starts a new
        chain after the bounded resolution window (docs/channel-spine.md).
        """
        bound = str(item.get("session_id") or "")
        if bound:
            return bound
        payload = dict(item.get("payload") or {})
        user = UserContext(user_id=str(payload.get("creator_user_id") or ""))
        agent_service = self._agent_service_getter()
        agent_id = str(item.get("agent_id") or "")
        deployment_id = str(item.get("deployment_id") or "")
        conversation_key = str(payload.get("conversation_key") or "")
        referenced_session = ""
        if not conversation_key and str(payload.get("reference") or ""):
            resolved = await self._resolve_reference(
                deployment_id, str(payload["reference"])
            )
            if resolved is not None:
                conversation_key = str(resolved.get("conversation_key") or "")
                referenced_session = str(resolved.get("session_id") or "")
        if conversation_key:
            session_id = await self._resolve_conversation_session(
                user=user,
                agent_service=agent_service,
                deployment_id=deployment_id,
                agent_id=agent_id,
                conversation_key=conversation_key,
            )
        elif referenced_session:
            # The referenced delivery was a one-shot session: continue it.
            session_id = referenced_session
        else:
            started = await agent_service.start_conversation(user, agent_id)
            session_id = str(started.get("session_id") or "")
        if session_id:
            await self._channel_repo.bind_inbound_dispatch(
                item_id=str(item["_id"]),
                owner_token=str(item["owner_token"]),
                generation=int(item["generation"]),
                session_id=session_id,
            )
            item["session_id"] = session_id
            if conversation_key:
                item["resolved_conversation_key"] = conversation_key
        return session_id

    async def _resolve_reference(
        self, deployment_id: str, reference: str
    ) -> dict[str, Any] | None:
        """Bounded alias lookup: a reply may race the alias write of the very
        delivery it quotes — wait briefly, then let a new chain start."""
        for attempt in range(_REFERENCE_RESOLVE_ATTEMPTS):
            resolved = await self._channel_repo.resolve_alias(
                deployment_id=deployment_id, alias=reference
            )
            if resolved is not None:
                return resolved
            if attempt + 1 < _REFERENCE_RESOLVE_ATTEMPTS:
                await asyncio.sleep(_REFERENCE_RESOLVE_WAIT_SECONDS)
        return None

    async def _drive_work_item(self, item: dict[str, Any]) -> None:
        """Drive one owned work item; cancellation hands ownership back and raises.

        The invariant-bearing order: begin dispatch (fence check) → conversation
        lock → session ready → attach-or-dispatch the turn → settle (fence
        check — a stolen item stops at that check and lets the successor
        deliver) → turn-bound outbox → delivery.
        """
        item_id = str(item["_id"])
        owner = str(item["owner_token"])
        generation = int(item["generation"])
        payload = dict(item.get("payload") or {})
        deployment_id = str(item.get("deployment_id") or "")
        conversation_key: str | None = None
        lock_generation: int | None = None
        try:
            if not await self._channel_repo.begin_inbound_dispatch(
                item_id=item_id, owner_token=owner, generation=generation
            ):
                return  # lost ownership before starting — successor owns it
            session_id = await self._resolve_item_session(item)
            if not session_id:
                await self._fail_item(item, "conversation start returned no session")
                return
            # Serialize on the effective conversation — a reply-chain
            # reference resolves into its alias's conversation, so the lock
            # key comes from resolution, not just the raw payload.
            conversation_key = (
                str(item.get("resolved_conversation_key") or "")
                or str(payload.get("conversation_key") or "")
            ) or None
            if conversation_key is not None:
                lock_generation = await self._acquire_conversation_lock_blocking(
                    deployment_id, conversation_key, owner
                )
                if lock_generation is None:
                    await self._fail_item(item, "conversation lock unavailable")
                    return
            if not await self._wait_session_ready(session_id):
                await self._fail_item(
                    item,
                    f"session {session_id} not ready in {_SESSION_READY_TIMEOUT_SECONDS}s",
                )
                return

            reply_context = payload.get("reply_context")
            has_reply = isinstance(reply_context, dict) and bool(reply_context)
            wants_streaming = has_reply and self._provider_streams(
                str(item.get("channel_name") or "")
            )
            token = _dispatch_token(item)
            command = await self._session_events_repo.find_command_by_client_message_id(
                session_id, client_message_id=token
            )
            drove = False
            streaming_started = False
            if command is None:
                # Fence the interaction side effect too: a worker that lost
                # ownership while waiting must not answer this session's UI.
                if not await self._channel_repo.renew_inbound_lease(
                    item_id=item_id, owner_token=owner, generation=generation
                ):
                    return
                user = UserContext(user_id=str(payload.get("creator_user_id") or ""))
                await self._decline_superseded_channel_question(
                    user=user,
                    session_id=session_id,
                )
                # The last fence before appending a command: a renew that
                # misses means the claim was stolen mid-wait — the new owner
                # drives this item, and appending here would run a second turn
                # for one inbound. A successful renew holds the lease for a
                # full TTL, so no steal can occur between this write and the
                # append.
                if not await self._channel_repo.renew_inbound_lease(
                    item_id=item_id, owner_token=owner, generation=generation
                ):
                    return
                content = str(payload.get("content") or "")
                if payload.get("retain_context"):
                    frozen = item.get("frozen_input")
                    if frozen is None:
                        context = await self._channel_repo.collect_unsubmitted_context(item)
                        frozen = await self._channel_repo.freeze_inbound_input(
                            item_id=item_id, owner_token=owner, generation=generation,
                            content=channel_turn_content(str(payload["content"]), context),
                            context=context,
                        )
                    if frozen is None:
                        return
                    if not isinstance(frozen, dict) or not isinstance(frozen.get("content"), str):
                        raise RuntimeError("retained channel input has no frozen content")
                    item["frozen_input"] = frozen
                    content = frozen["content"]
                agen = self._stream_message_events_ds(
                    user,
                    session_id,
                    content,
                    client_message_id=token,
                )
                drove = True
                drain_task = asyncio.ensure_future(
                    self._drain_with_lease_renewal(agen, item)
                )
                if wants_streaming:
                    # Bind as soon as the kernel accepts the command so the
                    # delivery session can tail the live turn's durable frames.
                    command = await self._await_command(
                        session_id, token, drain_task
                    )
                    if command is not None:
                        streaming_started = await self._start_streaming_delivery(
                            item, command, dict(reply_context or {}),
                            conversation_key,
                        )
                ok = await drain_task
                if not ok:
                    await self._fail_item(item, "turn stream failed")
                    return
                if command is None:
                    command = await self._session_events_repo.find_command_by_client_message_id(
                        session_id, client_message_id=token
                    )
                if command is None:
                    await self._fail_item(item, "turn drove but no command recorded")
                    return
            command_id = str(command.get("causation_id") or "")
            turn_id = str(command.get("turn_id") or "")
            await self._channel_repo.bind_inbound_dispatch(
                item_id=item_id,
                owner_token=owner,
                generation=generation,
                command_id=command_id,
                turn_id=turn_id,
            )
            if wants_streaming and not streaming_started:
                # Attach/recovery path (or the command landed after the live
                # window): the streaming deliverer resumes from its durable
                # cursor and observes the item's terminal state.
                streaming_started = await self._start_streaming_delivery(
                    item, command, dict(reply_context or {}), conversation_key
                )

            if not drove:
                user = UserContext(user_id=str(payload.get("creator_user_id") or ""))
                resumed = self._resume_command_stream(
                    user, session_id, command_id=command_id,
                )
                if not await self._drain_with_lease_renewal(resumed, item):
                    await self._fail_item(item, "attached turn stream failed")
                    return

            message = await self._message_view.get_assistant_message_for_turn(
                session_id, turn_id=turn_id
            )
            if message is None and not drove:
                await self._fail_item(item, "attached turn ended without output")
                return

            # The reply intent must be durable BEFORE the item goes terminal:
            # SETTLED is unreachable by the reconciler, so a crash
            # between settle and outbox-create would lose the reply with no
            # evidence. Creating the (idempotent, command-bound) row first
            # keeps every crash window recoverable: before settle the item is
            # still live and re-drives to the same row; after settle the
            # PENDING row is swept.
            outbox: dict[str, Any] | None = None
            if has_reply and not wants_streaming:
                outbox = await self._channel_repo.create_outbox_entry(
                    work_item_id=item_id,
                    deployment_id=deployment_id,
                    channel_name=str(item.get("channel_name") or ""),
                    session_id=session_id,
                    command_id=command_id,
                    turn_id=turn_id,
                    reply_context=dict(reply_context or {}),
                    conversation_key=conversation_key,
                )
            if payload.get("retain_context"):
                await self._channel_repo.mark_context_submitted(item)
            if not await self._channel_repo.settle_inbound(
                item_id=item_id,
                owner_token=owner,
                generation=generation,
                session_id=session_id,
            ):
                return  # stolen mid-turn — the successor settles and delivers
            if outbox is not None:
                await self._deliver_outbox_row(outbox)
            # Streaming rows settle themselves: the delivery session observes
            # the item's SETTLED state and emits the terminal event.
        except asyncio.CancelledError:
            released = await self._channel_repo.release_inbound_lease(
                item_id=item_id, owner_token=owner, generation=generation,
            )
            logger.info("channel drive cancelled: item=%s released=%s", item_id, released)
            raise
        except Exception as exc:
            logger.error("channel drive failed: item=%s error=%s", item_id, exc)
            with contextlib.suppress(Exception):
                await self._fail_item(item, f"{type(exc).__name__}: {exc}")
        finally:
            if lock_generation is not None and conversation_key is not None:
                with contextlib.suppress(Exception):
                    await self._channel_repo.release_conversation_lock(
                        deployment_id=deployment_id,
                        conversation_key=conversation_key,
                        owner=owner,
                        generation=lock_generation,
                    )

    async def _decline_superseded_channel_question(
        self,
        *,
        user: UserContext,
        session_id: str,
    ) -> None:
        """Close an unanswered form interaction before the next message.

        A channel participant cannot answer the browser form card. Their next
        independent message supersedes it, but remains a new turn: it must
        not be reinterpreted as the form answer. The platform interaction API
        owns both endings — declined through a live engine, or abandoned once
        that engine is gone — and this spine only sequences one of them before
        dispatching the new command.
        """
        session = await self._session_detail_getter(user, session_id)
        pending = session.get("pending_interaction")
        if not isinstance(pending, dict):
            return
        if str(pending.get("presentation") or "").strip() != PRESENTATION_FORM:
            return
        interaction_id = str(pending.get("interaction_id") or "").strip()
        if not interaction_id:
            raise RuntimeError("pending form interaction has no interaction_id")

        await self._supersede_pending_interaction(
            user,
            session_id,
            interaction_id,
        )
        deadline_polls = max(
            1, int(_SESSION_READY_TIMEOUT_SECONDS / _SESSION_READY_POLL_SECONDS)
        )
        for _ in range(deadline_polls):
            session = await self._session_detail_getter(user, session_id)
            current_pending = session.get("pending_interaction")
            if isinstance(current_pending, dict):
                current_id = str(current_pending.get("interaction_id") or "").strip()
                if current_id and current_id != interaction_id:
                    raise RuntimeError(
                        "a new interaction became pending while the superseded "
                        "channel question was settling"
                    )
            state = str(session.get("state") or "")
            current_turn_id = str(session.get("current_turn_id") or "").strip()
            if (
                current_pending is None
                and not current_turn_id
                and state in ("READY", "BACKGROUND_RUNNING")
            ):
                return
            if state in ("TERMINATED", "DELETED", "RECOVERY_REQUIRED"):
                raise RuntimeError(
                    f"session {session_id} became {state} while declining its "
                    "superseded channel question"
                )
            await asyncio.sleep(_SESSION_READY_POLL_SECONDS)
        raise RuntimeError(
            f"session {session_id} did not finish its superseded channel question "
            f"in {_SESSION_READY_TIMEOUT_SECONDS}s"
        )

    @staticmethod
    def _provider_streams(channel_name: str) -> bool:
        try:
            return bool(get_channel(channel_name).supports_streaming_delivery)
        except Exception:
            return False

    async def _await_command(
        self, session_id: str, token: str, drain_task: Any
    ) -> dict[str, Any] | None:
        """Poll the journal until this drive's command lands (or the drain ends)."""
        while True:
            command = await self._session_events_repo.find_command_by_client_message_id(
                session_id, client_message_id=token
            )
            if command is not None or drain_task.done():
                return command
            await asyncio.sleep(0.2)

    async def _start_streaming_delivery(
        self,
        item: dict[str, Any],
        command: dict[str, Any],
        reply_context: dict[str, Any],
        conversation_key: str | None,
    ) -> bool:
        """Create the dispatch-time streaming outbox row and spawn its session."""
        outbox = await self._channel_repo.create_outbox_entry(
            work_item_id=str(item["_id"]),
            deployment_id=str(item.get("deployment_id") or ""),
            channel_name=str(item.get("channel_name") or ""),
            session_id=str(item.get("session_id") or ""),
            command_id=str(command.get("causation_id") or ""),
            turn_id=str(command.get("turn_id") or ""),
            reply_context=reply_context,
            conversation_key=conversation_key,
            streaming=True,
        )
        self._spawn_background_task(
            self._deliver_outbox_row(outbox),
            name=f"channel-stream-{outbox['_id']}",
        )
        return True

    async def _fail_item(self, item: dict[str, Any], error: str) -> None:
        """Record a failed drive: backoff → RECEIVED, exhausted → DEAD."""
        attempts = int(item.get("attempts") or 0)
        if attempts < len(_INBOUND_RETRY_BACKOFF_SECONDS):
            retry_at: float | None = time.time() + _INBOUND_RETRY_BACKOFF_SECONDS[attempts]
        else:
            retry_at = None
        recorded = await self._channel_repo.fail_inbound(
            item_id=str(item["_id"]),
            owner_token=str(item["owner_token"]),
            generation=int(item["generation"]),
            error=error,
            retry_at_epoch=retry_at,
        )
        logger.error(
            "channel drive failed: item=%s attempts=%d dead=%s recorded=%s error=%s",
            item["_id"], attempts + 1, retry_at is None, recorded, error,
        )

    async def _drain_with_lease_renewal(
        self, agen: Any, item: dict[str, Any]
    ) -> bool:
        """Drain the turn stream; return True only if it completed without error.

        Renews the item lease periodically so a long turn is not judged
        abandoned and stolen mid-flight. (If a steal happens anyway, the
        settle fence stops this worker before delivery.)
        """
        ok = True
        last_renew = time.monotonic()
        renew_every = 60.0
        try:
            async for _ in agen:
                now = time.monotonic()
                if now - last_renew >= renew_every:
                    last_renew = now
                    with contextlib.suppress(Exception):
                        await self._channel_repo.renew_inbound_lease(
                            item_id=str(item["_id"]),
                            owner_token=str(item["owner_token"]),
                            generation=int(item["generation"]),
                        )
        except Exception as exc:
            ok = False
            logger.error("channel drain failed: item=%s error=%s", item["_id"], exc)
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()
        return ok

    async def _acquire_conversation_lock_blocking(
        self, deployment_id: str, conversation_key: str, owner: str
    ) -> int | None:
        """Acquire the conversation lock, waiting (bounded) for a prior turn."""
        deadline_polls = max(
            1, int(_SESSION_READY_TIMEOUT_SECONDS / _SESSION_READY_POLL_SECONDS)
        )
        for _ in range(deadline_polls):
            generation = await self._channel_repo.acquire_conversation_lock(
                deployment_id=deployment_id, conversation_key=conversation_key, owner=owner
            )
            if generation is not None:
                return generation
            await asyncio.sleep(_SESSION_READY_POLL_SECONDS)
        # Last try after the budget (a crashed holder's TTL may have lapsed).
        return await self._channel_repo.acquire_conversation_lock(
            deployment_id=deployment_id, conversation_key=conversation_key, owner=owner
        )

    async def _resolve_conversation_session(
        self,
        *,
        user: UserContext,
        agent_service: Any,
        deployment_id: str,
        agent_id: str,
        conversation_key: str,
    ) -> str:
        """The (binding, conversation_key) → session routing."""
        mapping = await self._channel_repo.get_conversation(
            deployment_id=deployment_id, conversation_key=conversation_key
        )
        mapped_session_id = str((mapping or {}).get("session_id") or "")
        if mapped_session_id:
            session = await self._sessions_repo.get_session(mapped_session_id)
            state = str((session or {}).get("state") or "")
            if session is not None and state not in (
                "TERMINATED", "DELETED", "RECOVERY_REQUIRED",
            ):
                await self._channel_repo.touch_conversation(
                    deployment_id=deployment_id, conversation_key=conversation_key
                )
                return mapped_session_id
            # Mapped session is gone: start a replacement and CAS-swap the
            # mapping; a racing replacement's winner is read back and used.
            started = await agent_service.start_conversation(user, agent_id)
            new_session_id = str(started.get("session_id") or "")
            swapped = await self._channel_repo.upsert_conversation(
                deployment_id=deployment_id,
                conversation_key=conversation_key,
                session_id=new_session_id,
                agent_id=agent_id,
                replaces_session_id=mapped_session_id,
            )
            if swapped is None:
                current = await self._channel_repo.get_conversation(
                    deployment_id=deployment_id, conversation_key=conversation_key
                )
                winner = str((current or {}).get("session_id") or "")
                if winner and winner != new_session_id:
                    logger.info(
                        "channel conversation replace lost race: binding=%s key=%s "
                        "using winner session=%s",
                        deployment_id, conversation_key, winner,
                    )
                    return winner
            return new_session_id

        started = await agent_service.start_conversation(user, agent_id)
        new_session_id = str(started.get("session_id") or "")
        existing = await self._channel_repo.upsert_conversation(
            deployment_id=deployment_id,
            conversation_key=conversation_key,
            session_id=new_session_id,
            agent_id=agent_id,
        )
        winner = str((existing or {}).get("session_id") or new_session_id)
        if winner != new_session_id:
            logger.info(
                "channel conversation create lost race: binding=%s key=%s "
                "using winner session=%s",
                deployment_id, conversation_key, winner,
            )
        return winner

    async def _wait_session_ready(self, session_id: str) -> bool:
        """Poll until the session's sandbox is READY (bounded). Returns readiness."""
        deadline_polls = max(
            1, int(_SESSION_READY_TIMEOUT_SECONDS / _SESSION_READY_POLL_SECONDS)
        )
        for _ in range(deadline_polls):
            session = await self._sessions_repo.get_session(session_id)
            state = str((session or {}).get("state") or "")
            if state in ("READY", "WAITING_INPUT", "BACKGROUND_RUNNING"):
                return True
            if state in ("TERMINATED", "DELETED", "RECOVERY_REQUIRED"):
                return False
            await asyncio.sleep(_SESSION_READY_POLL_SECONDS)
        return False

    # ── delivery (invariant C) ───────────────────────────────────────────

    async def _deliver_outbox_row(self, entry: dict[str, Any]) -> None:
        """At-least-once, exactly-one-owner reply delivery for one outbox row.

        CAS-leases the row (fenced) and routes: streaming rows run a delivery
        session over the turn's durable frames; simple rows post the bound
        turn's final text. Every settlement write carries the fenced lease,
        so a deliverer that lost its lease mid-flight cannot complete or fail
        the successor's row.
        """
        outbox_id = str(entry.get("_id") or "")
        owner = new_owner_token()
        generation = await self._channel_repo.claim_outbox_for_delivery(
            outbox_id, owner=owner
        )
        if generation is None:
            logger.info(
                "channel reply already leased by another deliverer (outbox=%s); skipping",
                outbox_id,
            )
            return
        current = await self._channel_repo.get_outbox_entry(outbox_id) or entry
        if bool(current.get("streaming")):
            await self._deliver_streaming(current, owner=owner, generation=generation)
        else:
            await self._deliver_simple(current, owner=owner, generation=generation)

    async def _persist_receipt(
        self,
        entry: dict[str, Any],
        *,
        owner: str,
        generation: int,
        known_aliases: list[str],
        receipt: ChannelDeliveryReceipt | None,
    ) -> list[str]:
        """Merge + persist receipt aliases ahead of any completion write."""
        if receipt is None or not receipt.message_ids:
            return known_aliases
        merged = list(dict.fromkeys([*known_aliases, *[str(a) for a in receipt.message_ids]]))
        if merged != known_aliases:
            await self._channel_repo.record_delivery_aliases(
                str(entry["_id"]),
                owner=owner,
                generation=generation,
                aliases=merged,
                deployment_id=str(entry.get("deployment_id") or "") or None,
                conversation_key=str(entry.get("conversation_key") or "") or None,
                session_id=str(entry.get("session_id") or "") or None,
            )
        return merged

    async def _deliver_simple(
        self, entry: dict[str, Any], *, owner: str, generation: int
    ) -> None:
        """Final-text delivery with capped inline retries, then DELIVERED/DEAD."""
        outbox_id = str(entry.get("_id") or "")
        channel_name = str(entry.get("channel_name") or "")
        session_id = str(entry.get("session_id") or "")
        turn_id = str(entry.get("turn_id") or "")

        # A row bound to a superseded turn attempt (the item failed and
        # retried under a new command) must never deliver its stale text —
        # the winning attempt's row carries the reply.
        item = await self._channel_repo.get_inbound(str(entry.get("work_item_id") or ""))
        item_command = str((item or {}).get("command_id") or "")
        if item is not None and item_command and item_command != str(entry.get("command_id") or ""):
            await self._channel_repo.record_outbox_failure(
                outbox_id,
                owner=owner,
                generation=generation,
                error="turn attempt superseded by a retry",
                next_attempt_at=None,
            )
            return

        message = await self._message_view.get_assistant_message_for_turn(
            session_id, turn_id=turn_id
        )
        text = str((message or {}).get("content") or "").strip()
        if not text:
            logger.warning(
                "channel %s outbox %s: bound turn %s has no assistant text",
                channel_name, outbox_id, turn_id,
            )
            await self._channel_repo.record_outbox_failure(
                outbox_id,
                owner=owner,
                generation=generation,
                error="no assistant text on the bound turn",
                next_attempt_at=None,
            )
            return

        known_aliases = [str(a) for a in (entry.get("delivery_aliases") or [])]
        attempts = len(_OUTBOX_RETRY_BACKOFF_SECONDS) + 1
        for attempt in range(1, attempts + 1):
            try:
                receipt = await get_channel(channel_name).deliver_outbound(
                    reply_context=dict(entry.get("reply_context") or {}),
                    text=text,
                    binding=await self._delivery_binding(entry),
                )
                known_aliases = await self._persist_receipt(
                    entry,
                    owner=owner,
                    generation=generation,
                    known_aliases=known_aliases,
                    receipt=receipt if isinstance(receipt, ChannelDeliveryReceipt) else None,
                )
                await self._channel_repo.mark_outbox_delivered(
                    outbox_id, owner=owner, generation=generation
                )
                return
            except Exception as exc:
                is_last = attempt >= attempts
                logger.warning(
                    "channel %s reply delivery attempt %d/%d failed (outbox=%s): %s",
                    channel_name, attempt, attempts, outbox_id, exc,
                )
                recorded = await self._channel_repo.record_outbox_failure(
                    outbox_id,
                    owner=owner,
                    generation=generation,
                    error=f"{type(exc).__name__}: {exc}",
                    next_attempt_at=None if is_last else utcnow_iso(),
                )
                if not recorded:
                    return  # lease stolen — the successor owns the retries
                if is_last:
                    logger.error(
                        "channel %s reply delivery exhausted; outbox %s marked DEAD",
                        channel_name, outbox_id,
                    )
                    return
                await asyncio.sleep(_OUTBOX_RETRY_BACKOFF_SECONDS[attempt - 1])

    @staticmethod
    def _coalesce_frame_text(frames: list[dict[str, Any]], text_so_far: str) -> str:
        for frame in frames:
            if str(frame.get("type") or "") == "text-delta":
                text_so_far = f"{text_so_far}{str(frame.get('delta') or '')}"
        return text_so_far

    async def _deliver_streaming(
        self, entry: dict[str, Any], *, owner: str, generation: int
    ) -> None:
        """One streaming delivery session over the turn's durable frame log.

        Projects frames after the durable ``frame_cursor`` into versioned
        channel events (invariant D: coalesced progress, no raw frames, no
        backpressure on the turn — this loop only reads what the turn already
        persisted). The work item is the terminal authority: SETTLED emits
        ``settled`` with the turn-bound final text; a superseded/dead item
        emits ``failed``. Aliases persist before every completion write, so a
        crashed session resumes with update-instead-of-create evidence.
        """
        outbox_id = str(entry.get("_id") or "")
        channel_name = str(entry.get("channel_name") or "")
        session_id = str(entry.get("session_id") or "")
        command_id = str(entry.get("command_id") or "")
        turn_id = str(entry.get("turn_id") or "")
        work_item_id = str(entry.get("work_item_id") or "")
        reply_context = dict(entry.get("reply_context") or {})
        known_aliases = [str(a) for a in (entry.get("delivery_aliases") or [])]
        cursor = int(entry.get("frame_cursor") if entry.get("frame_cursor") is not None else -1)

        async def _fail_delivery(error: str, *, dead: bool) -> None:
            await self._channel_repo.record_outbox_failure(
                outbox_id,
                owner=owner,
                generation=generation,
                error=error,
                next_attempt_at=None if dead else utcnow_iso(),
            )

        try:
            handle = await get_channel(channel_name).open_delivery(
                reply_context=reply_context,
                prior_aliases=list(known_aliases),
                binding=await self._delivery_binding(entry),
            )
        except Exception as exc:
            await _fail_delivery(f"open_delivery: {type(exc).__name__}: {exc}", dead=False)
            return

        seq = 0
        text_so_far = ""
        started = time.monotonic()
        try:
            # Rebuild the coalesced text already delivered (resume) and emit
            # turn_started exactly once per row (fresh cursor).
            if cursor >= 0:
                prior = await self._session_events_repo.list_frames(
                    session_id, command_id=command_id, after_seq=-1
                )
                text_so_far = self._coalesce_frame_text(
                    [f for f in prior if int(f.get("frame_seq") or 0) <= cursor], ""
                )
            if cursor < 0:
                receipt = await handle.emit(
                    ChannelEvent(
                        type=EVENT_TURN_STARTED, seq=seq,
                        command_id=command_id, turn_id=turn_id,
                    )
                )
                known_aliases = await self._persist_receipt(
                    entry, owner=owner, generation=generation,
                    known_aliases=known_aliases, receipt=receipt,
                )
            while True:
                if time.monotonic() - started > _STREAMING_MAX_SECONDS:
                    await _fail_delivery("streaming session budget exceeded", dead=False)
                    return
                if not await self._channel_repo.renew_outbox_lease(
                    outbox_id, owner=owner, generation=generation
                ):
                    return  # lease stolen — the successor resumes from the cursor
                new_frames = await self._session_events_repo.list_frames(
                    session_id, command_id=command_id, after_seq=cursor
                )
                if new_frames:
                    grown = self._coalesce_frame_text(new_frames, text_so_far)
                    last_seq = max(int(f.get("frame_seq") or 0) for f in new_frames)
                    if grown != text_so_far:
                        text_so_far = grown
                        seq += 1
                        receipt = await handle.emit(
                            ChannelEvent(
                                type=EVENT_PROGRESS, seq=seq, text=text_so_far,
                                command_id=command_id, turn_id=turn_id,
                            )
                        )
                        known_aliases = await self._persist_receipt(
                            entry, owner=owner, generation=generation,
                            known_aliases=known_aliases, receipt=receipt,
                        )
                    await self._channel_repo.advance_outbox_cursor(
                        outbox_id, owner=owner, generation=generation,
                        frame_seq=last_seq,
                    )
                    cursor = max(cursor, last_seq)

                item = await self._channel_repo.get_inbound(work_item_id)
                item_state = str((item or {}).get("state") or "")
                item_command = str((item or {}).get("command_id") or "")
                if item_state == INBOUND_SETTLED and item_command == command_id:
                    message = await self._message_view.get_assistant_message_for_turn(
                        session_id, turn_id=turn_id
                    )
                    final_text = (
                        str((message or {}).get("content") or "").strip() or text_so_far
                    )
                    seq += 1
                    receipt = await handle.emit(
                        ChannelEvent(
                            type=EVENT_SETTLED, seq=seq, text=final_text,
                            command_id=command_id, turn_id=turn_id,
                        )
                    )
                    known_aliases = await self._persist_receipt(
                        entry, owner=owner, generation=generation,
                        known_aliases=known_aliases, receipt=receipt,
                    )
                    await self._channel_repo.mark_outbox_delivered(
                        outbox_id, owner=owner, generation=generation
                    )
                    return
                if item is None or item_state == INBOUND_DEAD or (
                    item_command and item_command != command_id
                ) or (
                    item_state == INBOUND_RECEIVED
                    and int((item or {}).get("attempts") or 0) > 0
                ):
                    # This command's attempt failed (or was superseded by a
                    # retry that will get its own outbox row): terminal.
                    seq += 1
                    with contextlib.suppress(Exception):
                        await handle.emit(
                            ChannelEvent(
                                type=EVENT_FAILED, seq=seq,
                                error="turn attempt failed",
                                command_id=command_id, turn_id=turn_id,
                            )
                        )
                    await _fail_delivery("bound turn attempt failed", dead=True)
                    return
                await asyncio.sleep(_STREAMING_POLL_SECONDS)
        except Exception as exc:
            logger.warning(
                "channel %s streaming delivery failed (outbox=%s): %s",
                channel_name, outbox_id, exc,
            )
            await _fail_delivery(f"{type(exc).__name__}: {exc}", dead=False)
        finally:
            with contextlib.suppress(Exception):
                await handle.close()

    async def _delivery_binding(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Reload provider credentials without persisting them in the outbox."""
        deployment_id = str(entry.get("deployment_id") or "").strip()
        expected_channel = str(entry.get("channel_name") or "").strip()
        binding = await self._deployment_repo.get_by_id(deployment_id)
        actual_channel = channel_scene_name(str((binding or {}).get("scene") or ""))
        if (
            not binding
            or binding.get("deleted") is True
            or binding.get("enabled") is False
            or actual_channel != expected_channel
        ):
            raise RuntimeError(
                f"channel delivery binding {deployment_id!r} is missing, disabled, "
                "or no longer matches its durable outbox row"
            )
        return await self._channel_credentials.hydrate(
            binding, get_channel(expected_channel)
        )

    # ── recovery (the reconciler's entry points) ─────────────────────────

    async def recover_inbound(self, *, limit: int = 50) -> int:
        """Re-drive expired live work items (crash recovery, invariant A).

        Each candidate is CAS-claimed (fence forward) before driving; a live
        worker that renewed between the sweep read and the claim wins and the
        candidate is skipped. Returns how many items were re-driven.
        """
        items = await self._channel_repo.list_recoverable_inbound(limit=limit)
        recovered = 0
        for stale in items:
            claimed = await self._channel_repo.claim_inbound_for_recovery(
                item_id=str(stale["_id"]),
                expected_generation=int(stale.get("generation") or 0),
            )
            if claimed is None:
                continue
            recovered += 1
            await self._drive_work_item(claimed)
        if recovered:
            logger.info("channel inbound recovery re-drove %d items", recovered)
        return recovered

    async def sweep_pending_outbox(self, *, limit: int = 50) -> int:
        """Re-deliver abandoned outbox rows (boot + periodic recovery)."""
        entries = await self._channel_repo.list_pending_outbox(limit=limit)
        for entry in entries:
            await self._deliver_outbox_row(entry)
        if entries:
            logger.info("channel outbox sweep processed %d abandoned entries", len(entries))
        return len(entries)
