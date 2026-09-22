"""Vault unit gate — NO Docker, NO secret, NO live turn.

Exercises the vault vertical end-to-end in process against the real SQLite
collection backend in a temp state dir: vault/credential CRUD semantics
(write-only payloads, per-vault unique keys, the 20-credential cap, immutable
key fields, archive purge), the local encrypted secret store (AES-GCM roundtrip
+ ciphertext/identity binding), egress-side MCP credential resolution
(first-vault-match-wins, unauthenticated None, expired-token refresh-on-use),
and the session-attach validation that rejects ``environment_variable``
credentials on a backend without egress substitution.
"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
import unittest.mock
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator import vault_service as vault_module
from astrabox.core.service.orchestrator.vault_service import (
    MAX_CREDENTIALS_PER_VAULT,
    VaultService,
)
from astrabox.providers.secret_store import LocalEncryptedSecretStore

_USER = UserContext(user_id="test-user")
_OTHER = UserContext(user_id="other-user")
_OTHER_ORG = UserContext(user_id="other-org-admin", org_id="other-org")


@pytest.fixture(autouse=True, scope="module")
def _vault_environment() -> Any:
    """Install this module's vault settings, then restore process state.

    The vault vertical reads its state dir, DB backend, and AES-GCM master key
    from ``ASTRABOX_*`` env through the cached ``get_settings()``. A
    module-scoped ``MonkeyPatch`` exposes the values only while these tests run.
    Clearing the settings cache on entry makes the first test read the patched
    environment; clearing it on exit makes the next module read its own values.
    """
    state_dir = tempfile.mkdtemp(prefix="astrabox-vault-test-")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("ASTRABOX_STATE_DIR", state_dir)
        monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
        # Deterministic master key (32 bytes, urlsafe b64) so the store needs no key file.
        monkeypatch.setenv(
            "ASTRABOX_VAULT_MASTER_KEY",
            base64.urlsafe_b64encode(b"k" * 32).decode(),
        )
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def _run(coro):
    return asyncio.run(coro)


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


class VaultServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = VaultService()

    def test_http_basic_is_write_only_and_rotates_without_changing_destination(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="private Git")
            vault_id = vault["vault_id"]
            credential = await self.service.create_credential(
                _USER, vault_id,
                auth={
                    "type": "http_basic", "url": "https://GitHub.com/owner/skills.git/",
                    "username": "x-access-token", "password": "first-private-token",
                },
            )
            self.assertEqual(credential["auth"], {
                "type": "http_basic", "url": "https://github.com/owner/skills.git",
                "username": "x-access-token",
            })
            self.assertNotIn("first-private-token", json.dumps(await self.service.list_vaults(_USER)))
            stored = await self.service._repo.get_credential(credential["credential_id"])
            self.assertNotIn("first-private-token", json.dumps(stored))
            resolved = await self.service.resolve_egress_credentials([vault_id])
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0].credentials[0].password, "first-private-token")
            self.assertNotIn("first-private-token", repr(resolved))
            rotated = await self.service.update_credential(
                _USER, vault_id, credential["credential_id"], auth={"password": "second-private-token"},
            )
            self.assertEqual(rotated["auth"], credential["auth"])
            self.assertEqual((await self.service.resolve_egress_credentials([vault_id]))[0].credentials[0].password, "second-private-token")
            with self.assertRaises(APIError) as error:
                await self.service.update_credential(
                    _USER, vault_id, credential["credential_id"], auth={"url": "https://evil.test/repo.git"},
                )
            self.assertEqual(error.exception.status_code, 400)
            await self.service.archive_credential(_USER, vault_id, credential["credential_id"])
            archived = await self.service.resolve_egress_credentials([vault_id])
            self.assertEqual(archived[0].scope_id, resolved[0].scope_id)
            self.assertEqual(archived[0].credentials, ())
            self.assertIsNone(await self.service._store().get(
                scope=self.service._scope(vault_id), key=f"{credential['credential_id']}/password",
            ))

        _run(scenario())

    def test_http_basic_rejects_ambiguous_or_secret_bearing_destinations(self) -> None:
        for url in (
            "http://github.com/owner/repo.git", "https://github.com/",
            "https://user:token@github.com/owner/repo.git",
            "https://github.com/owner/repo.git?token=secret",
            "https://github.com/owner/*", "https://github.com/owner/../repo.git",
            "https://github.com/owner/%2frepo.git", "https://github.com:8443/owner/repo.git",
        ):
            with self.subTest(url=url), self.assertRaises(APIError) as error:
                self.service._split_auth_payload("http_basic", {
                    "type": "http_basic", "url": url, "username": "user", "password": "secret",
                })
            self.assertEqual(error.exception.status_code, 400)

    # ── CRUD + secrecy invariants ───────────────────────────────────────────

    def test_credential_views_never_carry_secret_payloads(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="Alice")
            vault_id = vault["vault_id"]
            credential = await self.service.create_credential(
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
                self.assertNotIn(secret, serialized)
            # Key/refresh metadata IS visible (audit surface).
            self.assertEqual(credential["auth"]["mcp_server_url"], "https://mcp.slack.com/mcp")
            self.assertEqual(credential["auth"]["refresh"]["auth_method"], "client_secret_post")
            listed = await self.service.list_credentials(_USER, vault_id)
            self.assertNotIn("xoxp-secret", json.dumps(listed))
            catalog = await self.service.list_vaults(_USER)
            self.assertEqual(catalog[0]["credentials"][0]["auth"]["mcp_server_url"], "https://mcp.slack.com/mcp")
            self.assertNotIn("xoxp-secret", json.dumps(catalog))

        _run(scenario())

    def test_unique_key_conflict_and_credential_cap(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="caps")
            vault_id = vault["vault_id"]
            await self.service.create_credential(
                _USER, vault_id, auth=_bearer_auth("https://mcp.example.com/a")
            )
            with self.assertRaises(APIError) as ctx:
                await self.service.create_credential(
                    # trailing slash normalizes to the same key -> 409
                    _USER, vault_id, auth=_bearer_auth("https://MCP.example.com/a/")
                )
            self.assertEqual(ctx.exception.status_code, 409)

            for index in range(1, MAX_CREDENTIALS_PER_VAULT):
                await self.service.create_credential(
                    _USER, vault_id, auth=_bearer_auth(f"https://mcp.example.com/s{index}")
                )
            with self.assertRaises(APIError) as capped:
                await self.service.create_credential(
                    _USER, vault_id, auth=_bearer_auth("https://mcp.example.com/overflow")
                )
            self.assertEqual(capped.exception.status_code, 400)

        _run(scenario())

    def test_key_fields_are_immutable_and_payload_rotates(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="rot")
            vault_id = vault["vault_id"]
            credential = await self.service.create_credential(
                _USER, vault_id, auth=_bearer_auth("https://mcp.example.com/x", "old-token")
            )
            credential_id = credential["credential_id"]
            with self.assertRaises(APIError):
                await self.service.update_credential(
                    _USER, vault_id, credential_id,
                    auth={"mcp_server_url": "https://elsewhere.example.com"},
                )
            await self.service.update_credential(
                _USER, vault_id, credential_id, auth={"token": "new-token"}
            )
            headers = await self.service.resolve_mcp_headers(
                [vault_id], "https://mcp.example.com/x"
            )
            self.assertEqual(headers, {"Authorization": "Bearer new-token"})

        _run(scenario())

    def test_archive_purges_payload_but_keeps_metadata(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="arch")
            vault_id = vault["vault_id"]
            credential = await self.service.create_credential(
                _USER, vault_id, auth=_bearer_auth("https://mcp.example.com/keep")
            )
            await self.service.archive_vault(_USER, vault_id)
            # Metadata survives for audit...
            view = await self.service.get_vault(_USER, vault_id)
            self.assertIsNotNone(view["archived_at"])
            listed = await self.service.list_credentials(_USER, vault_id)
            self.assertEqual(listed[0]["credential_id"], credential["credential_id"])
            self.assertIsNotNone(listed[0]["archived_at"])
            # ...but the payload is purged, so resolution finds nothing to authorize with.
            headers = await self.service.resolve_mcp_headers(
                [vault_id], "https://mcp.example.com/keep"
            )
            self.assertEqual(headers, {})
            # Archived vaults cannot be attached to future sessions.
            with self.assertRaises(APIError):
                await self.service.validate_bound_vaults_for_session(
                    [vault_id],
                    backend_name="open_sandbox",
                    backend_supports_egress_injection=False,
                )

        _run(scenario())

    def test_vault_is_shared_by_admins_in_org_and_hidden_cross_org(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="mine")
            self.assertEqual(
                (await self.service.get_vault(_OTHER, vault["vault_id"]))["vault_id"],
                vault["vault_id"],
            )
            with self.assertRaises(APIError) as ctx:
                await self.service.get_vault(_OTHER_ORG, vault["vault_id"])
            self.assertEqual(ctx.exception.status_code, 404)

        _run(scenario())

    # ── resolution ──────────────────────────────────────────────────────────

    def test_first_vault_match_wins_and_no_match_is_none(self) -> None:
        async def scenario() -> None:
            first = await self.service.create_vault(_USER, display_name="first")
            second = await self.service.create_vault(_USER, display_name="second")
            await self.service.create_credential(
                _USER, first["vault_id"], auth=_bearer_auth("https://mcp.example.com/m", "from-first")
            )
            await self.service.create_credential(
                _USER, second["vault_id"], auth=_bearer_auth("https://mcp.example.com/m", "from-second")
            )
            headers = await self.service.resolve_mcp_headers(
                [second["vault_id"], first["vault_id"]], "https://mcp.example.com/m"
            )
            self.assertEqual(headers, {"Authorization": "Bearer from-second"})
            self.assertEqual(
                await self.service.resolve_mcp_headers(
                    [first["vault_id"]], "https://unrelated.example.com"
                ),
                {},
            )

        _run(scenario())

    def test_direct_mcp_resolution_preserves_target_and_hides_headers(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="direct")
            credential = await self.service.create_credential(
                _USER,
                vault["vault_id"],
                auth=_static_header_auth(
                    "https://MCP.example.com/mcp/",
                    value="header-secret",
                ),
            )

            resolved = await self.service.resolve_mcp_credentials(
                [vault["vault_id"]],
                ["https://mcp.example.com/mcp", "not-a-url"],
            )

            self.assertEqual(len(resolved), 1)
            self.assertEqual(
                resolved[0].credential_id, credential["credential_id"]
            )
            self.assertEqual(
                resolved[0].target_url, "https://mcp.example.com/mcp"
            )
            self.assertEqual(dict(resolved[0].headers), {"apikey": "header-secret"})
            self.assertNotIn("header-secret", repr(resolved[0]))

        _run(scenario())

    def test_oauth_refresh_on_use(self) -> None:
        class _FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, Any]:
                return {
                    "access_token": "fresh-token",
                    "refresh_token": "rotated-rt",
                    "expires_in": 3600,
                }

        class _FakeAsyncClient:
            calls: list[dict[str, Any]] = []

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_FakeAsyncClient":
                return self

            async def __aexit__(self, *exc: Any) -> None:
                return None

            async def post(self, url: str, *, data: dict[str, str], auth: Any = None) -> Any:
                type(self).calls.append({"url": url, "data": dict(data), "auth": auth})
                return _FakeResponse()

        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="oauth")
            vault_id = vault["vault_id"]
            await self.service.create_credential(
                _USER,
                vault_id,
                auth={
                    "type": "mcp_oauth",
                    "mcp_server_url": "https://mcp.github.com/mcp",
                    "access_token": "stale-token",
                    "expires_at": "2020-01-01T00:00:00+00:00",  # long expired
                    "refresh": {
                        "token_endpoint": "https://github.com/oauth/token",
                        "client_id": "cid",
                        "auth_method": "client_secret_post",
                        "refresh_token": "rt-1",
                        "client_secret": "cs-1",
                    },
                },
            )
            original_client = vault_module.httpx.AsyncClient
            vault_module.httpx.AsyncClient = _FakeAsyncClient  # type: ignore[misc]
            try:
                headers = await self.service.resolve_mcp_headers(
                    [vault_id], "https://mcp.github.com/mcp"
                )
            finally:
                vault_module.httpx.AsyncClient = original_client  # type: ignore[misc]
            self.assertEqual(headers, {"Authorization": "Bearer fresh-token"})
            call = _FakeAsyncClient.calls[-1]
            self.assertEqual(call["url"], "https://github.com/oauth/token")
            self.assertEqual(call["data"]["grant_type"], "refresh_token")
            self.assertEqual(call["data"]["client_secret"], "cs-1")
            # The refreshed token persists: a second resolve needs NO refresh call.
            call_count = len(_FakeAsyncClient.calls)
            again = await self.service.resolve_mcp_headers(
                [vault_id], "https://mcp.github.com/mcp"
            )
            self.assertEqual(again, {"Authorization": "Bearer fresh-token"})
            self.assertEqual(len(_FakeAsyncClient.calls), call_count)

        _run(scenario())

    def test_static_header_is_write_only_rotatable_and_rejects_reserved_names(
        self,
    ) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="market data")
            vault_id = vault["vault_id"]
            credential = await self.service.create_credential(
                _USER,
                vault_id,
                display_name="Alpha Vantage",
                auth=_static_header_auth(
                    "https://mcp.alphavantage.co/mcp",
                    "alpha-secret",
                ),
            )
            serialized = json.dumps(credential)
            self.assertNotIn("alpha-secret", serialized)
            self.assertEqual(credential["auth"]["header_name"], "apikey")
            self.assertEqual(
                await self.service.resolve_mcp_headers(
                    [vault_id], "https://MCP.alphavantage.co/mcp/"
                ),
                {"apikey": "alpha-secret"},
            )

            credential_id = credential["credential_id"]
            await self.service.update_credential(
                _USER,
                vault_id,
                credential_id,
                auth={"value": "rotated-secret"},
            )
            self.assertEqual(
                await self.service.resolve_mcp_headers(
                    [vault_id], "https://mcp.alphavantage.co/mcp"
                ),
                {"apikey": "rotated-secret"},
            )
            with self.assertRaises(APIError):
                await self.service.update_credential(
                    _USER,
                    vault_id,
                    credential_id,
                    auth={"header_name": "X-API-Key"},
                )
            with self.assertRaises(APIError):
                await self.service.create_credential(
                    _USER,
                    vault_id,
                    auth=_static_header_auth(
                        "https://mcp.example.com/mcp",
                        header_name="Authorization",
                    ),
                )

            await self.service.archive_credential(
                _USER,
                vault_id,
                credential_id,
            )
            self.assertEqual(
                await self.service.resolve_mcp_headers(
                    [vault_id], "https://mcp.alphavantage.co/mcp"
                ),
                {},
            )

        _run(scenario())

    # ── env-var credentials vs backend capability ───────────────────────────

    def test_env_credential_create_is_capability_gated(self) -> None:
        """With no egress-capable backend registered, creation fails AT CREATE —
        the API never exposes a credential type that could only fail later."""

        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="env")
            vault_id = vault["vault_id"]
            with unittest.mock.patch(
                "astrabox.seams.sandbox.any_backend_supports_egress_credential_injection",
                return_value=False,
            ):
                with self.assertRaises(APIError) as ctx:
                    await self.service.create_credential(
                        _USER,
                        vault_id,
                        auth={
                            "type": "environment_variable",
                            "secret_name": "GITHUB_TOKEN",
                            "secret_value": "ghp-secret",
                            "networking": {
                                "type": "limited",
                                "allowed_hosts": ["api.github.com"],
                            },
                        },
                    )
            self.assertEqual(ctx.exception.code, "VAULT_ENV_CREDENTIAL_UNSUPPORTED")

        _run(scenario())

    def test_env_credentials_require_egress_capable_backend_at_attach(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="env")
            vault_id = vault["vault_id"]
            # An egress-capable backend exists at create time (plugin installed)…
            with unittest.mock.patch(
                "astrabox.seams.sandbox.any_backend_supports_egress_credential_injection",
                return_value=True,
            ):
                await self.service.create_credential(
                    _USER,
                    vault_id,
                    auth={
                        "type": "environment_variable",
                        "secret_name": "GITHUB_TOKEN",
                        "secret_value": "ghp-secret",
                        "networking": {
                            "type": "limited",
                            "allowed_hosts": ["api.github.com"],
                        },
                    },
                )
            # …but attaching to a template whose backend lacks the capability
            # still fails loud (per-template backends can differ).
            with self.assertRaises(APIError) as ctx:
                await self.service.validate_bound_vaults_for_session(
                    [vault_id],
                    backend_name="open_sandbox",
                    backend_supports_egress_injection=False,
                )
            self.assertEqual(ctx.exception.code, "VAULT_ENV_CREDENTIAL_UNSUPPORTED")
            cleaned = await self.service.validate_bound_vaults_for_session(
                [vault_id, "", f"  {vault_id}  "],
                backend_name="hypothetical-cloud",
                backend_supports_egress_injection=True,
            )
            self.assertEqual(cleaned, [vault_id])

            with unittest.mock.patch(
                "astrabox.common.utils.settings.load_astrabox_settings",
                return_value=unittest.mock.Mock(
                    sandbox_credential_vault_enabled=False
                ),
            ):
                with self.assertRaises(APIError) as disabled_ctx:
                    await self.service.validate_bound_vaults_for_session(
                        [vault_id],
                        backend_name="hypothetical-cloud",
                        backend_supports_egress_injection=True,
                    )
            self.assertEqual(
                disabled_ctx.exception.code, "VAULT_ENV_CREDENTIAL_UNSUPPORTED"
            )
            self.assertIn(
                "ASTRABOX_SANDBOX_CREDENTIAL_VAULT=1",
                disabled_ctx.exception.message,
            )

            with self.assertRaises(APIError) as assistant_ctx:
                await self.service.validate_bound_vaults_for_session(
                    [vault_id],
                    backend_name="open_sandbox",
                    backend_supports_egress_injection=False,
                    egress_injection_unsupported_reason=(
                        "Assistant conversations support saved MCP credentials only."
                    ),
                )
            self.assertIn("Assistant conversations", assistant_ctx.exception.message)

        _run(scenario())

    def test_env_credential_preserves_and_normalizes_allowed_requests(self) -> None:
        async def scenario() -> None:
            vault = await self.service.create_vault(_USER, display_name="request limits")
            vault_id = vault["vault_id"]
            with unittest.mock.patch(
                "astrabox.seams.sandbox.any_backend_supports_egress_credential_injection",
                return_value=True,
            ):
                created = await self.service.create_credential(
                    _USER,
                    vault_id,
                    auth={
                        "type": "environment_variable",
                        "secret_name": "GITHUB_TOKEN",
                        "secret_value": "ghp-secret",
                        "networking": {
                            "type": "limited",
                            "allowed_hosts": ["api.github.com"],
                        },
                        "injection_location": {"header": True, "body": False},
                        "allowed_requests": {
                            "methods": ["get", "POST", "GET"],
                            "paths": ["/repos/acme/private/*", "/repos/acme/private/*"],
                        },
                        "allow_insecure_http": False,
                    },
                )

            assert created["auth"]["allowed_requests"] == {
                "methods": ["GET", "POST"],
                "paths": ["/repos/acme/private/*"],
            }
            assert created["auth"]["allow_insecure_http"] is False
            assert "ghp-secret" not in json.dumps(created)

            resolved = await self.service.resolve_env_credentials([vault_id])
            assert len(resolved) == 1
            assert resolved[0].allowed_requests == created["auth"]["allowed_requests"]
            assert resolved[0].allow_insecure_http is False

            cleared = await self.service.update_credential(
                _USER,
                vault_id,
                created["credential_id"],
                auth={"allowed_requests": None},
            )
            assert "allowed_requests" not in cleared["auth"]
            resolved_after_clear = await self.service.resolve_env_credentials([vault_id])
            assert resolved_after_clear[0].allowed_requests == {}

        _run(scenario())

    def test_env_credential_rejects_misleading_request_limits(self) -> None:
        async def scenario() -> None:
            invalid_values = [
                {},
                {"methods": []},
                {"methods": ["GET /bad"]},
                {"paths": ["relative/path"]},
                {"paths": ["/ok?secret=not-a-path-pattern"]},
                {"unknown": ["GET"]},
            ]
            for index, allowed_requests in enumerate(invalid_values):
                vault = await self.service.create_vault(
                    _USER, display_name=f"bad request limits {index}"
                )
                with unittest.mock.patch(
                    "astrabox.seams.sandbox.any_backend_supports_egress_credential_injection",
                    return_value=True,
                ):
                    with self.assertRaises(APIError):
                        await self.service.create_credential(
                            _USER,
                            vault["vault_id"],
                            auth={
                                "type": "environment_variable",
                                "secret_name": "TOKEN",
                                "secret_value": "secret",
                                "networking": {
                                    "type": "limited",
                                    "allowed_hosts": ["api.example.com"],
                                },
                                "allowed_requests": allowed_requests,
                            },
                        )

        _run(scenario())


class LocalSecretStoreTest(unittest.TestCase):
    def test_roundtrip_and_identity_binding(self) -> None:
        async def scenario() -> None:
            store = LocalEncryptedSecretStore()
            await store.put(scope="vault/v1", key="c1/token", value="s3cret")
            self.assertEqual(await store.get(scope="vault/v1", key="c1/token"), "s3cret")
            # Ciphertext is bound to (scope, key): reading the same sealed blob
            # under a different identity must fail loud, not decrypt.
            sealed = store._seal(scope="vault/v1", key="c1/token", value="s3cret")
            with self.assertRaises(RuntimeError):
                store._unseal(scope="vault/v2", key="c1/token", sealed=sealed)
            await store.purge_scope(scope="vault/v1")
            self.assertIsNone(await store.get(scope="vault/v1", key="c1/token"))

        _run(scenario())


if __name__ == "__main__":
    unittest.main()
