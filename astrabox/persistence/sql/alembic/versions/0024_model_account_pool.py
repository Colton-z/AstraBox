"""Let an account's owner share it with the team, and remember which account a conversation spends.

An account shared with the team may be spent by another member's conversation
once that member's own account reached its plan usage limit; the account keeps
when the platform last handed it to such a conversation, and until when a
reported usage limit keeps it from being offered. A session keeps the account
whose access token its sandbox spends.

Revision ID: 0024
Revises: 0023
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import UTCDateTime, payload_text

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "model_accounts",
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("model_accounts", sa.Column("usage_limited_until", UTCDateTime(), nullable=True))
    op.add_column("model_accounts", sa.Column("pool_last_used_at", UTCDateTime(), nullable=True))
    op.add_column("sessions", sa.Column("model_account_id", payload_text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "model_account_id")
    op.drop_column("model_accounts", "pool_last_used_at")
    op.drop_column("model_accounts", "usage_limited_until")
    op.drop_column("model_accounts", "shared")
