"""A conversation's sandbox is held until one lease after its last recorded activity.

The deadline is read from the store, so this runs against PostgreSQL. The
provider is a recording stand-in: the property is which sandboxes are
renewed and to what deadline, given the activity the conversations recorded,
by the expiration watcher and by a runtime's own activity. Defaults apply: a
four-hour lease, renewed within its last hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import insert

from astrabox.core.service.orchestrator.conversation_lease import ConversationLeases
from astrabox.core.service.orchestrator.expiration_watcher import ExpirationWatcher
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.sql.models.sessions import SessionInput, SessionInteraction
from astrabox.persistence.sql.ops import transaction

pytestmark = pytest.mark.usefixtures("database")

_LEASE = timedelta(hours=4)


class _Provider:
    """Renews as the control plane does: to now+ttl, never shorter."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.expiry: dict[str, datetime] = {}

    async def renew_sandbox_by_id(self, sandbox_id: str, ttl_seconds: int) -> datetime:
        self.calls.append((sandbox_id, ttl_seconds))
        target = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        self.expiry[sandbox_id] = max(self.expiry.get(sandbox_id, target), target)
        return self.expiry[sandbox_id]


def _watcher(provider: _Provider) -> ExpirationWatcher:
    sessions = SessionRepository()
    return ExpirationWatcher(platform_service=SimpleNamespace(
        _sessions_repo=sessions,
        _conversation_leases=ConversationLeases(sessions_repo=sessions, runtime_manager=provider),
    ))


async def _conversation(session_id: str, sandbox_id: str, expires_at: datetime, **columns: Any) -> None:
    kind = columns.pop("session_kind", "agent_chat")
    await SessionRepository().create_session({
        "session_id": session_id, "user_id": "employee-a", "agent_id": "agent-a",
        "session_kind": kind, "engine_kind": "claude_code", "state": "READY",
    })
    await SessionRepository().update_session(session_id, {
        "sandbox_id": sandbox_id, "expires_at": expires_at.isoformat(),
        **{key: value.isoformat() if isinstance(value, datetime) else value for key, value in columns.items()},
    }, touch_updated_at=False)


async def _input(session_id: str, at: datetime, *, state: str = "admitted") -> None:
    await transaction(lambda conn: conn.execute(insert(SessionInput), [{
        "input_id": f"{session_id}-{at.timestamp()}", "session_id": session_id,
        "client_message_id": f"{session_id}-{at.timestamp()}", "content": "hello",
        "state": state, "created_at": at,
    }]))


async def _question(session_id: str, opened: datetime, answered: datetime | None = None) -> None:
    await transaction(lambda conn: conn.execute(insert(SessionInteraction), [{
        "session_id": session_id, "interaction_id": f"q-{opened.timestamp()}", "response_id": "r-1",
        "contract": {"kind": "question"}, "status": "open", "opened_at": opened,
        "answer": {"text": "yes"} if answered else None, "answered_at": answered,
    }]))


async def _expiry(session_id: str) -> datetime:
    session = await SessionRepository().get_session(session_id)
    assert session is not None and session["expires_at"]
    return datetime.fromisoformat(session["expires_at"])


def _near(actual: datetime, expected: datetime) -> bool:
    return abs((actual - expected).total_seconds()) < 2


async def test_a_sandbox_is_held_one_lease_after_its_conversations_last_activity() -> None:
    await RuntimeProfileRepository().put(
        "one", {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    await AgentRepository().create_agent(
        {"agent_id": "agent-a", "name": "A", "model": "model", "runtime_profile_name": "one", "created_by": "employee-a"}
    )
    now = datetime.now(timezone.utc)

    # The reported case: a question opened 30 minutes ago, unanswered, with no
    # event since; the lease the box was created with ends in 50 minutes.
    await _conversation("asked", "box-asked", now + timedelta(minutes=50), runtime_status="waiting")
    await _question("asked", now - timedelta(minutes=30))
    # Output alone, with no change of status.
    await _conversation("output", "box-output", now + timedelta(minutes=20), last_output_at=now - timedelta(minutes=10))
    # A shared box: the later activity of its two conversations decides.
    await _conversation("shared-1", "box-shared", now + timedelta(minutes=40))
    await _input("shared-1", now - timedelta(hours=2))
    await _conversation("shared-2", "box-shared", now + timedelta(minutes=40))
    await _question("shared-2", now - timedelta(hours=1), answered=now - timedelta(minutes=20))
    # Already held to one lease after its last activity; an imported message
    # was never given to the runtime and is no activity.
    last = now - timedelta(hours=3, minutes=30)
    await _conversation("held", "box-held", last + _LEASE, last_output_at=last, runtime_status="waiting")
    await _input("held", now - timedelta(minutes=1), state="imported")
    # Not due: more than the renewal threshold left.
    await _conversation("early", "box-early", now + timedelta(hours=3), last_output_at=now - timedelta(minutes=5))
    # A paused box is renewed by whoever parked it; renewing it fails it.
    await _conversation("parked", "box-parked", now + timedelta(minutes=10),
                        last_output_at=now - timedelta(minutes=5), sandbox_parked_at=now - timedelta(minutes=1))
    # An Assistant's workspace has its own renewal.
    await _conversation("assistant", "box-assistant", now + timedelta(minutes=10),
                        session_kind="assistant_chat", last_output_at=now - timedelta(minutes=5))

    provider = _Provider()
    summary = await _watcher(provider)._keep_conversation_sandboxes_leased()

    assert sorted(sandbox for sandbox, _ in provider.calls) == ["box-asked", "box-output", "box-shared"]
    assert summary == {"conversation_leases_renewed": 3, "conversation_lease_renew_failed": 0}
    assert _near(await _expiry("asked"), now - timedelta(minutes=30) + _LEASE)
    assert _near(await _expiry("output"), now - timedelta(minutes=10) + _LEASE)
    for sid in ("shared-1", "shared-2"):
        assert _near(await _expiry(sid), now - timedelta(minutes=20) + _LEASE)
    assert await _expiry("held") == last + _LEASE

    # A restarted replica reads the same deadlines and has nothing to renew;
    # the unanswered question still expires one lease after it was opened.
    restarted = _Provider()
    assert await _watcher(restarted)._keep_conversation_sandboxes_leased() == {}
    assert restarted.calls == []
    assert _near(await _expiry("asked"), now - timedelta(minutes=30) + _LEASE)

    # A message accepted two minutes before the lease ends renews the box as
    # the runtime starts working, without waiting for the watcher's tick.
    await _conversation("returning", "box-returning", now + timedelta(minutes=2))
    accepted = datetime.now(timezone.utc)
    await _input("returning", accepted)
    at_once = _Provider()
    await ConversationLeases(sessions_repo=SessionRepository(), runtime_manager=at_once).hold_sandbox("box-returning")
    assert [sandbox for sandbox, _ in at_once.calls] == ["box-returning"]
    assert _near(await _expiry("returning"), accepted + _LEASE)
