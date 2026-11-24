"""Keep operator evidence about the engine process a failed response ended with.

The response's ``error`` is what its conversation shows. The process's stderr
is output that conversation must not see, and the box that produced it can be
gone by the time an operator looks, so the process's account is kept on the
response in a column of its own.

Revision ID: 0031
Revises: 0030
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import payload_json

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("session_responses", sa.Column("diagnostic", payload_json(), nullable=True))


def downgrade() -> None:
    op.drop_column("session_responses", "diagnostic")
