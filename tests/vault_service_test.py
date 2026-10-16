"""Vault secrets on the PostgreSQL test database — NO Docker, NO live turn.

Payloads are write-only and never in a view, an archive purges them, a sealed
value is bound to its identity, Vaults are scoped to their organization, MCP
resolution hides headers, and an environment-variable credential needs a
backend that substitutes egress.

The concurrency cases race real transactions: each call takes its own pooled
connection, so the database, not the order of the coroutines, decides the
outcome.
"""

from __future__ import annotations

import asyncio
import base64
import json
import unittest.mock
from typing import Any

import pytest
from sqlalchemy import func, insert, select

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator import vault_service as vault_module
from astrabox.core.service.orchestrator.vault_service import (
    MAX_CREDENTIALS_PER_VAULT,
    VaultService,
)
from astrabox.persistence.sql import fetch_one, fetch_value
from astrabox.persistence.sql.engine import get_engine
from astrabox.persistence.sql.models.vaults import Secret, Vault, VaultCredential
from astrabox.providers import register_builtin_providers
from astrabox.providers.secret_store import LocalEncryptedSecretStore
from astrabox.seams.secrets import secret_store_for_name

pytestmark = pytest.mark.usefixtures("database")

_USER = UserContext(user_id="test-user")
_OTHER = UserContext(user_id="other-user")
_OTHER_ORG = UserContext(user_id="other-org-admin", org_id="other-org")

