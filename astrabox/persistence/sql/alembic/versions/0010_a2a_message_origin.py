"""Retain the platform-authenticated sender of a conversation message.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column(
        "a2a_access", sa.Text(), nullable=False, server_default=sa.text("'disabled'"),
    ))
    op.add_column("agents", sa.Column(
        "a2a_agent_ids", sa.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]"),
    ))
    op.create_check_constraint(
        op.f("ck_agents_a2a_access"), "agents", "a2a_access IN ('disabled', 'all', 'selected')",
    )
    op.add_column("agent_task_references", sa.Column(
        "origin", sa.JSON(none_as_null=True).with_variant(
            postgresql.JSONB(none_as_null=True), "postgresql",
        ), nullable=True,
    ))


def downgrade() -> None:
    op.drop_constraint(op.f("ck_agents_a2a_access"), "agents", type_="check")
    op.drop_column("agents", "a2a_agent_ids")
    op.drop_column("agents", "a2a_access")
    op.drop_column("agent_task_references", "origin")
