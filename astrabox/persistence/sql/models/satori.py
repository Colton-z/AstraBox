"""Owner-scoped Satori replay positions over the existing Session output log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, ForeignKeyConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import MYSQL_TABLE_OPTIONS
from astrabox.persistence.sql.types import KeyText, payload_text


class SatoriFeed(Base):
    __tablename__ = "satori_feeds"
    __table_args__ = (MYSQL_TABLE_OPTIONS,)

    feed_id: Mapped[str] = mapped_column(KeyText(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(KeyText(512))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id", ondelete="CASCADE"))
    last_sn: Mapped[int] = mapped_column(BigInteger, server_default="0")


class SatoriPosition(Base):
    __tablename__ = "satori_positions"
    __table_args__ = (MYSQL_TABLE_OPTIONS,)

    feed_id: Mapped[str] = mapped_column(ForeignKey("satori_feeds.feed_id", ondelete="CASCADE"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id", ondelete="CASCADE"), primary_key=True)
    output_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    state_digest: Mapped[str | None] = mapped_column(payload_text())


class SatoriEvent(Base):
    """A protocol event references immutable output, rather than copying token payloads."""

    __tablename__ = "satori_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feed_id", "session_id"], ["satori_positions.feed_id", "satori_positions.session_id"],
            ondelete="CASCADE",
        ),
        MYSQL_TABLE_OPTIONS,
    )

    feed_id: Mapped[str] = mapped_column(KeyText(64), primary_key=True)
    sn: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[str] = mapped_column(KeyText(128))
    descriptor: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
