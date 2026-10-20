"""Qualify Agent compute ledgers independently of shared authored identity.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from astrabox.persistence.sql.types import KeyText

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

_KEYS = {
    "agent_runtimes": ["agent_id"],
    "agent_prepared_slots": ["slot_id"],
    "agent_box_admissions": ["agent_id", "sandbox_id", "session_id"],
    "agent_retiring_client_pools": ["agent_id", "pool_name"],
    "agent_slot_gateway_entries": ["agent_id", "slot_id"],
    "agent_undestroyed_sandboxes": ["agent_id", "sandbox_id"],
}


def upgrade() -> None:
    for table, keys in _KEYS.items():
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(
                "execution_target", KeyText(128), nullable=False, server_default="astrabox",
            ))
            batch.drop_constraint(f"pk_{table}", type_="primary")
            # Keep the existing key order used by per-Agent and per-slot lookups.
            batch.create_primary_key(f"pk_{table}", [keys[0], "execution_target", *keys[1:]])


def downgrade() -> None:
    raise RuntimeError("execution-target ownership cannot be discarded by a schema downgrade")
