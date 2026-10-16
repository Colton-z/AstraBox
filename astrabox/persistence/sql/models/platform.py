"""Installation identity, user names, runtime errors, Hermes cron jobs and A2A task references."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base


class Installation(Base):
    """The one row naming this installation.

    Sandboxes carry the id in their create metadata, so an installation tells
    its own boxes from another's on a shared Docker daemon or Kubernetes
    namespace. The table holds at most one row: its key can only be true.
    """

    __tablename__ = "installation"
    __table_args__ = (
        CheckConstraint("singleton", name="singleton"),
        CheckConstraint("installation_id <> ''", name="installation_id_present"),
    )

    singleton: Mapped[bool] = mapped_column(primary_key=True, server_default=true())
    installation_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class UserProfile(Base):
    """The display name a verified identity carried when its user was first admitted.

    User ids are free strings issued by the identity provider; no table of
    users stands behind them. The name is for display only.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CapturedRuntimeError(Base):
    """One kind of ERROR log record, counted, for the admin errors page.

    Records with the same logger, exception type, location and message prefix
    share a ``signature`` and one row. A row whose error has not recurred for
    the retention period is deleted.
    """

    __tablename__ = "runtime_errors"
    # Serves the newest-first listing and the retention delete.
    __table_args__ = (Index(None, "last_seen"),)

    signature: Mapped[str] = mapped_column(Text, primary_key=True)
    logger: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text)
    count: Mapped[int] = mapped_column(BigInteger)
    first_seen: Mapped[datetime]
    last_seen: Mapped[datetime]


class HermesCronJob(Base):
    """The Assistant conversation a Hermes cron job reports to, and its delivery watermark.

    Hermes records no creator on a job a conversation schedules, so the
    conversation that created it is recorded here. The row goes with its
    conversation.
    """

    __tablename__ = "hermes_cron_jobs"
    # Leads with the conversation so the same index serves a reader's listing
    # and the cascade when the conversation is deleted.
    __table_args__ = (Index(None, "session_id", "profile_key"),)

    profile_key: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        Text, ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    #: The newest run already delivered; it only moves forward.
    delivered_run_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentTaskReference(Base):
    """An A2A protocol identity and the conversation that serves it.

    The id is derived from the caller's identity and its message or context
    id, so a retried request reaches the same row. ``kind`` is ``task`` (a new
    task's message), ``answer`` (a message continuing a task) or ``context``
    (the conversation an A2A context runs in). The conversation is bound once;
    the first binding wins.
    """

    __tablename__ = "agent_task_references"
    __table_args__ = (
        CheckConstraint("kind IN ('task', 'answer', 'context')", name="kind"),
        # A caller's tasks for one Agent, newest first, in keyset pages; the
        # same index serves the cascade when the Agent is deleted.
        Index(None, "agent_id", "user_id", "org_id", "created_at", "reference_id"),
        Index(None, "session_id"),
    )

    reference_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agents.agent_id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(Text)
    org_id: Mapped[str] = mapped_column(Text)
    context_id: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(Text)
    #: Digest of the request a message id first named, so a different request
    #: reusing the id is refused.
    fingerprint: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
