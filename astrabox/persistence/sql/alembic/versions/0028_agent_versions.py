"""Keep each version of an Agent's authored definition.

Every Agent starts its history at the definition and version it has when this
revision runs; the versions it had before were not kept.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import KeyText, StringList, UTCDateTime, payload_json, payload_text

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None

_MYSQL = {"mysql_engine": "InnoDB", "mysql_row_format": "DYNAMIC",
          "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}

#: Authored fields a definition carries under the column's own name.
_SCALARS = (
    "name", "description", "model", "system", "artifacts_enabled", "a2a_access",
    "terminal_panel", "diff_panel", "exposure_mode", "model_account_id",
    "idle_hibernate_seconds", "prepared_capacity", "enabled", "runtime_profile_name",
    "visibility",
)
_OBJECTS = ("engine_options", "mcp_servers", "default_repo")
_STRING_LISTS = ("use_cases", "a2a_agent_ids", "admins", "allowed_user_ids")
_JSON_LISTS = ("skills", "packages")

_AGENTS = sa.table(
    "agents",
    sa.column("agent_id", KeyText(128)),
    *(sa.column(name) for name in _SCALARS),
    *(sa.column(name, payload_json()) for name in (*_OBJECTS, *_JSON_LISTS, "catalog_skills")),
    *(sa.column(name, StringList()) for name in (*_STRING_LISTS, "tags", "catalog_skill_ids")),
    sa.column("display_name"),
    sa.column("icon"),
    sa.column("version", sa.Integer()),
    sa.column("updated_by"),
    sa.column("updated_at", UTCDateTime()),
)
_LINKS = sa.table(
    "agent_category_links",
    sa.column("agent_id", KeyText(128)), sa.column("category_id", KeyText(128)), sa.column("position"),
)
_VAULTS = sa.table(
    "agent_vault_bindings",
    sa.column("agent_id", KeyText(128)), sa.column("vault_id", KeyText(128)), sa.column("position"),
)
_ASSIGNMENTS = sa.table(
    "agent_mcp_assignments",
    sa.column("agent_id", KeyText(128)), sa.column("provider", KeyText(128)),
    sa.column("item_id", KeyText(1024)), sa.column("position"),
)
_CONNECTORS = sa.table(
    "agent_connector_servers",
    sa.column("agent_id", KeyText(128)), sa.column("connector_id", KeyText(512)),
    sa.column("hub_server_id"),
)


def _children(bind: sa.Connection, table: sa.TableClause, order: str) -> dict[str, list[Any]]:
    """Each Agent's rows of ``table``, in the order an Agent read returns them."""
    grouped: dict[str, list[Any]] = {}
    for row in bind.execute(sa.select(table).order_by(table.c.agent_id, table.c[order])).mappings():
        grouped.setdefault(row["agent_id"], []).append(row)
    return grouped


def _definition(
    agent: Any, links: list[Any], vaults: list[Any], assignments: list[Any], connectors: list[Any],
) -> dict[str, Any]:
    """The Agent's authored fields, as ``AgentRepository`` records them for a version."""
    display_meta: dict[str, Any] = {"tags": list(agent["tags"] or [])}
    for key in ("display_name", "icon"):
        if agent[key] is not None:
            display_meta[key] = agent[key]
    return {
        **{name: agent[name] for name in (*_SCALARS, *_OBJECTS)},
        **{name: list(agent[name] or []) for name in (*_STRING_LISTS, *_JSON_LISTS)},
        "display_meta": display_meta,
        "extension_catalog": {
            "skill_ids": list(agent["catalog_skill_ids"] or []),
            "skills": list(agent["catalog_skills"] or []),
        },
        "categories": [row["category_id"] for row in links],
        "credential_vault_ids": [row["vault_id"] for row in vaults],
        "mcp_assignments": [
            {"provider": row["provider"], "item_id": row["item_id"]} for row in assignments
        ],
        "connector_servers": (
            {row["connector_id"]: row["hub_server_id"] for row in connectors} or None
        ),
    }


def upgrade() -> None:
    versions = op.create_table(
        "agent_versions",
        sa.Column("agent_id", KeyText(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", payload_json(), nullable=False),
        sa.Column("operation", payload_text(), nullable=False),
        sa.Column("restored_from_version", sa.Integer(), nullable=True),
        sa.Column("author_user_id", payload_text(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("agent_id", "version", name=op.f("pk_agent_versions")),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.agent_id"], ondelete="CASCADE",
            name=op.f("fk_agent_versions_agent_id_agents"),
        ),
        sa.CheckConstraint(
            "operation IN ('create', 'update', 'restore', 'baseline')",
            name=op.f("ck_agent_versions_operation"),
        ),
        sa.CheckConstraint(
            "(operation = 'restore') = (restored_from_version IS NOT NULL)",
            name=op.f("ck_agent_versions_restore_source"),
        ),
        **_MYSQL,
    )
    bind = op.get_bind()
    links = _children(bind, _LINKS, "position")
    vaults = _children(bind, _VAULTS, "position")
    assignments = _children(bind, _ASSIGNMENTS, "position")
    connectors = _children(bind, _CONNECTORS, "connector_id")
    baselines = [
        {
            "agent_id": agent["agent_id"],
            "version": agent["version"],
            "definition": _definition(
                agent,
                links.get(agent["agent_id"], []),
                vaults.get(agent["agent_id"], []),
                assignments.get(agent["agent_id"], []),
                connectors.get(agent["agent_id"], []),
            ),
            "operation": "baseline",
            "author_user_id": agent["updated_by"],
            "created_at": agent["updated_at"],
        }
        for agent in bind.execute(sa.select(_AGENTS)).mappings()
    ]
    if baselines:
        op.bulk_insert(versions, baselines)


def downgrade() -> None:
    op.drop_table("agent_versions")
