"""Channel ingress spine — one typed boundary from inbound message to reply.

Contract: ``docs/channel-spine.md``. This service owns everything between a
provider-authenticated inbound message and the delivered reply:

* :meth:`ChannelIngressService.ingest` — the single post-auth ingress
  (invariant E). The HTTP webhook trigger and any trusted broker source both
  call it with a resolved binding + :class:`~astrabox.seams.channel.ChannelInbound`;
  nobody fabricates HTTP and no provider touches spine internals.
* The durable inbound work item (invariant A): the full typed payload is
  persisted before the caller may acknowledge the source. Sending the message
  never depends on the platform redelivering.
* Fenced ownership (invariant B): items, conversation locks, and delivery
  leases are held by ``owner_token`` + monotonic ``generation``; a stale
  worker's writes miss their CAS predicates and change nothing.
* One input per message: the item's id is the ``client_message_id`` of the
  input it sends (:meth:`SessionRuntimeService.send_as
  <astrabox.core.service.orchestrator.session_runtime.service.SessionRuntimeService.send_as>`),
  so the Session accepts a redelivered or re-driven message once.
* Replies: :meth:`ChannelIngressService.on_facts` reads each committed batch of
  the Session's runtime facts and turns the replies it opens or completes into
  deliveries for the Session's subscribed bindings
  (:mod:`~astrabox.core.service.orchestrator.channel_replies`); the deliverer
  reads each exact reply from the Session's output log.

The channel reconciler (:mod:`channel_spine_reconciler`) re-drives expired
items, projects output a consumer did not reach, and re-delivers abandoned
outbox rows through the same methods.
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
from astrabox.core.service.orchestrator.channel_interactions import (
    QuestionAnswer,
    Refusal,
    answer_action,
    answered_summary,
    channel_interaction,
    engine_answer,
    interaction_ref,
    parse_action,
    question_count,
    read_reply,
)
from astrabox.core.service.orchestrator.channel_replies import (
    REPLY_BOUNDARY_PARTS,
    ChannelReply,
    ReplyReader,
    reply_segments,
)
from astrabox.core.service.orchestrator.conversation_targets import (
    ConversationTarget,
    ConversationTargets,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    public_interaction_view,
)
from astrabox.core.service.orchestrator.session_runtime.service import SessionRuntimeService
from astrabox.persistence.repository.channel_repository import (
    CLAIM_OUTCOME_AUTHORIZED,
    CLAIM_OUTCOME_CLAIMED,
    CLAIM_OUTCOME_DUPLICATE,
    CLAIM_OUTCOME_IGNORED,
    CLAIM_OUTCOME_IN_PROGRESS,
    CLAIM_OUTCOME_OBSERVED,
    INBOUND_AUTHORIZED,
    INBOUND_IGNORED,
    INBOUND_OBSERVED,
    INBOUND_RECEIVED,
    INBOUND_SETTLED,
    INTERACTION_ANSWERED,
    INTERACTION_CLOSED,
    INTERACTION_OPEN,
    LEASE_RENEW_SECONDS,
    OUTBOX_DEAD,
    OUTBOX_PENDING,
    ChannelRepository,
    new_owner_token,
)
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch,
    SessionRuntimeRepository,
)
from astrabox.seams.channel import (
    EVENT_FAILED,
    EVENT_PROGRESS,
    EVENT_SETTLED,
    EVENT_TURN_STARTED,
    EXECUTION_ACCOUNT,
    EXECUTION_LINKED,
    EXECUTION_OWNER,
    SIGNAL_AUTHORIZATION,
    SIGNAL_OBSERVATION,
    ChannelDeliveryReceipt,
    ChannelEvent,
    ChannelInbound,
    ChannelInteraction,
    channel_scene_name,
    get_channel,
)
from astrabox.seams.channel_accounts import channel_account_resolver
from astrabox.seams.channel_response import (
    RESPONSE_POLICIES,
    RESPONSE_POLICY_PARTICIPATED,
    ChannelContextMessage,
    ChannelResponseContext,
)

logger = get_logger(__name__)

#: How long a drive waits for its conversation's lock, which another drive
#: holds only while it answers, stops or sends into the same Session. A drive
#: that cannot take it hands its item back and the reconciler drives it again.
_CONVERSATION_LOCK_WAIT_SECONDS = 120
_CONVERSATION_LOCK_POLL_SECONDS = 2.0

#: Re-drive backoff per recorded failure; after the last entry the item is
#: DEAD (durable evidence, revivable only by an explicit redelivery).
_INBOUND_RETRY_BACKOFF_SECONDS = (10.0, 60.0, 300.0, 900.0)

#: The wait before each retry of a failed reply delivery. After the last
#: failure the outbox row is DEAD (visible to operators) rather than retried
#: forever. The row's stored failure count picks the wait, so the budget holds
#: across deliverers and restarts.
_OUTBOX_RETRY_BACKOFF_SECONDS = (5.0, 30.0, 120.0)

#: How many output subscriptions one reconciler tick projects.
_OUTPUT_SUBSCRIPTION_PAGE = 50
#: How many Session output rows one projection step reads.
_OUTPUT_PAGE = 500

#: Streaming delivery session pacing: how often the deliverer reads the
#: reply's new parts from the Session's output log and renews its lease, and
#: its overall budget. A hung provider stalls only its own session (invariant
#: D); the budget bounds one delivery session of a reply that runs long.
_STREAMING_POLL_SECONDS = 1.0
_STREAMING_MAX_SECONDS = 1800.0

#: An answer refused with one of these found no wait left to answer: the
#: runtime closed the interaction, or it already holds someone's answer.
_ANSWER_GONE_CODES = frozenset({"INTERACTION_NOT_FOUND", "INTERACTION_NOT_OPEN"})

#: Reply-chain resolution window: an inbound referencing a platform alias that
#: is still being delivered (alias write racing) gets this many short waits
#: before it legitimately starts a new chain.
_REFERENCE_RESOLVE_ATTEMPTS = 3
_REFERENCE_RESOLVE_WAIT_SECONDS = 0.5

#: Binding attention policies (docs/channel-spine.md). ``all`` responds to
#: every inbound; ``mentions`` requires a typed attention signal. The two
#: response policies also require one, and send ordinary messages to the
#: response judge: ``participated`` only in topics the agent took part in,
#: ``proactive`` in any topic.
ATTENTION_POLICY_ALL = "all"
ATTENTION_POLICY_MENTIONS = "mentions"
VALID_ATTENTION_POLICIES = (ATTENTION_POLICY_ALL, ATTENTION_POLICY_MENTIONS, *RESPONSE_POLICIES)
_ADDRESSED_ONLY_POLICIES = (ATTENTION_POLICY_MENTIONS, *RESPONSE_POLICIES)
#: Host-clock bound on one response judgement; a slower judge is a failed drive.
_RESPONSE_JUDGE_TIMEOUT_SECONDS = 30.0


#: Fields of an inbound record that describe the message itself. The first
#: signal that records them wins; a later signal for the same message admits
#: it without replacing them.
_MESSAGE_FACTS = (
    "reply_text", "conversation_key", "reference", "participant",
    "retain_context", "message_timestamp", "provider_ignore_reason", "conversation",
)

#: Identity fields a later signal must agree on with the recorded message.
_IDENTITY_FIELDS = ("participant", "conversation_key")

#: Refusals the participant can act on, with what they are told. The message
#: is recorded as refused and leaves retained context; it never runs as
#: another account.
_REFUSAL_NOTICES = {
    "ENVIRONMENT_ACCESS_DENIED": (
        "This message was not answered because its execution account does not have "
        "access to this Environment."
    ),
    "CHANNEL_REFERENCE_OWNER_MISMATCH": (
        "This message was not answered because it continues a conversation you "
        "cannot continue. Send a new message to start your own."
    ),
    "CHANNEL_PARTICIPANT_REQUIRED": (
        "This message was not answered because the platform did not say who sent it."
    ),
    "CHANNEL_PARTICIPANT_NOT_LINKED": (
        "This message was not answered because your account here is not linked "
        "to an AstraBox account. Ask an administrator to link it."
    ),
    "CHANNEL_ACCOUNT_NOT_AUTHORIZED": (
        "This message was not answered because your linked AstraBox account "
        "cannot use this agent."
    ),
}

#: Why a message is refused when its binding's execution mode is not one its
#: provider supports: running it in any other mode would run it as an account
#: nobody chose.
_EXECUTION_MODE_UNSUPPORTED = "CHANNEL_EXECUTION_MODE_UNSUPPORTED"


def binding_execution_mode(binding: dict[str, Any]) -> str:
    """The execution mode a channel binding runs its messages in.

    A binding saved before execution modes existed stores none; its
    ``execution_user_id`` already said which of the two fixed modes it runs
    in.
    """
    mode = str(binding.get("execution_mode") or "").strip()
    if mode:
        return mode
    if str(binding.get("execution_user_id") or "").strip():
        return EXECUTION_ACCOUNT
    return EXECUTION_OWNER


#: What the participant is told when a message is not answered for a reason
#: they can act on. A participant who recalled a message is not told.
_IDENTITY_CONFLICT_NOTICE = (
    "This message could not be matched to the message it names, so it was not "
    "answered. Send it again."
)
_AUTHORIZATION_EXPIRED_NOTICE = (
    "This message was not answered because its content never arrived. Send it again."
)


@dataclass
class ChannelIngressReceipt:
    """What ``ingest`` durably promised the source.

    ``accepted`` — the work item is owned and will drive a turn;
    ``duplicate`` — this message already settled (idempotent redelivery ack);
    ``in_progress`` — a live owner is driving it;
    ``ignored`` — a provider or platform policy terminally classified it;
    ``observed`` — it is retained context and nothing runs for it now;
    ``authorized`` — it is admitted and runs when its observation arrives.
    Whatever the status, the source may acknowledge upstream: the outcome is
    durable (invariant A).
    """

    status: str
    item_id: str
    session_id: str | None = None
    ack_extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Classification:
    """The state an inbound is recorded in, decided before the write."""

    state: str
    reason: str | None = None
    excluded: bool = False
    #: An ordinary message the binding's response policy may send to the judge.
    judge_candidate: bool = False


@dataclass
class _DriveLeases:
    """The leases one drive holds: its item, and its conversation's lock once taken."""

    item_id: str
    owner: str
    generation: int
    deployment_id: str
    conversation_key: str | None = None
    execution_user_id: str = ""
    lock_generation: int | None = None

    def hold_conversation(
        self, conversation_key: str, execution_user_id: str, lock_generation: int,
    ) -> None:
        self.conversation_key = conversation_key
        self.execution_user_id = execution_user_id
        self.lock_generation = lock_generation