_SECRETS = Secret.__table__
_VAULTS = Vault.__table__
_CREDENTIALS = VaultCredential.__table__


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed master key, so the local store needs no key file."""
    monkeypatch.delenv("ASTRABOX_SECRET_STORE", raising=False)
    monkeypatch.setenv(
        "ASTRABOX_VAULT_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode()
    )


async def _nothing_bound() -> None:
    """A use check that finds no binding."""


def _bearer_auth(url: str, token: str = "tok-123") -> dict[str, Any]:
    return {"type": "static_bearer", "mcp_server_url": url, "token": token}


def _static_header_auth(
    url: str,
    value: str = "secret-value",
    *,
    header_name: str = "apikey",
) -> dict[str, Any]:
    return {
        "type": "mcp_static_header",
        "mcp_server_url": url,
        "header_name": header_name,
        "value": value,
    }


async def _mcp_headers(service: VaultService, vault_id: str, server_url: str) -> list[dict[str, str]]:
    """The headers the egress side resolves for one MCP server from one Vault."""
    return [
        credential.headers
        for credential in await service.resolve_mcp_credentials([vault_id], [server_url])
    ]


def _expired_oauth_auth(url: str, refresh_token: str) -> dict[str, Any]:
    return {
        "type": "mcp_oauth",
        "mcp_server_url": url,
        "access_token": "stale-token",
        "expires_at": "2020-01-01T00:00:00+00:00",  # long expired
        "refresh": {
            "token_endpoint": "https://idp.example.com/oauth/token",
            "client_id": "cid",
            "auth_method": "client_secret_post",
            "refresh_token": refresh_token,
            "client_secret": "cs-1",
        },
    }


class _TokenResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


async def test_http_basic_is_write_only_and_rotates_without_changing_destination() -> None:
    # An http_basic credential needs an egress-capable sandbox backend; the
    # built-in OpenSandbox backend is one, installed at platform startup.
    register_builtin_providers()
    service = VaultService()

    vault = await service.create_vault(_USER, display_name="private Git")
    vault_id = vault["vault_id"]
    credential = await service.create_credential(
        _USER, vault_id,
        auth={
            "type": "http_basic", "url": "https://GitHub.com/owner/skills.git/",
            "username": "x-access-token", "password": "first-private-token",
        },
    )
    assert credential["auth"] == {
        "type": "http_basic", "url": "https://github.com/owner/skills.git",
        "username": "x-access-token",
    }
    assert "first-private-token" not in json.dumps(await service.list_vaults(_USER))
    stored = await fetch_one(
        select(_CREDENTIALS).where(_CREDENTIALS.c.credential_id == credential["credential_id"])
    )
    assert "first-private-token" not in str(stored)
    resolved = await service.resolve_egress_credentials([vault_id])
    assert len(resolved) == 1
    assert resolved[0].credentials[0].password == "first-private-token"
    assert "first-private-token" not in repr(resolved)
    rotated = await service.update_credential(
        _USER, vault_id, credential["credential_id"], auth={"password": "second-private-token"},
    )
    assert rotated["auth"] == credential["auth"]
    assert (await service.resolve_egress_credentials([vault_id]))[0].credentials[0].password == (
        "second-private-token"
    )
    with pytest.raises(APIError) as error:
        await service.update_credential(
            _USER, vault_id, credential["credential_id"], auth={"url": "https://evil.test/repo.git"},
        )
    assert error.value.status_code == 400
    await service.archive_credential(_USER, vault_id, credential["credential_id"])
    archived = await service.resolve_egress_credentials([vault_id])
    assert archived[0].scope_id == resolved[0].scope_id
    assert archived[0].credentials == ()
    assert await service._store().get(
        scope=service._scope(vault_id), key=f"{credential['credential_id']}/password",
    ) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo.git", "https://github.com/",
        "https://user:token@github.com/owner/repo.git",
        "https://github.com/owner/repo.git?token=secret",
        "https://github.com/owner/*", "https://github.com/owner/../repo.git",
        "https://github.com/owner/%2frepo.git", "https://github.com:8443/owner/repo.git",
    ],
)
async def test_http_basic_rejects_ambiguous_or_secret_bearing_destinations(url: str) -> None:
    with pytest.raises(APIError) as error:
        VaultService()._split_auth_payload("http_basic", {
            "type": "http_basic", "url": url, "username": "user", "password": "secret",
        })
    assert error.value.status_code == 400


# ── CRUD + secrecy invariants ───────────────────────────────────────────────


async def test_credential_views_never_carry_secret_payloads() -> None:
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="Alice")
    vault_id = vault["vault_id"]
    credential = await service.create_credential(
        _USER,
        vault_id,
        display_name="slack",
        auth={
            "type": "mcp_oauth",
            "mcp_server_url": "https://mcp.slack.com/mcp",
            "access_token": "xoxp-secret",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "refresh": {
                "token_endpoint": "https://slack.com/oauth/token",
                "client_id": "cid",
                "auth_method": "client_secret_post",
                "refresh_token": "rt-secret",
                "client_secret": "cs-secret",
            },
        },
    )
    serialized = json.dumps(credential)
    for secret in ("xoxp-secret", "rt-secret", "cs-secret"):
        assert secret not in serialized
    # Key/refresh metadata IS visible (audit surface).
    assert credential["auth"]["mcp_server_url"] == "https://mcp.slack.com/mcp"
    assert credential["auth"]["refresh"]["auth_method"] == "client_secret_post"
    listed = await service.list_credentials(_USER, vault_id)
    assert "xoxp-secret" not in json.dumps(listed)
    catalog = await service.list_vaults(_USER)
    assert catalog[0]["credentials"][0]["auth"]["mcp_server_url"] == "https://mcp.slack.com/mcp"
    assert "xoxp-secret" not in json.dumps(catalog)


async def test_concurrent_creates_of_one_key_leave_one_active_credential() -> None:
    """Eight admins save a credential for the same MCP server into one Vault at once.

    Only the partial unique index can decide this: a check-then-insert lets
    every request see no existing credential. The refused creates must also
    leave no secret value behind, because they are written in the same
    transaction as the row that failed.
    """
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="race")
    vault_id = vault["vault_id"]

    async def create(index: int) -> dict[str, Any] | APIError:
        try:
            return await service.create_credential(
                _USER, vault_id, auth=_bearer_auth("https://mcp.example.com/same", f"tok-{index}")
            )
        except APIError as exc:
            return exc

    outcomes = await asyncio.gather(*(create(index) for index in range(8)))

    created = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, APIError)]
    assert len(created) == 1
    assert {(error.code, error.status_code) for error in refused} == {
        ("VAULT_CREDENTIAL_CONFLICT", 409)
    }
    stored = await fetch_value(
        select(func.count()).select_from(_SECRETS).where(_SECRETS.c.scope == f"vault/{vault_id}")
    )
    assert stored == 1


async def test_creates_count_the_vaults_credentials_after_its_lock() -> None:
    """A create counts a Vault's active credentials only once it holds the Vault's row lock.

    The Vault is one short of its limit, and this test's own transaction holds
    the lock, as a concurrent create does between counting and inserting; it
    then inserts the twentieth credential. Creates that counted without waiting
    for the lock would each see nineteen and all succeed. Waiting, they see
    twenty once the holder commits, and every one is refused.
    """
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="nearly full")
    vault_id = vault["vault_id"]
    for index in range(MAX_CREDENTIALS_PER_VAULT - 1):
        await service.create_credential(
            _USER, vault_id, auth=_bearer_auth(f"https://mcp.example.com/held{index}")
        )

    async def create(index: int) -> dict[str, Any] | APIError:
        try:
            return await service.create_credential(
                _USER, vault_id, auth=_bearer_auth(f"https://mcp.example.com/new{index}")
            )
        except APIError as exc:
            return exc

    async with get_engine().connect() as holder:
        transaction = await holder.begin()
        await holder.execute(
            select(_VAULTS.c.vault_id)
            .where(_VAULTS.c.vault_id == vault_id)
            .with_for_update(key_share=True)
        )
        creates = [asyncio.create_task(create(index)) for index in range(5)]
        # Long enough for the creates to reach the lock; a create that does not
        # wait for it finishes in this time.
        await asyncio.sleep(0.5)
        await holder.execute(
            insert(_CREDENTIALS).values(
                credential_id="vcr_twentieth",
                vault_id=vault_id,
                auth_type="static_bearer",
                mcp_server_url="https://mcp.example.com/twentieth",
            )
        )
        await transaction.commit()
    outcomes = await asyncio.gather(*creates)

    assert all(isinstance(outcome, APIError) for outcome in outcomes)
    assert all("per-vault maximum" in str(outcome) for outcome in outcomes)
    active = await fetch_value(
        select(func.count())
        .select_from(_CREDENTIALS)
        .where(_CREDENTIALS.c.vault_id == vault_id, _CREDENTIALS.c.archived_at.is_(None))
    )
    assert active == MAX_CREDENTIALS_PER_VAULT


async def test_archive_purges_payload_but_keeps_metadata() -> None:
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="arch")
    vault_id = vault["vault_id"]
    credential = await service.create_credential(
        _USER, vault_id, auth=_bearer_auth("https://mcp.example.com/keep")
    )
    await service.archive_vault(_USER, vault_id, ensure_unbound=_nothing_bound)
    # Metadata survives for audit...
    view = await service.get_vault(_USER, vault_id)
    assert view["archived_at"] is not None
    listed = await service.list_credentials(_USER, vault_id)
    assert listed[0]["credential_id"] == credential["credential_id"]
    assert listed[0]["archived_at"] is not None
    # ...but the payload is purged, so resolution finds nothing to authorize with.
    assert await _mcp_headers(service, vault_id, "https://mcp.example.com/keep") == []
    # Archived vaults cannot be attached to future sessions.
    with pytest.raises(APIError):
        await service.validate_bound_vaults_for_session(
            [vault_id],
            backend_name="open_sandbox",
            backend_supports_egress_injection=False,
        )


async def test_vault_is_shared_by_admins_in_org_and_hidden_cross_org() -> None:
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="mine")
    assert (await service.get_vault(_OTHER, vault["vault_id"]))["vault_id"] == vault["vault_id"]
    with pytest.raises(APIError) as hidden:
        await service.get_vault(_OTHER_ORG, vault["vault_id"])
    assert hidden.value.status_code == 404


# ── resolution ──────────────────────────────────────────────────────────────


async def test_direct_mcp_resolution_preserves_target_and_hides_headers() -> None:
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="direct")
    credential = await service.create_credential(
        _USER,
        vault["vault_id"],
        auth=_static_header_auth("https://MCP.example.com/mcp/", value="header-secret"),
    )

    resolved = await service.resolve_mcp_credentials(
        [vault["vault_id"]],
        ["https://mcp.example.com/mcp", "not-a-url"],
    )

    assert len(resolved) == 1
    assert resolved[0].credential_id == credential["credential_id"]
    assert resolved[0].target_url == "https://mcp.example.com/mcp"
    assert dict(resolved[0].headers) == {"apikey": "header-secret"}
    assert "header-secret" not in repr(resolved[0])


async def test_concurrent_turns_refresh_an_expired_token_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five turns find the same access token expired at once.

    The provider rotates the refresh token on every use and rejects a spent
    one, as providers with refresh-token reuse detection do (and such a
    provider may revoke the whole grant on the reuse). So only one exchange may
    happen; the other turns wait for it and use the token it returned.
    """
    exchanges: list[str] = []
    current_refresh_token = {"value": "rt-1"}

    class _RotatingProvider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _RotatingProvider:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str], auth: Any = None) -> Any:
            exchanges.append(data["refresh_token"])
            # The other turns arrive while this exchange is in flight.
            await asyncio.sleep(0.3)
            if data["refresh_token"] != current_refresh_token["value"]:
                return _TokenResponse(400, {"error": "invalid_grant"})
            current_refresh_token["value"] = f"rt-{len(exchanges) + 1}"
            return _TokenResponse(
                200,
                {
                    "access_token": f"fresh-{len(exchanges)}",
                    "refresh_token": current_refresh_token["value"],
                    "expires_in": 3600,
                },
            )

    service = VaultService()
    vault = await service.create_vault(_USER, display_name="rotating")
    vault_id = vault["vault_id"]
    await service.create_credential(
        _USER, vault_id, auth=_expired_oauth_auth("https://mcp.example.com/rotating", "rt-1")
    )
    monkeypatch.setattr(vault_module.httpx, "AsyncClient", _RotatingProvider)

    headers = await asyncio.gather(
        *(_mcp_headers(service, vault_id, "https://mcp.example.com/rotating") for _ in range(5))
    )

    assert exchanges == ["rt-1"]
    assert headers == [[{"Authorization": "Bearer fresh-1"}]] * 5
    # The rotated refresh token is what the next refresh will present.
    credential = (await service.list_credentials(_USER, vault_id))[0]
    assert await service._store().get(
        scope=f"vault/{vault_id}", key=f"{credential['credential_id']}/refresh_token"
    ) == "rt-2"


