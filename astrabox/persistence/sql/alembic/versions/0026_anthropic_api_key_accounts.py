"""Let a user keep Anthropic API keys among their model accounts.

An API key has no vendor-issued registration, so an account's subject and
issued client id become optional, required only of a ChatGPT account. A user
has one default account of each vendor rather than one in all.

Revision ID: 0026
Revises: 0025
"""

from alembic import op
import sqlalchemy as sa

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_model_accounts_known_vendor"), "model_accounts", type_="check")
    op.create_check_constraint(
        op.f("ck_model_accounts_known_vendor"), "model_accounts",
        "vendor IN ('chatgpt_plan', 'anthropic_api_key')",
    )
    op.alter_column("model_accounts", "subject", nullable=True)
    op.alter_column("model_accounts", "issued_client_id", nullable=True)
    op.create_check_constraint(
        op.f("ck_model_accounts_chatgpt_registration"), "model_accounts",
        "vendor <> 'chatgpt_plan' OR (subject IS NOT NULL AND issued_client_id IS NOT NULL)",
    )
    op.drop_index("ix_model_accounts_owner_default", table_name="model_accounts")
    op.create_index(
        "ix_model_accounts_owner_default", "model_accounts", ["owner_user_id", "vendor"],
        unique=True, postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM model_accounts WHERE vendor <> 'chatgpt_plan'"))
    op.drop_index("ix_model_accounts_owner_default", table_name="model_accounts")
    op.create_index(
        "ix_model_accounts_owner_default", "model_accounts", ["owner_user_id"],
        unique=True, postgresql_where=sa.text("is_default"),
    )
    op.drop_constraint(
        op.f("ck_model_accounts_chatgpt_registration"), "model_accounts", type_="check"
    )
    op.alter_column("model_accounts", "issued_client_id", nullable=False)
    op.alter_column("model_accounts", "subject", nullable=False)
    op.drop_constraint(op.f("ck_model_accounts_known_vendor"), "model_accounts", type_="check")
    op.create_check_constraint(
        op.f("ck_model_accounts_known_vendor"), "model_accounts", "vendor IN ('chatgpt_plan')",
    )
