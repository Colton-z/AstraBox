"""Let a RuntimeProfile size the CPU and memory of each of its boxes.

Revision ID: 0023
Revises: 0022
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import payload_text

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("runtime_profiles", sa.Column("sandbox_cpu", payload_text(), nullable=True))
    op.add_column("runtime_profiles", sa.Column("sandbox_memory", payload_text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runtime_profiles", "sandbox_memory")
    op.drop_column("runtime_profiles", "sandbox_cpu")
