"""Let a personal schedule run on its source Assistant.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None

_PERSONAL_OWNER = "ck_deployments_personal_owner"


def upgrade() -> None:
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
