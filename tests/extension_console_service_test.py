"""The extension console's ACL and the bundled LiteLLM adapter's credential scope."""

from __future__ import annotations

from typing import Any, Mapping

import httpx
import jwt
import pytest
from fastapi import FastAPI

from astrabox.api.routes import extensions as extension_routes
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_extension_service import (
    AgentExtensionService,
)
from astrabox.providers import litellm_extensions as adapter
from astrabox.providers.litellm_shared_auth import (
    AGENT_CONSOLE_PURPOSE,
    CAPABILITY_PREFIX,
    verify_litellm_capability,
)
from astrabox.seams.extensions import (
    ExtensionCatalog,
    ExtensionCatalogItem,
    ExtensionProvider,
    ExtensionProviderFeatures,
    ExtensionRuntimeSelection,
    ManagementConsoleCookie,
    ManagementConsoleSession,
)
_SIGNING_SECRET = "astrabox-extension-test-signing-key-32-bytes"


class _AgentRepo:
    def __init__(self) -> None:
        self.update_calls = 0
        self.agent: dict[str, Any] = {
            "agent_id": "agent-1",
            "created_by": "owner",
            "admins": ["coadmin"],
            "visibility": "allowlist",
            "allowed_user_ids": ["viewer"],
            "version": 1,
        }

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return dict(self.agent) if agent_id == "agent-1" else None

    async def update_definition(
        self,
        agent_id: str,
        *,
        expected_version: int,
        changes: dict[str, Any],
        updated_by: str | None,
    ) -> dict[str, Any] | None:
        if agent_id != "agent-1" or self.agent["version"] != expected_version:
            return None
        self.update_calls += 1
        self.agent.update(changes, version=expected_version + 1, updated_by=updated_by)
        return dict(self.agent)


class _ExtensionProvider(ExtensionProvider):
    name = "catalog-test"

    def __init__(self) -> None:
        self.catalog_calls = 0

    async def list_catalog(self, *, org_id: str) -> ExtensionCatalog:
        _ = org_id
        self.catalog_calls += 1
        return ExtensionCatalog(
            mcp_servers=(
                ExtensionCatalogItem(item_id="server-1", name="issue-tracker"),
            ),
            skills=(
                ExtensionCatalogItem(item_id="skill-1", name="triage"),
            ),
        )

    def materialize(
        self,
        *,
        mcp_servers: tuple[ExtensionCatalogItem, ...],
        skills: tuple[ExtensionCatalogItem, ...],
    ) -> ExtensionRuntimeSelection:
        # MCP servers are not materialized when a selection is saved: the
        # binding is produced from the catalog at Session start instead.
        assert mcp_servers == ()
        assert [item.item_id for item in skills] == ["skill-1"]
        return ExtensionRuntimeSelection(
            skill_descriptors=("https://git.example.test/team/triage",),
        )


@pytest.mark.asyncio
async def test_agent_acl_is_checked_before_catalog_provider_access() -> None:
    provider = _ExtensionProvider()
    service = AgentExtensionService(
        agent_repo=_AgentRepo(),  # type: ignore[arg-type]
        provider=provider,
    )

    with pytest.raises(APIError) as raised:
        await service.get_catalog(UserContext("viewer"), "agent-1")

    assert raised.value.code == "AGENT_MANAGEMENT_REQUIRED"
    assert provider.catalog_calls == 0


def test_browser_adapter_uses_a_scoped_session_not_a_generated_gateway_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "session_signing_secret", lambda: _SIGNING_SECRET)
    session = adapter._ManagementSessionService().create_agent_session(
        UserContext("owner", email="owner@example.test"),
        "agent-1",
        "mcp",
    )

    wrapper = jwt.decode(session.ui_token, _SIGNING_SECRET, algorithms=["HS256"])
    capability = str(wrapper["key"])
    assert capability.startswith(CAPABILITY_PREFIX)
    claims = verify_litellm_capability(
        capability,
        secret=_SIGNING_SECRET,
        purposes=(AGENT_CONSOLE_PURPOSE,),
    )
    assert claims["agent_id"] == "agent-1"
    assert claims["allowed_routes"] == list(adapter.AGENT_MANAGEMENT_ROUTES)
    assert "/key/generate" not in claims["allowed_routes"]


class _AdminIdentity:
    async def resolve(self, headers: Mapping[str, str]) -> UserContext:
        if headers.get("authorization") != "Bearer oidc-admin-access-token":
            raise APIError("AUTH_REQUIRED", "sign-in required", 401)
        return UserContext("admin-user", roles=["admin"])

    @staticmethod
    def login_url(next_url: str = "/") -> str:
        return f"/api/v1/auth/login?next={next_url}"


