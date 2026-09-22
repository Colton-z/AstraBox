"""LiteLLM extension catalog, MCP egress, and browser-management adapter."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
import jwt
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.extensions import register_extension_router
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    PLATFORM_ADMIN_ROLE,
    UserContext,
    default_org_id,
)
from astrabox.core.service.orchestrator.agent_extension_service import (
    AgentExtensionService,
)
from astrabox.identity.session_signing import session_signing_secret
from astrabox.providers.litellm_shared_auth import (
    ADMIN_UI_PURPOSE,
    AGENT_CONSOLE_PURPOSE,
    mint_litellm_capability,
    verify_litellm_capability,
)
from astrabox.providers.model import LiteLLMModelEndpointProvider
from astrabox.seams.extensions import (
    ExtensionCatalog,
    ExtensionCatalogItem,
    ExtensionProvider,
    ExtensionRuntimeSelection,
    MCPGatewayCredential,
    RuntimeMCPServer,
    register_extension_provider,
)

logger = get_logger(__name__)

EXTENSION_GRANT_COOKIE = "astrabox_extension_grant"
EXTENSION_UI_COOKIE = "astrabox_extension_ui"
ADMIN_UI_COOKIE = "astrabox_litellm_admin_ui"
LITELLM_UI_COOKIE = "token"

_DEFAULT_TTL_SECONDS = 15 * 60
_SECTIONS = {"mcp", "skills"}
_UI_PATHS = {"mcp-servers", "skills"}
_UPSTREAM_RESPONSE_HEADERS = {
    "cache-control",
    "content-language",
    "content-type",
    "etag",
    "last-modified",
}

# These are the complete management operations available to an Agent manager.
# The proxy adapter also receives this list through the signed capability and
# applies it before its role checks.
AGENT_MANAGEMENT_ROUTES = (
    "/claude-code/plugins",
    "/claude-code/plugins/*",
    "/claude-code/marketplace.json",
    "/public/skill_hub",
    "/v1/mcp/server",
    "/v1/mcp/server/*",
    "/v1/mcp/discover",
    "/v1/mcp/toolset",
    "/v1/mcp/toolset/*",
    "/v1/mcp/network/*",
    "/v1/mcp/user-env-vars/*",
    "/mcp-rest/tools/list",
    "/mcp-rest/tools/call",
    "/get/mcp_semantic_filter_settings",
    "/model_group/info",
)

_SHELL_STYLE = """
<style id="astrabox-extension-shell">
  header, aside.ant-layout-sider { display: none !important; }
  .ant-layout-has-sider { display: block !important; }
  main.flex-1 { min-height: 100vh !important; width: 100% !important; }
  main.flex-1 > div { margin: 0 !important; padding: 24px !important; }
  [role="tablist"] [role="tab"]:not(:first-child) { display: none !important; }
</style>
"""


def _failure(code: str, message: str, status: int) -> APIError:
    return APIError(code=code, message=message, status_code=status)


def _provider_base_url() -> str:
    """Return this provider family's canonical server-side address."""

    return LiteLLMModelEndpointProvider.server_side_base_url().rstrip("/")


def _service_credential() -> str:
    """Return the credential of the configured provider service."""

    return str(os.getenv("LITELLM_MASTER_KEY") or "").strip()


