"""An Assistant's own routines and its proactivity switch.

A personal schedule may name its source Assistant as the one that runs it, and
an Assistant's owner can switch off the jobs its Agent program scheduled for
itself.

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None

_PERSONAL_OWNER = "ck_deployments_personal_owner"


def upgrade() -> None:
    op.add_column("assistants", sa.Column(
        "proactive", sa.Boolean(), server_default=sa.text("true"), nullable=False,
    ))
    op.drop_constraint(op.f(_PERSONAL_OWNER), "deployments", type_="check")
    op.create_check_constraint(
        op.f(_PERSONAL_OWNER), "deployments",
        "scope IS NULL OR (scope = 'personal' AND owner_user_id IS NOT NULL "
        "AND owner_org_id IS NOT NULL AND source_assistant_id IS NOT NULL "
        "AND (agent_id IS NOT NULL OR assistant_id = source_assistant_id))",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(_PERSONAL_OWNER), "deployments", type_="check")
    op.create_check_constraint(
        op.f(_PERSONAL_OWNER), "deployments",
        "scope IS NULL OR (scope = 'personal' AND agent_id IS NOT NULL "
        "AND owner_user_id IS NOT NULL AND owner_org_id IS NOT NULL "
        "AND source_assistant_id IS NOT NULL)",
    )
    op.drop_column("assistants", "proactive")
