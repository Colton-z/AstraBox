"""The team pool of ChatGPT accounts, on the PostgreSQL test database.

Accounts are written through the repository with an access token in the
secret store that is not due for renewal, so choosing one never calls OpenAI.
The deployment switch is the environment variable an operator sets.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import update

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, utcnow
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.model_accounts.service import ModelAccountService
from astrabox.core.service.orchestrator.engine.codex_runtime_client import _Stream
from astrabox.core.service.orchestrator.engine.runtime import ResponseEnded
from astrabox.core.service.orchestrator.session_runtime.host import ConnectedRuntime
from astrabox.core.service.orchestrator.session_runtime.platform import reader_hooks
from astrabox.core.service.secret_changes import prepare_secret_change
from astrabox.persistence.repository.model_account_repository import (
    ModelAccountRepository,
    new_account_id,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.model_accounts import ModelAccount
from astrabox.seams.secrets import secret_store_for_name

pytestmark = pytest.mark.usefixtures("database")

_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed master key, so the local store needs no key file."""
    monkeypatch.delenv("ASTRABOX_SECRET_STORE", raising=False)
    monkeypatch.setenv(
        "ASTRABOX_VAULT_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode()
    )


def _pool(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ASTRABOX_MODEL_ACCOUNT_POOL_ENABLED", "true" if enabled else "false")


async def _account(owner: str, label: str, *, org: str = "org-a", shared: bool = False) -> str:
    """A linked, active account whose access token ``at-<label>`` lasts two hours."""
    account_id = new_account_id()
    change = await prepare_secret_change(
        secret_store_for_name(None), f"model-account/{account_id}",
        put={"access_token": f"at-{label}"},
    )
    repo = ModelAccountRepository()
    await repo.create_account(
        {
            "account_id": account_id, "org_id": org, "owner_user_id": owner,
            "vendor": "chatgpt_plan", "label": label, "email": f"{label}@example.com",
            "subject": f"sub-{label}", "issued_client_id": f"oaiapp_{label}", "scopes": _SCOPES,
        },
        access_expires_at=(utcnow() + timedelta(hours=2)).isoformat(),
        secrets=change.apply,
    )
    if shared:
        await repo.update_account(account_id, owner_user_id=owner, shared=True)
    return account_id


async def _session(session_id: str, user_id: str, *, org: str = "org-a") -> None:
    await SessionRepository().create_session({
        "session_id": session_id, "user_id": user_id, "org_id": org,
        "session_kind": "agent_chat", "engine_kind": "codex", "state": "READY",
    })


async def _spends(session_id: str) -> str | None:
    row = await SessionRepository().get_session(session_id)
    assert row is not None
    return row["model_account_id"]


async def _hold_until(account_id: str, until: datetime) -> None:
    table = ModelAccount.__table__
    await execute(
        update(table).where(table.c.account_id == account_id).values(usage_limited_until=until)
    )


async def test_a_limited_account_gives_way_to_the_owners_others_then_to_shared_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the pool on, each reported limit moves the conversation to the next account in order.

    The order is the owner's preferred account, their other accounts with the
    default first, then accounts other members of the same organization
    shared, oldest first. An account nobody shared, or one shared in another
    organization, is never spent: picking either would charge a plan whose
    owner did not consent.
    """
    _pool(monkeypatch, True)
    service = ModelAccountService()
    a_oldest = await _account("alice", "a-oldest")
    a_named = await _account("alice", "a-named")
    a_default = await _account("alice", "a-default")
    await service.update_account(UserContext(user_id="alice"), a_default, is_default=True)
    b_shared = await _account("bob", "b-shared", shared=True)
    await _account("bob", "b-private")
    await _account("carol", "c-elsewhere", org="org-b", shared=True)
    d_shared = await _account("dave", "d-shared", shared=True)
    await _session("s1", "alice")

    async def spent() -> tuple[str, str]:
        token = await service.conversation_token(user_id="alice", account_id=a_named, session_id="s1")
        assert await _spends("s1") == token.account_id
        return token.account_id, token.access_token

    assert await spent() == (a_named, "at-a-named")
    expected = [
        (a_default, "at-a-default"),
        (a_oldest, "at-a-oldest"),
        (b_shared, "at-b-shared"),
        (d_shared, "at-d-shared"),
    ]
    limited = a_named
    for account_id, token in expected:
        await service.record_usage_limit(limited)
        assert await spent() == (account_id, token)
        limited = account_id

    [bobs_shared, _] = await service.list_accounts(UserContext(user_id="bob"))
    assert bobs_shared["shared"] is True
    assert bobs_shared["pool_last_used_at"] is not None
    assert bobs_shared["usage_limited_until"] is not None

    await service.record_usage_limit(d_shared)
    with pytest.raises(APIError) as refused:
        await spent()
    assert (refused.value.code, refused.value.status_code) == ("MODEL_PLAN_USAGE_LIMIT", 429)
    assert refused.value.evidence is not None
    assert refused.value.evidence["account_id"] == a_named


async def test_a_limit_holds_an_account_for_an_hour_and_never_moves_a_running_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reported limit holds the account back for one hour, after which it is tried again.

    OpenAI names no reset time, so the hold is the platform's hour. While the
    hold lasts, a renewal for a response in progress (``keep``) still answers
    the account that response started on; switching mid-response is what the
    pool must never do. Once the hour has passed, the owner's own account
    comes first again instead of a teammate's.
    """
    _pool(monkeypatch, True)
    service = ModelAccountService()
    mine = await _account("alice", "mine")
    theirs = await _account("bob", "theirs", shared=True)
    await _session("s1", "alice")
    assert (await service.conversation_token(user_id="alice", session_id="s1")).account_id == mine

    reading = await service.record_usage_limit(mine)
    assert reading == {"code": "MODEL_PLAN_USAGE_LIMIT", "account_id": mine, "resets_at": None}
    [view] = await service.list_accounts(UserContext(user_id="alice"))
    held = parse_iso(view["usage_limited_until"]) - utcnow()
    assert timedelta(minutes=59) < held <= timedelta(hours=1)

    kept = await service.conversation_token(user_id="alice", session_id="s1", keep=True)
    assert kept.account_id == mine
    chosen = await service.conversation_token(user_id="alice", session_id="s1")
    assert (chosen.account_id, await _spends("s1")) == (theirs, theirs)
    kept = await service.conversation_token(user_id="alice", session_id="s1", keep=True)
    assert kept.account_id == theirs

    await _hold_until(mine, datetime.now(timezone.utc) - timedelta(seconds=1))
    [view] = await service.list_accounts(UserContext(user_id="alice"))
    assert view["usage_limited_until"] is None
    chosen = await service.conversation_token(user_id="alice", session_id="s1")
    assert (chosen.account_id, await _spends("s1")) == (mine, mine)


async def test_with_the_pool_off_nothing_switches_and_nothing_can_be_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off, a conversation spends its preferred account, and nothing can be shared.

    A limited account is still the one spent, a flag left set while the pool
    was on lends nothing, and a request to share is refused rather than
    stored as a setting that does nothing.
    """
    _pool(monkeypatch, False)
    service = ModelAccountService()
    alice = UserContext(user_id="alice")
    mine = await _account("alice", "mine")
    await _account("alice", "spare")
    await _account("bob", "theirs", shared=True)
    await _session("s1", "alice")

    await service.record_usage_limit(mine)
    chosen = await service.conversation_token(user_id="alice", session_id="s1")
    assert (chosen.account_id, await _spends("s1")) == (mine, mine)

    assert await service.team_pool(alice) == {"enabled": False}
    with pytest.raises(APIError) as refused:
        await service.update_account(alice, mine, shared=True)
    assert refused.value.code == "MODEL_ACCOUNT_POOL_DISABLED"
    assert (await service.update_account(alice, mine, shared=False))["shared"] is False


def _codex_turn_failed(error: dict[str, Any]) -> dict[str, Any]:
    """A ``turn/completed`` for a failed turn, as Codex 0.169's app-server sends it.

    The shape is ``TurnCompletedNotification`` with ``Turn`` and ``TurnError``
    (codex-rs/app-server-protocol/src/protocol/v2/turn.rs and thread_data.rs at
    rust-v0.169.0, serialized camelCase). A failed turn's ``error`` is the
    ``TurnError`` of the turn's last error event, whose ``additionalDetails`` is
    null (``EventMsg::Error`` in codex-rs/app-server/src/bespoke_event_handling.rs).
    """
    return {
        "method": "turn/completed",
        "params": {
            "threadId": "thr-1",
            "turn": {
                "id": "turn-1", "rootTurnId": None, "items": [], "itemsView": "full",
                "status": "failed", "error": error,
                "startedAt": 1_763_100_000, "completedAt": 1_763_100_003, "durationMs": 3_000,
            },
        },
    }


def _ended(notification: dict[str, Any]) -> ResponseEnded:
    stream = _Stream("thr-1")
    stream.notification(notification)
    [ended] = [event for event, _ in stream.drain() if isinstance(event, ResponseEnded)]
    return ended


async def test_a_codex_usage_limit_failure_fails_the_response_with_the_account_that_met_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex's report of a 429 becomes MODEL_PLAN_USAGE_LIMIT on the account the session spends.

    OpenAI answers plan exhaustion with HTTP 429 and the code
    ``subscription_sharing_usage_limit_exceeded``. Codex 0.169 does not know
    that code: its ``map_api_error_details`` (codex-rs/codex-api/src/api_bridge.rs)
    turns such a 429 into ``RetryLimit``, reported as
    ``responseTooManyFailedAttempts`` with ``httpStatusCode`` 429 and the
    message ``RetryLimitReachedError`` prints (codex-rs/protocol/src/error.rs);
    the JSON spelling is Codex's own test
    ``codex_error_info_serializes_http_status_code_in_camel_case``. A 429 of
    the ChatGPT backend's ``usage_limit_reached`` type is reported as
    ``usageLimitExceeded``. Both must hold the account back; an unrelated
    failure must not. The response stays failed, Codex's report is kept
    verbatim, and the platform adds the stable code and the account.
    """
    retry_limit = {
        "message": "exceeded retry limit, last status: 429 Too Many Requests, request id: req_1",
        "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 429}},
        "additionalDetails": None,
        "misalignment": None,
    }
    plan_limit = {
        "message": "You've hit your usage limit.",
        "codexErrorInfo": "usageLimitExceeded",
        "additionalDetails": None,
        "misalignment": None,
    }
    unauthorized = {
        "message": "unexpected status 401 Unauthorized",
        "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}},
        "additionalDetails": None,
        "misalignment": None,
    }
    ended = _ended(_codex_turn_failed(retry_limit))
    assert (ended.outcome, ended.native_reason, ended.error, ended.usage_limited) == (
        "failed", "failed", retry_limit, True,
    )
    assert _ended(_codex_turn_failed(plan_limit)).usage_limited is True
    assert _ended(_codex_turn_failed(unauthorized)).usage_limited is False

    _pool(monkeypatch, True)
    service = ModelAccountService()
    mine = await _account("alice", "mine")
    await _session("s1", "alice")
    await _session("s-gateway", "alice")
    await service.conversation_token(user_id="alice", session_id="s1")

    async def nothing(*_args: Any) -> None:
        return None

    build = reader_hooks(
        sessions=SessionRepository(), runtime_manager=None,
        realign_live_sandbox_owners=nothing, committed=nothing, deliver_attachments=nothing,
    )

    def hooks(session_id: str) -> Any:
        connected = ConnectedRuntime(
            client=None, sandbox_id="box", sandbox_handle=None, runtime_identity={},
            engine_kind="codex",
        )
        return build(session_id, connected)

    recorded = await hooks("s1").usage_limited(ended)
    assert recorded.outcome == "failed"
    assert recorded.error == {
        **retry_limit, "code": "MODEL_PLAN_USAGE_LIMIT", "account_id": mine, "resets_at": None,
    }
    [view] = await service.list_accounts(UserContext(user_id="alice"))
    assert view["usage_limited_until"] is not None

    # A session on a model endpoint the deployment pays for spends no account.
    assert await hooks("s-gateway").usage_limited(ended) == ended
