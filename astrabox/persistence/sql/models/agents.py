"""Agents, their access and catalog entries, and the runtime ledgers of their shared boxes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Double,
    ForeignKey,
    Index,
    Integer,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from astrabox.persistence.sql.base import Base
from astrabox.persistence.sql.schema import (
    MYSQL_TABLE_OPTIONS,
    EmptyListDefault,
    EmptyObjectDefault,
    NullCount,
    TextDefault,
    boolean_checks,
    string_list_check,
)
from astrabox.persistence.sql.types import KeyText, StringList, payload_json, payload_text

_LIVE = text("state <> 'DELETED'")


class Agent(Base):
    """An Agent's authored definition; Sessions, Deployments and its ledgers reference it.

    ``version`` moves with every change to an authored field, so a write that
    names the version it read cannot overwrite a change made since.

    ``state`` is ``ACTIVE``, ``DELETING`` while a deletion runs, or ``DELETED``:
    a deleted Agent is kept for the conversations that still name it and is
    hidden from every other read. A live Agent always names an existing
    Environment; deleting an Environment clears the name on Agents already
    deleted, and the ``live_environment`` check refuses it for any other.
    """

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("state IN ('ACTIVE', 'DELETING', 'DELETED')", name="state"),
        CheckConstraint(
            "state = 'DELETED' OR environment_name IS NOT NULL", name="live_environment"
        ),
        CheckConstraint("prepared_capacity BETWEEN 0 AND 1000", name="prepared_capacity"),
        CheckConstraint(
            "visibility IN ('public', 'private', 'allowlist')", name="visibility"
        ),
        Index(None, "name_key", "agent_id", postgresql_where=_LIVE),
        Index(None, "updated_at", "agent_id", postgresql_where=_LIVE),
        # Environment deletion's reference check and the Environment's
        # reconciliation pages.
        Index(None, "environment_name", "agent_id"),
        # The prepared-capacity sweep's pages.
        Index(
            "ix_agents_prepared_capacity_sweep",
            "agent_id",
            postgresql_where=text("prepared_capacity > 0 AND state = 'ACTIVE'"),
        ),
        *boolean_checks("artifacts_enabled", "terminal_panel", "diff_panel", "enabled"),
        string_list_check("tags"),
        string_list_check("use_cases"),
        string_list_check("admins"),
        string_list_check("allowed_user_ids"),
        string_list_check("catalog_skill_ids"),
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    name: Mapped[str] = mapped_column(payload_text())
    #: The name without its case (``str.casefold``): the console list's order,
    #: compared byte by byte so the order and a page's resume point agree.
    name_key: Mapped[str] = mapped_column(KeyText(512, postgres_collation="C"))
    display_name: Mapped[str | None] = mapped_column(payload_text())
    icon: Mapped[str | None] = mapped_column(payload_text())
    tags: Mapped[list[str]] = mapped_column(StringList(), server_default=EmptyListDefault(text_array=True))
    description: Mapped[str | None] = mapped_column(payload_text())
    use_cases: Mapped[list[str]] = mapped_column(StringList(), server_default=EmptyListDefault(text_array=True))
    model: Mapped[str] = mapped_column(payload_text())
    system: Mapped[str | None] = mapped_column(payload_text())
    #: Interpreted only by the Environment's engine adapter.
    engine_options: Mapped[dict[str, Any] | None] = mapped_column(payload_json())
    artifacts_enabled: Mapped[bool] = mapped_column(server_default=true())
    #: Skill sources, materialized by the runtime as given.
    skills: Mapped[list[Any]] = mapped_column(server_default=EmptyListDefault())
    #: Name-keyed MCP server definitions handed to the engine.
    mcp_servers: Mapped[dict[str, Any] | None] = mapped_column(payload_json())
    #: Package sources the runtime checks out as given.
    packages: Mapped[list[Any]] = mapped_column(server_default=EmptyListDefault())
    default_repo: Mapped[dict[str, Any] | None] = mapped_column(payload_json())
    terminal_panel: Mapped[bool] = mapped_column(server_default=text("false"))
    diff_panel: Mapped[bool] = mapped_column(server_default=text("false"))
    exposure_mode: Mapped[str | None] = mapped_column(payload_text())
    idle_hibernate_seconds: Mapped[int | None] = mapped_column(Integer)
    prepared_capacity: Mapped[int] = mapped_column(Integer, server_default="0")
    enabled: Mapped[bool] = mapped_column(server_default=true())
    environment_name: Mapped[str | None] = mapped_column(
        KeyText(512), ForeignKey(
            "environments.name", ondelete="SET NULL", info={"mysql_restrict_environment": True},
        ),
    )
    visibility: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault("private"))
    admins: Mapped[list[str]] = mapped_column(StringList(), server_default=EmptyListDefault(text_array=True))
    allowed_user_ids: Mapped[list[str]] = mapped_column(StringList(), server_default=EmptyListDefault(text_array=True))
    #: Catalog Skill ids an extension console selected, and the Skills they compiled to.
    catalog_skill_ids: Mapped[list[str]] = mapped_column(StringList(), server_default=EmptyListDefault(text_array=True))
    catalog_skills: Mapped[list[Any]] = mapped_column(server_default=EmptyListDefault())
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    state: Mapped[str] = mapped_column(payload_text(), server_default=TextDefault("ACTIVE"))
    created_by: Mapped[str] = mapped_column(payload_text())
    org_id: Mapped[str | None] = mapped_column(payload_text())
    updated_by: Mapped[str | None] = mapped_column(payload_text())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentCategory(Base):
    """A category a platform administrator defines; Agents are listed under it."""

    __tablename__ = "agent_categories"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    category_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    name: Mapped[str] = mapped_column(payload_text())
    #: The name without its case; two categories cannot read the same.
    name_key: Mapped[str] = mapped_column(KeyText(512), unique=True)
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentCategoryLink(Base):
    """One category an Agent is listed under, in the Agent's order."""

    __tablename__ = "agent_category_links"
    __table_args__ = (
        Index(None, "category_id"),
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agent_categories.category_id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer)


