"""Vaults, their credentials, and sealed secrets."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import (
    MYSQL_TABLE_OPTIONS,
    EmptyObjectDefault,
    boolean_checks,
    conditional_unique,
    string_list_check,
)
from astrabox.persistence.sql.types import KeyText, StringList, payload_text


class Vault(Base):
    """A vault of credentials that Agents, Assistants and Sessions select.

    A table that binds a Vault to something must reference it ``ON DELETE
    RESTRICT``: deleting a Vault that is still bound then fails in the database
    instead of leaving the binding pointing at nothing.
    """

    __tablename__ = "vaults"
    __table_args__ = (
        Index(None, "org_id", "created_at"),
        MYSQL_TABLE_OPTIONS,
    )

    vault_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(KeyText(512))
    created_by: Mapped[str] = mapped_column(payload_text())
    display_name: Mapped[str] = mapped_column(payload_text())
    #: Free-form administrator labels, stored and returned as given. The
    #: attribute carries a trailing underscore because declarative classes
    #: reserve ``metadata``; the column is ``metadata``.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", server_default=EmptyObjectDefault(postgres_sql="'{}'::jsonb")
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
        *conditional_unique(
            "ix_vault_credentials_vault_id_mcp_server_url", "vault_id", "mcp_server_url",
            predicate=text("archived_at IS NULL AND mcp_server_url IS NOT NULL"), marker="_mcp_slot",
        ),
        *conditional_unique(
            "ix_vault_credentials_vault_id_url", "vault_id", "url",
            predicate=text("archived_at IS NULL AND url IS NOT NULL"), marker="_http_slot",
        ),
        *conditional_unique(
            "ix_vault_credentials_vault_id_secret_name", "vault_id", "secret_name",
            predicate=text("archived_at IS NULL AND secret_name IS NOT NULL"), marker="_environment_slot",
        ),
        Index(None, "vault_id", "created_at"),
        *boolean_checks("inject_header", "inject_body", "allow_insecure_http"),
        string_list_check("allowed_hosts"),
        string_list_check("allowed_methods"),
        string_list_check("allowed_paths"),
        MYSQL_TABLE_OPTIONS,
    )

    credential_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    vault_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("vaults.vault_id", ondelete="CASCADE")
    )
    display_name: Mapped[str | None] = mapped_column(payload_text())
    auth_type: Mapped[str] = mapped_column(payload_text())
    mcp_server_url: Mapped[str | None] = mapped_column(KeyText(2048))
    url: Mapped[str | None] = mapped_column(KeyText(2048))
    secret_name: Mapped[str | None] = mapped_column(KeyText(512))
    username: Mapped[str | None] = mapped_column(payload_text())
    header_name: Mapped[str | None] = mapped_column(payload_text())
    #: When an ``mcp_oauth`` access token expires; ``NULL`` when unknown.
    expires_at: Mapped[datetime | None]
    #: An ``mcp_oauth`` refresh configuration is present exactly when its token
    #: endpoint is.
    refresh_token_endpoint: Mapped[str | None] = mapped_column(payload_text())
    refresh_client_id: Mapped[str | None] = mapped_column(payload_text())
    refresh_auth_method: Mapped[str | None] = mapped_column(payload_text())
    refresh_scope: Mapped[str | None] = mapped_column(payload_text())
    refresh_claimed_until: Mapped[datetime | None]
    #: ``environment_variable`` delivery rules. ``network_access`` is
    #: ``limited`` (with ``allowed_hosts``) or ``unrestricted``; a ``NULL``
    #: method or path list leaves that part of the request unrestricted.
    network_access: Mapped[str | None] = mapped_column(payload_text())
    allowed_hosts: Mapped[list[str] | None] = mapped_column(StringList())
    inject_header: Mapped[bool | None]
    inject_body: Mapped[bool | None]
    allowed_methods: Mapped[list[str] | None] = mapped_column(StringList())
    allowed_paths: Mapped[list[str] | None] = mapped_column(StringList())
    allow_insecure_http: Mapped[bool | None]
    #: How an ``environment_variable`` credential reaches the egress boundary:
    #: ``substitution`` or ``header`` (``astrabox/seams/egress_credentials.py``).
    #: ``NULL`` for every other credential type.
    delivery: Mapped[str | None] = mapped_column(payload_text())
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
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    scope: Mapped[str] = mapped_column(KeyText(768), primary_key=True)
    key: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    sealed: Mapped[str] = mapped_column(payload_text())
