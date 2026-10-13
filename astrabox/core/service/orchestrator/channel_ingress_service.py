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
* Session output subscriptions create response-bound deliveries independently
  of input dispatch. The deliverer reads each exact response, never the
  session's latest message or an engine's task notifications.

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
from astrabox.core.service.orchestrator.conversation_targets import (
    ConversationTarget,
    ConversationTargets,
)
from astrabox.persistence.repository.channel_repository import (
    CLAIM_OUTCOME_AUTHORIZED,
    CLAIM_OUTCOME_CLAIMED,
    CLAIM_OUTCOME_DUPLICATE,
    CLAIM_OUTCOME_IGNORED,
    CLAIM_OUTCOME_IN_PROGRESS,
    CLAIM_OUTCOME_OBSERVED,
    INBOUND_AUTHORIZED,
    INBOUND_DEAD,
    INBOUND_IGNORED,
    INBOUND_OBSERVED,
    INBOUND_RECEIVED,
    INBOUND_SETTLED,
    INTERACTION_ANSWERED,
    INTERACTION_CLOSED,
    INTERACTION_OPEN,
    ChannelRepository,
    new_owner_token,
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

#: An answer refused with one of these found no wait left to answer: the
#: engine runtime is absent, its sandbox is gone, or the wait expired.
_ANSWER_GONE_CODES = frozenset(
    {"ENGINE_RUNTIME_UNAVAILABLE", "INTERACTION_EXPIRED", "SANDBOX_GONE"}
)

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
        answer_pending_interaction: Any,
        supersede_pending_interaction: Any,
        spawn_background_task: Any,
        channel_repo: ChannelRepository | None = None,
        channel_credentials: ChannelCredentialService | None = None,
        read_session_output: Any,
        session_output_cursor: Any,
        assistant_service_getter: Any = None,
        observe_session_output: Any = None,
    ) -> None:
        self._deployment_repo = deployment_repo
        self._targets = ConversationTargets(
            agent_repo=agent_repo,
            agent_service_getter=agent_service_getter,
            assistant_service_getter=assistant_service_getter,
        )
        self._stream_message_events_ds = stream_message_events_ds
        self._resume_command_stream = resume_command_stream
        self._sessions_repo = sessions_repo
        self._session_events_repo = session_events_repo
        self._message_view = message_view
        self._session_detail_getter = session_detail_getter
        self._answer_pending_interaction = answer_pending_interaction
        self._supersede_pending_interaction = supersede_pending_interaction
        self._spawn_background_task = spawn_background_task
        self._channel_repo = channel_repo or ChannelRepository()
        self._channel_credentials = channel_credentials or ChannelCredentialService()
        self._read_session_output = read_session_output
        self._observe_session_output = observe_session_output
        self._session_output_cursor = session_output_cursor
        self._output_subscription_cursor = ""
        self._delivering: set[str] = set()

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
            "attention": inbound.attention.to_dict() if inbound.attention else None,
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
            "signal": inbound.signal,
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
            await self._channel_repo.note_explicit_attention(item_id=str(item["_id"]))
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
            item_id=str(item["_id"]),
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
                name=f"channel-drive-{item['_id']}",
            )
            return ChannelIngressReceipt(
                status="accepted", item_id=str(item["_id"]), ack_extra=ack_extra,
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
                    status=CLAIM_OUTCOME_IGNORED, item_id=str(item["_id"]),
                    ack_extra={**ack_extra, "reason": exc.code},
                )
            raise
        self._spawn_background_task(
            self._drive_work_item(item),
            name=f"channel-drive-{item['_id']}",
        )
        logger.warning(
            "channel inbound accepted: binding=%s %s=%s session=%s item=%s",
            deployment_id, target.kind, target.target_id, session_id, item["_id"],
        )
        return ChannelIngressReceipt(
            status="accepted",
            item_id=str(item["_id"]),
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
        item_id = str(item["_id"])
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
        item_id = str(item["_id"])
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
                item_id=str(item["_id"]),
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
            item_id=str(item["_id"]), owner_token=str(item["owner_token"]),
            generation=int(item["generation"]), user_id=account,
        )
        if captured is None:
            raise RuntimeError(
                f"channel item {item['_id']} lost its dispatch ownership before "
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
            item_id=str(item["_id"]), owner_token=str(item["owner_token"]),
            generation=int(item["generation"]), reason=code, exclude_from_context=True,
        )
        if recorded:
            try:
                await self._notify(item, _REFUSAL_NOTICES[code])
            except Exception:
                # The refusal is durable; a notice the platform rejects must
                # not turn it into a redelivered, re-refused message.
                logger.exception("channel refusal notice failed: item=%s reason=%s", item["_id"], code)
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
            if payload.get("selection") == "judge" and not await self._judged_to_respond(
                item, owner=owner, generation=generation,
            ):
                return
            try:
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
                lock_generation = await self._acquire_conversation_lock_blocking(
                    deployment_id, conversation_key, owner,
                    execution_user_id=str(payload.get("execution_user_id") or ""),
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
            if has_reply:
                boundary = await self._session_output_cursor(session_id)
                await self._channel_repo.subscribe_to_output(
                    session_id=session_id, deployment_id=deployment_id,
                    channel_name=str(item["channel_name"]),
                    reply_context=dict(reply_context or {}), conversation_key=conversation_key,
                    after_seq=boundary,
                    streaming=self._provider_streams(str(item["channel_name"])),
                )
            token = _dispatch_token(item)
            command = await self._session_events_repo.find_command_by_client_message_id(
                session_id, client_message_id=token
            )
            drove = False
            if command is None:
                # Fence the interaction side effect too: a worker that lost
                # ownership while waiting must not answer this session's UI.
                if not await self._channel_repo.renew_inbound_lease(
                    item_id=item_id, owner_token=owner, generation=generation
                ):
                    return
                user = UserContext(user_id=str(payload.get("creator_user_id") or ""))
                if await self._settle_pending_interaction(
                    item, user=user, session_id=session_id,
                ):
                    # The message answered a question: no turn is started for it.
                    if payload.get("retain_context"):
                        await self._channel_repo.exclude_inbound_from_context(item)
                    await self._channel_repo.settle_inbound(
                        item_id=item_id,
                        owner_token=owner,
                        generation=generation,
                        session_id=session_id,
                    )
                    return
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
                ok = await self._drain_with_lease_renewal(agen, item)
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

            if payload.get("retain_context"):
                await self._channel_repo.mark_context_submitted(item)
            if not await self._channel_repo.settle_inbound(
                item_id=item_id,
                owner_token=owner,
                generation=generation,
                session_id=session_id,
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
            if lock_generation is not None and conversation_key is not None:
                with contextlib.suppress(Exception):
                    await self._channel_repo.release_conversation_lock(
                        deployment_id=deployment_id,
                        conversation_key=conversation_key,
                        owner=owner,
                        generation=lock_generation,
                        execution_user_id=str(payload.get("execution_user_id") or ""),
                    )

    async def _settle_pending_interaction(
        self,
        item: dict[str, Any],
        *,
        user: UserContext,
        session_id: str,
    ) -> bool:
        """Answer the question this conversation shows, or stop the turn waiting on it.

        Returns True when the message was an answer, or an attempt at one that
        was refused with an explanation: it is settled here and never becomes
        a turn. Otherwise a turn still parked on an interaction is stopped
        (``supersede_pending_interaction``) and the message goes on as the
        next input. Crash recovery checks the durable dispatch command before
        this runs, so re-driving an older item cannot stop a turn a newer
        message started.
        """
        payload = dict(item.get("payload") or {})
        deployment_id = str(item.get("deployment_id") or "")
        session = await self._session_detail_getter(user, session_id)
        pending = session.get("pending_interaction")
        pending = pending if isinstance(pending, dict) else None
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
        await self._supersede_parked_turn(
            user=user, session_id=session_id, interaction_id=interaction_id,
        )
        if record is not None:
            await self._channel_repo.settle_channel_interaction(
                deployment_id=deployment_id, ref=ref, state=INTERACTION_CLOSED,
                participant=payload.get("participant"),
            )
        return False

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
        if record.get("answering_item") == str(item["_id"]):
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
            item_id=str(item["_id"]),
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
                answers=[*(record.get("answers") or []), kept],
            ):
                await self._notify(item, "That question was already answered.")
                return True
            await self._show_question(item, record, pending, question_index + 1)
            return True
        if not await self._channel_repo.stage_channel_answer(
            deployment_id=deployment_id, ref=ref, item_id=str(item["_id"]),
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
        """Give the engine the staged answer, unless it already has it.

        A re-drive may follow a worker that died after the engine accepted the
        answer: the Session then has no pending interaction, or the engine
        refuses a second copy, and both are read as that acceptance. A re-drive
        cannot tell it apart from the question closing before the first
        attempt reached the engine; that needs a second fault in the same
        window, and the Session's own interaction record stays authoritative.
        """
        deployment_id = str(record["deployment_id"])
        ref = str(record["ref"])
        participant = dict(item.get("payload") or {}).get("participant")
        answers = [
            QuestionAnswer(row=dict(kept.get("row") or {}), choice=kept.get("choice"))
            for kept in [*(record.get("answers") or []), record.get("staged_answer") or {}]
        ]
        interaction_id = str(record.get("interaction_id") or "")
        if pending is not None and str(pending.get("interaction_id") or "") == interaction_id:
            try:
                await self._answer_pending_interaction(
                    user, str(record["session_id"]), interaction_id,
                    engine_answer(pending, answers),
                )
            except APIError as exc:
                gone = exc.code in _ANSWER_GONE_CODES
                if not gone and exc.code == "INVALID_REQUEST":
                    # The Session itself says whether the question is still
                    # waiting; a refusal for any other reason is a defect.
                    current = (
                        await self._session_detail_getter(user, str(record["session_id"]))
                    ).get("pending_interaction")
                    gone = (
                        not isinstance(current, dict)
                        or str(current.get("interaction_id") or "") != interaction_id
                    )
                if not gone:
                    raise
                if not redrive:
                    await self._channel_repo.settle_channel_interaction(
                        deployment_id=deployment_id, ref=ref, state=INTERACTION_CLOSED,
                        participant=participant,
                    )
                    await self._notify(
                        item, "This question is no longer open. Send a new message to continue.",
                    )
                    return True
        await self._channel_repo.settle_channel_interaction(
            deployment_id=deployment_id, ref=ref, state=INTERACTION_ANSWERED,
            participant=participant,
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
                item.get("_id"), text,
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

    async def _supersede_parked_turn(
        self,
        *,
        user: UserContext,
        session_id: str,
        interaction_id: str,
    ) -> None:
        """Stop the turn parked on an interaction and wait until it has settled.

        The platform interaction API owns both endings — stopped through a live
        engine, or abandoned once that engine is gone — and this spine only
        sequences one of them before dispatching the new command.
        """
        if not interaction_id:
            raise RuntimeError("pending interaction has no interaction_id")
        await self._supersede_pending_interaction(user, session_id, interaction_id)
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
                    f"session {session_id} became {state} while stopping the turn "
                    "its superseded channel question parked"
                )
            await asyncio.sleep(_SESSION_READY_POLL_SECONDS)
        raise RuntimeError(
            f"session {session_id} did not settle its superseded channel question "
            f"in {_SESSION_READY_TIMEOUT_SECONDS}s"
        )

    @staticmethod
    def _provider_streams(channel_name: str) -> bool:
        return bool(get_channel(channel_name).supports_streaming_delivery)

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
        """Acquire the conversation lock, waiting (bounded) for a prior turn."""
        deadline_polls = max(
            1, int(_SESSION_READY_TIMEOUT_SECONDS / _SESSION_READY_POLL_SECONDS)
        )
        for _ in range(deadline_polls):
            generation = await self._channel_repo.acquire_conversation_lock(
                deployment_id=deployment_id, conversation_key=conversation_key, owner=owner,
                execution_user_id=execution_user_id,
            )
            if generation is not None:
                return generation
            await asyncio.sleep(_SESSION_READY_POLL_SECONDS)
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
                await self._channel_repo.touch_conversation(
                    deployment_id=deployment_id, conversation_key=conversation_key,
                    execution_user_id=execution_user_id,
                )
                return mapped_session_id
            # Mapped session is gone: start a replacement and CAS-swap the
            # mapping; a racing replacement's winner is read back and used.
            started = await self._start_channel_conversation(
                target, user, execution_user_id,
            )
            new_session_id = str(started.get("session_id") or "")
            swapped = await self._channel_repo.upsert_conversation(
                deployment_id=deployment_id,
                conversation_key=conversation_key,
                session_id=new_session_id,
                **target.fields,
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
            **target.fields,
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
        interaction: ChannelInteraction | None = None,
    ) -> list[str]:
        """Merge + persist receipt aliases ahead of any completion write."""
        if receipt is None or not receipt.message_ids:
            return known_aliases
        merged = list(dict.fromkeys([*known_aliases, *[str(a) for a in receipt.message_ids]]))
        # A streamed reply's question arrives on an update of a message its
        # earlier progress already recorded; it still needs its reply target.
        if merged != known_aliases or interaction is not None:
            await self._channel_repo.record_delivery_aliases(
                str(entry["_id"]),
                owner=owner,
                generation=generation,
                aliases=merged,
                deployment_id=str(entry.get("deployment_id") or "") or None,
                conversation_key=str(entry.get("conversation_key") or "") or None,
                session_id=str(entry.get("session_id") or "") or None,
                interaction_ref=(
                    interaction_ref(
                        str(entry.get("deployment_id") or ""),
                        str(entry.get("session_id") or ""),
                        interaction.interaction_id,
                    )
                    if interaction is not None
                    else None
                ),
            )
        return merged

    async def _reply_with_question(
        self, entry: dict[str, Any], response: Any, provider: Any,
    ) -> tuple[str, ChannelInteraction | None]:
        """The reply's text and, when its turn is waiting on a question, that question.

        The question is shown only while the turn still waits on it: one
        already answered in the console, or stopped by a newer message before
        this delivery ran, would only invite an answer nobody can use.
        """
        view = response.interaction
        if view is None:
            return response.text, None
        deployment_id = str(entry.get("deployment_id") or "")
        session_id = str(entry.get("session_id") or "")
        interaction_id = str(view.get("interaction_id") or "")
        session = await self._sessions_repo.get_session(session_id)
        detail = await self._session_detail_getter(
            UserContext(user_id=str((session or {}).get("user_id") or "")), session_id,
        )
        pending = detail.get("pending_interaction")
        if not isinstance(pending, dict) or str(pending.get("interaction_id") or "") != interaction_id:
            return response.text, None
        ref = interaction_ref(deployment_id, session_id, interaction_id)
        await self._channel_repo.open_channel_interaction(
            deployment_id=deployment_id,
            ref=ref,
            session_id=session_id,
            interaction_id=interaction_id,
            conversation_key=str(entry.get("conversation_key") or "") or None,
            channel_name=str(entry.get("channel_name") or ""),
            reply_context=dict(entry.get("reply_context") or {}),
            view=view,
        )
        shown = channel_interaction(
            view, question_index=0, ref=ref, actions=provider.supports_interaction_actions,
        )
        text = response.text.strip()
        return (f"{text}\n\n{shown.text}" if text else shown.text), shown

    async def _deliver_simple(
        self, entry: dict[str, Any], *, owner: str, generation: int
    ) -> None:
        """Final-text delivery with capped inline retries, then DELIVERED/DEAD."""
        outbox_id = str(entry.get("_id") or "")
        channel_name = str(entry.get("channel_name") or "")
        session_id = str(entry.get("session_id") or "")
        turn_id = str(entry.get("turn_id") or "")

        response = await self._read_outbox_response(entry)
        if not response.complete:
            raise RuntimeError("simple delivery requires a completed Session response")
        provider = get_channel(channel_name)
        text, shown = await self._reply_with_question(entry, response, provider)
        text = text.strip()
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
                receipt = await provider.deliver_outbound(
                    reply_context=dict(entry.get("reply_context") or {}),
                    text=text,
                    binding=await self._delivery_binding(entry),
                    **self._interaction_argument(provider, shown),
                )
                known_aliases = await self._persist_receipt(
                    entry,
                    owner=owner,
                    generation=generation,
                    known_aliases=known_aliases,
                    receipt=receipt if isinstance(receipt, ChannelDeliveryReceipt) else None,
                    interaction=shown,
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

    async def _read_outbox_response(self, entry: dict[str, Any]) -> Any:
        batch = await self._read_session_output(
            str(entry["session_id"]), after_seq=int(entry["start_after_seq"]),
        )
        for response in batch.responses:
            if response.response_id == entry["response_id"]:
                return response
        raise RuntimeError("subscribed Session response is missing from its journal")

    async def _deliver_streaming(
        self, entry: dict[str, Any], *, owner: str, generation: int
    ) -> None:
        """Render the platform subscription as provider progress and settlement.

        Reply identity, reconstruction and completion come from Session output;
        the channel owns only its lease, transport receipt and delivery cursor.
        """
        outbox_id = str(entry.get("_id") or "")
        channel_name = str(entry.get("channel_name") or "")
        turn_id = str(entry.get("turn_id") or "")
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
            if cursor < 0:
                receipt = await handle.emit(
                    ChannelEvent(
                        type=EVENT_TURN_STARTED, seq=seq, turn_id=turn_id,
                        response_id=str(entry["response_id"]),
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
                response = await self._read_outbox_response(entry)
                if response.text != text_so_far:
                    text_so_far = response.text
                    seq += 1
                    receipt = await handle.emit(
                        ChannelEvent(
                            type=EVENT_PROGRESS, seq=seq, text=text_so_far,
                            turn_id=turn_id,
                            response_id=str(entry["response_id"]),
                        )
                    )
                    known_aliases = await self._persist_receipt(
                        entry, owner=owner, generation=generation,
                        known_aliases=known_aliases, receipt=receipt,
                    )
                    await self._channel_repo.advance_outbox_cursor(
                        outbox_id, owner=owner, generation=generation,
                        frame_seq=response.response_seq,
                    )
                if response.complete and response.error is None:
                    provider = get_channel(channel_name)
                    final_text, shown = await self._reply_with_question(
                        entry, response, provider,
                    )
                    seq += 1
                    receipt = await handle.emit(
                        ChannelEvent(
                            type=EVENT_SETTLED, seq=seq, text=final_text,
                            turn_id=turn_id,
                            response_id=str(entry["response_id"]),
                            interaction=(
                                shown if provider.supports_interaction_actions else None
                            ),
                        )
                    )
                    known_aliases = await self._persist_receipt(
                        entry, owner=owner, generation=generation,
                        known_aliases=known_aliases, receipt=receipt,
                        interaction=shown,
                    )
                    await self._channel_repo.mark_outbox_delivered(
                        outbox_id, owner=owner, generation=generation
                    )
                    return
                if response.error is not None:
                    seq += 1
                    with contextlib.suppress(Exception):
                        await handle.emit(
                            ChannelEvent(
                                type=EVENT_FAILED, seq=seq,
                                error=response.error, turn_id=turn_id,
                                response_id=str(entry["response_id"]),
                            )
                        )
                    await _fail_delivery("bound response failed", dead=True)
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

    async def project_session_outputs(self) -> int:
        """Project every main-conversation response through the channel seam."""
        subscriptions = await self._channel_repo.list_output_subscriptions(
            after_id=self._output_subscription_cursor,
        )
        self._output_subscription_cursor = str(subscriptions[-1]["_id"]) if len(subscriptions) == 50 else ""
        count = 0
        for subscription in subscriptions:
            try:
                count += await self._project_session_output(subscription)
            except Exception:
                logger.exception("channel Session output failed subscription=%s", subscription["_id"])
        return count

    async def _project_session_output(self, subscription: dict[str, Any]) -> int:
        session_id = str(subscription["session_id"])
        session = await self._sessions_repo.get_session(session_id)
        if session is None:
            await self._channel_repo.remove_output_subscription(str(subscription["_id"]))
            return 0
        if self._observe_session_output is not None:
            # A destination follows the Session continuously, so it keeps the
            # engine's reader attached: a scheduled run or a background
            # completion is delivered without a new message or a viewer.
            await self._observe_session_output(session_id)
        cursor = int(subscription["after_seq"])
        batch = await self._read_session_output(session_id, after_seq=cursor)
        count = 0
        for response in batch.responses:
            if not response.complete and not subscription["streaming"]:
                continue
            # Text-only destinations have nothing to send for a tool-only reply,
            # unless the reply stopped at a question.
            if (
                response.complete and not response.text.strip()
                and response.interaction is None and not subscription["streaming"]
            ):
                continue
            await self._channel_repo.create_outbox_entry(
                deployment_id=str(subscription["deployment_id"]),
                channel_name=str(subscription["channel_name"]), session_id=session_id,
                turn_id=response.turn_id,
                response_id=response.response_id, response_seq=response.response_seq,
                start_after_seq=response.start_after_seq,
                reply_context=dict(subscription["reply_context"]),
                conversation_key=subscription.get("conversation_key"),
                streaming=bool(subscription["streaming"]),
            )
            count += 1
        if batch.after_seq > cursor:
            await self._channel_repo.advance_output_subscription(
                str(subscription["_id"]), expected_seq=cursor, sequence=batch.after_seq,
            )
        return count

    async def _run_delivery(self, entry: dict[str, Any]) -> None:
        try:
            await self._deliver_outbox_row(entry)
        finally:
            self._delivering.discard(str(entry["_id"]))

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
            self._spawn_background_task(
                self._drive_work_item(claimed), name=f"channel-recovery-{claimed['_id']}",
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
                record["_id"], record.get("deployment_id"),
            )
            if record.get("recall") is not None:
                # The participant withdrew the message; nothing is owed to them.
                continue
            try:
                await self._notify(record, _AUTHORIZATION_EXPIRED_NOTICE)
            except Exception:
                logger.exception(
                    "channel authorization expiry notice failed: item=%s", record["_id"],
                )
        return len(expired)

    async def sweep_pending_outbox(self, *, limit: int = 50) -> int:
        """Re-deliver abandoned outbox rows (boot + periodic recovery)."""
        entries = await self._channel_repo.list_pending_outbox(limit=limit)
        for entry in entries:
            identity = str(entry["_id"])
            if identity in self._delivering:
                continue
            self._delivering.add(identity)
            self._spawn_background_task(
                self._run_delivery(entry), name=f"channel-delivery-{identity}",
            )
        if entries:
            logger.info("channel outbox sweep processed %d abandoned entries", len(entries))
        return len(entries)