async def test_static_header_is_write_only_rotatable_and_rejects_reserved_names() -> None:
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="market data")
    vault_id = vault["vault_id"]
    credential = await service.create_credential(
        _USER,
        vault_id,
        display_name="Alpha Vantage",
        auth=_static_header_auth("https://mcp.alphavantage.co/mcp", "alpha-secret"),
    )
    assert "alpha-secret" not in json.dumps(credential)
    assert credential["auth"]["header_name"] == "apikey"
    assert await _mcp_headers(service, vault_id, "https://MCP.alphavantage.co/mcp/") == [
        {"apikey": "alpha-secret"}
    ]

    credential_id = credential["credential_id"]
    await service.update_credential(
        _USER, vault_id, credential_id, auth={"value": "rotated-secret"}
    )
    assert await _mcp_headers(service, vault_id, "https://mcp.alphavantage.co/mcp") == [
        {"apikey": "rotated-secret"}
    ]
    with pytest.raises(APIError):
        await service.update_credential(
            _USER, vault_id, credential_id, auth={"header_name": "X-API-Key"}
        )
    with pytest.raises(APIError):
        await service.create_credential(
            _USER,
            vault_id,
            auth=_static_header_auth("https://mcp.example.com/mcp", header_name="Authorization"),
        )

    await service.archive_credential(_USER, vault_id, credential_id)
    assert await _mcp_headers(service, vault_id, "https://mcp.alphavantage.co/mcp") == []


