"""Assistants, their workspaces and profiles, and deployment-wide Assistant defaults."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base

#: The states an Assistant workspace moves through; ``AssistantWorkspaceService``
#: documents each transition.
WORKSPACE_STATES = ("MATERIALIZING", "READY", "HIBERNATING", "RECOVERY_REQUIRED")


class Assistant(Base):
    """An Assistant: its owner's definition, its Environment, and its bookkeeping.

    ``version`` moves with every authored write, and each authored write is a
    compare-and-set on the version it read, so two concurrent edits cannot
    both land. ``updated_at`` moves only when an authored value changed; the
    platform's own fields (``main_session_id``, ``workspace_id``,
    ``is_primary``) move neither.

    A deleted Assistant keeps its row: the identity of a primary Assistant is
    derived from how many its owner has had, deleted ones included
    (``AssistantService.ensure_primary_assistant``), and the cleanup checks of
    an Environment read deleted Assistants that still own compute. A deleted
    row holds no workspace and binds no Vault.

    ``environment_name`` is set at creation and never changes. Deleting the
    Environment sets it to NULL on deleted rows only: the check constraint
    refuses to orphan a live Assistant, so the Environment's delete fails
    while one still runs in it.
    """

    __tablename__ = "assistants"
    __table_args__ = (
        Index(None, "owner_id", "updated_at", "assistant_id"),
        Index(None, "environment_name"),
        # An owner has at most one live primary Assistant, whichever of two
        # concurrent first opens inserts it.
        Index(
            "ix_assistants_live_primary",
            "owner_id",
            unique=True,
            postgresql_where=text("is_primary AND NOT deleted"),
        ),
        CheckConstraint("deleted OR environment_name IS NOT NULL", name="live_environment"),
    )

    assistant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text)
    org_id: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(server_default=text("false"))
    display_name: Mapped[str] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    engine_kind: Mapped[str] = mapped_column(Text)
    environment_name: Mapped[str | None] = mapped_column(
        Text, ForeignKey("environments.name", ondelete="SET NULL")
    )
    permission_mode_default: Mapped[str | None] = mapped_column(Text)
    system: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    #: The model the Assistant's own override names; the inherited layers'
    #: model applies when it is NULL.
    model_name: Mapped[str | None] = mapped_column(Text)
    #: The engine-native documents below are validated against what the
    #: Assistant's engine declares and then stored verbatim.
    mcp_servers: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    skills: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    packages: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    engine_options: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    #: The Skill descriptors a catalog selection compiled, as the extension
    #: provider produced them.
    extension_catalog: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    credentials_updated_by: Mapped[str | None] = mapped_column(Text)
    credentials_updated_at: Mapped[datetime | None]
    #: Sessions also name their Assistant, so this constraint is added after
    #: both tables exist (``use_alter``).
    main_session_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("sessions.session_id", ondelete="SET NULL", use_alter=True)
    )
    #: The platform's name for the Assistant's durable files, minted once.
    workspace_id: Mapped[str | None] = mapped_column(Text, unique=True)
    version: Mapped[int] = mapped_column(server_default=text("1"))
    deleted: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssistantVaultBinding(Base):
    """A Credential Vault bound to an Assistant, at its place in the Assistant's order."""

    __tablename__ = "assistant_vault_bindings"
    __table_args__ = (
        UniqueConstraint("assistant_id", "vault_id"),
        Index(None, "vault_id"),
    )

    assistant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("assistants.assistant_id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    vault_id: Mapped[str] = mapped_column(Text, ForeignKey("vaults.vault_id"))


class AssistantMcpAssignment(Base):
    """A catalog MCP server assigned to an Assistant, at its place in the Assistant's order."""

    __tablename__ = "assistant_mcp_assignments"
    __table_args__ = (UniqueConstraint("assistant_id", "provider", "item_id"),)

    assistant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("assistants.assistant_id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    item_id: Mapped[str] = mapped_column(Text)


class AssistantConnector(Base):
    """A connector an Assistant offers, turned off, or both.

    ``hub_server_id`` is the connector hub's virtual server the platform relays
    the connector's calls to; ``turned_off`` records that the owner turned the
    app off on this Assistant, which a later connection does not undo.
    """

    __tablename__ = "assistant_connectors"
    __table_args__ = (
        CheckConstraint("hub_server_id IS NOT NULL OR turned_off", name="records_a_choice"),
    )

    assistant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("assistants.assistant_id", ondelete="CASCADE"), primary_key=True
    )
    connector_id: Mapped[str] = mapped_column(Text, primary_key=True)
    hub_server_id: Mapped[str | None] = mapped_column(Text)
    turned_off: Mapped[bool] = mapped_column(server_default=text("false"))


class AssistantWorkspace(Base):
    """The one workspace an Assistant runs in, keyed by the Assistant.

    ``current_sandbox_id`` is the name of the box the workspace runs in, and
    the only record of a box whose destruction is still unconfirmed. The
    ``provisioning_*`` pair names the Session elected to build the next box;
    that Session's id is reserved here before the Session row is written, so
    it is not a foreign key. ``profile_refresh_*`` names the Session preparing
    a newer revision of the profile on the current box, and
    ``prepared_operation_*`` the one external resource synchronization in
    flight.
    """

    __tablename__ = "assistant_workspaces"
    __table_args__ = (
        CheckConstraint(
            "state IN ('MATERIALIZING', 'READY', 'HIBERNATING', 'RECOVERY_REQUIRED')",
            name="state",
        ),
        Index(None, "current_sandbox_id"),
        # The lifecycle sweeps read the workspaces that name a box, in
        # assistant_id order.
        Index(
            "ix_assistant_workspaces_bound",
            "assistant_id",
            postgresql_where=text("current_sandbox_id IS NOT NULL"),
        ),
    )

    assistant_id: Mapped[str] = mapped_column(
        Text, ForeignKey("assistants.assistant_id", ondelete="CASCADE"), primary_key=True
    )
    created_by_user_id: Mapped[str] = mapped_column(Text)
    engine_kind: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    current_sandbox_id: Mapped[str | None] = mapped_column(Text)
    current_sandbox_expires_at: Mapped[datetime | None]
    #: The workload identity the engine adapter assigned in the box.
    runtime_identity: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    provisioning_session_id: Mapped[str | None] = mapped_column(Text)
    provisioning_sandbox_generation: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    hibernated_at: Mapped[datetime | None]
    post_commit_cleanup_pending: Mapped[bool] = mapped_column(server_default=text("false"))
    profile_refresh_revision: Mapped[str | None] = mapped_column(Text)
    profile_refresh_session_id: Mapped[str | None] = mapped_column(Text)
    profile_refresh_started_at: Mapped[datetime | None]
    prepared_operation_provider: Mapped[str | None] = mapped_column(Text)
    prepared_operation_id: Mapped[str | None] = mapped_column(Text)
    prepared_operation_deadline: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssistantProfile(Base):
    """A user's Agent-program profile, prepared in the workspace's current box.

    ``marker_key`` is
    :func:`~astrabox.core.service.orchestrator.assistant.assistant_workspace_service.assistant_profile_marker_key`
    of the user and Assistant. A row is valid only while ``sandbox_id`` is the
    workspace's current box and ``configuration_revision`` the Assistant's
    current revision; the workspace transitions that retire a box delete its
    rows in the same statement.
    """

    __tablename__ = "assistant_profiles"

    assistant_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("assistant_workspaces.assistant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    marker_key: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text)
    sandbox_id: Mapped[str] = mapped_column(Text)
    configuration_revision: Mapped[str] = mapped_column(Text)
    ready_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssistantDefaults(Base):
    """One layer every Assistant beneath it inherits; ``deployment`` covers the deployment.

    Saves are a compare-and-set on ``version``. ``environment_name`` chooses
    the Environment of new Assistants, so the Environment cannot be deleted
    while a layer names it.
    """

    __tablename__ = "assistant_defaults"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    environment_name: Mapped[str | None] = mapped_column(Text, ForeignKey("environments.name"))
    model_name: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    mcp_servers: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    packages: Mapped[list[Any] | None] = mapped_column(JSONB(none_as_null=True))
    engine_options: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    extension_catalog: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    version: Mapped[int] = mapped_column(server_default=text("1"))
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssistantDefaultVaultBinding(Base):
    """A Credential Vault a defaults layer binds, at its place in the layer's order."""

    __tablename__ = "assistant_default_vault_bindings"
    __table_args__ = (
        UniqueConstraint("scope", "vault_id"),
        Index(None, "vault_id"),
    )

    scope: Mapped[str] = mapped_column(
        Text, ForeignKey("assistant_defaults.scope", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    vault_id: Mapped[str] = mapped_column(Text, ForeignKey("vaults.vault_id"))


class AssistantDefaultMcpAssignment(Base):
    """A catalog MCP server a defaults layer assigns, at its place in the layer's order."""

    __tablename__ = "assistant_default_mcp_assignments"
    __table_args__ = (UniqueConstraint("scope", "provider", "item_id"),)

    scope: Mapped[str] = mapped_column(
        Text, ForeignKey("assistant_defaults.scope", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    item_id: Mapped[str] = mapped_column(Text)
