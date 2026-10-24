"""Order conversation messages by native observations rather than receipt time.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    for table in ("session_inputs", "session_responses"):
        op.add_column(table, sa.Column("conversation_order", sa.BigInteger(), nullable=True))
        op.create_index(f"ix_{table}_conversation_order", table, ["session_id", "conversation_order", "seq"])

    # The shared history repository indexes existing native output on every
    # relational provider; this revision owns only the physical schema.


def downgrade() -> None:
    for table in ("session_inputs", "session_responses"):
        op.drop_index(f"ix_{table}_conversation_order", table_name=table)
        op.drop_column(table, "conversation_order")
