"""Anthropic API keys among a user's model accounts, on the PostgreSQL test database.

A stand-in for Anthropic answers ``GET /v1/models`` through an httpx
``MockTransport`` in the shape Anthropic documents
(https://platform.claude.com/docs/en/api/models/list): a ``data`` array of
models newest first, paged by ``has_more`` and ``last_id``, and an
``authentication_error`` for a key it does not know. It checks that every
request carries the key in ``x-api-key`` and names an ``anthropic-version``.
"""

from __future__ import annotations

import base64
import functools
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.service.model_accounts import service as service_module
from astrabox.core.service.model_accounts.service import (
    ANTHROPIC_MODELS_ENDPOINT,
    VENDOR_ANTHROPIC_API_KEY,
    ModelAccountService,
)
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

_USER = UserContext(user_id="key-user")
_KEY = "sk-ant-api03-first-key-AAAA"
_RealAsyncClient = httpx.AsyncClient


class _Anthropic:
    """Anthropic's model list for the keys it knows."""

    def __init__(self) -> None:
        self.keys = {_KEY, "sk-ant-api03-second-key-BBBB"}
        self.models = [
            {"type": "model", "id": f"claude-model-{index}", "display_name": f"Claude {index}",
             "created_at": "2026-09-21T00:00:00Z"}
            for index in range(5)
        ]
        self.page_size = 2
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not str(request.url).startswith(ANTHROPIC_MODELS_ENDPOINT):
            return httpx.Response(404)
        assert request.headers.get("anthropic-version")
        if request.headers.get("x-api-key") not in self.keys:
            return httpx.Response(401, json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            })
        after = request.url.params.get("after_id")
        limit = min(int(request.url.params.get("limit", "20")), self.page_size)
        start = 0 if after is None else [m["id"] for m in self.models].index(after) + 1
        page = self.models[start:start + limit]
        return httpx.Response(200, json={
            "data": page,
            "has_more": start + limit < len(self.models),
            "first_id": page[0]["id"] if page else None,
            "last_id": page[-1]["id"] if page else None,
        })


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTRABOX_SECRET_STORE", raising=False)
    monkeypatch.setenv("ASTRABOX_VAULT_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())


@pytest.fixture
def anthropic(monkeypatch: pytest.MonkeyPatch) -> _Anthropic:
    fake = _Anthropic()
    monkeypatch.setattr(
        service_module.httpx, "AsyncClient",
        functools.partial(_RealAsyncClient, transport=httpx.MockTransport(fake.handle)),
    )
    return fake


async def _secret(account_id: str) -> str | None:
    return await secret_store_for_name(None).get(scope=f"model-account/{account_id}", key="api_key")


async def test_a_key_anthropic_accepts_is_kept_and_no_view_carries_it(anthropic: _Anthropic) -> None:
    """The key is checked with Anthropic first, kept in the secret store, and named by its tail."""
    service = ModelAccountService()

    account = await service.add_api_key(_USER, api_key=f"  {_KEY}\n")

    [check] = anthropic.requests
    assert check.headers["x-api-key"] == _KEY and check.url.params["limit"] == "1"
    assert account["vendor"] == VENDOR_ANTHROPIC_API_KEY
    assert account["label"] == "Anthropic API key …AAAA"
    assert account["is_default"] is True and account["status"] == "active"
    assert await _secret(account["account_id"]) == _KEY
    assert _KEY not in json.dumps([account, await service.list_accounts(_USER)])
    assert await service.api_key(user_id=_USER.user_id) == _KEY


async def test_a_key_anthropic_refuses_is_not_kept(anthropic: _Anthropic) -> None:
    service = ModelAccountService()
    with pytest.raises(APIError) as refused:
        await service.add_api_key(_USER, api_key="sk-ant-api03-unknown", label="Work")
    assert refused.value.code == "MODEL_ACCOUNT_API_KEY_REJECTED"
    assert "authentication_error" in refused.value.message
    assert await service.list_accounts(_USER) == []
    with pytest.raises(APIError) as malformed:
        await service.add_api_key(_USER, api_key="sk-ant with space")
    assert malformed.value.code == "INVALID_REQUEST"


async def test_each_vendor_has_its_own_default(anthropic: _Anthropic) -> None:
    """A ChatGPT default and an Anthropic default coexist, and one vendor's account never serves the other."""
    service = ModelAccountService()
    chatgpt_id = new_account_id()

    async def no_secrets(_conn: Any) -> None:
        return None

    chatgpt = await ModelAccountRepository().create_account(
        {
            "account_id": chatgpt_id, "org_id": "org", "owner_user_id": _USER.user_id,
            "vendor": "chatgpt_plan", "label": "ChatGPT", "email": None,
            "subject": "chatgpt-user", "issued_client_id": "oaiapp", "scopes": "",
        },
        access_expires_at=None,
        secrets=no_secrets,
    )
    first = await service.add_api_key(_USER, api_key=_KEY)
    second = await service.add_api_key(_USER, api_key="sk-ant-api03-second-key-BBBB", label="Team")
    assert (chatgpt["is_default"], first["is_default"], second["is_default"]) == (True, True, False)

    await service.update_account(_USER, second["account_id"], is_default=True)
    defaults = {row["account_id"] for row in await service.list_accounts(_USER) if row["is_default"]}
    assert defaults == {chatgpt_id, second["account_id"]}
    assert await service.api_key(user_id=_USER.user_id) == "sk-ant-api03-second-key-BBBB"

    with pytest.raises(APIError) as wrong_vendor:
        await service.api_key(user_id=_USER.user_id, account_id=chatgpt_id)
    assert wrong_vendor.value.code == "MODEL_ACCOUNT_NOT_FOUND"
    with pytest.raises(APIError) as no_key:
        await service.api_key(user_id="someone-else")
    assert no_key.value.code == "MODEL_ACCOUNT_REQUIRED"


async def test_a_conversation_spends_only_an_account_of_its_endpoints_vendor(
    anthropic: _Anthropic, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The choice, the kept account and the team pool all stay within one vendor.

    With the pool on and the ChatGPT default held back by a usage limit, the
    owner's Anthropic key and a teammate's Anthropic key (shared through the
    repository, which the service would refuse) are both passed over: spending
    either would answer a Codex conversation with a key OpenAI never accepts.
    An account recorded on the session for one endpoint is not kept for the
    other. Only a ChatGPT account can be shared.
    """
    monkeypatch.setenv("ASTRABOX_MODEL_ACCOUNT_POOL_ENABLED", "true")
    service = ModelAccountService()
    repo = ModelAccountRepository()
    chatgpt_id = new_account_id()
    change = await prepare_secret_change(
        secret_store_for_name(None), f"model-account/{chatgpt_id}", put={"access_token": "at-plan"}
    )
    await repo.create_account(
        {
            "account_id": chatgpt_id, "org_id": default_org_id(), "owner_user_id": _USER.user_id,
            "vendor": "chatgpt_plan", "label": "ChatGPT", "email": None,
            "subject": "chatgpt-user", "issued_client_id": "oaiapp",
            "scopes": "openid offline_access chatgpt.tokens.use.direct",
        },
        access_expires_at=(utcnow() + timedelta(hours=2)).isoformat(),
        secrets=change.apply,
    )
    key = await service.add_api_key(_USER, api_key=_KEY)
    teammate = await service.add_api_key(
        UserContext(user_id="teammate"), api_key="sk-ant-api03-second-key-BBBB"
    )
    await repo.update_account(teammate["account_id"], owner_user_id="teammate", shared=True)
    assert teammate["shared"] is False and (await repo.get_account(teammate["account_id"]) or {})["org_id"] == default_org_id()
    await SessionRepository().create_session({
        "session_id": "s-claude", "user_id": _USER.user_id, "org_id": default_org_id(),
        "session_kind": "agent_chat", "engine_kind": "claude_code", "state": "READY",
    })

    claude = await service.conversation_token(
        user_id=_USER.user_id, vendor=VENDOR_ANTHROPIC_API_KEY, session_id="s-claude"
    )
    assert (claude.account_id, claude.access_token, claude.expires_at) == (
        key["account_id"], _KEY, None,
    )
    assert (await SessionRepository().get_session("s-claude") or {})["model_account_id"] == key["account_id"]
    codex = await service.conversation_token(
        user_id=_USER.user_id, vendor="chatgpt_plan", session_id="s-claude", keep=True
    )
    assert (codex.account_id, codex.access_token) == (chatgpt_id, "at-plan")

    table = ModelAccount.__table__
    await execute(
        update(table)
        .where(table.c.account_id == chatgpt_id)
        .values(usage_limited_until=utcnow() + timedelta(hours=1))
    )
    with pytest.raises(APIError) as limited:
        await service.conversation_token(user_id=_USER.user_id, vendor="chatgpt_plan", session_id="s-claude")
    assert limited.value.code == "MODEL_PLAN_USAGE_LIMIT"
    with pytest.raises(APIError) as not_shareable:
        await service.update_account(_USER, key["account_id"], shared=True)
    assert not_shareable.value.code == "MODEL_ACCOUNT_NOT_SHAREABLE"
    assert (await service.update_account(_USER, key["account_id"], shared=False))["shared"] is False


async def test_a_keys_models_are_every_page_in_anthropic_order(anthropic: _Anthropic) -> None:
    """The list follows ``last_id`` until ``has_more`` is false; ``default`` needs its vendor named."""
    service = ModelAccountService()
    account = await service.add_api_key(_USER, api_key=_KEY)
    listed = [{"id": m["id"], "display_name": m["display_name"]} for m in anthropic.models]

    assert await service.list_models(_USER, account["account_id"]) == listed
    assert await service.list_models(_USER, "default", vendor=VENDOR_ANTHROPIC_API_KEY) == listed
    assert [request.url.params.get("after_id") for request in anthropic.requests[-3:]] == [
        None, "claude-model-1", "claude-model-3",
    ]
    with pytest.raises(APIError) as unnamed:
        await service.list_models(_USER, "default")
    assert unnamed.value.code == "INVALID_REQUEST"


async def test_a_replaced_key_is_checked_and_then_served(anthropic: _Anthropic) -> None:
    service = ModelAccountService()
    account = await service.add_api_key(_USER, api_key=_KEY)

    with pytest.raises(APIError) as refused:
        await service.update_account(_USER, account["account_id"], api_key="sk-ant-api03-revoked")
    assert refused.value.code == "MODEL_ACCOUNT_API_KEY_REJECTED"
    assert await _secret(account["account_id"]) == _KEY

    await service.update_account(_USER, account["account_id"], api_key="sk-ant-api03-second-key-BBBB")
    assert await service.api_key(user_id=_USER.user_id) == "sk-ant-api03-second-key-BBBB"
    removed = await service.delete_account(_USER, account["account_id"])
    assert removed["revocation_confirmed"] is None
    assert await _secret(account["account_id"]) is None
