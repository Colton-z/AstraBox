"""Hosted artifacts: records, versions, documents, live events, viewers and budgets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, ForeignKeyConstraint, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import MYSQL_TABLE_OPTIONS, boolean_checks, conditional_unique
from astrabox.persistence.sql.types import KeyText, payload_text

_CASCADE = "CASCADE"


class Artifact(Base):
    """A hosted artifact: owner, current version and manifest, origin, and its counters.

    Every mutation of one artifact first locks this row, which serializes the
    mutations across replicas: version numbers, journal sequences and the
    document count are allocated under that lock. ``revision`` counts the
    mutations and is compared on every write.

    The origin columns record the conversation, Agent or Assistant that made
    the artifact. They are recorded values rather than foreign keys: an
    artifact outlives its conversation, and keeps naming an Agent or Assistant
    after that origin is deleted (its ``sample`` and ``agent`` capabilities then
    answer that the origin is unavailable).
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        # The owner's gallery, newest first, paged by (updated_at, artifact_id).
        Index(None, "owner_id", "updated_at", "artifact_id"),
        # A conversation's and an Assistant's artifacts panels.
        Index(
            None, "origin_session_id", "updated_at", "artifact_id",
            postgresql_where=text("origin_session_id IS NOT NULL"),
        ),
        Index(
            None, "origin_assistant_id", "updated_at", "artifact_id",
            postgresql_where=text("origin_assistant_id IS NOT NULL"),
        ),
        # An Agent's artifacts tools list one user's artifacts of that Agent.
        Index(
            None, "owner_id", "origin_agent_id", "updated_at", "artifact_id",
            postgresql_where=text("origin_agent_id IS NOT NULL"),
        ),
        MYSQL_TABLE_OPTIONS,
    )

    artifact_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(KeyText(512))
    title: Mapped[str] = mapped_column(payload_text())
    description: Mapped[str] = mapped_column(payload_text())
    kind: Mapped[str] = mapped_column(payload_text())
    language: Mapped[str | None] = mapped_column(payload_text())
    current_version: Mapped[int]
    #: The validated capability map every runtime operation is authorized against.
    manifest: Mapped[dict[str, Any]]
    origin_session_kind: Mapped[str | None] = mapped_column(payload_text())
    origin_session_id: Mapped[str | None] = mapped_column(KeyText(128))
    origin_agent_id: Mapped[str | None] = mapped_column(KeyText(128))
    origin_assistant_id: Mapped[str | None] = mapped_column(KeyText(128))
    revision: Mapped[int] = mapped_column(BigInteger)
    #: The last journal sequence allocated; readers start their tail here.
    event_seq: Mapped[int] = mapped_column(BigInteger)
    document_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ArtifactVersion(Base):
    """One immutable published version's metadata; its content is a row of its own.

    Version lists read this table only, so they never load content of up to
    4 MiB per version.
    """

    __tablename__ = "artifact_versions"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    artifact_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("artifacts.artifact_id", ondelete=_CASCADE), primary_key=True
    )
    version: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(payload_text())
    byte_size: Mapped[int]
    kind: Mapped[str] = mapped_column(payload_text())
    title: Mapped[str] = mapped_column(payload_text())
    language: Mapped[str | None] = mapped_column(payload_text())
    #: The manifest published with this version; a restore republishes it.
    manifest: Mapped[dict[str, Any]]
    author_user_id: Mapped[str] = mapped_column(payload_text())
    author_kind: Mapped[str] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ArtifactVersionContent(Base):
    """A version's UTF-8 content, read only where a page or tool is served it."""

    __tablename__ = "artifact_version_contents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_id", "version"],
            ["artifact_versions.artifact_id", "artifact_versions.version"],
            ondelete=_CASCADE,
        ),
        MYSQL_TABLE_OPTIONS,
    )

    artifact_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(payload_text())


class ArtifactShare(Base):
    """An artifact's share link, created the first time its owner shares it.

    The token is unique, so resolving a link is one index lookup. Disabling a
    share keeps its row and token: re-enabling it reuses the same link.
    """

    __tablename__ = "artifact_shares"
    __table_args__ = (
        # A pinned share serves one of the artifact's own versions. Deleting an
        # artifact removes its versions and its share in one statement, so
        # this reference cascades as well rather than checking the order.
        ForeignKeyConstraint(
            ["artifact_id", "version"],
            ["artifact_versions.artifact_id", "artifact_versions.version"],
            ondelete=_CASCADE,
        ),
        *boolean_checks("enabled"),
        MYSQL_TABLE_OPTIONS,
    )

    artifact_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("artifacts.artifact_id", ondelete=_CASCADE), primary_key=True
    )
    token: Mapped[str] = mapped_column(KeyText(512), unique=True)
    enabled: Mapped[bool]
    access: Mapped[str] = mapped_column(payload_text())
    audience: Mapped[str] = mapped_column(payload_text())
    #: The pinned version, or ``None`` to follow the latest.
    version: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime | None]


