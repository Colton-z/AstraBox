"""The owner's session list follows the owner's last action, not replies.

Runs against PostgreSQL through the writers a live conversation uses: the
accepted input, the reader's recorded response, a runtime lifecycle update, a
submitted answer and a stop. Sessions stored before the order existed take it
from the actions their history recorded when the schema is upgraded. Every
check reads the list a page at a time, so the order is also the one the
cursor continues.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from alembic import command
from sqlalchemy import insert

from astrabox.common.utils.user_context import UserContext
from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.response_folds import ResponseFolds
from astrabox.core.service.orchestrator.session_runtime.service import SessionRuntimeService
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch,
    InteractionOpen,
    OutputPart,
    ResponseEnd,
    ResponseStart,
    SessionRuntimeRepository,
)
from astrabox.persistence.sql.engine import dispose_engine
from astrabox.persistence.sql.migrate import alembic_config
from astrabox.persistence.sql.models.sessions import (
    Session,
    SessionControl,
    SessionInput,
    SessionInteraction,
    TerminalCommand,
)
from astrabox.persistence.sql.ops import transaction
from tests._agent_ledger import create_agent

_TTL = 30.0


async def _listed() -> list[str]:
    """The owner's list, read two rows a page."""
    order: list[str] = []
    cursor = None
    while True:
        page = await SessionRepository().list_user_sessions_page("u1", limit=2, cursor=cursor)
        order += [row["session_id"] for row in page["sessions"]]
        if not page["has_more"]:
            return order
        cursor = page["next_cursor"]


async def _migrate(url: str, revision: str) -> None:
    await dispose_engine()
    config = alembic_config(url)
    await asyncio.to_thread(
        command.upgrade if revision == "head" else command.downgrade, config, revision,
    )
    await dispose_engine()


async def _stored_before_the_order(now: datetime) -> None:
    """Sessions with history, as the previous schema stored them."""
    def ago(minutes: int) -> datetime:
        return now - timedelta(minutes=minutes)

    sessions = [
        # Created an hour ago; nothing sent yet.
        ("old-new", ago(60)),
        ("old-stopped", ago(300)),
        ("old-terminal", ago(300)),
        ("old-answered", ago(300)),
        # Its history was imported; nothing was sent to it here.
        ("old-imported", ago(150)),
        # A reply ended a minute ago, after the last message four hours ago.
        ("old-replied", ago(300)),
    ]
    inputs = [
        ("old-stopped", ago(280), "admitted"),
        ("old-answered", ago(180), "admitted"),
        ("old-imported", ago(1), "imported"),
        ("old-replied", ago(240), "admitted"),
    ]

    async def write(conn):
        await conn.execute(insert(Session.__table__), [{
            "session_id": sid, "environment_name": get_settings().environment_name, "user_id": "u1",
            "session_kind": "agent_chat", "workspace_kind": "agent", "agent_id": "a1", "state": "READY",
            "created_at": created, "updated_at": ago(1),
        } for sid, created in sessions])
        await conn.execute(insert(SessionInput.__table__), [{
            "input_id": f"{sid}-in", "session_id": sid, "client_message_id": f"{sid}-in",
            "content": "hello", "state": state, "created_at": at,
        } for sid, at, state in inputs])
        await conn.execute(insert(SessionInteraction.__table__), [{
            "session_id": "old-answered", "interaction_id": "q-old", "response_id": "r-old",
            "contract": {"kind": "question"}, "status": "answered", "opened_at": ago(150),
            "answer": {"text": "yes"}, "answered_at": ago(120),
        }])
        await conn.execute(insert(SessionControl.__table__), [{
            "control_id": "stop-old", "session_id": "old-stopped", "kind": "interrupt",
            "reader_generation": 1, "requested_by": "u1", "requested_at": ago(90),
        }])
        await conn.execute(insert(TerminalCommand.__table__), [{
            "command_id": "cmd-old", "session_id": "old-terminal", "command": "ls", "status": "interrupted",
            "started_at": ago(200), "interrupt_requested_at": ago(100), "expires_at": ago(50),
        }])

    await transaction(write)


async def _send(session_id: str, client_message_id: str) -> None:
    accepted = await SessionRuntimeRepository().accept_input(
        session_id=session_id, input_id=f"in-{client_message_id}", client_message_id=client_message_id,
        content="hello", content_blocks=None, owner_user_id="u1", author_user_id="u1", source="console",
    )
    assert accepted is not None


async def test_the_list_follows_the_owners_last_action(database: str, environment_member_accounts: set[str]) -> None:
    environment_member_accounts.add("u1")
    await create_agent("a1")
    now = datetime.now(timezone.utc)

    await _migrate(database, "0016")
    await _stored_before_the_order(now)
    await _migrate(database, "head")
    old = ["old-new", "old-stopped", "old-terminal", "old-answered", "old-imported", "old-replied"]
    assert await _listed() == old

    for sid in ("a", "b"):
        await SessionRepository().create_session(
            {"session_id": sid, "user_id": "u1", "session_kind": "agent_chat", "agent_id": "a1", "state": "READY"}
        )
    await _send("a", "a-1")
    await _send("b", "b-1")
    assert await _listed() == ["b", "a", *old]

    # A's reply ends while the owner is in B, and A's runtime records a
    # lifecycle change, as a recovery does.
    runtime = SessionRuntimeRepository()
    reader = await runtime.claim_reader("a", owner="reader", ttl_s=_TTL)
    assert reader is not None
    assert await runtime.record(reader, FactBatch(
        output=[OutputPart(1, "r-1", {"type": "text-delta", "id": "t", "delta": "done"})],
        facts=[ResponseStart("r-1", "input", ("in-a-1",)), ResponseEnd("r-1", "completed")],
        status="idle",
    ), ttl_s=_TTL)
    await SessionRepository().update_session("a", {"runtime_unavailable": False, "last_error": None})
    assert await _listed() == ["b", "a", *old]

    # A asks a question on its own; answering it is the owner's.
    assert await runtime.record(reader, FactBatch(
        facts=[ResponseStart("r-2", "engine"), InteractionOpen("r-2", "q-1", {"kind": "question"})],
        status="waiting",
    ), ttl_s=_TTL)
    assert await _listed() == ["b", "a", *old]
    assert await runtime.request_answer("a", "q-1", answer={"text": "yes"}, answered_by="u1")
    assert await _listed() == ["a", "b", *old]

    # B's response is running; the owner stops it.
    b_reader = await runtime.claim_reader("b", owner="reader", ttl_s=_TTL)
    assert b_reader is not None
    assert await runtime.record(b_reader, FactBatch(
        facts=[ResponseStart("r-b", "input", ("in-b-1",))], status="working",
    ), ttl_s=_TTL)
    assert await _listed() == ["a", "b", *old]
    facts = SessionRuntimeRepository()
    service = SessionRuntimeService(
        sessions=SessionRepository(), repo=facts, feed=SessionFeed(), host=cast(Any, None),
        folds=ResponseFolds(facts=facts, reads=SessionReadRepository()),
    )
    stopped = await service.interrupt(UserContext(user_id="u1"), "b")
    assert stopped["status"] == "accepted"
    assert await _listed() == ["b", "a", *old]

    # The same message delivered again is not a new one.
    await _send("a", "a-1")
    assert await _listed() == ["b", "a", *old]
