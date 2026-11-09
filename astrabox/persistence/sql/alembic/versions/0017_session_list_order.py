"""Order the owner's session list by the owner's last action on each conversation.

An existing session takes the latest owner action its history records: a
message, an answer, a stop, a terminal command or its interrupt, or a change
to its share link, and its creation when there is none. Renames, permission
mode changes, file changes and recoveries left no time behind. A message
imported from another system was not sent to this conversation and does not
count.

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import UTCDateTime

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None

_SESSIONS = sa.table(
    "sessions",
    sa.column("session_id"), sa.column("created_at"), sa.column("last_user_activity_at"),
)
#: Where each recorded owner action keeps its time.
_RECORDED_ACTIONS = (
    ("session_inputs", "created_at"),
    ("session_interactions", "answered_at"),
    ("session_controls", "requested_at"),
    ("terminal_commands", "started_at"),
    ("terminal_commands", "interrupt_requested_at"),
    ("session_shares", "updated_at"),
)


def upgrade() -> None:
    op.add_column("sessions", sa.Column("last_user_activity_at", UTCDateTime(), nullable=True))
    op.execute(_SESSIONS.update().values(last_user_activity_at=_SESSIONS.c.created_at))
    for name, at in _RECORDED_ACTIONS:
        table = sa.table(name, sa.column("session_id"), sa.column(at), sa.column("state"))
        latest = sa.select(sa.func.max(table.c[at])).where(table.c.session_id == _SESSIONS.c.session_id)
        if name == "session_inputs":
            latest = latest.where(table.c.state != "imported")
        latest_at = latest.scalar_subquery()
        op.execute(
            _SESSIONS.update()
            .where(latest_at > _SESSIONS.c.last_user_activity_at)
            .values(last_user_activity_at=latest_at)
        )
    op.alter_column(
        "sessions", "last_user_activity_at", existing_type=UTCDateTime(), nullable=False,
        server_default=sa.func.now(),
    )
    op.drop_index("ix_sessions_user_list", table_name="sessions")
    op.create_index(
        "ix_sessions_user_list", "sessions",
        ["user_id", "hidden", "deleted", sa.literal_column("last_user_activity_at DESC"),
         sa.literal_column("session_id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_user_list", table_name="sessions")
    op.create_index(
        "ix_sessions_user_list", "sessions",
        ["user_id", "hidden", "deleted", sa.literal_column("updated_at DESC"), sa.literal_column("session_id DESC")],
    )
    op.drop_column("sessions", "last_user_activity_at")