class AgentFavorite(Base):
    """One user's favorite Agent."""

    __tablename__ = "agent_favorites"
    __table_args__ = (
        Index(None, "agent_id"),
        MYSQL_TABLE_OPTIONS,
    )

    user_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentVaultBinding(Base):
    """A credential vault an administrator bound to an Agent, in the bound order."""

    __tablename__ = "agent_vault_bindings"
    __table_args__ = (
        Index(None, "vault_id"),
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    vault_id: Mapped[str] = mapped_column(KeyText(128), ForeignKey("vaults.vault_id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class AgentMCPAssignment(Base):
    """One MCP server assigned to an Agent from an extension catalog, in order.

    ``item_id`` is the server's id in the catalog ``provider`` names. Catalogs
    are separate systems, so the reference is checked when it is assigned and
    resolved through the provider at each Session start.
    """

    __tablename__ = "agent_mcp_assignments"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    item_id: Mapped[str] = mapped_column(KeyText(1024), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class AgentConnectorServer(Base):
    """The connector hub's server for one connector an Agent offers."""

    __tablename__ = "agent_connector_servers"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    connector_id: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    hub_server_id: Mapped[str] = mapped_column(payload_text())


class AgentRuntime(Base):
    """The runtime record of one Agent, created with it.

    What the runtime keeps per Agent rather than per box or per slot: its
    resident shared box, the runtime generation its prepared capacity follows,
    its client pool, the counter its conversations' POSIX owners are drawn
    from and the durable workspace its shared boxes mount.

    Every write that changes the Agent's prepared slots or admits a
    conversation locks this row first, so those writers serialize per Agent
    without deadlocking each other. ``slots_revision`` moves with every change
    to the slots and ``admissions_revision`` with every new admission; a writer
    whose decision rests on what it read names the revision it read. Releasing
    an admission takes no lock: it only leaves more room than a writer counted.
    """

    __tablename__ = "agent_runtimes"
    __table_args__ = (
        CheckConstraint(
            "(sandbox_id IS NULL) = (sandbox_backend IS NULL)", name="resident_backend"
        ),
        CheckConstraint(
            "(client_pool_name IS NULL) = (client_pool_backend IS NULL)", name="client_pool_backend"
        ),
        CheckConstraint(
            NullCount("resources_operation_provider", "resources_operation_id",
                      "resources_operation_deadline").in_((0, 3)),
            name="resources_operation",
        ),
        Index(None, "sandbox_id", postgresql_where=text("sandbox_id IS NOT NULL")),
        # The resident-box sweeps' pages.
        Index(
            "ix_agent_runtimes_resident_sweep",
            "agent_id",
            postgresql_where=text("sandbox_id IS NOT NULL"),
        ),
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    #: The resident shared box new conversations of the Agent join.
    sandbox_id: Mapped[str | None] = mapped_column(KeyText(128))
    sandbox_backend: Mapped[str | None] = mapped_column(payload_text())
    #: The box generation the resident box was created for.
    resident_generation: Mapped[str | None] = mapped_column(payload_text())
    #: The resident box's lease end as the control plane last reported it.
    expires_at: Mapped[datetime | None]
    #: The runtime recipe prepared capacity follows, and the base-box recipe.
    runtime_generation: Mapped[str | None] = mapped_column(payload_text())
    sandbox_generation: Mapped[str | None] = mapped_column(payload_text())
    #: The lifetime of the client pool started for this runtime generation.
    client_pool_epoch: Mapped[str | None] = mapped_column(payload_text())
    client_pool_name: Mapped[str | None] = mapped_column(payload_text())
    client_pool_backend: Mapped[str | None] = mapped_column(payload_text())
    #: An administrator's explicit request to rebuild prepared capacity.
    prewarm_revision: Mapped[str | None] = mapped_column(payload_text())
    #: The generation the last reconciliation prepared, and why it failed.
    prepared_runtime_generation: Mapped[str | None] = mapped_column(payload_text())
    prepared_runtime_error: Mapped[str | None] = mapped_column(payload_text())
    #: The external resource synchronization in flight for this Agent: the
    #: provider, its operation id and its deadline in epoch seconds.
    resources_operation_provider: Mapped[str | None] = mapped_column(payload_text())
    resources_operation_id: Mapped[str | None] = mapped_column(payload_text())
    resources_operation_deadline: Mapped[float | None] = mapped_column(Double)
    slots_revision: Mapped[int] = mapped_column(BigInteger, server_default="0")
    admissions_revision: Mapped[int] = mapped_column(BigInteger, server_default="0")
    #: The last POSIX owner handed to one of the Agent's conversations. It only
    #: moves forward: a conversation's files outlive it, so a number is never
    #: handed out twice.
    last_conversation_uid: Mapped[int | None] = mapped_column(Integer)
    #: The durable root every shared box of the Agent mounts; set once.
    workspace_id: Mapped[str | None] = mapped_column(KeyText(128), unique=True)
    #: How many seats the Agent's latest pool box held, which sizes its pool.
    seats_per_box: Mapped[int] = mapped_column(Integer, server_default="0")
    #: When a new conversation last found no prepared unit to claim.
    capacity_last_miss_at: Mapped[datetime | None]


class AgentPreparedSlot(Base):
    """One engine seat prepared in a shared box of the Agent, waiting for a conversation.

    A seat is ``prepared`` until a conversation claims it (``claimed``) or it
    is withdrawn (``retiring``) until its placement is closed. ``manifest``
    holds the rest of the engine's preparation receipt as given.
    """

    __tablename__ = "agent_prepared_slots"
    __table_args__ = (
        CheckConstraint("state IN ('prepared', 'claimed', 'retiring')", name="state"),
        Index(None, "agent_id", "prepared_at"),
        Index(None, "sandbox_id"),
        MYSQL_TABLE_OPTIONS,
    )

    slot_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE")
    )
    state: Mapped[str] = mapped_column(payload_text())
    placement: Mapped[str] = mapped_column(payload_text())
    sandbox_id: Mapped[str] = mapped_column(KeyText(128))
    sandbox_backend: Mapped[str] = mapped_column(payload_text())
    runtime_generation: Mapped[str | None] = mapped_column(payload_text())
    prepared_at: Mapped[datetime | None]
    claimed_session_id: Mapped[str | None] = mapped_column(payload_text())
    claimed_at: Mapped[datetime | None]
    retire_reason: Mapped[str | None] = mapped_column(payload_text())
    retiring_at: Mapped[datetime | None]
    manifest: Mapped[dict[str, Any]] = mapped_column(server_default=EmptyObjectDefault())


class AgentBoxAdmission(Base):
    """A conversation admitted to one of the Agent's boxes, counted against its memory.

    The box's own memory reading does not cover a conversation for a while
    after it is admitted; until then the admission reserves its growth.
    """

    __tablename__ = "agent_box_admissions"
    __table_args__ = (
        Index(None, "sandbox_id"),
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    sandbox_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    admitted_at: Mapped[datetime]


class AgentRetiringClientPool(Base):
    """A client pool of the Agent that a newer one displaced, until it is retired."""

    __tablename__ = "agent_retiring_client_pools"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    pool_name: Mapped[str] = mapped_column(KeyText(512), primary_key=True)
    backend: Mapped[str] = mapped_column(payload_text())
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentSlotGatewayEntry(Base):
    """Which Session owns the model gateway key a claimed prepared slot spends.

    Kept until the Session ends; removing the entry is what releases the key,
    through the endpoint provider that issued it.
    """

    __tablename__ = "agent_slot_gateway_entries"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    slot_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(payload_text())
    #: The model endpoint provider that issued the key; empty for the default one.
    endpoint_provider: Mapped[str | None] = mapped_column(payload_text())
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentUndestroyedSandbox(Base):
    """A sandbox of the Agent whose destruction could not be confirmed.

    Its name stays here, where it can still be addressed, until something
    confirms the box is gone.
    """

    __tablename__ = "agent_undestroyed_sandboxes"
    __table_args__ = (
        MYSQL_TABLE_OPTIONS,
    )

    agent_id: Mapped[str] = mapped_column(
        KeyText(128), ForeignKey("agents.agent_id", ondelete="CASCADE"), primary_key=True
    )
    sandbox_id: Mapped[str] = mapped_column(KeyText(128), primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())
