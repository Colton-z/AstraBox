"""Keep the registered code of a session's last error beside its message.

Revision ID: 0027
Revises: 0026
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import payload_text

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("last_error_code", payload_text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "last_error_code")
