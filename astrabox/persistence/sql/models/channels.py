"""The channel spine: inbound work items, conversations, output subscriptions and deliveries.

Every record belongs to one channel binding (a Deployment) and is deleted with
it. Records that follow a Session are deleted with the Session, except an
inbound message, which outlives it as the message's record. The contract these
tables serve is ``docs/channel-spine.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import (
    MYSQL_TABLE_OPTIONS,
    EmptyListDefault,
    TextDefault,
    boolean_checks,
    string_list_check,
)
from astrabox.persistence.sql.types import KeyText, StringList, payload_json, payload_text

_BINDING = "deployments.deployment_id"
_SESSION = "sessions.session_id"


class ChannelInbound(Base):
    """One platform message on one binding: its record and its unit of work.

    A message is identified by ``(deployment_id, dedup_key)``; a message
    without a platform id (``dedup_key`` null) is always a new record. The
    columns from ``content`` to ``response_instruction`` are the message as it
    was admitted: an admitted message's input never changes. ``owner_token``,
    ``generation`` and ``lease_expires_at`` fence the worker that drives it.
    ``agent_id``/``assistant_id`` copy the binding's target at admission, so a
    re-drive resolves it without reading the binding.
    """

    __tablename__ = "channel_inbound"
    __table_args__ = (
        UniqueConstraint("deployment_id", "dedup_key"),
        CheckConstraint(
            "(agent_id IS NULL) <> (assistant_id IS NULL)", name="one_target"
        ),
        CheckConstraint(
            "state IN ('RECEIVED', 'DISPATCHING', 'SETTLED', 'IGNORED', 'DEAD', "
            "'OBSERVED', 'AUTHORIZED')",
            name="state",
        ),
        Index(
            "ix_channel_inbound_recoverable",
            "lease_expires_at",
            postgresql_where=text("state IN ('RECEIVED', 'DISPATCHING')"),
        ),
        Index(
            "ix_channel_inbound_authorizations",
            "authorization_expires_at",
            postgresql_where=text("state = 'AUTHORIZED'"),
        ),
        Index(
            "ix_channel_inbound_participation",
            "deployment_id",
            "conversation",
            "binding_revision",
            postgresql_where=text("state = 'SETTLED'"),
        ),
        Index(
            "ix_channel_inbound_context",
            "deployment_id",
            "conversation",
            "binding_revision",
            "message_timestamp",
            "dedup_key",
            postgresql_where=text(
                "retain_context AND NOT context_submitted AND NOT context_excluded "
                "AND provider_ignore_reason IS NULL"
            ),
        ),
        Index(None, "session_id", postgresql_where=text("session_id IS NOT NULL")),
        *boolean_checks("retain_context", "participated", "judgment_respond", "explicit_attention", "context_excluded", "context_submitted"),
        string_list_check("frozen_context_ids"),
        MYSQL_TABLE_OPTIONS,
    )

    item_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_BINDING, ondelete="CASCADE")
    )
    dedup_key: Mapped[str | None] = mapped_column(KeyText(1024))
    channel_name: Mapped[str] = mapped_column(payload_text())
    agent_id: Mapped[str | None] = mapped_column(payload_text())
    assistant_id: Mapped[str | None] = mapped_column(payload_text())
    state: Mapped[str] = mapped_column(payload_text())
    owner_token: Mapped[str | None] = mapped_column(payload_text())
    generation: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    lease_expires_at: Mapped[datetime | None]
    authorization_expires_at: Mapped[datetime | None]
    #: Recorded drive failures; also part of the turn's dispatch token.
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    #: Evidence of the last failure, kept for operators.
    last_error: Mapped[str | None] = mapped_column(payload_text())
    #: Why the message was refused or classified away, kept for operators.
    ignored_reason: Mapped[str | None] = mapped_column(payload_text())
    session_id: Mapped[str | None] = mapped_column(
        KeyText(128), ForeignKey(_SESSION, ondelete="SET NULL")
    )
    #: The conversation the bound Session was resolved for: a re-drive takes
    #: that conversation's lock without resolving the Session again.
    resolved_conversation_key: Mapped[str | None] = mapped_column(payload_text())

    content: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault(""))
    reply_text: Mapped[str | None] = mapped_column(payload_text())
    action: Mapped[str | None] = mapped_column(payload_text())
    conversation_key: Mapped[str | None] = mapped_column(payload_text())
    #: The conversation the message continues, decided before it was recorded.
    conversation: Mapped[str | None] = mapped_column(KeyText(1024))
    #: The provider's destination for a reply, handed back to the provider.
    reply_context: Mapped[dict[str, Any] | None] = mapped_column(payload_json())
    reference: Mapped[str | None] = mapped_column(payload_text())
    participant: Mapped[str | None] = mapped_column(payload_text())
    provider_ignore_reason: Mapped[str | None] = mapped_column(payload_text())
    creator_user_id: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault(""))
    #: Empty until a linked binding's message captures its account.
    execution_user_id: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault(""))
    execution_mode: Mapped[str | None] = mapped_column(payload_text())
    retain_context: Mapped[bool] = mapped_column(server_default=text("false"))
    #: The provider's message time; retained context orders by it as text.
    message_timestamp: Mapped[str | None] = mapped_column(KeyText(128))
    #: The binding's ``updated_at`` when the message was admitted.
    binding_revision: Mapped[str | None] = mapped_column(KeyText(64))
    prompt_prefix: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault(""))
    selection: Mapped[str | None] = mapped_column(payload_text())
    selection_reason: Mapped[str | None] = mapped_column(payload_text())
    participated: Mapped[bool | None]
    response_policy_mode: Mapped[str | None] = mapped_column(payload_text())
    response_instruction: Mapped[str | None] = mapped_column(payload_text())

    judgment_respond: Mapped[bool | None]
    judgment_reason: Mapped[str | None] = mapped_column(payload_text())
    judgment_judge: Mapped[str | None] = mapped_column(payload_text())
    judged_at: Mapped[datetime | None]
    explicit_attention: Mapped[bool] = mapped_column(server_default=text("false"))
    answer_ref: Mapped[str | None] = mapped_column(payload_text())
    answer_question_index: Mapped[int | None] = mapped_column(Integer)
    recall_event_id: Mapped[str | None] = mapped_column(payload_text())
    recall_timestamp: Mapped[str | None] = mapped_column(payload_text())
    #: The turn input as submitted, frozen before the first submission so a
    #: re-drive submits exactly the same input.
    frozen_content: Mapped[str | None] = mapped_column(payload_text())
    #: The retained messages ``frozen_content`` quotes, oldest first.
    frozen_context_ids: Mapped[list[str] | None] = mapped_column(StringList())
    context_excluded: Mapped[bool] = mapped_column(server_default=text("false"))
    #: Consumed as context: quoted by a turn, or older than the window a turn
    #: quoted. A consumed message is never quoted again.
    context_submitted: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChannelConversation(Base):
    """The Session a binding's conversation continues in, per execution account.

    ``execution_user_id`` is empty for a binding that runs as its owner.
    """

    __tablename__ = "channel_conversations"
    __table_args__ = (
        Index(None, "session_id"),
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_BINDING, ondelete="CASCADE"), primary_key=True
    )
    execution_user_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    conversation_key: Mapped[str] = mapped_column(KeyText(1024), primary_key=True)
    session_id: Mapped[str] = mapped_column(KeyText(128), ForeignKey(_SESSION, ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChannelConversationLock(Base):
    """The lease that serializes one conversation's drives.

    Held by a drive's owner token until ``expires_at``; the holder renews it
    while its drive runs. ``generation`` grows on every acquisition, and
    renewal and release match it, so a holder whose lease lapsed cannot touch
    its successor's lock.
    """

    __tablename__ = "channel_conversation_locks"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_BINDING, ondelete="CASCADE"), primary_key=True
    )
    execution_user_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    conversation_key: Mapped[str] = mapped_column(KeyText(1024), primary_key=True)
    owner: Mapped[str | None] = mapped_column(payload_text())
    generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    expires_at: Mapped[datetime | None]


class ChannelOutputSubscription(Base):
    """A binding's destination for one Session's replies.

    ``after_seq`` is the Session output position through which the Session's
    replies have been turned into deliveries. It starts at the output the
    Session had when the binding subscribed, so earlier replies are not sent.
    """

    __tablename__ = "channel_output_subscriptions"
    __table_args__ = (
        Index(None, "session_id"),
        *boolean_checks("streaming"),
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_BINDING, ondelete="CASCADE"), primary_key=True
    )
    session_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_SESSION, ondelete="CASCADE"), primary_key=True
    )
    channel_name: Mapped[str] = mapped_column(payload_text())
    reply_context: Mapped[dict[str, Any]]
    conversation_key: Mapped[str | None] = mapped_column(payload_text())
    streaming: Mapped[bool]
    after_seq: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChannelOutbox(Base):
    """The delivery of one Session reply to one binding.

    A row is written when its reply starts. ``PENDING`` rows are delivered
    from ``next_attempt_at``; a final-text reply's row has none until the
    reply is complete. ``SENDING`` rows are held by ``lease_owner`` at
    ``lease_generation`` until ``lease_expires_at``. A failed attempt counts
    in ``attempts`` and the row waits out its backoff as ``PENDING``, or is
    ``DEAD`` once the retries are spent. Message bodies stay in the Session's
    output log.
    """

    __tablename__ = "channel_outbox"
    __table_args__ = (
        UniqueConstraint("deployment_id", "session_id", "response_id"),
        CheckConstraint(
            "state IN ('PENDING', 'SENDING', 'DELIVERED', 'DEAD')", name="state"
        ),
        # The undelivered rows: the sweep reads its due rows from them and the
        # claim checks a Session's earlier replies in them, so neither reads
        # the delivered history.
        Index(
            "ix_channel_outbox_undelivered",
            "deployment_id",
            "session_id",
            "response_seq",
            postgresql_where=text("state IN ('PENDING', 'SENDING')"),
        ),
        Index(None, "session_id"),
        *boolean_checks("streaming", "stream_started"),
        string_list_check("delivery_aliases"),
        MYSQL_TABLE_OPTIONS,
    )

    outbox_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_BINDING, ondelete="CASCADE")
    )
    session_id: Mapped[str] = mapped_column(KeyText(128), ForeignKey(_SESSION, ondelete="CASCADE"))
    #: The reply: the response id, or ``<response_id>~<interaction_id>`` for
    #: what the response says after that interaction
    #: (:mod:`astrabox.core.service.orchestrator.channel_replies`).
    response_id: Mapped[str] = mapped_column(KeyText(1024))
    #: The Session output position of the reply's ``start`` part; replies of
    #: one Session are delivered in this order.
    response_seq: Mapped[int] = mapped_column(BigInteger)
    channel_name: Mapped[str] = mapped_column(payload_text())
    conversation_key: Mapped[str | None] = mapped_column(payload_text())
    reply_context: Mapped[dict[str, Any]]
    streaming: Mapped[bool]
    #: A streaming delivery already opened the reply on the platform.
    stream_started: Mapped[bool] = mapped_column(server_default=text("false"))
    state: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault("PENDING"))
    lease_owner: Mapped[str | None] = mapped_column(payload_text())
    lease_generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    lease_expires_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(payload_text())
    next_attempt_at: Mapped[datetime | None] = mapped_column(server_default=func.now())
    #: The platform message ids the delivery created, in creation order.
    delivery_aliases: Mapped[list[str]] = mapped_column(
        StringList(), server_default=EmptyListDefault(text_array=True)
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChannelAlias(Base):
    """A platform message the spine delivered, and the conversation it belongs to.

    A reply quoting the message continues that conversation; a message that
    showed a question also names the question (``interaction_ref``) and which
    of its questions (``question_index``) a reply answers.
    """

    __tablename__ = "channel_aliases"
    __table_args__ = (
        Index(None, "session_id"),
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_BINDING, ondelete="CASCADE"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(KeyText(1024), primary_key=True)
    conversation_key: Mapped[str | None] = mapped_column(payload_text())
    session_id: Mapped[str] = mapped_column(KeyText(128), ForeignKey(_SESSION, ondelete="CASCADE"))
    interaction_ref: Mapped[str | None] = mapped_column(payload_text())
    question_index: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChannelInteraction(Base):
    """A question a binding showed, one question at a time.

    ``ref`` is the short name its buttons carry. ``view`` is the interaction's
    public view as it was shown, so a reply that arrives after the question
    closed is still recognised as an answer to it. ``answering_item`` is the
    inbound message that claimed the last question's answer.
    """

    __tablename__ = "channel_interactions"
    __table_args__ = (
        CheckConstraint("state IN ('OPEN', 'ANSWERED', 'CLOSED')", name="state"),
        Index(None, "session_id"),
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_BINDING, ondelete="CASCADE"), primary_key=True
    )
    ref: Mapped[str] = mapped_column(KeyText(1024), primary_key=True)
    session_id: Mapped[str] = mapped_column(KeyText(128), ForeignKey(_SESSION, ondelete="CASCADE"))
    interaction_id: Mapped[str] = mapped_column(payload_text())
    conversation_key: Mapped[str | None] = mapped_column(payload_text())
    channel_name: Mapped[str] = mapped_column(payload_text())
    reply_context: Mapped[dict[str, Any]]
    view: Mapped[dict[str, Any]]
    state: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault("OPEN"))
    #: The question being asked now, from 0.
    question_index: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    answering_item: Mapped[str | None] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChannelInteractionAnswer(Base):
    """One question's answer, kept until every question of the form has one."""

    __tablename__ = "channel_interaction_answers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["deployment_id", "ref"],
            ["channel_interactions.deployment_id", "channel_interactions.ref"],
            ondelete="CASCADE",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    ref: Mapped[str] = mapped_column(KeyText(1024), primary_key=True)
    question_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: The answer in the interaction contract's row shape, given to the engine.
    answer_row: Mapped[dict[str, Any]]
    #: The chosen label of a decision or approval; empty for a form row.
    choice: Mapped[str | None] = mapped_column(payload_text())
    participant: Mapped[str | None] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ChannelAccountLink(Base):
    """The AstraBox account one participant of a linked binding runs as."""

    __tablename__ = "channel_account_links"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    deployment_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey(_BINDING, ondelete="CASCADE"), primary_key=True
    )
    participant: Mapped[str] = mapped_column(KeyText(1024), primary_key=True)
    user_id: Mapped[str] = mapped_column(payload_text())
    linked_by: Mapped[str] = mapped_column(payload_text())
    linked_at: Mapped[datetime] = mapped_column(server_default=func.now())
