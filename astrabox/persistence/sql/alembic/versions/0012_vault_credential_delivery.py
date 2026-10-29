"""Record how each environment-variable credential is delivered.

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("vault_credentials", sa.Column("delivery", sa.Text(), nullable=True))
    # Every environment-variable credential saved before this revision was a
    # substitution credential: that was the only delivery there was.
    op.execute(
        "UPDATE vault_credentials SET delivery = 'substitution' "
        "WHERE auth_type = 'environment_variable'"
    )


def downgrade() -> None:
    op.drop_column("vault_credentials", "delivery")
