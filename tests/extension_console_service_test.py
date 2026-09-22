"""Extension seam behavior and the bundled provider adapter."""

from __future__ import annotations

from typing import Any, Mapping

import httpx
import jwt
import pytest
from fastapi import FastAPI

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_extension_service import (
    AGENT_EXTENSION_CATALOG_FIELD,
    AgentExtensionService,
)
from astrabox.core.service.orchestrator.mcp_assignments import (
    AGENT_MCP_ASSIGNMENTS_FIELD,
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
    ExtensionRuntimeSelection,
    RuntimeMCPServer,
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

    async def compare_and_update_agent(
        self,
        agent_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        if agent_id != "agent-1" or self.agent["version"] != expected["version"]:
            return False
        self.update_calls += 1
        self.agent.update(updates)
        return True


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
async def test_a_console_selection_becomes_a_provider_qualified_assignment() -> None:
    repo = _AgentRepo()
    provider = _ExtensionProvider()
    service = AgentExtensionService(agent_repo=repo, provider=provider)  # type: ignore[arg-type]

    result = await service.set_assignment(
        UserContext("owner"),
        "agent-1",
        mcp_server_ids=["server-1"],
        skill_ids=["skill-1"],
    )

    assert result["selected_mcp_server_ids"] == ["server-1"]
    assert repo.agent[AGENT_MCP_ASSIGNMENTS_FIELD] == [
        {"provider": "catalog-test", "item_id": "server-1"}
    ]
    # The snapshot carries Skill material only; no second MCP list beside the
    # assignment the runtime reads.
    persisted = repo.agent[AGENT_EXTENSION_CATALOG_FIELD]
    assert "mcp_servers" not in persisted
    assert persisted["skills"] == ["https://git.example.test/team/triage"]


@pytest.mark.asyncio
async def test_saving_an_unchanged_selection_does_not_rotate_the_agent_pool_version() -> None:
    repo = _AgentRepo()
    service = AgentExtensionService(
        agent_repo=repo,  # type: ignore[arg-type]
        provider=_ExtensionProvider(),
    )

    first = await service.set_assignment(
        UserContext("owner"),
        "agent-1",
        mcp_server_ids=["server-1"],
        skill_ids=["skill-1"],
    )
    first_version = repo.agent["version"]
    second = await service.set_assignment(
        UserContext("owner"),
        "agent-1",
        mcp_server_ids=["server-1"],
        skill_ids=["skill-1"],
    )

    assert second == first
    assert repo.agent["version"] == first_version
    assert repo.update_calls == 1


@pytest.mark.asyncio
async def test_an_applied_assignment_schedules_reconciliation_and_a_noop_does_not() -> None:
    repo = _AgentRepo()
    repo.agent["prewarm_enabled"] = True
    scheduled: list[str] = []
    service = AgentExtensionService(
        agent_repo=repo,  # type: ignore[arg-type]
        provider=_ExtensionProvider(),
        schedule_runtime_reconciliation=scheduled.append,
    )

    await service.set_assignment(
        UserContext("owner"),
        "agent-1",
        mcp_server_ids=["server-1"],
        skill_ids=["skill-1"],
    )
    assert scheduled == ["agent-1"]

    await service.set_assignment(
        UserContext("owner"),
        "agent-1",
        mcp_server_ids=["server-1"],
        skill_ids=["skill-1"],
    )
    assert scheduled == ["agent-1"]


@pytest.mark.asyncio
async def test_saving_one_catalog_leaves_another_catalogs_assignments_alone() -> None:
    """Each surface owns its own rows — an Agent may be assigned from several."""
    repo = _AgentRepo()
    repo.agent[AGENT_MCP_ASSIGNMENTS_FIELD] = [
        {"provider": "builtin", "item_id": "mcp_kept"},
        {"provider": "catalog-test", "item_id": "server-stale"},
    ]
    service = AgentExtensionService(
        agent_repo=repo,  # type: ignore[arg-type]
        provider=_ExtensionProvider(),
    )

    await service.set_assignment(
        UserContext("owner"),
        "agent-1",
        mcp_server_ids=["server-1"],
        skill_ids=["skill-1"],
    )

    assert repo.agent[AGENT_MCP_ASSIGNMENTS_FIELD] == [
        {"provider": "builtin", "item_id": "mcp_kept"},
        {"provider": "catalog-test", "item_id": "server-1"},
    ]


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


class _CatalogClient:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def __aenter__(self) -> _CatalogClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        _ = args

    async def get(self, url: str, **kwargs: Any):
        self.calls.append(url)
        assert kwargs["headers"] == {"Authorization": "Bearer service-credential"}
        if url.endswith("/v1/mcp/server"):
            payload: Any = [
                {
                    "server_id": "mcp-1",
                    "server_name": "search",
                    "url": "https://search.example.test/mcp",
                    "transport": "http",
                    "extra_headers": ["apikey"],
                    "approval_status": "active",
                }
            ]
        elif url.endswith("/claude-code/plugins"):
            payload = {
                "plugins": [
                    {
                        "id": "skill-1",
                        "name": "review",
                        "source": {
                            "source": "github",
                            "repo": "example/review-skill",
                        },
                    }
                ]
            }
        else:  # pragma: no cover - catches adapter protocol drift
            raise AssertionError(f"unexpected catalog request: {url}")

        class _Response:
            is_success = True

            def json(self) -> Any:
                return payload

        return _Response()


@pytest.mark.asyncio
async def test_bundled_adapter_owns_catalog_and_runtime_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("ASTRABOX_LITELLM_SERVER_BASE_URL", "http://gateway.test")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "service-credential")
    monkeypatch.setattr(
        adapter.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _CatalogClient(calls),
    )
    provider = adapter.LiteLLMExtensionProvider()

    catalog = await provider.list_catalog(org_id="org-a")
    runtime = provider.materialize(
        mcp_servers=catalog.mcp_servers,
        skills=catalog.skills,
    )

    assert calls == [
        "http://gateway.test/v1/mcp/server",
        "http://gateway.test/claude-code/plugins",
    ]
    assert runtime.mcp_servers[0].url == "http://gateway.test/search/mcp"
    assert (
        runtime.mcp_servers[0].credential_target_url
        == "https://search.example.test/mcp"
    )
    # The runtime binding stays secret-free: the gateway's key is declared for
    # the sandbox's egress vault, not carried here and not put in a header the
    # box would hold.
    assert runtime.mcp_servers[0].headers == {}
    assert runtime.skill_descriptors == ("https://github.com/example/review-skill",)


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
