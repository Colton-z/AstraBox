"""MCP servers an administrator registers, and the keys MCP clients authenticate with."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base


class MCPServer(Base):
    """A remote MCP server registered for an organization.

    Names are unique per organization; the database enforces it, so two
    concurrent registrations of one name cannot both succeed.
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("org_id", "name"),
        Index(None, "org_id", "updated_at"),
    )

    mcp_server_id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    transport: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool]
    created_by: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MCPClientToken(Base):
    """An issued MCP client key, stored as the digest of its secret.

    The row never holds the secret: verification hashes what the caller sent
    and looks the digest up, so a leaked backup holds no usable credential.
    """

    __tablename__ = "mcp_client_tokens"
    __table_args__ = (Index(None, "user_id", "created_at"),)

    token_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    org_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    #: Unique because a digest collision would put two identities behind one secret.
    secret_digest: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime | None]