class ChannelIngressService:
    """The channel spine between provider auth and platform reply."""

    def __init__(
        self,
        *,
        deployment_repo: Any,
        agent_repo: Any,
        agent_service_getter: Any,
        sessions_repo: Any,
        session_runtime: SessionRuntimeService,
        session_facts: SessionRuntimeRepository,
        spawn_background_task: Any,
        channel_repo: ChannelRepository | None = None,
        channel_credentials: ChannelCredentialService | None = None,
        assistant_service_getter: Any = None,
    ) -> None:
        self._deployment_repo = deployment_repo
        self._targets = ConversationTargets(
            agent_repo=agent_repo,
            agent_service_getter=agent_service_getter,
            assistant_service_getter=assistant_service_getter,
        )
        self._sessions_repo = sessions_repo
        self._session_runtime = session_runtime
        self._facts = session_facts
        self._spawn_background_task = spawn_background_task
        self._channel_repo = channel_repo or ChannelRepository()
        self._channel_credentials = channel_credentials or ChannelCredentialService()
        #: Where the reconciler's pass over subscriptions resumes.
        self._output_subscription_cursor: tuple[str, str] | None = None
        self._delivering: set[str] = set()

    # ── ingress (invariants A + E) ───────────────────────────────────────

    async def ingest(
        self, deployment_id: str, inbound: ChannelInbound
    ) -> ChannelIngressReceipt:
        """The single post-auth typed ingress for every channel source."""
        binding = await self._deployment_repo.get_by_id(deployment_id)
        if not binding or binding.get("enabled") is False:
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
        from astrabox.core.service.orchestrator.runtime.environment_ownership import require_environment_ownership

        require_environment_ownership(binding)
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
        target = await self._targets.resolve(binding)
        if target is None:
            raise APIError(
                code="NOT_FOUND", message="bound agent not found", status_code=404
            )
        execution_mode = binding_execution_mode(binding)
        if execution_mode == EXECUTION_LINKED:
            # Each message runs as its participant's linked account, which is
            # resolved once, at the message's first drive.
            execution_user_id = creator_user_id = ""
        else:
            execution_user_id = (
                str(binding.get("execution_user_id") or "").strip()
                if execution_mode == EXECUTION_ACCOUNT else ""
            )
            if execution_mode == EXECUTION_ACCOUNT and not execution_user_id:
                raise RuntimeError(
                    f"channel binding {deployment_id} runs as a chosen account but names none"
                )
            creator_user_id = execution_user_id or target.owner_user_id
            if not creator_user_id:
                raise APIError(
                    code="DEPLOYMENT_NO_OWNER",
                    message="bound agent has no creator to run as",
                    status_code=409,
                )

        if inbound.retain_context and not all((
            inbound.dedup_key, inbound.conversation_key,
            inbound.message_timestamp, inbound.participant, binding.get("updated_at"),
        )):
            raise RuntimeError("retained channel message lacks ordering identity")
        prompt_prefix = str(binding.get("prompt_prefix") or "").strip()
        authorization = inbound.signal == SIGNAL_AUTHORIZATION
        payload = {
            # An authorization carries no text: its turn input is built from
            # the observed message when the two meet (_admit_recorded).
            "content": "" if authorization else self._prefixed(prompt_prefix, inbound.content),
            # What the participant typed, before any binding prompt prefix: a
            # reply to a question and retained context are read from this.
            "reply_text": inbound.content,
            "action": inbound.action,
            "conversation_key": inbound.conversation_key,
            "reply_context": dict(inbound.reply_context or {}) or None,
            "reference": inbound.reference,
            "participant": inbound.participant,
            "provider_ignore_reason": inbound.ignore_reason,
            "creator_user_id": creator_user_id,
            "execution_user_id": execution_user_id,
            "execution_mode": execution_mode,
            "retain_context": inbound.retain_context,
            "message_timestamp": inbound.message_timestamp,
            "binding_revision": binding.get("updated_at"),
            "prompt_prefix": prompt_prefix,
        }
        decision = await self._classify(binding, inbound, deployment_id)
        if decision.state in (INBOUND_RECEIVED, INBOUND_AUTHORIZED) and (
            execution_mode not in get_channel(channel_name).describe().execution_modes
        ):
            logger.error(
                "channel binding %s runs as %s, which provider %s does not support: "
                "its messages are refused until the binding's execution mode is changed",
                deployment_id, execution_mode, channel_name,
            )
            decision = _Classification(INBOUND_IGNORED, _EXECUTION_MODE_UNSUPPORTED, excluded=True)
        if decision.state != INBOUND_IGNORED:
            payload["conversation"] = await self._message_conversation(
                inbound, deployment_id=deployment_id,
                wait=decision.state == INBOUND_RECEIVED,
            )
        if decision.judge_candidate:
            selection = await self._response_selection(binding, payload, deployment_id)
            if selection is None:
                decision = _Classification(INBOUND_OBSERVED)
            else:
                payload.update(selection)
        item, outcome = await self._channel_repo.claim_inbound(
            deployment_id=deployment_id,
            dedup_key=inbound.dedup_key,
            channel_name=channel_name,
            payload=payload,
            state=decision.state,
            ignored_reason=decision.reason,
            context_excluded=decision.excluded,
            **target.fields,
        )
        ack_extra = dict(inbound.ack_extra)
        if (
            outcome in (CLAIM_OUTCOME_IN_PROGRESS, CLAIM_OUTCOME_CLAIMED)
            and decision.state == INBOUND_RECEIVED
            and not decision.judge_candidate
            and (item.get("payload") or {}).get("selection") == "judge"
        ):
            # This copy addressed the agent while another copy of the same
            # message is under judgment, or is reclaiming it: the explicit
            # signal must win over any decision the judge makes.
            await self._channel_repo.note_explicit_attention(item_id=str(item["item_id"]))
        if outcome == CLAIM_OUTCOME_CLAIMED:
            return await self._admit(item, target=target, ack_extra=ack_extra)
        # An ordinary copy of a recorded message is not a new reason to answer
        # it: only an explicit signal admits a record kept as context.
        admits = (
            decision.state in (INBOUND_RECEIVED, INBOUND_AUTHORIZED)
            and not decision.judge_candidate
        )
        if outcome == CLAIM_OUTCOME_OBSERVED and admits:
            # The message was recorded as context only; this signal admits it.
            return await self._admit_recorded(
                item, signal_payload=payload, target=target, ack_extra=ack_extra,
            )
        if (
            outcome == CLAIM_OUTCOME_AUTHORIZED
            and not authorization
            and decision.state != INBOUND_IGNORED
        ):
            # The message an earlier authorization admitted has arrived.
            return await self._admit_recorded(
                item, signal_payload=payload, target=target, ack_extra=ack_extra,
            )
        status = outcome
        if decision.state == INBOUND_OBSERVED:
            # Whatever record the message already has, nothing runs for an
            # inbound that only reports it.
            status = CLAIM_OUTCOME_OBSERVED
        elif decision.state == INBOUND_IGNORED and outcome in (
            CLAIM_OUTCOME_OBSERVED, CLAIM_OUTCOME_AUTHORIZED,
        ):
            # A refused signal neither admits nor completes the record.
            status = CLAIM_OUTCOME_IGNORED
        logger.info(
            "channel ingest suppressed: binding=%s outcome=%s dedup_key=%s",
            deployment_id, outcome, inbound.dedup_key,
        )
        return ChannelIngressReceipt(
            status=status,
            item_id=str(item["item_id"]),
            session_id=str(item.get("session_id") or "") or None,
            ack_extra=ack_extra,
        )

    async def _admit(
        self, item: dict[str, Any], *, target: ConversationTarget, ack_extra: dict[str, Any],
    ) -> ChannelIngressReceipt:
        """Resolve an owned RECEIVED item's session and spawn its drive."""
        deployment_id = str(item.get("deployment_id") or "")
        if (item.get("payload") or {}).get("selection") == "judge":
            # A message the judge may decline must not create a session or a
            # conversation; its drive resolves one only after a yes.
            self._spawn_background_task(
                self._drive_work_item(item),
                name=f"channel-drive-{item['item_id']}",
            )
            return ChannelIngressReceipt(
                status="accepted", item_id=str(item["item_id"]), ack_extra=ack_extra,
            )
        # Resolve the target session inline so the ack carries it (and so
        # redeliveries get a session in their in_progress/duplicate acks).
        # Idempotent under crash: the session binds onto the item via CAS and
        # a re-drive reuses it.
        try:
            session_id = await self._resolve_item_session(item)
        except APIError as exc:
            if exc.code in _REFUSAL_NOTICES and await self._refuse_item(item, exc.code):
                return ChannelIngressReceipt(
                    status=CLAIM_OUTCOME_IGNORED, item_id=str(item["item_id"]),
                    ack_extra={**ack_extra, "reason": exc.code},
                )
            raise
        self._spawn_background_task(
            self._drive_work_item(item),
            name=f"channel-drive-{item['item_id']}",
        )
        logger.warning(
            "channel inbound accepted: binding=%s %s=%s session=%s item=%s",
            deployment_id, target.kind, target.target_id, session_id, item["item_id"],
        )
        return ChannelIngressReceipt(
            status="accepted",
            item_id=str(item["item_id"]),
            session_id=session_id or None,
            ack_extra=ack_extra,
        )

    async def _admit_recorded(
        self,
        item: dict[str, Any],
        *,
        signal_payload: dict[str, Any],
        target: ConversationTarget,
        ack_extra: dict[str, Any],
        attempts: int = 3,
    ) -> ChannelIngressReceipt:
        """Admit an OBSERVED or AUTHORIZED message once both halves are known.

        An OBSERVED record holds the message's content and ``signal_payload``
        admits it; an AUTHORIZED record holds the admission and
        ``signal_payload`` brings the content. The turn runs the recorded
        content with the admitting signal's destination, attention, execution
        account and binding revision. A signal naming another sender or
        conversation than the record holds is refused visibly; a recalled
        message is never admitted. A record another signal or a recall changed
        between the read and the write is evaluated again as it is now.
        """
        item_id = str(item["item_id"])
        state = str(item["state"])
        generation = int(item.get("generation") or 0)
        recorded = dict(item.get("payload") or {})
        facts, admission = (
            (recorded, signal_payload) if state == INBOUND_OBSERVED
            else (signal_payload, recorded)
        )
        conflicts = [
            name for name in _IDENTITY_FIELDS
            if facts.get(name) and admission.get(name) and facts[name] != admission[name]
        ]
        if conflicts:
            reason = f"CHANNEL_SIGNAL_IDENTITY_CONFLICT: {', '.join(conflicts)}"
            if await self._channel_repo.refuse_unadmitted_inbound(
                item_id=item_id, expected_state=state,
                expected_generation=generation, reason=reason,
            ):
                logger.error(
                    "channel signals disagree on one message: item=%s fields=%s",
                    item_id, conflicts,
                )
                try:
                    await self._notify({**item, "payload": admission}, _IDENTITY_CONFLICT_NOTICE)
                except Exception:
                    # The refusal is durable; a notice the platform rejects
                    # must not turn it into a redelivered, re-refused message.
                    logger.exception("channel conflict notice failed: item=%s", item_id)
            return ChannelIngressReceipt(
                status=CLAIM_OUTCOME_IGNORED, item_id=item_id,
                ack_extra={**ack_extra, "reason": "CHANNEL_SIGNAL_IDENTITY_CONFLICT"},
            )
        if item.get("recall") is not None:
            if state == INBOUND_OBSERVED:
                return ChannelIngressReceipt(
                    status=CLAIM_OUTCOME_IGNORED, item_id=item_id,
                    ack_extra={**ack_extra, "reason": "recalled"},
                )
            # The authorization never runs a recalled message, but the content
            # its observation brings still has a place in retained context.
            observed = await self._channel_repo.observe_authorized_inbound(
                item_id=item_id, expected_generation=generation, payload=facts,
            )
            if observed is not None:
                return ChannelIngressReceipt(
                    status=CLAIM_OUTCOME_OBSERVED, item_id=item_id, ack_extra=ack_extra,
                )
        else:
            promoted = await self._channel_repo.promote_inbound(
                item_id=item_id,
                expected_state=state,
                expected_generation=generation,
                payload=self._admitted_payload(facts, admission),
            )
            if promoted is not None:
                return await self._admit(promoted, target=target, ack_extra=ack_extra)
        current = await self._channel_repo.get_inbound(item_id) or item
        if current.get("state") == state and attempts > 1:
            return await self._admit_recorded(
                current, signal_payload=signal_payload, target=target,
                ack_extra=ack_extra, attempts=attempts - 1,
            )
        return ChannelIngressReceipt(
            status=self._receipt_status(str(current.get("state") or "")),
            item_id=item_id,
            session_id=str(current.get("session_id") or "") or None,
            ack_extra=ack_extra,
        )

    @classmethod
    def _admitted_payload(
        cls, facts: dict[str, Any], admission: dict[str, Any]
    ) -> dict[str, Any]:
        """The input an admitted message runs with: its first recorded content."""
        admitted = dict(admission)
        admitted.update({name: facts.get(name) for name in _MESSAGE_FACTS})
        admitted["content"] = cls._prefixed(
            str(admission.get("prompt_prefix") or ""), str(facts.get("reply_text") or ""),
        )
        return admitted

    @staticmethod
    def _receipt_status(state: str) -> str:
        return {
            INBOUND_SETTLED: CLAIM_OUTCOME_DUPLICATE,
            INBOUND_IGNORED: CLAIM_OUTCOME_IGNORED,
            INBOUND_OBSERVED: CLAIM_OUTCOME_OBSERVED,
            INBOUND_AUTHORIZED: CLAIM_OUTCOME_AUTHORIZED,
        }.get(state, CLAIM_OUTCOME_IN_PROGRESS)

    async def _judged_to_respond(
        self, item: dict[str, Any], *, owner: str, generation: int,
    ) -> bool:
        """Decide a judge-selected message once; True continues to its turn.

        Runs before a session or conversation exists, so a declined message
        leaves nothing behind but its retained context. A judge error raises
        and the drive fails with backoff; it is never read as a decision.
        """
        item_id = str(item["item_id"])
        judgment = item.get("judgment")
        if not isinstance(judgment, dict):
            current = await self._channel_repo.get_inbound(item_id) or item
            if current.get("explicit_attention"):
                judgment = {"respond": True, "reason": "explicit_attention", "judge": None}
            else:
                from astrabox.core.service.orchestrator.channel_response_judge import (
                    active_channel_response_judge,
                )

                judge = active_channel_response_judge()
                if judge is None:
                    raise RuntimeError("no channel response judge is available")
                decision = await asyncio.wait_for(
                    judge.judge(context=self._response_context(item, await (
                        self._channel_repo.collect_unsubmitted_context(item)
                    ))),
                    timeout=_RESPONSE_JUDGE_TIMEOUT_SECONDS,
                )
                judgment = {
                    "respond": decision.respond, "reason": decision.reason[:500],
                    "judge": judge.name,
                }
            judgment["judged_at"] = utcnow_iso()
            if not await self._channel_repo.record_inbound_judgment(
                item_id=item_id, owner_token=owner, generation=generation, judgment=judgment,
            ):
                return False  # lost ownership; the successor reads or makes the decision
            item["judgment"] = judgment
        if judgment.get("respond") is True:
            return True
        if await self._channel_repo.suppress_judged_inbound(
            item_id=item_id, owner_token=owner, generation=generation,
        ) is not None:
            logger.info(
                "channel message kept as context by response judgment: item=%s reason=%s",
                item_id, judgment.get("reason"),
            )
            return False
        # The suppression lost to a copy that addressed the agent meanwhile.
        current = await self._channel_repo.get_inbound(item_id) or {}
        return bool(current.get("explicit_attention")) and current.get("owner_token") == owner

    @staticmethod
    def _response_context(
        item: dict[str, Any], context: list[dict[str, Any]],
    ) -> ChannelResponseContext:
        payload = item["payload"]
        policy = dict(payload.get("response_policy") or {})
        return ChannelResponseContext(
            deployment_id=str(item["deployment_id"]),
            channel_name=str(item.get("channel_name") or ""),
            conversation=str(payload["conversation"]),
            message=ChannelContextMessage(
                participant=str(payload.get("participant") or ""),
                content=str(payload.get("reply_text") or ""),
                timestamp=str(payload.get("message_timestamp") or ""),
            ),
            policy=policy["mode"],
            instruction=str(policy.get("instruction") or ""),
            binding_revision=str(payload.get("binding_revision") or ""),
            selection_reason=str(payload.get("selection_reason") or ""),
            participated=bool(payload.get("participated")),
            context=tuple(
                ChannelContextMessage(
                    participant=str(row.get("participant") or ""),
                    content=str(row.get("content") or ""),
                    timestamp=str(row.get("timestamp") or ""),
                )
                for row in context
            ),
        )

    async def _classify(
        self, binding: dict[str, Any], inbound: ChannelInbound, deployment_id: str,
    ) -> _Classification:
        """Decide the state an inbound is recorded in, before it is written.

        The decision is persisted with the record, so a process lost between
        the write and any later step cannot leave an unaddressed message for
        recovery to drive.
        """
        refused = self._participant_refusal(binding, inbound)
        if refused is not None:
            return _Classification(INBOUND_IGNORED, refused, excluded=True)
        provider_reason = str(inbound.ignore_reason or "").strip()
        if provider_reason:
            return _Classification(INBOUND_IGNORED, provider_reason)
        if inbound.signal == SIGNAL_OBSERVATION:
            return _Classification(INBOUND_OBSERVED)
        if inbound.signal == SIGNAL_AUTHORIZATION:
            return _Classification(INBOUND_AUTHORIZED)
        if inbound.action:
            pressed = parse_action(str(inbound.action))
            if pressed is None or await self._channel_repo.get_channel_interaction(
                deployment_id=deployment_id, ref=pressed.ref,
            ) is None:
                # A press on a button this platform never showed has no
                # question to answer and must not start a conversation.
                return _Classification(INBOUND_IGNORED, "unknown_interaction_action")
            return _Classification(INBOUND_RECEIVED)
        if self._attention_ignore_reason(binding, inbound) is not None:
            if self._binding_policy(binding) in RESPONSE_POLICIES and inbound.retain_context:
                return _Classification(INBOUND_RECEIVED, judge_candidate=True)
            return _Classification(INBOUND_OBSERVED)
        return _Classification(INBOUND_RECEIVED)

    async def _message_conversation(
        self, inbound: ChannelInbound, *, deployment_id: str, wait: bool,
    ) -> str | None:
        """The conversation a message continues, decided before its record is written.

        It is the message's ``conversation_key``, unless the provider asked it
        to continue the conversation of the message its reference names
        (``conversation_from_reference``) and core recorded that message. An
        authorization takes the conversation its observation recorded, and a
        button press the conversation that showed it, so neither has one of
        its own. ``wait`` lets a reply that will run wait briefly for the
        receipt of the delivery it quotes.
        """
        if inbound.signal == SIGNAL_AUTHORIZATION or inbound.action:
            return None
        if inbound.conversation_from_reference:
            joined = await self._referenced_conversation(
                deployment_id, str(inbound.reference), wait=wait,
            )
            if joined:
                return joined
        return inbound.conversation_key

    async def _referenced_conversation(
        self, deployment_id: str, reference: str, *, wait: bool,
    ) -> str | None:
        """The conversation of a message core recorded, or ``None``.

        A reply core delivered names its conversation through its alias, and
        an inbound message through the conversation recorded with it.
        """
        attempts = _REFERENCE_RESOLVE_ATTEMPTS if wait else 1
        for attempt in range(attempts):
            alias = await self._channel_repo.resolve_alias(
                deployment_id=deployment_id, alias=reference,
            )
            if alias is not None and alias.get("conversation_key"):
                return str(alias["conversation_key"])
            recorded = await self._channel_repo.get_message_record(
                deployment_id=deployment_id, dedup_key=reference,
            )
            conversation = ((recorded or {}).get("payload") or {}).get("conversation")
            if conversation:
                return str(conversation)
            if attempt + 1 < attempts:
                await asyncio.sleep(_REFERENCE_RESOLVE_WAIT_SECONDS)
        return None

    @staticmethod
    def _prefixed(prefix: str, content: str) -> str:
        return f"{prefix}\n\n{content}" if prefix else content

    @staticmethod
    def _participant_refusal(
        binding: dict[str, Any], inbound: ChannelInbound
    ) -> str | None:
        """Refuse senders outside the binding's ``allowed_participants``.

        An empty list admits every sender. A refused message never reaches the
        target and never joins a later turn's retained context.
        """
        allowed = binding.get("allowed_participants") or []
        if not allowed or str(inbound.participant or "") in allowed:
            return None
        return "participant is not allowed by this binding"

    @staticmethod
    def _attention_ignore_reason(
        binding: dict[str, Any], inbound: ChannelInbound
    ) -> str | None:
        """Deterministic respond/ignore policy from typed signals.

        ``all`` (default) responds to everything. ``mentions`` requires the
        provider to have reported at least one attention signal — the Claude
        Tag "a mention starts the thread" behavior. Providers signal; core
        decides. A message the policy does not address is kept as retained
        context, so a later signal that addresses it can still run it.
        """
        policy = ChannelIngressService._binding_policy(binding)
        if policy not in _ADDRESSED_ONLY_POLICIES:
            return None
        if inbound.attention is not None and inbound.attention.any_signal:
            return None
        return f"attention_policy={policy} with no attention signal"

    @staticmethod
    def _binding_policy(binding: dict[str, Any]) -> str:
        return str(binding.get("attention_policy") or ATTENTION_POLICY_ALL).strip().lower()

    async def _response_selection(
        self, binding: dict[str, Any], payload: dict[str, Any], deployment_id: str,
    ) -> dict[str, Any] | None:
        """Freeze a judge candidate's selection, or None to keep it as context.

        Under ``participated`` a topic the agent has not answered in yet is
        only context; the judge is not asked. The policy and its instruction
        are frozen with the message, so a later edit does not change how it
        is judged on recovery.
        """
        conversation = payload.get("conversation")
        revision = payload.get("binding_revision")
        if not conversation or not revision:
            return None
        policy = self._binding_policy(binding)
        participated = await self._channel_repo.topic_has_participation(
            deployment_id=deployment_id, conversation=str(conversation),
            binding_revision=str(revision),
        )
        if policy == RESPONSE_POLICY_PARTICIPATED and not participated:
            return None
        return {
            "selection": "judge",
            "selection_reason": "participated_topic" if participated else "proactive_policy",
            "participated": participated,
            "response_policy": {
                "mode": policy,
                "instruction": str(binding.get("response_instruction") or ""),
            },
        }

    # ── dispatch (invariant B + attach-not-append) ───────────────────────

    async def _require_item_environment_access(self, item: dict[str, Any]) -> None:
        """Authorize the captured execution account before judging or answering."""
        payload = dict(item.get("payload") or {})
        if payload.get("execution_mode") == EXECUTION_LINKED and not payload.get("execution_user_id"):
            target = await self._targets.resolve(item)
            if target is None:
                raise APIError(code="NOT_FOUND", message="bound agent not found", status_code=404)
            payload = await self._capture_linked_account(item, payload, target)
        from astrabox.core.service.environment_access import require_environment_access

        await require_environment_access(
            UserContext(user_id=str(payload.get("creator_user_id") or "")),
            operation="channel input",
        )

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
        target = await self._targets.resolve(item)
        if target is None:
            raise APIError(
                code="NOT_FOUND", message="bound agent not found", status_code=404
            )
        if payload.get("execution_mode") == EXECUTION_LINKED and not payload.get(
            "execution_user_id"
        ):
            payload = await self._capture_linked_account(item, payload, target)
        user = UserContext(user_id=str(payload.get("creator_user_id") or ""))
        deployment_id = str(item.get("deployment_id") or "")
        conversation_key = str(payload.get("conversation") or "")
        execution_user_id = str(payload.get("execution_user_id") or "")
        referenced_session = ""
        pressed = parse_action(str(payload.get("action") or ""))
        if pressed is not None:
            # A button press belongs to the conversation that showed it.
            record = await self._channel_repo.get_channel_interaction(
                deployment_id=deployment_id, ref=pressed.ref,
            )
            if record is not None:
                referenced_session = str(record.get("session_id") or "")
                conversation_key = str(record.get("conversation_key") or "")
                await self._require_account_of(referenced_session, user)
        reference = str(payload.get("reference") or "")
        if reference:
            if conversation_key:
                # An explicit channel key already selects the conversation;
                # only known delivered aliases need an ownership check here.
                resolved = await self._channel_repo.resolve_alias(
                    deployment_id=deployment_id, alias=reference,
                )
            else:
                resolved = await self._resolve_reference(deployment_id, reference)
            if resolved is not None:
                if not conversation_key:
                    conversation_key = str(resolved.get("conversation_key") or "")
                referenced_session = str(resolved.get("session_id") or "")
                await self._require_account_of(referenced_session, user)
            else:
                # A reply to another participant's message that ran continues
                # the conversation it ran in; that conversation has an account.
                recorded = await self._channel_repo.get_message_record(
                    deployment_id=deployment_id, dedup_key=reference,
                )
                ran_in = str((recorded or {}).get("session_id") or "")
                if ran_in:
                    await self._require_account_of(ran_in, user)
        if conversation_key:
            session_id = await self._resolve_conversation_session(
                user=user,
                target=target,
                deployment_id=deployment_id,
                conversation_key=conversation_key,
                execution_user_id=execution_user_id,
            )
        elif referenced_session:
            # The referenced delivery was a one-shot session: continue it.
            session_id = referenced_session
        else:
            started = await self._start_channel_conversation(
                target, user, execution_user_id,
            )
            session_id = str(started.get("session_id") or "")
        if session_id:
            session = await self._sessions_repo.get_session(session_id)
            if session is None or str(session.get("user_id") or "") != user.user_id:
                raise APIError(
                    code="CHANNEL_SESSION_OWNER_MISMATCH",
                    message="the channel conversation belongs to another execution account",
                    status_code=409,
                )
            await self._channel_repo.bind_inbound_dispatch(
                item_id=str(item["item_id"]),
                owner_token=str(item["owner_token"]),
                generation=int(item["generation"]),
                session_id=session_id,
                conversation_key=conversation_key or None,
            )
            item["session_id"] = session_id
            if conversation_key:
                item["resolved_conversation_key"] = conversation_key
        return session_id

    async def _capture_linked_account(
        self, item: dict[str, Any], payload: dict[str, Any], target: ConversationTarget,
    ) -> dict[str, Any]:
        """Resolve and keep the account a linked binding's message runs as.

        It is resolved once, at the message's first drive, and written onto
        the record before any session is chosen; recovery, a steal and a
        redelivery run the message as that account. A message whose
        participant has no linked account, or whose account cannot use the
        Agent, is refused: it never runs as the Agent's owner instead.
        """
        participant = str(payload.get("participant") or "").strip()
        if not participant:
            raise APIError(
                code="CHANNEL_PARTICIPANT_REQUIRED",
                message="a message runs as its participant's linked account, and it names none",
                status_code=409,
            )
        account = str(await channel_account_resolver().resolve(
            deployment_id=str(item.get("deployment_id") or ""),
            channel=str(item.get("channel_name") or ""),
            participant=participant,
        ) or "").strip()
        if not account:
            raise APIError(
                code="CHANNEL_PARTICIPANT_NOT_LINKED",
                message=f"participant {participant!r} is linked to no account on this binding",
                status_code=403,
            )
        if not await self._targets.may_start_as(target, account):
            raise APIError(
                code="CHANNEL_ACCOUNT_NOT_AUTHORIZED",
                message="the participant's linked account cannot use this agent",
                status_code=403,
            )
        captured = await self._channel_repo.capture_inbound_account(
            item_id=str(item["item_id"]), owner_token=str(item["owner_token"]),
            generation=int(item["generation"]), user_id=account,
        )
        if captured is None:
            raise RuntimeError(
                f"channel item {item['item_id']} lost its dispatch ownership before "
                "its account was kept"
            )
        item["payload"] = captured
        return dict(captured)

    async def _require_account_of(self, session_id: str, user: UserContext) -> None:
        """Refuse input that would continue another account's conversation."""
        referenced = await self._sessions_repo.get_session(session_id)
        if referenced is None or str(referenced.get("user_id") or "") != user.user_id:
            raise APIError(
                code="CHANNEL_REFERENCE_OWNER_MISMATCH",
                message="the referenced conversation belongs to another execution account",
                status_code=409,
            )

    async def _refuse_item(self, item: dict[str, Any], code: str) -> bool:
        """Record a refusal the participant can act on, and tell them why.

        The message leaves retained context. Only the worker whose write
        records the refusal sends the notice, so a redelivery is not told
        twice. Returns whether this worker recorded it.
        """
        recorded = await self._channel_repo.mark_inbound_ignored(
            item_id=str(item["item_id"]), owner_token=str(item["owner_token"]),
            generation=int(item["generation"]), reason=code, exclude_from_context=True,
        )
        if recorded:
            try:
                await self._notify(item, _REFUSAL_NOTICES[code])
            except Exception:
                # The refusal is durable; a notice the platform rejects must
                # not turn it into a redelivered, re-refused message.
                logger.exception("channel refusal notice failed: item=%s reason=%s", item["item_id"], code)
        return recorded

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

        The invariant-bearing order: begin dispatch (fence check) → resolve
        the Session → conversation lock → subscribe the reply destination →
        answer or stop the open question → send the message once → settle
        (fence check — a stolen item stops at that check and lets the
        successor settle it). The drive's item lease and conversation lock
        are renewed on a timer for as long as it runs
        (:meth:`_renew_drive_leases`).

        Sending does not wait for the reply: the Session accepts the message
        whatever its runtime is doing, its runtime answers it in order, and
        the reply reaches the channel through the Session's output
        subscription.
        """
        item_id = str(item["item_id"])
        owner = str(item["owner_token"])
        generation = int(item["generation"])
        payload = dict(item.get("payload") or {})
        leases = _DriveLeases(
            item_id=item_id, owner=owner, generation=generation,
            deployment_id=str(item.get("deployment_id") or ""),
        )
        heartbeat: asyncio.Task[None] | None = None
        try:
            if not await self._channel_repo.begin_inbound_dispatch(
                item_id=item_id, owner_token=owner, generation=generation
            ):
                return  # lost ownership before starting — successor owns it
            heartbeat = asyncio.create_task(
                self._renew_drive_leases(leases), name=f"channel-drive-leases-{item_id}",
            )
            try:
                await self._require_item_environment_access(item)
                if payload.get("selection") == "judge" and not await self._judged_to_respond(
                    item, owner=owner, generation=generation,
                ):
                    return
                session_id = await self._resolve_item_session(item)
            except APIError as exc:
                if exc.code not in _REFUSAL_NOTICES:
                    raise
                await self._refuse_item(item, exc.code)
                return
            # A linked binding's message keeps its account on the record now.
            payload = dict(item.get("payload") or {})
            if not session_id:
                await self._fail_item(item, "conversation start returned no session")
                return
            # Serialize on the conversation the session was resolved for. A
            # re-drive finds its session already bound and reads that key from
            # the item, so it takes the same lock as the first drive.
            conversation_key = str(item.get("resolved_conversation_key") or "") or None
            if conversation_key is not None:
                execution_user_id = str(payload.get("execution_user_id") or "")
                lock_generation = await self._acquire_conversation_lock_blocking(
                    leases.deployment_id, conversation_key, owner,
                    execution_user_id=execution_user_id,
                )
                if lock_generation is None:
                    # Another message of the conversation is being answered,
                    # stopped or sent. Waiting for it is not a failure: the item
                    # hands its lease back with its retries intact, and the
                    # reconciler drives it again.
                    await self._channel_repo.release_inbound_lease(
                        item_id=item_id, owner_token=owner, generation=generation,
                    )
                    return
                leases.hold_conversation(conversation_key, execution_user_id, lock_generation)

            reply_context = payload.get("reply_context")
            if isinstance(reply_context, dict) and reply_context:
                output_seq, _cursor = await self._facts.resume_point(session_id)
                await self._channel_repo.subscribe_to_output(
                    session_id=session_id, deployment_id=leases.deployment_id,
                    channel_name=str(item["channel_name"]),
                    reply_context=dict(reply_context), conversation_key=conversation_key,
                    after_seq=output_seq,
                    streaming=self._provider_streams(str(item["channel_name"])),
                )
            user = UserContext(user_id=str(payload.get("creator_user_id") or ""))
            sent = await self._facts.input_for_client_message(session_id, item_id)
            if sent is None:
                # Fence the question side effects too: a worker that lost
                # ownership while waiting must not answer or stop this
                # Session's question.
                if not await self._channel_repo.renew_inbound_lease(
                    item_id=item_id, owner_token=owner, generation=generation
                ):
                    return
                if await self._settle_pending_interaction(
                    item, user=user, session_id=session_id,
                ):
                    # The message answered a question: it is not sent.
                    if payload.get("retain_context"):
                        await self._channel_repo.exclude_inbound_from_context(item)
                    await self._channel_repo.settle_inbound(
                        item_id=item_id,
                        owner_token=owner,
                        generation=generation,
                        session_id=session_id,
                    )
                    return
                # The last fence before the message is sent: a renew that
                # misses means the item was stolen mid-wait, and the new owner
                # sends it. A successful renew holds the lease for a full TTL,
                # so no steal can occur between this write and the send.
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
                await self._session_runtime.send_as(
                    session_id,
                    content,
                    author_user_id=user.user_id,
                    source="channel",
                    client_message_id=item_id,
                )
            if not await self._channel_repo.settle_inbound(
                item_id=item_id,
                owner_token=owner,
                generation=generation,
                session_id=session_id,
                submit_context=bool(payload.get("retain_context")),
            ):
                return  # The successor owns input settlement.
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
            # A renewal still in flight after the release changes nothing: it
            # matches the owner and generation the release cleared.
            if heartbeat is not None:
                heartbeat.cancel()
            if leases.lock_generation is not None and leases.conversation_key is not None:
                with contextlib.suppress(Exception):
                    await self._channel_repo.release_conversation_lock(
                        deployment_id=leases.deployment_id,
                        conversation_key=leases.conversation_key,
                        owner=owner,
                        generation=leases.lock_generation,
                        execution_user_id=leases.execution_user_id,
                    )

    async def _renew_drive_leases(self, leases: _DriveLeases) -> None:
        """Renew a drive's item lease, and its conversation lock once held, until cancelled.

        A drive can wait on a response judge, a conversation lock or a slow
        provider notice, so renewal follows a clock rather than the drive's
        progress. Once the item lease is another worker's, the drive holds
        nothing and renewal stops; the drive's own fences stop it before it
        changes anything that worker owns.
        """
        while True:
            await asyncio.sleep(LEASE_RENEW_SECONDS)
            try:
                if not await self._channel_repo.renew_inbound_lease(
                    item_id=leases.item_id, owner_token=leases.owner,
                    generation=leases.generation,
                ):
                    logger.warning(
                        "channel drive stopped renewing item=%s: it settled or another worker holds it",
                        leases.item_id,
                    )
                    return
                if leases.conversation_key is None or leases.lock_generation is None:
                    continue
                if not await self._channel_repo.renew_conversation_lock(
                    deployment_id=leases.deployment_id,
                    conversation_key=leases.conversation_key,
                    owner=leases.owner,
                    generation=leases.lock_generation,
                    execution_user_id=leases.execution_user_id,
                ):
                    logger.error(
                        "channel drive lost its conversation lock: item=%s conversation=%s",
                        leases.item_id, leases.conversation_key,
                    )
            except Exception:
                # The next beat renews again; both leases outlive several beats.
                logger.warning(
                    "channel drive lease renewal failed: item=%s", leases.item_id, exc_info=True,
                )

    async def _settle_pending_interaction(
        self,
        item: dict[str, Any],
        *,
        user: UserContext,
        session_id: str,
    ) -> bool:
        """Answer the question this conversation shows, or stop the response waiting on it.

        Returns True when the message was an answer, or an attempt at one that
        was refused with an explanation: it is settled here and never sent.
        Otherwise a response still waiting on a question is stopped
        (:meth:`_supersede_question`) and the message goes on as the next
        input. The drive runs this only while the Session holds no input of
        this item, so re-driving a message already sent cannot stop the
        response it started.
        """
        payload = dict(item.get("payload") or {})
        deployment_id = str(item.get("deployment_id") or "")
        pending = await self._pending_interaction(session_id)
        if item.get("answer_ref"):
            record = await self._channel_repo.get_channel_interaction(
                deployment_id=deployment_id, ref=str(item["answer_ref"]),
            )
            if record is not None and await self._resume_answer(
                item, user=user, record=record, pending=pending,
            ):
                return True
            if record is not None and (
                record.get("state") != INTERACTION_OPEN
                or pending is None
                or str(pending.get("interaction_id") or "") != str(record.get("interaction_id") or "")
            ):
                # This message was read as an answer to a question that has
                # since closed; it must not answer, or stop, whatever is
                # pending now.
                await self._notify(item, "This question is no longer open. Send a new message to continue.")
                return True
        if payload.get("action"):
            target = parse_action(str(payload["action"]))
            if target is None:
                raise RuntimeError("channel action was not minted by this platform")
            record = await self._channel_repo.get_channel_interaction(
                deployment_id=deployment_id, ref=target.ref,
            )
            if record is None or not await self._question_is_open(item, record, session_id, pending):
                return True
            assert pending is not None
            if target.question_index != int(record.get("question_index") or 0):
                await self._notify(item, "That question was already answered.")
                return True
            return await self._take_answer(
                item, user=user, record=record, pending=pending,
                outcome=answer_action(pending, target=target), confirm=True,
            )
        if await self._answers_a_closed_question(item, pending):
            return True
        if pending is None:
            return False
        interaction_id = str(pending.get("interaction_id") or "")
        ref = interaction_ref(deployment_id, session_id, interaction_id)
        record = await self._channel_repo.get_channel_interaction(
            deployment_id=deployment_id, ref=ref,
        )
        if record is not None and record.get("state") == INTERACTION_OPEN:
            question_index = int(record.get("question_index") or 0)
            quoted = False
            reference = str(payload.get("reference") or "")
            if reference:
                alias = await self._channel_repo.resolve_alias(
                    deployment_id=deployment_id, alias=reference,
                )
                if alias is not None and alias.get("interaction_ref") == ref:
                    if int(alias.get("question_index") or 0) != question_index:
                        await self._notify(
                            item, "That question was already answered; reply to the latest one.",
                        )
                        return True
                    quoted = True
            outcome = read_reply(
                pending,
                question_index=question_index,
                text=str(payload.get("reply_text") or ""),
                quoted=quoted,
            )
            if outcome is not None:
                return await self._take_answer(
                    item, user=user, record=record, pending=pending,
                    outcome=outcome, confirm=False,
                )
        await self._supersede_question(user, session_id, interaction_id)
        if record is not None:
            await self._channel_repo.settle_channel_interaction(
                deployment_id=deployment_id, ref=ref, state=INTERACTION_CLOSED,
            )
        return False

    async def _pending_interaction(self, session_id: str) -> dict[str, Any] | None:
        """The public view of the question the Session's runtime waits on, or ``None``.

        The earliest open interaction nobody has answered yet: an interaction
        that already holds an answer waits only for the Session's reader to
        hand it over, and is no question to answer or stop.
        """
        activity = await self._facts.activity(session_id)
        for interaction in activity["open_interactions"]:
            if interaction.get("answer") is None:
                return public_interaction_view(dict(interaction["contract"]))
        return None

    async def _answers_a_closed_question(
        self, item: dict[str, Any], pending: dict[str, Any] | None,
    ) -> bool:
        """Refuse an answer that quotes a question which has closed.

        Quoting an old question with one of its option numbers or labels is an
        answer to that question, and running it as a new message would let the
        Agent act on a choice nobody can still make. Free text quoting it is an
        ordinary message.
        """
        payload = dict(item.get("payload") or {})
        reference = str(payload.get("reference") or "")
        if not reference:
            return False
        deployment_id = str(item.get("deployment_id") or "")
        alias = await self._channel_repo.resolve_alias(deployment_id=deployment_id, alias=reference)
        if alias is None or not alias.get("interaction_ref"):
            return False
        record = await self._channel_repo.get_channel_interaction(
            deployment_id=deployment_id, ref=str(alias["interaction_ref"]),
        )
        if record is None or not isinstance(record.get("view"), dict):
            return False
        if (
            record.get("state") == INTERACTION_OPEN
            and pending is not None
            and str(pending.get("interaction_id") or "") == str(record.get("interaction_id") or "")
        ):
            return False
        if read_reply(
            record["view"],
            question_index=int(alias.get("question_index") or 0),
            text=str(payload.get("reply_text") or ""),
            quoted=False,
        ) is None:
            return False
        await self._notify(item, "This question is no longer open. Send a new message to continue.")
        return True

    async def _question_is_open(
        self,
        item: dict[str, Any],
        record: dict[str, Any],
        session_id: str,
        pending: dict[str, Any] | None,
    ) -> bool:
        """Whether a pressed button's question can still be answered; explain if not."""
        if record.get("state") == INTERACTION_ANSWERED:
            await self._notify(item, "This question was already answered.")
            return False
        if str(record.get("session_id") or "") != session_id:
            # A button reached another conversation (a forwarded message, or
            # a replaced Session); the question it names is not this one's.
            await self._notify(item, "This question is no longer open.")
            return False
        if (
            record.get("state") != INTERACTION_OPEN
            or pending is None
            or str(pending.get("interaction_id") or "") != str(record.get("interaction_id") or "")
        ):
            await self._channel_repo.settle_channel_interaction(
                deployment_id=str(record["deployment_id"]), ref=str(record["ref"]),
                state=INTERACTION_CLOSED,
            )
            await self._notify(item, "This question is no longer open.")
            return False
        return True

    async def _resume_answer(
        self,
        item: dict[str, Any],
        *,
        user: UserContext,
        record: dict[str, Any],
        pending: dict[str, Any] | None,
    ) -> bool:
        """Finish an answer a previous drive of this item began; False if it began nothing."""
        payload = dict(item.get("payload") or {})
        if record.get("answering_item") == str(item["item_id"]):
            return await self._finish_answer(
                item, user=user, record=record, pending=pending,
                confirm=bool(payload.get("action")), redrive=True,
            )
        marked = int(item.get("answer_question_index") or 0)
        current = int(record.get("question_index") or 0)
        if current <= marked:
            return False
        # The step was kept; its follow-up question may not have been sent.
        if (
            record.get("state") == INTERACTION_OPEN
            and pending is not None
            and str(pending.get("interaction_id") or "") == str(record.get("interaction_id") or "")
            and current < question_count(pending)
        ):
            await self._show_question(item, record, pending, current)
        return True

    async def _take_answer(
        self,
        item: dict[str, Any],
        *,
        user: UserContext,
        record: dict[str, Any],
        pending: dict[str, Any],
        outcome: QuestionAnswer | Refusal,
        confirm: bool,
    ) -> bool:
        """Keep one question's answer; once every question has one, answer the engine."""
        if isinstance(outcome, Refusal):
            await self._notify(item, outcome.message)
            return True
        deployment_id = str(record["deployment_id"])
        ref = str(record["ref"])
        participant = dict(item.get("payload") or {}).get("participant")
        question_index = int(record.get("question_index") or 0)
        if not await self._channel_repo.mark_inbound_answer(
            item_id=str(item["item_id"]),
            owner_token=str(item["owner_token"]),
            generation=int(item["generation"]),
            interaction_ref=ref,
            question_index=question_index,
        ):
            return True  # Lost ownership; the successor re-drives this item.
        kept = {"row": outcome.row, "choice": outcome.choice, "participant": participant}
        if question_index + 1 < question_count(pending):
            if not await self._channel_repo.record_channel_answer(
                deployment_id=deployment_id, ref=ref, question_index=question_index,
                answer=kept,
            ):
                await self._notify(item, "That question was already answered.")
                return True
            await self._show_question(item, record, pending, question_index + 1)
            return True
        if not await self._channel_repo.stage_channel_answer(
            deployment_id=deployment_id, ref=ref, item_id=str(item["item_id"]),
            question_index=question_index, answer=kept,
        ):
            await self._notify(item, "That question was already answered.")
            return True
        staged = await self._channel_repo.get_channel_interaction(
            deployment_id=deployment_id, ref=ref,
        )
        if staged is None:
            raise RuntimeError(f"channel interaction {ref} vanished after its answer was staged")
        return await self._finish_answer(
            item, user=user, record=staged, pending=pending, confirm=confirm, redrive=False,
        )

    async def _finish_answer(
        self,
        item: dict[str, Any],
        *,
        user: UserContext,
        record: dict[str, Any],
        pending: dict[str, Any] | None,
        confirm: bool,
        redrive: bool,
    ) -> bool:
        """Store the staged answer for the runtime, unless it already has one.

        The answer goes through the Session's own answer path
        (:meth:`SessionRuntimeService.answer
        <astrabox.core.service.orchestrator.session_runtime.service.SessionRuntimeService.answer>`),
        which checks it against the interaction's contract and stores it for
        the Session's reader to hand over. A re-drive may follow a worker that
        died after the answer was stored: the interaction then holds an answer
        or is closed, and both are read as that acceptance. A re-drive cannot
        tell it apart from the question closing before the first attempt
        stored anything; that needs a second fault in the same window, and the
        Session's own interaction record stays authoritative.
        """
        deployment_id = str(record["deployment_id"])
        ref = str(record["ref"])
        # Every question's answer, the staged last one included, in question order.
        answers = [
            QuestionAnswer(row=dict(kept.get("row") or {}), choice=kept.get("choice"))
            for kept in record.get("answers") or []
        ]
        interaction_id = str(record.get("interaction_id") or "")
        if pending is not None and str(pending.get("interaction_id") or "") == interaction_id:
            try:
                await self._session_runtime.answer(
                    user, str(record["session_id"]), interaction_id,
                    engine_answer(pending, answers),
                )
            except APIError as exc:
                if exc.code not in _ANSWER_GONE_CODES:
                    raise
                if not redrive:
                    await self._channel_repo.settle_channel_interaction(
                        deployment_id=deployment_id, ref=ref, state=INTERACTION_CLOSED,
                    )
                    await self._notify(
                        item, "This question is no longer open. Send a new message to continue.",
                    )
                    return True
        await self._channel_repo.settle_channel_interaction(
            deployment_id=deployment_id, ref=ref, state=INTERACTION_ANSWERED,
        )
        if confirm and pending is not None:
            await self._notify(item, answered_summary(pending, answers))
        return True

    async def _show_question(
        self,
        item: dict[str, Any],
        record: dict[str, Any],
        pending: dict[str, Any],
        question_index: int,
    ) -> None:
        """Send the next question of a form and make its message a reply target."""
        channel_name = str(record.get("channel_name") or "")
        provider = get_channel(channel_name)
        shown = channel_interaction(
            pending, question_index=question_index, ref=str(record["ref"]),
            actions=provider.supports_interaction_actions,
        )
        receipt = await provider.deliver_outbound(
            reply_context=dict(record.get("reply_context") or {}),
            text=shown.text,
            binding=await self._delivery_binding(record),
            **self._interaction_argument(provider, shown),
        )
        if isinstance(receipt, ChannelDeliveryReceipt) and receipt.message_ids:
            await self._channel_repo.record_question_aliases(
                deployment_id=str(record["deployment_id"]),
                aliases=[str(alias) for alias in receipt.message_ids],
                conversation_key=record.get("conversation_key"),
                session_id=str(record["session_id"]),
                interaction_ref=str(record["ref"]),
                question_index=question_index,
            )

    async def _notify(self, item: dict[str, Any], text: str) -> None:
        """Tell the participant why their answer was not used, or what was answered."""
        payload = dict(item.get("payload") or {})
        reply_context = payload.get("reply_context")
        if not isinstance(reply_context, dict) or not reply_context:
            logger.info(
                "channel answer notice has no destination: item=%s notice=%s",
                item.get("item_id"), text,
            )
            return
        await get_channel(str(item["channel_name"])).deliver_outbound(
            reply_context=dict(reply_context),
            text=text,
            binding=await self._delivery_binding(item),
        )

    @staticmethod
    def _interaction_argument(
        provider: Any, shown: ChannelInteraction | None
    ) -> dict[str, Any]:
        """``interaction=`` only for a provider that declared it can take one."""
        if shown is None or not provider.supports_interaction_actions:
            return {}
        return {"interaction": shown}

    async def _supersede_question(
        self, user: UserContext, session_id: str, interaction_id: str,
    ) -> None:
        """Stop the response waiting on a question a newer message replaces.

        Someone who sends a new message instead of answering has moved on. An
        answer the platform made up would let the runtime continue on a guess
        before it reads the message, so the response is interrupted instead:
        the runtime withdraws the question and ends the response. The
        Session's reader hands the interrupt to the runtime before the input
        sent next, so the runtime reads the message after the stopped
        response. Raises when no reader is live to hand the interrupt over;
        the drive then fails and is retried once the Session's reader runs
        again.
        """
        if not interaction_id:
            raise RuntimeError("pending interaction has no interaction_id")
        stopped = await self._session_runtime.interrupt(user, session_id)
        if stopped.get("status") != "accepted":
            raise RuntimeError(
                f"session {session_id} has no live reader to stop the response "
                f"waiting on question {interaction_id}"
            )

    @staticmethod
    def _provider_streams(channel_name: str) -> bool:
        return bool(get_channel(channel_name).supports_streaming_delivery)

    async def _fail_item(self, item: dict[str, Any], error: str) -> None:
        """Record a failed drive: RECEIVED after its backoff, or DEAD once retries are spent."""
        state = await self._channel_repo.fail_inbound(
            item_id=str(item["item_id"]),
            owner_token=str(item["owner_token"]),
            generation=int(item["generation"]),
            error=error,
            backoff=_INBOUND_RETRY_BACKOFF_SECONDS,
        )
        logger.error(
            "channel drive failed: item=%s attempt=%d state=%s error=%s",
            item["item_id"], int(item.get("attempts") or 0) + 1, state or "held by another worker", error,
        )

    async def _start_channel_conversation(
        self, target: ConversationTarget, user: UserContext, execution_user_id: str,
    ) -> dict[str, Any]:
        if execution_user_id and execution_user_id != user.user_id:
            raise RuntimeError("channel execution identity differs from its admitted account")
        return await self._targets.start_conversation(
            target, user, source_type="channel", execution_account=bool(execution_user_id),
        )

    async def _acquire_conversation_lock_blocking(
        self, deployment_id: str, conversation_key: str, owner: str, *,
        execution_user_id: str = "",
    ) -> int | None:
        """Acquire the conversation lock, waiting (bounded) for another drive of it."""
        deadline_polls = max(
            1, int(_CONVERSATION_LOCK_WAIT_SECONDS / _CONVERSATION_LOCK_POLL_SECONDS)
        )
        for _ in range(deadline_polls):
            generation = await self._channel_repo.acquire_conversation_lock(
                deployment_id=deployment_id, conversation_key=conversation_key, owner=owner,
                execution_user_id=execution_user_id,
            )
            if generation is not None:
                return generation
            await asyncio.sleep(_CONVERSATION_LOCK_POLL_SECONDS)
        # Last try after the budget (a crashed holder's TTL may have lapsed).
        return await self._channel_repo.acquire_conversation_lock(
            deployment_id=deployment_id, conversation_key=conversation_key, owner=owner,
            execution_user_id=execution_user_id,
        )

    async def _resolve_conversation_session(
        self,
        *,
        user: UserContext,
        target: ConversationTarget,
        deployment_id: str,
        conversation_key: str,
        execution_user_id: str = "",
    ) -> str:
        """Route one channel conversation within its configured execution account."""
        mapping = await self._channel_repo.get_conversation(
            deployment_id=deployment_id, conversation_key=conversation_key,
            execution_user_id=execution_user_id,
        )
        mapped_session_id = str((mapping or {}).get("session_id") or "")
        if mapped_session_id:
            session = await self._sessions_repo.get_session(mapped_session_id)
            state = str((session or {}).get("state") or "")
            if (
                session is not None
                and str(session.get("user_id") or "") == user.user_id
                and state not in ("TERMINATED", "DELETED", "RECOVERY_REQUIRED")
            ):
                return mapped_session_id
            # Mapped session is gone: start a replacement and swap the mapping
            # if it still names the gone session; a racing replacement's winner
            # is read back and used.
            started = await self._start_channel_conversation(
                target, user, execution_user_id,
            )
            new_session_id = str(started.get("session_id") or "")
            swapped = await self._channel_repo.upsert_conversation(
                deployment_id=deployment_id,
                conversation_key=conversation_key,
                session_id=new_session_id,
                execution_user_id=execution_user_id,
                replaces_session_id=mapped_session_id,
            )
            if swapped is None:
                current = await self._channel_repo.get_conversation(
                    deployment_id=deployment_id, conversation_key=conversation_key,
                    execution_user_id=execution_user_id,
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

        started = await self._start_channel_conversation(
            target, user, execution_user_id,
        )
        new_session_id = str(started.get("session_id") or "")
        existing = await self._channel_repo.upsert_conversation(
            deployment_id=deployment_id,
            conversation_key=conversation_key,
            session_id=new_session_id,
            execution_user_id=execution_user_id,
        )
        winner = str((existing or {}).get("session_id") or new_session_id)
        if winner != new_session_id:
            logger.info(
                "channel conversation create lost race: binding=%s key=%s "
                "using winner session=%s",
                deployment_id, conversation_key, winner,
            )
        return winner

    # ── replies (invariant C) ────────────────────────────────────────────

    async def on_facts(self, session_id: str, batch: FactBatch) -> None:
        """Turn the replies a committed batch opens or completes into deliveries.

        The Session's reader calls this after each committed batch of its
        runtime's facts (``reader_hooks`` in
        ``astrabox/core/service/orchestrator/session_runtime/platform.py``).
        Only a batch whose output opens or closes a reply can change what a
        channel delivers; for those, every enabled binding subscribed to the
        Session projects the Session's output from its own position, and the
        deliveries that became due start at once. A batch this misses (the
        process died after the commit) is projected by the reconciler.
        """
        if not any(part.part.get("type") in REPLY_BOUNDARY_PARTS for part in batch.output):
            return
        for subscription in await self._channel_repo.session_output_subscriptions(session_id):
            try:
                due = await self._project_subscription(subscription)
            except Exception:
                # One binding's failure must not hold back another's replies;
                # the reconciler projects this subscription again.
                logger.exception(
                    "channel Session output failed binding=%s session=%s",
                    subscription["deployment_id"], session_id,
                )
                continue
            self._start_deliveries(due)

    async def _project_subscription(self, subscription: dict[str, Any]) -> list[dict[str, Any]]:
        """Write deliveries for the replies the Session recorded past the subscription's position.

        A reply's delivery row is written where its ``start`` part is
        recorded, so only replies that began after the binding subscribed
        reach it. A streaming destination's row is due at once and its
        delivery follows the reply as it grows. A final-text destination's
        row waits until the reply's ``finish`` is recorded; the reply is then
        due, or its row settles without delivery when it has neither text nor
        a question to send. The position moves after the rows exist, by
        compare-and-set, so a crash replays into the same rows and two
        projectors move it once.
        Returns the rows that became due.
        """
        session_id = str(subscription["session_id"])
        deployment_id = str(subscription["deployment_id"])
        await self._channel_repo.close_ended_channel_questions(
            deployment_id=deployment_id, session_id=session_id,
        )
        streaming = bool(subscription["streaming"])
        cursor = int(subscription["after_seq"])
        due: list[dict[str, Any]] = []
        while True:
            rows = await self._facts.read_output(session_id, after_seq=cursor, limit=_OUTPUT_PAGE)
            if not rows:
                return due
            for row in rows:
                response_id = str(row.get("response_id") or "")
                kind = row["part"].get("type")
                if not response_id or kind not in REPLY_BOUNDARY_PARTS:
                    continue
                seq = int(row["seq"])
                reply = await self._reply_through(session_id, response_id, seq)
                if reply is None or not reply.channel_delivery:
                    continue
                if kind == "start" and reply.first_seq == seq:
                    entry = await self._channel_repo.create_outbox_entry(
                        deployment_id=deployment_id,
                        channel_name=str(subscription["channel_name"]),
                        session_id=session_id,
                        response_id=reply.reply_id,
                        response_seq=reply.first_seq,
                        reply_context=dict(subscription["reply_context"]),
                        conversation_key=subscription.get("conversation_key"),
                        streaming=streaming,
                        due=streaming,
                    )
                    if streaming and entry["state"] == OUTBOX_PENDING:
                        due.append(entry)
                elif kind == "finish" and not streaming and reply.complete and reply.last_seq == seq:
                    if reply.text.strip() or reply.interaction_id is not None:
                        completed = await self._channel_repo.complete_reply(
                            deployment_id=deployment_id, session_id=session_id,
                            response_id=reply.reply_id,
                        )
                        if completed is not None:
                            due.append(completed)
                    else:
                        # Text-only destinations have nothing to send for a
                        # reply of only tool work.
                        await self._channel_repo.discard_reply(
                            deployment_id=deployment_id, session_id=session_id,
                            response_id=reply.reply_id,
                        )
            last = int(rows[-1]["seq"])
            if not await self._channel_repo.advance_output_subscription(
                deployment_id=deployment_id, session_id=session_id,
                expected_seq=cursor, sequence=last,
            ):
                # Another projector moved the position; it projects what follows.
                return due
            cursor = last
            if len(rows) < _OUTPUT_PAGE:
                return due

    async def _reply_through(
        self, session_id: str, response_id: str, seq: int,
    ) -> ChannelReply | None:
        """The last reply of a response, as its parts through output ``seq`` form it."""
        rows = await self._facts.output_for_responses(session_id, [response_id], through_seq=seq)
        replies = reply_segments(response_id, rows)
        return replies[-1] if replies else None

    def _start_deliveries(self, entries: list[dict[str, Any]]) -> None:
        """Deliver due rows in the background, each row once at a time in this process."""
        for entry in entries:
            identity = str(entry["outbox_id"])
            if identity in self._delivering:
                continue
            self._delivering.add(identity)
            self._spawn_background_task(
                self._run_delivery(entry), name=f"channel-delivery-{identity}",
            )

    async def _run_delivery(self, entry: dict[str, Any]) -> None:
        try:
            attempted = await self._deliver_outbox_row(entry)
        finally:
            self._delivering.discard(str(entry["outbox_id"]))
        if attempted:
            # The Session's later replies to this binding waited behind this one.
            self._start_deliveries(await self._channel_repo.list_pending_outbox(
                deployment_id=str(entry["deployment_id"]), session_id=str(entry["session_id"]),
            ))

    async def _deliver_outbox_row(self, entry: dict[str, Any]) -> bool:
        """At-least-once, exactly-one-owner reply delivery for one outbox row.

        Leases the row (fenced) and routes: streaming rows run a delivery
        session over the reply as the Session's output log grows; final-text
        rows post the complete reply's text. Every settlement write carries
        the fenced lease, so a deliverer that lost its lease mid-flight cannot
        complete or fail the successor's row. A failed attempt counts against
        the row's stored retry budget. Returns whether this call held the row.
        """
        outbox_id = str(entry.get("outbox_id") or "")
        owner = new_owner_token()
        claimed = await self._channel_repo.claim_outbox_for_delivery(outbox_id, owner=owner)
        if claimed is None:
            logger.info(
                "channel reply is not due or another deliverer holds it (outbox=%s); skipping",
                outbox_id,
            )
            return False
        generation = int(claimed["lease_generation"])
        if bool(claimed.get("streaming")):
            await self._deliver_streaming(claimed, owner=owner, generation=generation)
        else:
            await self._deliver_simple(claimed, owner=owner, generation=generation)
        return True

    async def _record_delivery_failure(
        self,
        entry: dict[str, Any],
        *,
        owner: str,
        generation: int,
        error: str,
        retry: bool = True,
    ) -> None:
        """Count a failed attempt: the row waits out its next backoff, or is DEAD."""
        state = await self._channel_repo.record_outbox_failure(
            str(entry["outbox_id"]),
            owner=owner,
            generation=generation,
            error=error,
            backoff=_OUTBOX_RETRY_BACKOFF_SECONDS,
            retry=retry,
        )
        if state == OUTBOX_DEAD:
            logger.error(
                "channel %s reply delivery gave up; outbox %s is DEAD: %s",
                entry.get("channel_name"), entry["outbox_id"], error,
            )
            return
        logger.warning(
            "channel %s reply delivery attempt %d failed (outbox=%s, now %s): %s",
            entry.get("channel_name"), int(entry.get("attempts") or 0) + 1,
            entry["outbox_id"], state or "held by another deliverer", error,
        )

    async def _persist_receipt(
        self,
        entry: dict[str, Any],
        *,
        owner: str,
        generation: int,
        known_aliases: list[str],
        receipt: ChannelDeliveryReceipt | None,
        opening: dict[str, Any] | None = None,
    ) -> list[str]:
        """Persist what a delivery created ahead of any completion write; the aliases now known.

        ``opening`` is the question the delivery showed (see
        :meth:`_reply_with_question`): it is recorded only now, once the
        platform accepted the message that shows it.
        """
        received = [str(alias) for alias in receipt.message_ids] if receipt is not None else []
        merged = list(dict.fromkeys([*known_aliases, *received]))
        # A streamed reply's question arrives on an update of a message its
        # earlier progress already recorded; it still needs its reply target.
        if merged != known_aliases or opening is not None:
            await self._channel_repo.record_delivery_aliases(
                str(entry["outbox_id"]),
                owner=owner,
                generation=generation,
                aliases=merged,
                interaction=opening,
            )
        return merged

    def _reply_reader(self, entry: dict[str, Any]) -> ReplyReader:
        return ReplyReader(
            self._facts,
            str(entry["session_id"]),
            reply_id=str(entry["response_id"]),
            first_seq=int(entry["response_seq"]),
        )

    async def _reply_with_question(
        self, entry: dict[str, Any], reply: ChannelReply, provider: Any,
    ) -> tuple[str, ChannelInteraction | None, dict[str, Any] | None]:
        """The reply's text and, when its response still waits on its question, that question.

        The question is shown only while the Session's runtime still waits on
        it and nobody answered it: one answered in the console, or stopped by
        a newer message before this delivery ran, would only invite an answer
        nobody can use. The third value is the question's record, written by
        :meth:`_persist_receipt` once a delivery showed it; a delivery that
        fails leaves no open question that nobody saw.
        """
        if reply.interaction_id is None:
            return reply.text, None, None
        deployment_id = str(entry.get("deployment_id") or "")
        session_id = str(entry.get("session_id") or "")
        interaction = await self._facts.get_interaction(session_id, reply.interaction_id)
        if (
            interaction is None
            or interaction["status"] != "open"
            or interaction.get("answer") is not None
        ):
            return reply.text, None, None
        view = public_interaction_view(dict(interaction["contract"]))
        assert view is not None
        ref = interaction_ref(deployment_id, session_id, reply.interaction_id)
        shown = channel_interaction(
            view, question_index=0, ref=ref, actions=provider.supports_interaction_actions,
        )
        text = reply.text.strip()
        opening = {"ref": ref, "interaction_id": reply.interaction_id, "view": view}
        return (f"{text}\n\n{shown.text}" if text else shown.text), shown, opening

    async def _deliver_simple(
        self, entry: dict[str, Any], *, owner: str, generation: int
    ) -> None:
        """One final-text delivery attempt: DELIVERED, or a counted failure."""
        outbox_id = str(entry["outbox_id"])
        channel_name = str(entry.get("channel_name") or "")
        try:
            reply = await self._reply_reader(entry).read()
            if not reply.complete:
                raise RuntimeError("final-text delivery requires a complete reply")
            provider = get_channel(channel_name)
            text, shown, opening = await self._reply_with_question(entry, reply, provider)
            text = text.strip()
            if not text:
                # The reply's only content was its question, and the question
                # closed before this delivery: nothing is left to send.
                await self._channel_repo.mark_outbox_delivered(
                    outbox_id, owner=owner, generation=generation
                )
                return
            receipt = await provider.deliver_outbound(
                reply_context=dict(entry.get("reply_context") or {}),
                text=text,
                binding=await self._delivery_binding(entry),
                **self._interaction_argument(provider, shown),
            )
            await self._persist_receipt(
                entry,
                owner=owner,
                generation=generation,
                known_aliases=[str(alias) for alias in (entry.get("delivery_aliases") or [])],
                receipt=receipt if isinstance(receipt, ChannelDeliveryReceipt) else None,
                opening=opening,
            )
            await self._channel_repo.mark_outbox_delivered(
                outbox_id, owner=owner, generation=generation
            )
        except Exception as exc:
            await self._record_delivery_failure(
                entry, owner=owner, generation=generation, error=f"{type(exc).__name__}: {exc}",
            )

    async def _deliver_streaming(
        self, entry: dict[str, Any], *, owner: str, generation: int
    ) -> None:
        """Render the reply as provider progress and settlement.

        Reply identity, text and completion come from the Session's output
        log, read as the runtime's parts are recorded; the channel owns only
        its lease, the transport receipts and whether the reply was opened on
        the platform (``stream_started``), so a resumed delivery continues the
        reply rather than opening a second one. The reply is opened once it
        has text, a question or a failure to show: a reply of only tool work
        shows nothing.
        """
        outbox_id = str(entry["outbox_id"])
        channel_name = str(entry.get("channel_name") or "")
        response_id = str(entry["response_id"])
        known_aliases = [str(alias) for alias in (entry.get("delivery_aliases") or [])]
        provider = get_channel(channel_name)
        try:
            handle = await provider.open_delivery(
                reply_context=dict(entry.get("reply_context") or {}),
                prior_aliases=list(known_aliases),
                binding=await self._delivery_binding(entry),
            )
        except Exception as exc:
            await self._record_delivery_failure(
                entry, owner=owner, generation=generation,
                error=f"open_delivery: {type(exc).__name__}: {exc}",
            )
            return

        reader = self._reply_reader(entry)
        seq = 0
        text_so_far = ""
        opened = bool(entry.get("stream_started"))
        started = time.monotonic()

        async def open_reply() -> None:
            nonlocal known_aliases, opened
            if opened:
                return
            receipt = await handle.emit(
                ChannelEvent(type=EVENT_TURN_STARTED, seq=seq, response_id=response_id)
            )
            known_aliases = await self._persist_receipt(
                entry, owner=owner, generation=generation,
                known_aliases=known_aliases, receipt=receipt,
            )
            await self._channel_repo.mark_stream_started(
                outbox_id, owner=owner, generation=generation,
            )
            opened = True

        try:
            while True:
                if time.monotonic() - started > _STREAMING_MAX_SECONDS:
                    # A reply still streaming after one session's budget goes
                    # on in a fresh delivery session; that is not a failure.
                    await self._channel_repo.release_outbox_lease(
                        outbox_id, owner=owner, generation=generation,
                    )
                    return
                if not await self._channel_repo.renew_outbox_lease(
                    outbox_id, owner=owner, generation=generation
                ):
                    return  # lease stolen — the successor resumes the delivery
                reply = await reader.read()
                if reply.text and reply.text != text_so_far:
                    await open_reply()
                    text_so_far = reply.text
                    seq += 1
                    receipt = await handle.emit(
                        ChannelEvent(
                            type=EVENT_PROGRESS, seq=seq, text=text_so_far,
                            response_id=response_id,
                        )
                    )
                    known_aliases = await self._persist_receipt(
                        entry, owner=owner, generation=generation,
                        known_aliases=known_aliases, receipt=receipt,
                    )
                if reply.complete and reply.error is not None:
                    await open_reply()
                    seq += 1
                    with contextlib.suppress(Exception):
                        await handle.emit(
                            ChannelEvent(
                                type=EVENT_FAILED, seq=seq, error=reply.error,
                                response_id=response_id,
                            )
                        )
                    await self._record_delivery_failure(
                        entry, owner=owner, generation=generation,
                        error="bound response failed", retry=False,
                    )
                    return
                if reply.complete:
                    final_text, shown, opening = await self._reply_with_question(
                        entry, reply, provider,
                    )
                    if not opened and not final_text.strip():
                        await self._channel_repo.mark_outbox_delivered(
                            outbox_id, owner=owner, generation=generation
                        )
                        return
                    await open_reply()
                    seq += 1
                    receipt = await handle.emit(
                        ChannelEvent(
                            type=EVENT_SETTLED, seq=seq, text=final_text,
                            response_id=response_id,
                            interaction=(
                                shown if provider.supports_interaction_actions else None
                            ),
                        )
                    )
                    known_aliases = await self._persist_receipt(
                        entry, owner=owner, generation=generation,
                        known_aliases=known_aliases, receipt=receipt, opening=opening,
                    )
                    await self._channel_repo.mark_outbox_delivered(
                        outbox_id, owner=owner, generation=generation
                    )
                    return
                await asyncio.sleep(_STREAMING_POLL_SECONDS)
        except Exception as exc:
            await self._record_delivery_failure(
                entry, owner=owner, generation=generation, error=f"{type(exc).__name__}: {exc}",
            )
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
            or binding.get("enabled") is False
            or actual_channel != expected_channel
        ):
            raise RuntimeError(
                f"channel delivery binding {deployment_id!r} is missing, disabled, "
                "or no longer matches its delivery row"
            )
        from astrabox.core.service.orchestrator.runtime.environment_ownership import require_environment_ownership

        require_environment_ownership(binding)
        return await self._channel_credentials.hydrate(
            binding, get_channel(expected_channel)
        )

    # ── recovery (the reconciler's entry points) ─────────────────────────

    async def project_session_outputs(self) -> int:
        """Project the subscriptions with work, one page per call; how many deliveries became due.

        Subscriptions are read in key order, a page per reconciler tick; a
        short page ends the pass and the next call starts over. A subscription
        has work when a shown question ended, its Session recorded output past
        its position (a reply boundary no consumer projected, or parts a consumer
        had no reason to read), or when its binding is disabled or its Session
        deleted, which ends it.
        """
        subscriptions = await self._channel_repo.list_output_subscriptions(
            after=self._output_subscription_cursor, limit=_OUTPUT_SUBSCRIPTION_PAGE,
        )
        self._output_subscription_cursor = (
            (str(subscriptions[-1]["deployment_id"]), str(subscriptions[-1]["session_id"]))
            if len(subscriptions) == _OUTPUT_SUBSCRIPTION_PAGE
            else None
        )
        count = 0
        for subscription in subscriptions:
            try:
                if not subscription.get("binding_enabled") or subscription.get("session_deleted"):
                    # A disabled binding delivers nothing. Re-enabled, it
                    # follows a Session again from that Session's next channel
                    # input, without replaying what the Session said meanwhile.
                    await self._channel_repo.remove_output_subscription(
                        deployment_id=str(subscription["deployment_id"]),
                        session_id=str(subscription["session_id"]),
                    )
                    continue
                due = await self._project_subscription(subscription)
            except Exception:
                logger.exception(
                    "channel Session output failed binding=%s session=%s",
                    subscription["deployment_id"], subscription["session_id"],
                )
                continue
            self._start_deliveries(due)
            count += len(due)
        return count

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
                item_id=str(stale["item_id"]),
                expected_generation=int(stale.get("generation") or 0),
            )
            if claimed is None:
                continue
            recovered += 1
            self._spawn_background_task(
                self._drive_work_item(claimed), name=f"channel-recovery-{claimed['item_id']}",
            )
        if recovered:
            logger.info("channel inbound recovery re-drove %d items", recovered)
        return recovered

    async def expire_authorizations(
        self, *, limit: int = 50, now: float | None = None,
    ) -> int:
        """Retire authorizations whose observation never arrived.

        The record dies with evidence and the participant who invoked the
        agent is told the message was not answered. A notice the platform
        rejects is logged; the durable DEAD record stays the operator's
        evidence. Returns how many authorizations expired.
        """
        expired = await self._channel_repo.expire_authorizations(limit=limit, now=now)
        for record in expired:
            logger.error(
                "channel authorization expired without its observation: item=%s binding=%s",
                record["item_id"], record.get("deployment_id"),
            )
            if record.get("recall") is not None:
                # The participant withdrew the message; nothing is owed to them.
                continue
            try:
                await self._notify(record, _AUTHORIZATION_EXPIRED_NOTICE)
            except Exception:
                logger.exception(
                    "channel authorization expiry notice failed: item=%s", record["item_id"],
                )
        return len(expired)

    async def sweep_pending_outbox(self, *, limit: int = 50) -> int:
        """Deliver due outbox rows nobody is delivering (boot + periodic recovery)."""
        entries = await self._channel_repo.list_pending_outbox(limit=limit)
        self._start_deliveries(entries)
        if entries:
            logger.info("channel outbox sweep processed %d due entries", len(entries))
        return len(entries)
