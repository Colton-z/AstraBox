"""Let an Agent name the model account its creator's conversations spend.

Revision ID: 0021
Revises: 0020
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import payload_text

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("model_account_id", payload_text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "model_account_id")
