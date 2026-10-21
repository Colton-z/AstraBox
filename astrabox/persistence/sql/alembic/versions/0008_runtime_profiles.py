"""Give runtime recipes their own identity, separate from Environment ownership.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.rename_table("environments", "runtime_profiles")
    for table in ("agents", "assistants", "assistant_defaults"):
        op.alter_column(table, "environment_name", new_column_name="runtime_profile_name")
        op.execute(
            f"ALTER TABLE {table} RENAME CONSTRAINT "
            f"fk_{table}_environment_name_environments TO "
            f"fk_{table}_runtime_profile_name_runtime_profiles"
        )
    op.execute(
        "ALTER TABLE runtime_profiles RENAME CONSTRAINT pk_environments TO pk_runtime_profiles"
    )
    for table in ("agents", "assistants"):
        op.execute(
            f"ALTER TABLE {table} RENAME CONSTRAINT "
            f"ck_{table}_live_environment TO ck_{table}_live_runtime_profile"
        )
    op.execute(
        "ALTER INDEX ix_agents_environment_name_agent_id "
        "RENAME TO ix_agents_runtime_profile_name_agent_id"
    )
    op.execute(
        "ALTER INDEX ix_assistants_environment_name RENAME TO ix_assistants_runtime_profile_name"
    )


def downgrade() -> None:
    raise RuntimeError("runtime profile identities cannot be discarded by a schema downgrade")
