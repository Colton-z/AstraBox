"""Retain the sandbox serving a browser endpoint through conversation idleness.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("exposed_sandbox_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "exposed_sandbox_id")
