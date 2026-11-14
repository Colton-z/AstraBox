"""Let a RuntimeProfile ask for a container engine in each of its boxes.

Revision ID: 0022
Revises: 0021
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import payload_text

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_profiles", sa.Column("sandbox_container_engine", payload_text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("runtime_profiles", "sandbox_container_engine")
