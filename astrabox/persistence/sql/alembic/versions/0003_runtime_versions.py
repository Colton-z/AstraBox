"""Keep observed versions with the runtime instance they describe.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from astrabox.persistence.sql.types import payload_json

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("runtime_versions", payload_json(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "runtime_versions")
