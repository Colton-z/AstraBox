"""Keep conversation and Assistant compute ownership with the durable binding.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from astrabox.persistence.sql.types import KeyText

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    for table, key in (("sessions", "session_id"), ("assistant_workspaces", "assistant_id")):
        op.add_column(table, sa.Column(
            "execution_target", KeyText(128), nullable=False, server_default="astrabox",
        ))
        op.create_index(f"ix_{table}_execution_target_{key}", table, ["execution_target", key])


def downgrade() -> None:
    raise RuntimeError("execution-target ownership cannot be discarded by a schema downgrade")