@pytest.mark.asyncio
async def test_bundled_admin_ui_reuses_the_shared_web_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_registered_on", None)
    monkeypatch.setattr(adapter, "session_signing_secret", lambda: _SIGNING_SECRET)
    upstream_paths: list[str] = []

    async def _proxy(
        _self: Any,
        _request: Any,
        upstream_path: str,
        *,
        ui_token: str | None = None,
    ) -> httpx.Response:
        upstream_paths.append(upstream_path)
        assert ui_token
        return httpx.Response(
            200,
            content=b"<html>gateway administration</html>",
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(adapter._ManagementSessionService, "proxy", _proxy)
    app = FastAPI()
    app.state.web_identity_resolver = _AdminIdentity()
    adapter.register_litellm_extension_routes(app)

    async with httpx.AsyncClient(
        base_url="http://astrabox.test",
        transport=httpx.ASGITransport(app=app),
        follow_redirects=False,
    ) as client:
        signed_out = await client.get("/litellm")
        signed_in = await client.get(
            "/litellm",
            headers={"Authorization": "Bearer oidc-admin-access-token"},
        )

    assert signed_out.status_code == 302
    assert signed_out.headers["location"] == "/api/v1/auth/login?next=/litellm"
    assert signed_in.status_code == 200
    assert signed_in.text == "<html>gateway administration</html>"
    assert adapter.ADMIN_UI_COOKIE in signed_in.cookies
    assert adapter.LITELLM_UI_COOKIE in signed_in.cookies
    assert upstream_paths == ["/ui/"]


async def test_gateway_key_and_inference_routes_are_refused_by_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_registered_on", None)
    upstream_paths: list[str] = []

    async def _proxy(
        _self: Any,
        _request: Any,
        upstream_path: str,
        *,
        ui_token: str | None = None,
    ) -> httpx.Response:
        upstream_paths.append(upstream_path)
        return httpx.Response(200, content=b"{}")

    monkeypatch.setattr(adapter._ManagementSessionService, "proxy", _proxy)
    app = FastAPI()
    adapter.register_litellm_extension_routes(app)

    async with httpx.AsyncClient(
        base_url="http://astrabox.test",
        transport=httpx.ASGITransport(app=app),
    ) as client:
        key_post = await client.post("/key/generate", content="{}")
        inference_post = await client.post("/v1/chat/completions", content="{}")
        key_get = await client.get("/key/generate")

    # 403 must come from policy, not from the SPA catch-all's method table:
    # a GET probe against the catch-all would be served index.html instead.
    assert key_post.status_code == 403
    assert inference_post.status_code == 403
    assert key_get.status_code == 403
    assert upstream_paths == []


class _DeclaredProvider(ExtensionProvider):
    name = "declared"
    features = ExtensionProviderFeatures(display_name="Declared")

    async def list_catalog(self, *, org_id: str) -> ExtensionCatalog:
        _ = org_id
        return ExtensionCatalog()

    def materialize(
        self,
        *,
        mcp_servers: tuple[ExtensionCatalogItem, ...],
        skills: tuple[ExtensionCatalogItem, ...],
    ) -> ExtensionRuntimeSelection:
        _ = (mcp_servers, skills)
        return ExtensionRuntimeSelection()


class _ConsoleProvider(_DeclaredProvider):
    name = "console"
    features = ExtensionProviderFeatures(display_name="Console", management_console=True)

    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    async def open_management_console(
        self, *, user: UserContext, agent_id: str, section: Any
    ) -> ManagementConsoleSession:
        self.opened.append((agent_id, section))
        return ManagementConsoleSession(
            open_url="/console/open",
            expires_in=60,
            cookies=(ManagementConsoleCookie("console_grant", "grant-token", max_age=60),),
        )


async def test_the_console_session_opens_only_a_console_the_selected_provider_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _manager(_self: Any, _user: Any, agent_id: str) -> dict[str, Any]:
        return {"agent_id": agent_id}

    async def _user(_request: Any) -> UserContext:
        return UserContext("owner")

    monkeypatch.setattr(AgentExtensionService, "assert_can_manage", _manager)
    monkeypatch.setattr(extension_routes, "get_current_user_context", _user)
    monkeypatch.setattr(extension_routes, "_registered_on", None)
    selected: list[ExtensionProvider | None] = []
    monkeypatch.setattr(extension_routes, "configured_extension_provider", lambda: selected[-1])
    app = FastAPI()
    extension_routes.register_extension_routes(app)

    async with httpx.AsyncClient(
        base_url="https://astrabox.test", transport=httpx.ASGITransport(app=app)
    ) as client:
        console = _ConsoleProvider()
        selected.append(console)
        opened = await client.post(
            "/api/v1/agents/agent-1/extension-console/session", json={"section": "skills"}
        )
        assert opened.status_code == 200
        assert opened.json()["data"] == {"open_url": "/console/open", "expires_in": 60}
        assert console.opened == [("agent-1", "skills")]
        cookie = opened.headers["set-cookie"]
        assert cookie.startswith("console_grant=grant-token;")
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        assert opened.headers["cache-control"] == "no-store"

        # A provider that declares no console is never asked to open one.
        selected.append(_DeclaredProvider())
        with pytest.raises(APIError) as unsupported:
            await client.post("/api/v1/agents/agent-1/extension-console/session", json={})
        assert unsupported.value.code == "EXTENSION_CONSOLE_UNSUPPORTED"

        selected.append(None)
        with pytest.raises(APIError) as disabled:
            await client.post("/api/v1/agents/agent-1/extension-console/session", json={})
        assert disabled.value.code == "EXTENSION_CATALOG_DISABLED"
