"""Let a RuntimeProfile join its boxes to host networks and mount host storage.

Revision ID: 0025
Revises: 0024
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_profiles", sa.Column("sandbox_networks", postgresql.ARRAY(sa.Text()), nullable=True)
    )
    op.add_column(
        "runtime_profiles",
        sa.Column(
            "sandbox_mounts",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("runtime_profiles", "sandbox_mounts")
    op.drop_column("runtime_profiles", "sandbox_networks")
