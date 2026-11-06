"""Keep the files a message carries, staged by their caller and then owned by the input.

A channel message records the provider's references to its files, which are
fetched when the message is sent.

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import KeyText, UTCDateTime, binary_payload, payload_json, payload_text

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None

_MYSQL = {"mysql_engine": "InnoDB", "mysql_row_format": "DYNAMIC",
          "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "input_attachments",
        sa.Column("attachment_id", KeyText(64), nullable=False),
        sa.Column("owner_user_id", KeyText(512), nullable=False),
        sa.Column("scope", KeyText(256), nullable=False),
        sa.Column("session_id", KeyText(128), nullable=True),
        sa.Column("input_id", KeyText(128), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=True),
        sa.Column("kind", KeyText(16), nullable=True),
        sa.Column("name", payload_text(), nullable=False),
        sa.Column("media_type", KeyText(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("data", binary_payload(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("attachment_id", name=op.f("pk_input_attachments")),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"],
                                name=op.f("fk_input_attachments_session_id_sessions"), ondelete="CASCADE"),
        **_MYSQL,
    )
    op.create_index(op.f("ix_input_attachments_session_id"), "input_attachments",
                    ["session_id", "input_id", "ordinal"])
    op.create_index(op.f("ix_input_attachments_expires_at"), "input_attachments", ["expires_at"])
    op.add_column("channel_inbound", sa.Column("attachments", payload_json(), nullable=True))


def downgrade() -> None:
    op.drop_column("channel_inbound", "attachments")
    op.drop_table("input_attachments")
