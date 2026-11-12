"""Record the background tasks a session's runtime reports with its status.

A runtime can keep work running in its box after its response ended, such as a
Claude Code background shell. The session row keeps those tasks with the
status they came with, so the conversation can show and stop them.

Revision ID: 0018
Revises: 0017
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import payload_json

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("runtime_background_tasks", payload_json(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "runtime_background_tasks")
