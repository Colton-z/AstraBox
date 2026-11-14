"""Keep the model-vendor accounts users link, and their sign-ins in progress.

A user links a ChatGPT account through OpenAI's sign-in; the platform keeps the
account's tokens in the secret store and renews the access token under a claim
on the account's row. A sign-in in progress keeps its state, nonce and PKCE
verifier on the server until its callback completes it.

Revision ID: 0020
Revises: 0019
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import KeyText, UTCDateTime, payload_text

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None

_MYSQL = {"mysql_engine": "InnoDB", "mysql_row_format": "DYNAMIC",
          "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "model_accounts",
        sa.Column("account_id", KeyText(128), nullable=False),
        sa.Column("org_id", payload_text(), nullable=False),
        sa.Column("owner_user_id", KeyText(512), nullable=False),
        sa.Column("vendor", payload_text(), nullable=False),
        sa.Column("label", payload_text(), nullable=False),
        sa.Column("email", payload_text(), nullable=True),
        sa.Column("subject", KeyText(512), nullable=False),
        sa.Column("issued_client_id", KeyText(512), nullable=False),
        sa.Column("scopes", payload_text(), nullable=False),
        sa.Column("status", payload_text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("access_expires_at", UTCDateTime(), nullable=True),
        sa.Column("refresh_claimed_until", UTCDateTime(), nullable=True),
        sa.Column("last_error_code", payload_text(), nullable=True),
        sa.Column("last_error_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", UTCDateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_model_accounts")),
        sa.CheckConstraint("vendor IN ('chatgpt_plan')", name=op.f("ck_model_accounts_known_vendor")),
        sa.CheckConstraint(
            "status IN ('active', 'sign_in_required')", name=op.f("ck_model_accounts_known_status")
        ),
        sa.UniqueConstraint(
            "owner_user_id", "subject", "issued_client_id",
            name=op.f("uq_model_accounts_owner_user_id_subject_issued_client_id"),
        ),
        **_MYSQL,
    )
    op.create_index(
        "ix_model_accounts_owner_default", "model_accounts", ["owner_user_id"],
        unique=True, postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        op.f("ix_model_accounts_owner_user_id_created_at"), "model_accounts",
        ["owner_user_id", "created_at"],
    )
    op.create_table(
        "model_account_authorizations",
        sa.Column("authorization_id", KeyText(128), nullable=False),
        sa.Column("owner_user_id", KeyText(512), nullable=False),
        sa.Column("account_id", KeyText(128), nullable=True),
        sa.Column("client_id", payload_text(), nullable=False),
        sa.Column("redirect_uri", payload_text(), nullable=False),
        sa.Column("state", payload_text(), nullable=False),
        sa.Column("nonce", payload_text(), nullable=False),
        sa.Column("code_verifier", payload_text(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("authorization_id", name=op.f("pk_model_account_authorizations")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["model_accounts.account_id"],
            name=op.f("fk_model_account_authorizations_account_id_model_accounts"),
            ondelete="CASCADE",
        ),
        **_MYSQL,
    )
    op.create_index(
        op.f("ix_model_account_authorizations_expires_at"), "model_account_authorizations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("model_account_authorizations")
    op.drop_table("model_accounts")