# ── env-var credentials vs backend capability ───────────────────────────────


def _env_auth(**extra: Any) -> dict[str, Any]:
    return {
        "type": "environment_variable",
        "secret_name": "GITHUB_TOKEN",
        "secret_value": "ghp-secret",
        "networking": {"type": "limited", "allowed_hosts": ["api.github.com"]},
        **extra,
    }


async def test_env_credentials_require_egress_capable_backend_at_attach() -> None:
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="env")
    vault_id = vault["vault_id"]
    # An egress-capable backend exists at create time (plugin installed)…
    with unittest.mock.patch(
        "astrabox.seams.sandbox.any_enabled_backend_supports_egress_credential_injection",
        return_value=True,
    ):
        await service.create_credential(_USER, vault_id, auth=_env_auth())
    # …but attaching to a template whose backend lacks the capability
    # still fails loud (per-template backends can differ).
    with pytest.raises(APIError) as unsupported:
        await service.validate_bound_vaults_for_session(
            [vault_id],
            backend_name="open_sandbox",
            backend_supports_egress_injection=False,
        )
    assert unsupported.value.code == "VAULT_ENV_CREDENTIAL_UNSUPPORTED"
    cleaned = await service.validate_bound_vaults_for_session(
        [vault_id, "", f"  {vault_id}  "],
        backend_name="hypothetical-cloud",
        backend_supports_egress_injection=True,
    )
    assert cleaned == [vault_id]

    with unittest.mock.patch(
        "astrabox.common.utils.settings.load_astrabox_settings",
        return_value=unittest.mock.Mock(sandbox_credential_vault_enabled=False),
    ):
        with pytest.raises(APIError) as disabled:
            await service.validate_bound_vaults_for_session(
                [vault_id],
                backend_name="hypothetical-cloud",
                backend_supports_egress_injection=True,
            )
    assert disabled.value.code == "VAULT_ENV_CREDENTIAL_UNSUPPORTED"
    assert "ASTRABOX_SANDBOX_CREDENTIAL_VAULT=1" in disabled.value.message

    with pytest.raises(APIError) as assistant:
        await service.validate_bound_vaults_for_session(
            [vault_id],
            backend_name="open_sandbox",
            backend_supports_egress_injection=False,
            egress_injection_unsupported_reason=(
                "Assistant conversations support saved MCP credentials only."
            ),
        )
    assert "Assistant conversations" in assistant.value.message