class ArtifactDocument(Base):
    """One document of an artifact's ``db``, addressed by its slash-separated path.

    Paths sort by code point (the ``C`` collation), the order a collection
    query returns its documents in before the page's own ordering.
    """

    __tablename__ = "artifact_documents"
    __table_args__ = (
        Index(None, "artifact_id", "collection", "path"),
        MYSQL_TABLE_OPTIONS,
    )

    artifact_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("artifacts.artifact_id", ondelete=_CASCADE), primary_key=True
    )
    path: Mapped[str] = mapped_column(KeyText(1024, postgres_collation="C"), primary_key=True)
    #: The parent collection's path, which collection queries select by.
    collection: Mapped[str] = mapped_column(KeyText(1024, postgres_collation="C"))
    #: The page's JSON object, stored as written.
    data: Mapped[dict[str, Any]]
    #: The writer's opaque viewer id.
    updated_by: Mapped[str] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ArtifactEvent(Base):
    """The per-artifact live journal that viewers tail by sequence.

    ``kind`` is ``version``, ``data``, ``room`` or ``presence``. The journal
    keeps one ``version`` event per artifact and one ``data`` event per
    document path, each moved to the newest sequence by a later event of the
    same key: a reader at any position still receives every later version and
    every document path changed since. Room events are delivered for ten
    minutes and presence for 45 seconds; every room or presence write deletes a
    batch of expired ones, of any artifact.
    """

    __tablename__ = "artifact_events"
    __table_args__ = (
        *conditional_unique(
            "ix_artifact_events_artifact_id", "artifact_id",
            predicate=text("kind = 'version'"), marker="_version_slot",
        ),
        *conditional_unique(
            "ix_artifact_events_artifact_id_path", "artifact_id", "path",
            predicate=text("kind = 'data'"), marker="_data_slot",
        ),
        # Expired room and presence events, across artifacts.
        Index(None, "kind", "created_at", postgresql_where=text("kind IN ('room', 'presence')")),
        # The presence a reconnecting reader is replayed.
        Index(None, "artifact_id", "seq", postgresql_where=text("kind = 'presence'")),
        MYSQL_TABLE_OPTIONS,
    )

    artifact_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("artifacts.artifact_id", ondelete=_CASCADE), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(KeyText(32))
    #: The document path of a ``data`` event.
    path: Mapped[str | None] = mapped_column(KeyText(1024))
    #: What a reader is sent: page room data is stored as the page sent it.
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ArtifactViewer(Base):
    """A signed-in viewer's opaque per-artifact id and display name, for ``user.profiles``."""

    __tablename__ = "artifact_viewers"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    artifact_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("artifacts.artifact_id", ondelete=_CASCADE), primary_key=True
    )
    viewer_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    user_id: Mapped[str] = mapped_column(payload_text())
    display_name: Mapped[str] = mapped_column(payload_text())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ArtifactBudget(Base):
    """One viewer's budget of one kind, across artifacts and replicas.

    ``budget_key`` is derived from the viewer's identity, so every replica
    finds the same row. A ``room`` row is the viewer's live-write token bucket.
    A ``sample`` or ``agent`` row is what that viewer's reservations are
    serialized on: a reservation is admitted while its budget row is locked.
    A room bucket untouched for long enough to have refilled is the same as no
    row, and is deleted.
    """

    __tablename__ = "artifact_budgets"
    __table_args__ = (
        Index(None, "kind", "updated_at"),
        MYSQL_TABLE_OPTIONS,
    )

    budget_key: Mapped[str] = mapped_column(KeyText(1024), primary_key=True)
    kind: Mapped[str] = mapped_column(KeyText(32))
    #: A room bucket's tokens as of ``updated_at``.
    tokens: Mapped[float | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ArtifactSampleReservation(Base):
    """One admitted model round, counted against its viewer's ``sample`` budget.

    It holds a concurrency slot until released or expired, and counts toward
    the per-minute limit for a minute after it started.
    """

    __tablename__ = "artifact_sample_reservations"
    __table_args__ = (
        Index(None, "budget_key", "created_at"),
        MYSQL_TABLE_OPTIONS,
    )

    reservation_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    budget_key: Mapped[str] = mapped_column(
        KeyText(1024), ForeignKey("artifact_budgets.budget_key", ondelete=_CASCADE)
    )
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    released_at: Mapped[datetime | None]


class ArtifactRunReservation(Base):
    """One page-started Agent run, counted against its viewer's ``agent`` budget.

    ``artifact_id`` and ``session_id`` are recorded values: the hourly budget
    outlives the artifact, and the run's state is read from its Session.
    """

    __tablename__ = "artifact_run_reservations"
    __table_args__ = (
        Index(None, "budget_key", "created_at"),
        MYSQL_TABLE_OPTIONS,
    )

    reservation_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    budget_key: Mapped[str] = mapped_column(
        KeyText(1024), ForeignKey("artifact_budgets.budget_key", ondelete=_CASCADE)
    )
    artifact_id: Mapped[str] = mapped_column(payload_text())
    #: The run's Session, once its conversation has started.
    session_id: Mapped[str | None] = mapped_column(payload_text())
    created_at: Mapped[datetime]
