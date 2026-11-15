"""Record the OAuth2 grant an environment credential obtains its value by.

An ``environment_variable`` credential can hold, instead of a stored value, the
configuration of an OAuth2 grant; the platform then obtains the access token
and renews it before it expires. The grant type is the one field of that
configuration the credential row had no column for.

Revision ID: 0019
Revises: 0018
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import payload_text

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("vault_credentials", sa.Column("token_grant_type", payload_text(), nullable=True))


def downgrade() -> None:
    op.drop_column("vault_credentials", "token_grant_type")
