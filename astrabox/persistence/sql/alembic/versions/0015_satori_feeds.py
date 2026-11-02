"""Keep Satori replay numbers and core output positions in one transaction.

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import KeyText, UTCDateTime, payload_json, payload_text

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None

_MYSQL = {"mysql_engine": "InnoDB", "mysql_row_format": "DYNAMIC",
          "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def upgrade() -> None:
    op.create_table(
        "satori_feeds",
        sa.Column("feed_id", KeyText(64), nullable=False),
        sa.Column("user_id", KeyText(512), nullable=False),
        sa.Column("agent_id", KeyText(128), nullable=False),
        sa.Column("last_sn", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("feed_id", name=op.f("pk_satori_feeds")),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"],
                                name=op.f("fk_satori_feeds_agent_id_agents"), ondelete="CASCADE"),
        **_MYSQL,
    )
    op.create_table(
        "satori_positions",
        sa.Column("feed_id", KeyText(64), nullable=False),
        sa.Column("session_id", KeyText(128), nullable=False),
        sa.Column("output_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("state_digest", payload_text(), nullable=True),
        sa.PrimaryKeyConstraint("feed_id", "session_id", name=op.f("pk_satori_positions")),
        sa.ForeignKeyConstraint(["feed_id"], ["satori_feeds.feed_id"],
                                name=op.f("fk_satori_positions_feed_id_satori_feeds"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"],
                                name=op.f("fk_satori_positions_session_id_sessions"), ondelete="CASCADE"),
        **_MYSQL,
    )
    op.create_table(
        "satori_events",
        sa.Column("feed_id", KeyText(64), nullable=False),
        sa.Column("sn", sa.BigInteger(), nullable=False),
        sa.Column("session_id", KeyText(128), nullable=False),
        sa.Column("descriptor", payload_json(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("feed_id", "sn", name=op.f("pk_satori_events")),
        sa.ForeignKeyConstraint(["feed_id", "session_id"], ["satori_positions.feed_id", "satori_positions.session_id"],
                                name=op.f("fk_satori_events_feed_id_session_id_satori_positions"), ondelete="CASCADE"),
        **_MYSQL,
    )


def downgrade() -> None:
    op.drop_table("satori_events")
    op.drop_table("satori_positions")
    op.drop_table("satori_feeds")
