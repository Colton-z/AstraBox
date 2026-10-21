"""Keep automatic trigger ownership independent of the process serving its metadata.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from astrabox.persistence.sql.types import KeyText

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column(
        "execution_target", KeyText(128), nullable=False, server_default="astrabox",
    ))


def downgrade() -> None:
    raise RuntimeError("execution-target ownership cannot be discarded by a schema downgrade")