def _git_url(raw: Any) -> str:
    value = str(raw or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _skill_descriptor(item: ExtensionCatalogItem) -> str:
    source_value = item.provider_data.get("source")
    source = source_value if isinstance(source_value, dict) else {}
    source_kind = str(source.get("source") or "").strip().lower()
    repo = ""
    if source_kind == "github":
        github_repo = str(source.get("repo") or "").strip().strip("/")
        if github_repo and "/" in github_repo:
            repo = f"https://github.com/{github_repo}"
    elif source_kind in {"git-subdir", "url"}:
        repo = _git_url(source.get("url"))
    if not repo:
        return ""
    ref = str(
        source.get("ref") or source.get("branch") or source.get("revision") or ""
    ).strip()
    path = str(source.get("path") or "").strip().strip("/")
    if "@" in ref or "#" in ref or "#" in path:
        return ""
    descriptor = repo
    if ref:
        descriptor += f"@{ref}"
    if path:
        descriptor += f"#{path}"
    return descriptor


class LiteLLMExtensionProvider(ExtensionProvider):
    """Normalize LiteLLM's catalog and authorize its MCP gateway hop."""

    name = "litellm"
    is_default = True

    async def list_catalog(self, *, org_id: str) -> ExtensionCatalog:
        # The gateway's catalog is one list per deployment, not per tenant, so
        # every org is offered the same entries. Tenant separation for these
        # servers is the Agent assignment, not the catalog.
        _ = org_id
        key = _service_credential()
        if not key:
            raise _failure(
                "EXTENSION_PROVIDER_UNAVAILABLE",
                "extension management service credential is not configured",
                503,
            )
        headers = {"Authorization": f"Bearer {key}"}
        base = _provider_base_url()
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                mcp_response, skill_response = await asyncio.gather(
                    client.get(base + "/v1/mcp/server", headers=headers),
                    client.get(base + "/claude-code/plugins", headers=headers),
                )
        except httpx.HTTPError as exc:
            raise _failure(
                "EXTENSION_PROVIDER_UNAVAILABLE",
                "extension catalog is temporarily unavailable",
                503,
            ) from exc
        if not mcp_response.is_success or not skill_response.is_success:
            raise _failure(
                "EXTENSION_PROVIDER_UNAVAILABLE",
                "extension catalog is not configured correctly",
                503,
            )
        try:
            raw_mcp = mcp_response.json()
            raw_skills = skill_response.json()
        except ValueError as exc:
            raise _failure(
                "EXTENSION_PROVIDER_UNAVAILABLE",
                "extension catalog returned invalid data",
                503,
            ) from exc

        mcp_items = raw_mcp if isinstance(raw_mcp, list) else []
        skill_items = raw_skills.get("plugins") if isinstance(raw_skills, dict) else []
        return ExtensionCatalog(
            mcp_servers=tuple(
                ExtensionCatalogItem(
                    item_id=str(item.get("server_id") or "").strip(),
                    name=str(item.get("server_name") or "").strip(),
                    description=str(item.get("description") or "").strip() or None,
                    transport=str(item.get("transport") or "").strip() or None,
                    provider_data=dict(item),
                )
                for item in mcp_items
                if isinstance(item, dict)
                and str(item.get("server_id") or "").strip()
                and str(item.get("server_name") or "").strip()
                and str(item.get("approval_status") or "active").strip().lower()
                == "active"
            ),
            skills=tuple(
                ExtensionCatalogItem(
                    item_id=str(item.get("id") or "").strip(),
                    name=str(item.get("name") or "").strip(),
                    description=str(item.get("description") or "").strip() or None,
                    provider_data={"source": dict(item.get("source") or {})},
                )
                for item in (skill_items if isinstance(skill_items, list) else [])
                if isinstance(item, dict)
                and str(item.get("id") or "").strip()
                and str(item.get("name") or "").strip()
                and item.get("enabled") is not False
            ),
        )

    def materialize(
        self,
        *,
        mcp_servers: tuple[ExtensionCatalogItem, ...],
        skills: tuple[ExtensionCatalogItem, ...],
    ) -> ExtensionRuntimeSelection:
        runtime_skills: list[str] = []
        for skill in skills:
            descriptor = _skill_descriptor(skill)
            if not descriptor:
                raise _failure(
                    "SKILL_SOURCE_UNSUPPORTED",
                    f"Skill '{skill.name}' has no supported Git source",
                    400,
                )
            runtime_skills.append(descriptor)
        base = _provider_base_url()
        return ExtensionRuntimeSelection(
            mcp_servers=tuple(
                RuntimeMCPServer(
                    name=item.name,
                    transport="streamable_http",
                    url=f"{base}/{quote(item.name, safe='')}/mcp",
                    credential_target_url=(
                        str(item.provider_data.get("url") or "").strip() or None
                    ),
                )
                for item in mcp_servers
            ),
            skill_descriptors=tuple(runtime_skills),
        )

    def mcp_gateway_credential(self) -> MCPGatewayCredential | None:
        """The service key the sidecar swaps in for every call to this gateway.

        One credential covers the whole gateway rather than one per server: the
        key authorizes at the gateway, and which servers it may reach is the
        gateway's own per-key scoping, not something a binding here could
        narrow.
        """

        key = _service_credential()
        if not key:
            raise RuntimeError("LiteLLM MCP service credential is not configured")
        return MCPGatewayCredential(
            header="Authorization",
            value=key,
            base_url=_provider_base_url(),
        )


@dataclass(frozen=True)
class _BrowserSession:
    grant_token: str
    ui_token: str
    open_url: str
    expires_in: int


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ui_wrapper(
    user: UserContext,
    capability: str,
    ttl: int,
    *,
    server_root_path: str,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "use": "litellm-ui-wrapper",
            "user_id": user.user_id,
            "key": capability,
            "user_email": str(user.email or ""),
            "user_role": "proxy_admin",
            "login_method": "astrabox",
            "premium_user": False,
            "auth_header_name": "Authorization",
            "server_root_path": server_root_path,
            "iat": now,
            "exp": now + ttl,
        },
        session_signing_secret(),
        algorithm="HS256",
    )


