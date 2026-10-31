"""Index native child relationships without repeatedly reading conversation bodies.

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "transcript_child_run_indexes",
        sa.Column("scope_id", sa.BigInteger(), nullable=False),
        sa.Column("engine_kind", sa.Text().with_variant(mysql.VARBINARY(64), "mysql"), nullable=False),
        sa.Column("through_seq", sa.BigInteger(), nullable=False),
        sa.Column("entries", sa.JSON(none_as_null=True).with_variant(
            postgresql.JSONB(none_as_null=True), "postgresql",
        ), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_id"], ["transcript_scopes.scope_id"],
            name=op.f("fk_transcript_child_run_indexes_scope_id_transcript_scopes"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scope_id", "engine_kind", name=op.f("pk_transcript_child_run_indexes")),
        mysql_engine="InnoDB", mysql_row_format="DYNAMIC", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_bin",
    )


def downgrade() -> None:
    op.drop_table("transcript_child_run_indexes")