@pytest.mark.parametrize(
    "allowed_requests",
    [
        {},
        {"methods": []},
        {"methods": ["GET /bad"]},
        {"paths": ["relative/path"]},
        {"paths": ["/ok?secret=not-a-path-pattern"]},
        {"unknown": ["GET"]},
    ],
)
async def test_env_credential_rejects_misleading_request_limits(
    allowed_requests: dict[str, Any],
) -> None:
    service = VaultService()
    vault = await service.create_vault(_USER, display_name="bad request limits")
    with unittest.mock.patch(
        "astrabox.seams.sandbox.any_enabled_backend_supports_egress_credential_injection",
        return_value=True,
    ):
        with pytest.raises(APIError):
            await service.create_credential(
                _USER,
                vault["vault_id"],
                auth=_env_auth(
                    secret_name="TOKEN",
                    networking={"type": "limited", "allowed_hosts": ["api.example.com"]},
                    allowed_requests=allowed_requests,
                ),
            )


# ── the local secret store ──────────────────────────────────────────────────


async def test_local_store_round_trip_is_bound_to_its_identity() -> None:
    store = LocalEncryptedSecretStore()
    await store.put(scope="vault/v1", key="c1/token", value="s3cret")
    assert await store.get(scope="vault/v1", key="c1/token") == "s3cret"
    # Ciphertext is bound to (scope, key): reading the same sealed blob
    # under a different identity must fail loud, not decrypt.
    sealed = store._seal(scope="vault/v1", key="c1/token", value="s3cret")
    with pytest.raises(RuntimeError):
        store._unseal(scope="vault/v2", key="c1/token", sealed=sealed)
    await store.purge_scope(scope="vault/v1")
    assert await store.get(scope="vault/v1", key="c1/token") is None


async def test_concurrent_first_writes_of_one_secret_leave_one_row() -> None:
    """Eight writers store the same (scope, key) for the first time at once.

    The key is the table's primary key and each write is one upsert, so the
    racing writers replace one row instead of each inserting their own.
    """
    store = secret_store_for_name("local")

    await asyncio.gather(
        *(store.put(scope="deployment/d1", key="channel_credentials", value=f"v{index}")
          for index in range(8))
    )

    rows = await fetch_value(
        select(func.count()).select_from(_SECRETS).where(_SECRETS.c.scope == "deployment/d1")
    )
    assert rows == 1
    assert await store.get(scope="deployment/d1", key="channel_credentials") in {
        f"v{index}" for index in range(8)
    }