class _ManagementSessionService:
    """Create and verify the browser sessions required by LiteLLM's UI."""

    def create_agent_session(
        self,
        user: UserContext,
        agent_id: str,
        section: str,
    ) -> _BrowserSession:
        normalized_section = str(section or "").strip().lower()
        if normalized_section not in _SECTIONS:
            raise _failure(
                "INVALID_EXTENSION_SECTION",
                "section must be 'mcp' or 'skills'",
                400,
            )
        ttl = _DEFAULT_TTL_SECONDS
        capability = mint_litellm_capability(
            secret=session_signing_secret(),
            purpose=AGENT_CONSOLE_PURPOSE,
            subject=user.user_id,
            email=user.email,
            display_name=user.display_name,
            org_id=user.org_id,
            roles=user.roles,
            ttl_seconds=ttl,
            extra_claims={
                "agent_id": agent_id,
                "section": normalized_section,
                "allowed_routes": list(AGENT_MANAGEMENT_ROUTES),
            },
        )
        now = int(time.time())
        grant = jwt.encode(
            {
                "use": "extension-console",
                "sub": user.user_id,
                "org_id": str(user.org_id or default_org_id()),
                "roles": list(user.roles),
                "agent_id": agent_id,
                "section": normalized_section,
                "capability_sha256": _sha256(capability),
                "iat": now,
                "exp": now + ttl,
            },
            session_signing_secret(),
            algorithm="HS256",
        )
        return _BrowserSession(
            grant_token=grant,
            ui_token=_ui_wrapper(user, capability, ttl, server_root_path=""),
            open_url="/extension-console/open",
            expires_in=ttl,
        )

    def create_admin_session(self, user: UserContext) -> _BrowserSession:
        if PLATFORM_ADMIN_ROLE not in user.roles:
            raise _failure("ADMIN_ROLE_REQUIRED", "administrator role required", 403)
        ttl = _DEFAULT_TTL_SECONDS
        capability = mint_litellm_capability(
            secret=session_signing_secret(),
            purpose=ADMIN_UI_PURPOSE,
            subject=user.user_id,
            email=user.email,
            display_name=user.display_name,
            org_id=user.org_id,
            roles=user.roles,
            ttl_seconds=ttl,
        )
        return _BrowserSession(
            grant_token="",
            ui_token=_ui_wrapper(
                user,
                capability,
                ttl,
                server_root_path="/litellm",
            ),
            open_url="/litellm/ui/",
            expires_in=ttl,
        )

    def authorize_grant(
        self,
        token: str,
        *,
        authorization: str | None = None,
        require_capability: bool = False,
    ) -> dict[str, Any]:
        claims = self._decode(token, expected_use="extension-console")
        if require_capability:
            supplied = str(authorization or "").strip()
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:].strip()
            expected = str(claims.get("capability_sha256") or "")
            if (
                not supplied
                or not expected
                or not hmac.compare_digest(_sha256(supplied), expected)
            ):
                raise _failure(
                    "EXTENSION_CONSOLE_CAPABILITY_REQUIRED",
                    "extension console capability required",
                    403,
                )
        return claims

    def verify_agent_ui_token(
        self,
        token: str,
        grant: dict[str, Any],
    ) -> dict[str, Any]:
        claims = self._decode(token, expected_use="litellm-ui-wrapper")
        capability = str(claims.get("key") or "")
        expected = str(grant.get("capability_sha256") or "")
        if (
            not capability
            or not expected
            or not hmac.compare_digest(_sha256(capability), expected)
        ):
            raise _failure(
                "EXTENSION_CONSOLE_SESSION_INVALID",
                "extension console session is invalid",
                403,
            )
        try:
            verify_litellm_capability(
                capability,
                secret=session_signing_secret(),
                purposes=(AGENT_CONSOLE_PURPOSE,),
            )
        except jwt.PyJWTError as exc:
            raise _failure(
                "EXTENSION_CONSOLE_SESSION_INVALID",
                "extension console session is invalid or expired",
                403,
            ) from exc
        return claims

    def verify_admin_ui_token(
        self,
        token: str,
        user: UserContext,
    ) -> dict[str, Any]:
        claims = self._decode(token, expected_use="litellm-ui-wrapper")
        capability = str(claims.get("key") or "")
        try:
            identity = verify_litellm_capability(
                capability,
                secret=session_signing_secret(),
                purposes=(ADMIN_UI_PURPOSE,),
            )
        except jwt.PyJWTError as exc:
            raise _failure(
                "EXTENSION_ADMIN_SESSION_INVALID",
                "extension administration session is invalid or expired",
                401,
            ) from exc
        if (
            identity.get("sub") != user.user_id
            or PLATFORM_ADMIN_ROLE not in user.roles
        ):
            raise _failure("ADMIN_ROLE_REQUIRED", "administrator role required", 403)
        return claims

    async def proxy(
        self,
        request: Request,
        upstream_path: str,
        *,
        ui_token: str | None = None,
    ) -> httpx.Response:
        path = "/" + str(upstream_path or "").lstrip("/")
        query = bytes(request.scope.get("query_string") or b"").decode("latin-1")
        url = urljoin(_provider_base_url() + "/", path.lstrip("/"))
        if query:
            url += "?" + query
        headers: dict[str, str] = {"Accept-Encoding": "identity"}
        for name in (
            "accept",
            "content-type",
            "authorization",
            "user-agent",
            "x-litellm-api-key",
        ):
            value = str(request.headers.get(name) or "").strip()
            if value:
                headers[name] = value
        if ui_token:
            headers["cookie"] = f"{LITELLM_UI_COOKIE}={ui_token}"
        body = await request.body()
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                return await client.request(
                    request.method,
                    url,
                    headers=headers,
                    content=body if body else None,
                )
        except httpx.HTTPError as exc:
            logger.warning("LiteLLM management request failed: %s", exc)
            raise _failure(
                "EXTENSION_PROVIDER_UNAVAILABLE",
                "extension management is temporarily unavailable",
                503,
            ) from exc

    @staticmethod
    def _decode(token: str, *, expected_use: str) -> dict[str, Any]:
        if not str(token or "").strip():
            raise _failure(
                "EXTENSION_CONSOLE_SESSION_REQUIRED",
                "open extension management from an Agent",
                403,
            )
        try:
            claims = jwt.decode(
                token,
                session_signing_secret(),
                algorithms=["HS256"],
            )
        except jwt.PyJWTError as exc:
            raise _failure(
                "EXTENSION_CONSOLE_SESSION_INVALID",
                "extension console session is invalid or expired",
                403,
            ) from exc
        if claims.get("use") != expected_use:
            raise _failure(
                "EXTENSION_CONSOLE_SESSION_INVALID",
                "extension console session has the wrong purpose",
                403,
            )
        return claims


