"""Keep sandbox inventory and quota reservations in their physical execution target.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from astrabox.persistence.sql.types import KeyText

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

_KEYS = {
    "sandbox_capacity": ["backend"],
    "sandbox_reservations": ["backend", "reservation_key"],
    "sandbox_inventory": ["backend", "sandbox_id"],
}


def upgrade() -> None:
    for table in ("sandbox_reservations", "sandbox_inventory"):
        op.drop_constraint(f"fk_{table}_backend_sandbox_capacity", table, type_="foreignkey")
    for table, keys in _KEYS.items():
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(
                "execution_target", KeyText(128), nullable=False, server_default="astrabox",
            ))
            batch.drop_constraint(f"pk_{table}", type_="primary")
            batch.create_primary_key(f"pk_{table}", [keys[0], "execution_target", *keys[1:]])
    for table in ("sandbox_reservations", "sandbox_inventory"):
        op.create_foreign_key(
            f"fk_{table}_capacity", table, "sandbox_capacity",
            ["backend", "execution_target"], ["backend", "execution_target"], ondelete="CASCADE",
        )


def downgrade() -> None:
    raise RuntimeError("execution-target ownership cannot be discarded by a schema downgrade")
