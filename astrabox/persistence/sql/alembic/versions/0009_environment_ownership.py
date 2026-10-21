"""Use Environment ownership for runtime references without an implicit owner.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = (
    "agent_runtimes", "agent_prepared_slots", "agent_box_admissions",
    "agent_retiring_client_pools", "agent_slot_gateway_entries", "agent_undestroyed_sandboxes",
    "sessions", "assistant_workspaces", "sandbox_capacity", "sandbox_reservations",
    "sandbox_inventory", "deployments",
)


def upgrade() -> None:
    name = context.config.get_main_option("astrabox.environment_name")
    if not name:
        raise RuntimeError("Environment ownership requires an explicit configured name")
    owner = str(sa.literal(name).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    # An old shared database cannot be assigned to one of its compute owners
    # while other owners' runtime references still exist.
    for table in _TABLES:
        op.execute(f"""
            DO $environment$
            BEGIN
                IF EXISTS (SELECT 1 FROM {table} WHERE execution_target IS DISTINCT FROM {owner}) THEN
                    RAISE EXCEPTION 'database contains another execution owner in {table}; separate the Environment databases before upgrading';
                END IF;
            END $environment$
        """)
    for table in _TABLES:
        op.alter_column(table, "execution_target", new_column_name="environment_name")
        op.alter_column(table, "environment_name", server_default=None)
    for table, key in (("sessions", "session_id"), ("assistant_workspaces", "assistant_id")):
        op.execute(
            f"ALTER INDEX ix_{table}_execution_target_{key} "
            f"RENAME TO ix_{table}_environment_name_{key}"
        )
    op.create_table(
        "environments",
        sa.Column("singleton", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("singleton", name="pk_environments"),
        sa.CheckConstraint("singleton", name="ck_environments_singleton"),
        sa.UniqueConstraint("name", name="uq_environments_name"),
    )
    op.create_table(
        "environment_members",
        sa.Column("environment_name", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("environment_name", "user_id", name="pk_environment_members"),
        sa.ForeignKeyConstraint(
            ["environment_name"], ["environments.name"], ondelete="CASCADE",
            name="fk_environment_members_environment_name_environments",
        ),
    )
    op.create_index(
        "ix_environment_members_user_id_environment_name", "environment_members",
        ["user_id", "environment_name"],
    )


def downgrade() -> None:
    raise RuntimeError("Environment ownership cannot be discarded by a schema downgrade")