class _CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Literal["mcp", "skills"] = "mcp"


class ExtensionConsoleSession(BaseModel):
    """Where to send the browser, and how long the grant lasts.

    The session itself lives in the two cookies the route sets; the body carries
    no token, so a client that logs or forwards it discloses nothing. The
    obligations that come with declaring a response model are in
    :mod:`astrabox.api.routes.response_envelope`.
    """

    model_config = ConfigDict(extra="allow")

    open_url: str
    expires_in: int


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https"


def _response_from_upstream(
    upstream: httpx.Response,
    *,
    inject_shell: bool = False,
) -> Response:
    content = bytes(upstream.content or b"")
    content_type = str(upstream.headers.get("content-type") or "")
    if inject_shell and "text/html" in content_type.lower():
        html = content.decode("utf-8", errors="replace")
        html = (
            html.replace("</head>", _SHELL_STYLE + "</head>", 1)
            if "</head>" in html
            else _SHELL_STYLE + html
        )
        content = html.encode("utf-8")
    response = Response(content=content, status_code=int(upstream.status_code))
    for name in _UPSTREAM_RESPONSE_HEADERS:
        value = upstream.headers.get(name)
        if value:
            response.headers[name] = value
    return response


_registered_on: int | None = None


def register_litellm_extension_routes(app: Any) -> None:
    """Register LiteLLM's management adapter through the API-router extension."""

    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    provider = _PROVIDER
    agent_extensions = AgentExtensionService(provider=provider)
    sessions = _ManagementSessionService()

    async def _grant(
        request: Request,
        *,
        require_capability: bool,
        recheck_agent: bool = True,
    ) -> dict[str, Any]:
        claims = sessions.authorize_grant(
            str(request.cookies.get(EXTENSION_GRANT_COOKIE) or ""),
            authorization=request.headers.get("authorization"),
            require_capability=require_capability,
        )
        if recheck_agent:
            user = UserContext(
                user_id=str(claims.get("sub") or ""),
                org_id=str(claims.get("org_id") or default_org_id()),
                roles=[str(role) for role in claims.get("roles") or []],
            )
            await agent_extensions.assert_can_manage(
                user,
                str(claims.get("agent_id") or ""),
            )
        return claims

    # The handler answers a JSONResponse because the session is carried by the
    # cookies it sets, and a Response object bypasses ``response_model``: the
    # declaration below documents the body without validating it, so it has to
    # be read against ``success_response(...)`` at the return statement.
    @app.post(
        "/api/v1/agents/{agent_id}/extension-console/session",
        response_model=ApiEnvelope[ExtensionConsoleSession],
        response_model_exclude_unset=True,
    )
    async def create_extension_console_session(
        agent_id: str,
        request: Request,
        body: _CreateSessionRequest,
    ) -> JSONResponse:
        from astrabox.common.utils.user_context import get_current_user_context

        user = await get_current_user_context(request)
        agent = await agent_extensions.assert_can_manage(user, agent_id)
        normalized_agent_id = str(agent.get("agent_id") or agent_id)
        session = sessions.create_agent_session(
            user,
            normalized_agent_id,
            body.section,
        )
        response = JSONResponse(
            success_response(
                {"open_url": session.open_url, "expires_in": session.expires_in}
            )
        )
        response.set_cookie(
            EXTENSION_GRANT_COOKIE,
            session.grant_token,
            max_age=session.expires_in,
            httponly=True,
            secure=_secure_cookie(request),
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            EXTENSION_UI_COOKIE,
            session.ui_token,
            max_age=session.expires_in,
            httponly=True,
            secure=_secure_cookie(request),
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/extension-console/open", include_in_schema=False)
    async def open_extension_console(request: Request) -> HTMLResponse:
        grant = await _grant(request, require_capability=False)
        ui_token = str(request.cookies.get(EXTENSION_UI_COOKIE) or "")
        sessions.verify_agent_ui_token(ui_token, grant)
        target = (
            "/ui/skills/"
            if str(grant.get("section") or "mcp") == "skills"
            else "/ui/mcp-servers/"
        )
        nonce = secrets.token_urlsafe(18)
        token_json = json.dumps(ui_token).replace("</", "<\\/")
        target_json = json.dumps(target)
        html = f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><meta name="robots" content="noindex"><title>Extension management</title></head>
  <body>
    <script nonce="{nonce}">
      sessionStorage.setItem("token", {token_json});
      window.location.replace({target_json});
    </script>
  </body>
</html>"""
        response = HTMLResponse(html)
        max_age = max(1, int(grant["exp"]) - int(grant["iat"]))
        response.set_cookie(
            EXTENSION_UI_COOKIE,
            ui_token,
            max_age=max_age,
            httponly=True,
            secure=_secure_cookie(request),
            samesite="lax",
            path="/ui",
        )
        response.delete_cookie(EXTENSION_UI_COOKIE, path="/")
        response.set_cookie(
            LITELLM_UI_COOKIE,
            ui_token,
            max_age=max_age,
            httponly=False,
            secure=_secure_cookie(request),
            samesite="lax",
            path="/ui",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'"
        )
        return response

    @app.api_route("/ui/{ui_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def extension_console_ui(ui_path: str, request: Request) -> Response:
        first = str(ui_path or "").strip("/").split("/", 1)[0]
        if first == "assets":
            await _grant(request, require_capability=False, recheck_agent=False)
            upstream = await sessions.proxy(request, f"/ui/{ui_path}")
            return _response_from_upstream(upstream)
        if first not in _UI_PATHS:
            return Response(status_code=403)
        grant = await _grant(request, require_capability=False)
        ui_token = str(request.cookies.get(EXTENSION_UI_COOKIE) or "")
        sessions.verify_agent_ui_token(ui_token, grant)
        upstream = await sessions.proxy(request, f"/ui/{ui_path}", ui_token=ui_token)
        return _response_from_upstream(upstream, inject_shell=True)

    @app.api_route("/_next/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def extension_console_asset(asset_path: str, request: Request) -> Response:
        await _grant(request, require_capability=False, recheck_agent=False)
        upstream = await sessions.proxy(request, f"/_next/{asset_path}")
        return _response_from_upstream(upstream)

    @app.api_route(
        "/litellm-asset-prefix/_next/{asset_path:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def extension_console_prefixed_asset(
        asset_path: str,
        request: Request,
    ) -> Response:
        await _grant(request, require_capability=False, recheck_agent=False)
        upstream = await sessions.proxy(
            request,
            f"/litellm-asset-prefix/_next/{asset_path}",
        )
        return _response_from_upstream(upstream)

    async def _extension_api(request: Request, upstream_path: str) -> Response:
        await _grant(request, require_capability=True)
        upstream = await sessions.proxy(request, upstream_path)
        return _response_from_upstream(upstream)

    @app.api_route(
        "/claude-code/{extension_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def extension_console_claude_code(
        extension_path: str,
        request: Request,
    ) -> Response:
        return await _extension_api(request, f"/claude-code/{extension_path}")

    @app.get("/public/skill_hub", include_in_schema=False)
    async def extension_console_skill_hub(request: Request) -> Response:
        return await _extension_api(request, "/public/skill_hub")

    @app.api_route(
        "/v1/mcp/{extension_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def extension_console_mcp(
        extension_path: str,
        request: Request,
    ) -> Response:
        if str(extension_path or "").strip("/").startswith("server/submissions"):
            return Response(status_code=403)
        return await _extension_api(request, f"/v1/mcp/{extension_path}")

    @app.api_route(
        "/mcp-rest/{extension_path:path}",
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    async def extension_console_mcp_runtime(
        extension_path: str,
        request: Request,
    ) -> Response:
        path = str(extension_path or "").strip("/")
        if path not in {"tools/list", "tools/call"}:
            return Response(status_code=403)
        return await _extension_api(request, f"/mcp-rest/{path}")

    # Pages served through the embedded shell can address any gateway route;
    # only the management surface above is exposed. Key minting and inference
    # are refused by policy — without these routes a probe falls through to
    # the SPA catch-all and is refused by accident (405 on POST, index.html
    # on GET). ``/v1/mcp/*`` stays reachable: it is registered first and
    # Starlette matches in registration order.
    @app.api_route(
        "/key/{blocked_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    @app.api_route(
        "/v1/{blocked_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def extension_console_gateway_refused(
        blocked_path: str,
        request: Request,
    ) -> Response:
        return Response(status_code=403)

    @app.get("/get/mcp_semantic_filter_settings", include_in_schema=False)
    async def extension_console_mcp_semantic_filter(request: Request) -> Response:
        return await _extension_api(request, "/get/mcp_semantic_filter_settings")

    @app.get("/model_group/info", include_in_schema=False)
    async def extension_console_model_group_info(request: Request) -> Response:
        return await _extension_api(request, "/model_group/info")

    async def _direct_admin_user(request: Request) -> UserContext | RedirectResponse:
        resolver = getattr(request.app.state, "web_identity_resolver", None)
        if resolver is None:
            raise _failure(
                "AUTH_REQUIRED",
                "web identity resolver is unavailable",
                401,
            )
        headers = {str(name).lower(): str(value) for name, value in request.headers.items()}
        try:
            user = await resolver.resolve(headers)
        except APIError as exc:
            login_url = resolver.login_url(str(request.url.path))
            if exc.status_code == 401 and login_url:
                return RedirectResponse(login_url, status_code=302)
            raise
        if user is None:
            raise _failure("AUTH_REQUIRED", "sign-in required", 401)
        if PLATFORM_ADMIN_ROLE not in user.roles:
            raise _failure("ADMIN_ROLE_REQUIRED", "administrator role required", 403)
        return user

    async def _admin_proxy(upstream_path: str, request: Request) -> Response:
        identity = await _direct_admin_user(request)
        if isinstance(identity, RedirectResponse):
            return identity
        ui_token = str(request.cookies.get(ADMIN_UI_COOKIE) or "")
        try:
            sessions.verify_admin_ui_token(ui_token, identity)
            expires_in = _DEFAULT_TTL_SECONDS
        except APIError:
            session = sessions.create_admin_session(identity)
            ui_token = session.ui_token
            expires_in = session.expires_in
        upstream = await sessions.proxy(
            request,
            "/" + str(upstream_path or "").lstrip("/"),
            ui_token=ui_token,
        )
        response = _response_from_upstream(upstream)
        response.set_cookie(
            ADMIN_UI_COOKIE,
            ui_token,
            max_age=expires_in,
            httponly=True,
            secure=_secure_cookie(request),
            samesite="lax",
            path="/litellm",
        )
        response.set_cookie(
            LITELLM_UI_COOKIE,
            ui_token,
            max_age=expires_in,
            httponly=False,
            secure=_secure_cookie(request),
            samesite="lax",
            path="/litellm",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.api_route(
        "/litellm",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def litellm_admin_root(request: Request) -> Response:
        return await _admin_proxy("/ui/", request)

    @app.api_route(
        "/litellm/{upstream_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def litellm_admin_proxy(upstream_path: str, request: Request) -> Response:
        return await _admin_proxy(upstream_path, request)


_PROVIDER = LiteLLMExtensionProvider()
register_extension_provider(_PROVIDER)
register_extension_router("litellm_extensions", register_litellm_extension_routes)


__all__ = [
    "AGENT_MANAGEMENT_ROUTES",
    "LiteLLMExtensionProvider",
    "register_litellm_extension_routes",
]
