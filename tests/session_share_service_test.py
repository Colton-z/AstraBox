"""A share link: its owner turns it on, reads it back and turns it off; the token is the whole capability.

Sharing a session again keeps its token, so links already handed out keep
working; a disabled, expired or unknown link answers 404 alike.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from sqlalchemy import update

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.process_summaries import ProcessSummaries
from astrabox.core.service.orchestrator.session_runtime.service import SessionRuntimeService
from astrabox.core.service.orchestrator.session_runtime.views import SessionReads
from astrabox.core.service.orchestrator.session_share_service import SessionShareService
from astrabox.persistence.repository.process_summary_repository import ProcessSummaryRepository
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from astrabox.persistence.repository.session_share_repository import SessionShareRepository
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.sessions import SessionShare
from tests._claude_session import create_session

pytestmark = pytest.mark.usefixtures("database")

_OWNER = UserContext(user_id="u1")


def _service() -> SessionShareService:
    facts = SessionRuntimeRepository()
    reads = SessionReadRepository()
    session_reads = SessionReads(
        sessions=SessionRepository(),
        facts=facts,
        reads=reads,
        runtime=SessionRuntimeService(
            sessions=SessionRepository(), repo=facts, feed=SessionFeed(), host=cast(Any, None)
        ),
        summaries=ProcessSummaries(
            facts=facts,
            reads=reads,
            repo=ProcessSummaryRepository(),
            model=cast(Any, None),
            enabled=False,
            spawn=lambda coroutine, **_: pytest.fail("a share starts no label"),
        ),
    )
    return SessionShareService(
        sessions=SessionRepository(),
        shares=SessionShareRepository(),
        reads=session_reads,
        session_file_service=cast(Any, None),
    )


async def _refused(call: Any) -> str:
    with pytest.raises(APIError) as refused:
        await call
    assert refused.value.status_code == 404
    return str(refused.value.code)


async def test_a_link_keeps_its_token_and_answers_only_while_enabled_and_unexpired() -> None:
    session_id = await create_session()
    service = _service()

    created = await service.create_session_share(_OWNER, session_id, allow_download=True)
    again = await service.create_session_share(_OWNER, session_id, expires_in_seconds=3600)
    shared = await service.get_shared_session(created["token"])
    await service.revoke_session_share(_OWNER, session_id)
    revoked = await service.get_session_share(_OWNER, session_id)
    revoked_code = await _refused(service.get_shared_session(created["token"]))
    await service.create_session_share(_OWNER, session_id)
    await execute(update(SessionShare.__table__).values(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc)))
    expired_code = await _refused(service.get_shared_messages(created["token"]))

    assert again["token"] == created["token"]
    assert (again["allow_download"], again["expires_at"] is not None) == (False, True)
    assert shared == {"title": None, "pending_interaction": None, "share_allow_download": False}
    assert (revoked["enabled"], revoked["token"]) == (False, created["token"])
    assert (revoked_code, expired_code) == ("SHARE_NOT_FOUND", "SHARE_EXPIRED")
    assert await _refused(service.get_shared_session("not-a-token")) == "SHARE_NOT_FOUND"
    assert await _refused(service.create_session_share(UserContext(user_id="someone-else"), session_id)) == (
        "SESSION_NOT_FOUND"
    )
