"""One database's immutable Environment identity and locally granted access."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func, true
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import MYSQL_TABLE_OPTIONS
from astrabox.persistence.sql.types import KeyText, payload_text

class Environment(Base):
    """The single Environment owning every business record in this database."""

    __tablename__ = "environments"
    __table_args__ = (
        CheckConstraint("singleton", name="singleton"),
        UniqueConstraint("name"),
        MYSQL_TABLE_OPTIONS,
    )

    singleton: Mapped[bool] = mapped_column(primary_key=True, server_default=true())
    name: Mapped[str] = mapped_column(KeyText(128))
    display_name: Mapped[str] = mapped_column(payload_text())
    description: Mapped[str | None] = mapped_column(payload_text())
    version: Mapped[int] = mapped_column(server_default="1")
    updated_by: Mapped[str | None] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EnvironmentMember(Base):
    """Permission to enter an Environment, distinct from resource permissions."""

    __tablename__ = "environment_members"
    __table_args__ = (Index(None, "user_id", "environment_name"), MYSQL_TABLE_OPTIONS)

    environment_name: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("environments.name", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    created_by: Mapped[str | None] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
