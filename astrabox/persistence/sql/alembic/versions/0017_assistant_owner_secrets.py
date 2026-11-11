"""Record which credentials an Assistant's owner has supplied, and when.

The values live in the secret store; the row keeps only what an owner may read
back and what moves the Assistant's revision when a value changes.

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import payload_json

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("assistants", sa.Column("owner_secrets", payload_json(), nullable=True))


def downgrade() -> None:
    op.drop_column("assistants", "owner_secrets")
