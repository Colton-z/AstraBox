"""Vaults, their credentials, and sealed secrets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base


class Vault(Base):
    """A vault of credentials that Agents, Assistants and Sessions select.

    A table that binds a Vault to something must reference it ``ON DELETE
    RESTRICT``: deleting a Vault that is still bound then fails in the database
    instead of leaving the binding pointing at nothing.
    """

    __tablename__ = "vaults"
    __table_args__ = (Index(None, "org_id", "created_at"),)

    vault_id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    #: Free-form administrator labels, stored and returned as given. The
    #: attribute carries a trailing underscore because declarative classes
    #: reserve ``metadata``; the column is ``metadata``.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", server_default=text("'{}'::jsonb")
    )
    archived_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VaultCredential(Base):
    """One credential in a Vault, without its secret values.

    The secret values (tokens, passwords, refresh material) live in the
    configured secret store under the scope ``vault/<vault_id>``; this row holds
    what the platform matches and enforces on.

    A credential is matched by one key, chosen by its type: ``mcp_server_url``
    for the MCP types, ``url`` for ``http_basic`` and ``secret_name`` for
    ``environment_variable``. The check constraint makes exactly that key
    present, and each key has a partial unique index over the Vault's active
    credentials, so two concurrent creates of one key cannot both succeed.
    Archived credentials stay for audit and leave the indexes.

    ``refresh_claimed_until`` is the OAuth refresh claim: a replica refreshing
    an expired ``mcp_oauth`` access token holds it until then, and no other
    replica may refresh the token meanwhile.
    """

    __tablename__ = "vault_credentials"
    __table_args__ = (
        CheckConstraint(
            "(auth_type IN ('mcp_oauth', 'static_bearer', 'mcp_static_header')"
            " AND mcp_server_url IS NOT NULL AND url IS NULL AND secret_name IS NULL)"
            " OR (auth_type = 'http_basic'"
            " AND url IS NOT NULL AND mcp_server_url IS NULL AND secret_name IS NULL)"
            " OR (auth_type = 'environment_variable'"
            " AND secret_name IS NOT NULL AND mcp_server_url IS NULL AND url IS NULL)",
            name="one_key_per_type",
        ),
        Index(
            None,
            "vault_id",
            "mcp_server_url",
            unique=True,
            postgresql_where=text("archived_at IS NULL AND mcp_server_url IS NOT NULL"),
        ),
        Index(
            None,
            "vault_id",
            "url",
            unique=True,
            postgresql_where=text("archived_at IS NULL AND url IS NOT NULL"),
        ),
        Index(
            None,
            "vault_id",
            "secret_name",
            unique=True,
            postgresql_where=text("archived_at IS NULL AND secret_name IS NOT NULL"),
        ),
        Index(None, "vault_id", "created_at"),
    )

    credential_id: Mapped[str] = mapped_column(Text, primary_key=True)
    vault_id: Mapped[str] = mapped_column(
        Text, ForeignKey("vaults.vault_id", ondelete="CASCADE")
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    auth_type: Mapped[str] = mapped_column(Text)
    mcp_server_url: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    secret_name: Mapped[str | None] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)
    header_name: Mapped[str | None] = mapped_column(Text)
    #: When an ``mcp_oauth`` access token expires; ``NULL`` when unknown.
    expires_at: Mapped[datetime | None]
    #: An ``mcp_oauth`` refresh configuration is present exactly when its token
    #: endpoint is.
    refresh_token_endpoint: Mapped[str | None] = mapped_column(Text)
    refresh_client_id: Mapped[str | None] = mapped_column(Text)
    refresh_auth_method: Mapped[str | None] = mapped_column(Text)
    refresh_scope: Mapped[str | None] = mapped_column(Text)
    refresh_claimed_until: Mapped[datetime | None]
    #: ``environment_variable`` delivery rules. ``network_access`` is
    #: ``limited`` (with ``allowed_hosts``) or ``unrestricted``; a ``NULL``
    #: method or path list leaves that part of the request unrestricted.
    network_access: Mapped[str | None] = mapped_column(Text)
    allowed_hosts: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    inject_header: Mapped[bool | None]
    inject_body: Mapped[bool | None]
    allowed_methods: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    allowed_paths: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    allow_insecure_http: Mapped[bool | None]
    archived_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Secret(Base):
    """One sealed value, written by a secret store that keeps its values here.

    ``scope`` names the record that owns the value (``vault/<vault_id>``,
    ``deployment/<deployment_id>``) and ``key`` its slot there. ``sealed`` is
    the store's ciphertext, bound to the scope and key, so a value copied onto
    another row fails to open.
    """

    __tablename__ = "secrets"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    sealed: Mapped[str] = mapped_column(Text)
