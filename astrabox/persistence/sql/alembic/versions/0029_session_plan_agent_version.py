"""Record on each Agent Session's capability plan the Agent version it installs.

A Session reconnecting to its sandbox launches the Agent version its capability
plan records. A plan made before this revision records none; it is given the
version its Agent has now, the oldest that revision 0028 kept, which is the
version the Session would have resolved before.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from astrabox.persistence.sql.types import KeyText, payload_json

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | None = None
depends_on: str | None = None

_SESSIONS = sa.table(
    "sessions",
    sa.column("session_id", KeyText(128)),
    sa.column("session_kind", KeyText(32)),
    sa.column("agent_id"),
    sa.column("runtime_identity", payload_json()),
)
_AGENTS = sa.table("agents", sa.column("agent_id"), sa.column("version", sa.Integer()))


def _rewrite(add_version: bool) -> None:
    bind = op.get_bind()
    versions = dict(bind.execute(sa.select(_AGENTS.c.agent_id, _AGENTS.c.version)).all())
    rows = bind.execute(
        sa.select(_SESSIONS.c.session_id, _SESSIONS.c.agent_id, _SESSIONS.c.runtime_identity).where(
            _SESSIONS.c.session_kind == "agent_chat", _SESSIONS.c.agent_id.is_not(None)
        )
    ).all()
    for session_id, agent_id, identity in rows:
        plan = identity.get("capability_plan") if isinstance(identity, dict) else None
        if not isinstance(plan, dict) or ("agent_version" in plan) == add_version:
            continue
        if add_version:
            if agent_id not in versions:
                continue
            plan = {**plan, "agent_version": versions[agent_id]}
        else:
            plan = {key: value for key, value in plan.items() if key != "agent_version"}
        bind.execute(
            sa.update(_SESSIONS)
            .where(_SESSIONS.c.session_id == session_id)
            .values(runtime_identity={**identity, "capability_plan": plan})
        )


def upgrade() -> None:
    _rewrite(add_version=True)


def downgrade() -> None:
    _rewrite(add_version=False)
