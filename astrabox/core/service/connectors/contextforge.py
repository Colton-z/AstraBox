"""The connector hub: ContextForge, reached over its REST and MCP APIs.

ContextForge holds each user's OAuth tokens for the vendors, refreshes them,
and forwards MCP calls to the vendor's server with the calling user's token.
AstraBox decides who the caller is: a sandbox reaches a connector only through
the platform's MCP route, which knows the Session behind it, and AstraBox
relays each request to the hub with a session token it signs for that Session's
person (HS256 with the hub's ``JWT_SECRET_KEY``, the claims
``mcpgateway/utils/verify_credentials.py`` checks at v1.0.11). Neither the
token nor the hub's address reaches the sandbox.

Names in the hub follow AstraBox's: a connector's gateway and its virtual
server are both named by the connector id. A user can call a shared server's
tools only as a member of a non-personal team (the hub excludes personal-team
roles from any-team checks), so every user AstraBox sends is a member of one
team, ``astrabox``. The team grants that role and nothing else: servers are
public within the hub and each vendor token belongs to one user.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from astrabox.common.utils.errors import APIError

_ISSUER = "mcpgateway"
_AUDIENCE = "mcpgateway-api"
_MEMBER_TEAM = "astrabox"

API_URL_ENV = "ASTRABOX_CONTEXTFORGE_API_URL"
PUBLIC_URL_ENV = "ASTRABOX_CONTEXTFORGE_PUBLIC_URL"
JWT_SECRET_ENV = "ASTRABOX_CONTEXTFORGE_JWT_SECRET"
ADMIN_EMAIL_ENV = "ASTRABOX_CONTEXTFORGE_ADMIN_EMAIL"


def _hub_password() -> str:
    """A password the hub's policy accepts, which no one ever uses.

    Hub users sign in only through tokens AstraBox signs, but the hub
    requires each to have a password that passes its policy, which refuses a
    run of three ascending or descending letters or digits
    (``password_policy_service._has_sequential_chars`` at v1.0.11).
    """

    alphabet = string.ascii_letters + string.digits + "!#%&*+-=?@^_~"
    chosen = ["A", "a", "7", "!"]
    while len(chosen) < 40:
        candidate = secrets.choice(alphabet)
        a, b = chosen[-2].lower(), chosen[-1].lower()
        step = ord(b) - ord(a)
        if step in (1, -1) and ord(candidate.lower()) - ord(b) == step:
            continue
        chosen.append(candidate)
    return "".join(chosen)


def _hub_error(message: str, *, status_code: int = 502) -> APIError:
    return APIError(code="CONNECTOR_HUB_ERROR", message=message, status_code=status_code)


def hub_from_env() -> ContextForge | None:
    """The configured hub, or ``None`` when the deployment runs none.

    A deployment without a hub has no connectors. A partly configured one is
    refused, because it would leave connectors that look available and fail on
    first use.
    """

    values = {
        API_URL_ENV: str(os.environ.get(API_URL_ENV) or "").strip(),
        PUBLIC_URL_ENV: str(os.environ.get(PUBLIC_URL_ENV) or "").strip(),
        JWT_SECRET_ENV: str(os.environ.get(JWT_SECRET_ENV) or "").strip(),
        ADMIN_EMAIL_ENV: str(os.environ.get(ADMIN_EMAIL_ENV) or "").strip(),
    }
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise APIError(
            code="CONNECTOR_HUB_NOT_CONFIGURED",
            message=f"the connector hub is partly configured; set {', '.join(missing)}",
            status_code=500,
        )
    return ContextForge(
        HubConfig(
            api_url=values[API_URL_ENV],
            public_url=values[PUBLIC_URL_ENV],
            jwt_secret=values[JWT_SECRET_ENV],
            admin_email=values[ADMIN_EMAIL_ENV],
        )
    )


@dataclass(frozen=True)
class HubConfig:
    #: Where the AstraBox server reaches the hub's API and MCP endpoints.
    api_url: str
    #: The hub's own public origin, which the vendor redirects the browser to.
    public_url: str
    jwt_secret: str
    admin_email: str


@dataclass(frozen=True)
class RelayAnswer:
    """The hub's answer to one relayed MCP message."""

    status: int
    #: The JSON-RPC response, or ``None`` for an accepted notification.
    message: dict[str, Any] | None
    #: The hub's MCP session, which the engine sends back on its next request.
    session_id: str | None


class ContextForge:
    def __init__(self, config: HubConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._transport = transport

    @property
    def config(self) -> HubConfig:
        return self._config

    # ── identity ─────────────────────────────────────────────────────────
    def member_email(self, user_id: str) -> str:
        """The hub user that stands for one AstraBox user.

        Derived from the user id rather than taken from the sign-in provider:
        the hub keys users by an email it validates, which rejects the
        reserved domains self-hosted directories often use (``.local``), and
        the hub then never holds a person's real address. The domain is the
        hub administrator's, which the hub already accepted.
        """

        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        domain = self._config.admin_email.rsplit("@", 1)[-1]
        return f"u-{digest}@{domain}"

    def session_token(self, email: str, *, ttl_seconds: int, admin: bool = False) -> str:
        """A hub session token that makes the hub act as ``email``.

        ``token_use: session`` makes the hub resolve the user's teams from its
        own records; without it a token without a ``teams`` claim is limited
        to public resources and cannot execute tools.
        """

        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": email,
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": str(uuid.uuid4()),
            "token_use": "session",
        }
        if admin:
            claims.update({"is_admin": True, "teams": None, "user": {"email": email, "is_admin": True}})
        return jwt.encode(claims, self._config.jwt_secret, algorithm="HS256")

    async def relay(
        self,
        *,
        server_id: str,
        email: str,
        admin: bool,
        message: dict[str, Any],
        session_id: str | None,
        protocol_version: str | None,
    ) -> RelayAnswer:
        """Send one MCP message to a virtual server as ``email``.

        The hub answers a request in JSON or as a server-sent event stream;
        the engine gets the JSON-RPC response either way, so the relay keeps
        no state between requests.
        """

        headers = {
            "Authorization": f"Bearer {self.session_token(email, ttl_seconds=300, admin=admin)}",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        if protocol_version:
            headers["MCP-Protocol-Version"] = protocol_version
        async with httpx.AsyncClient(
            base_url=self._config.api_url.rstrip("/"), timeout=120.0, transport=self._transport,
        ) as client:
            try:
                response = await client.post(
                    f"/servers/{quote(server_id, safe='')}/mcp", json=message, headers=headers,
                )
            except httpx.HTTPError as exc:
                raise _hub_error(f"the connector hub is unreachable: {exc}", status_code=503) from exc
        hub_session = response.headers.get("mcp-session-id") or None
        if response.status_code == 202 or not response.content:
            return RelayAnswer(response.status_code, None, hub_session)
        return RelayAnswer(response.status_code, _rpc_response(response, message.get("id")), hub_session)

    def callback_url(self) -> str:
        """The redirect URI each vendor OAuth app must register."""

        return f"{self._config.public_url.rstrip('/')}/oauth/callback"

    # ── transport ────────────────────────────────────────────────────────
    async def _call(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: Any = None,
        ok: tuple[int, ...] = (200, 201),
        follow_redirects: bool = True,
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self._config.api_url.rstrip("/"),
            timeout=30.0,
            transport=self._transport,
            follow_redirects=follow_redirects,
        ) as client:
            try:
                response = await client.request(
                    method, path, json=body, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                raise _hub_error(f"the connector hub is unreachable: {exc}", status_code=503) from exc
        if response.status_code not in ok:
            raise _hub_error(
                f"the connector hub answered {method} {path} with {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response

    def _admin(self) -> str:
        return self.session_token(self._config.admin_email, ttl_seconds=300, admin=True)

    # ── users and teams ──────────────────────────────────────────────────
    async def ensure_member(self, *, email: str, full_name: str) -> None:
        """Make ``email`` a hub user and a member of the ``astrabox`` team."""

        admin = self._admin()
        users = await self._call(
            "POST",
            "/v1/auth/email/admin/users",
            token=admin,
            body={"email": email, "password": _hub_password(),
                  "full_name": full_name or email, "is_admin": False},
            ok=(201, 409),
        )
        _ = users
        team_id = await self._member_team(admin=admin)
        await self._call(
            "POST",
            f"/v1/teams/{quote(team_id, safe='')}/members",
            token=admin,
            body={"email": email, "role": "member"},
            ok=(201, 409),
        )

    async def _member_team(self, *, admin: str) -> str:
        name = _MEMBER_TEAM
        listed = (await self._call(
            "GET", f"/v1/teams/?search_query={quote(name, safe='')}&limit=50", token=admin
        )).json()
        teams = listed.get("teams") if isinstance(listed, dict) else listed
        for team in teams or []:
            if team.get("name") == name and not team.get("is_personal"):
                return str(team["id"])
        created = await self._call(
            "POST", "/v1/teams/", token=admin,
            body={"name": name, "description": "Users AstraBox signs in", "visibility": "private"},
        )
        return str(created.json()["id"])

    # ── gateways and servers ─────────────────────────────────────────────
    async def list_gateways(self) -> list[dict[str, Any]]:
        listed = (await self._call("GET", "/v1/gateways?limit=0", token=self._admin())).json()
        return [row for row in listed or [] if isinstance(row, dict)]

    async def find_gateway(self, name: str) -> dict[str, Any] | None:
        return next((row for row in await self.list_gateways() if row.get("name") == name), None)

    async def put_gateway(self, *, name: str, url: str, oauth_config: dict[str, Any]) -> str:
        """Create or replace the gateway ``name``; return its id.

        Replacing an authorization-code gateway's OAuth configuration drops
        its discovered tools, so the caller fetches them again afterwards.
        """

        admin = self._admin()
        existing = await self.find_gateway(name)
        if existing is None:
            created = await self._call(
                "POST", "/v1/gateways", token=admin,
                body={"name": name, "url": url, "transport": "STREAMABLEHTTP", "visibility": "public",
                      "auth_type": "oauth", "oauth_config": oauth_config},
            )
            return str(created.json()["id"])
        gateway_id = str(existing["id"])
        await self._call(
            "PUT", f"/v1/gateways/{quote(gateway_id, safe='')}", token=admin,
            body={"url": url, "auth_type": "oauth", "oauth_config": oauth_config},
        )
        return gateway_id

    async def delete_gateway(self, name: str) -> None:
        existing = await self.find_gateway(name)
        if existing is not None:
            await self._call("DELETE", f"/v1/gateways/{quote(str(existing['id']), safe='')}",
                             token=self._admin(), ok=(200, 204))
        server = await self.find_server(name)
        if server is not None:
            await self._call("DELETE", f"/v1/servers/{quote(str(server['id']), safe='')}",
                             token=self._admin(), ok=(200, 204))

    async def publish_tools(self, *, name: str, gateway_id: str, operator_email: str) -> str:
        """Discover the gateway's tools with the operator's token and serve them.

        The hub lists an authorization-code gateway's tools only with a stored
        token, so the operator must have connected their own account first.
        Returns the virtual server's id.
        """

        admin_as_operator = self.session_token(operator_email, ttl_seconds=300, admin=True)
        await self._call(
            "POST", f"/oauth/fetch-tools/{quote(gateway_id, safe='')}", token=admin_as_operator
        )
        admin = self._admin()
        tools = (await self._call(
            "GET", f"/v1/tools?gateway_id={quote(gateway_id, safe='')}&limit=0", token=admin
        )).json()
        tool_ids = [str(tool["id"]) for tool in tools or []]
        server = await self.find_server(name)
        if server is None:
            created = await self._call(
                "POST", "/v1/servers", token=admin,
                body={"server": {"name": name, "associated_tools": tool_ids, "visibility": "public"}},
            )
            return str(created.json()["id"])
        server_id = str(server["id"])
        await self._call("PUT", f"/v1/servers/{quote(server_id, safe='')}", token=admin,
                         body={"associated_tools": tool_ids})
        return server_id

    async def list_servers(self) -> list[dict[str, Any]]:
        listed = (await self._call("GET", "/v1/servers?limit=0", token=self._admin())).json()
        return [row for row in listed or [] if isinstance(row, dict)]

    async def find_server(self, name: str) -> dict[str, Any] | None:
        return next((row for row in await self.list_servers() if row.get("name") == name), None)

    # ── one user's consent ───────────────────────────────────────────────
    async def oauth_callback(self, query: str) -> httpx.Response:
        """Hand the vendor's browser redirect to the hub, which finishes the grant.

        The hub binds the consent to its ``state`` and needs no sign-in here;
        its answer is a redirect back to the console, returned as it is.
        """

        async with httpx.AsyncClient(
            base_url=self._config.api_url.rstrip("/"), timeout=30.0, transport=self._transport
        ) as client:
            try:
                return await client.get(f"/oauth/callback?{query}")
            except httpx.HTTPError as exc:
                raise _hub_error(f"the connector hub is unreachable: {exc}", status_code=503) from exc

    async def authorize_url(self, *, gateway_id: str, email: str) -> str:
        response = await self._call(
            "GET", f"/oauth/authorize/{quote(gateway_id, safe='')}",
            token=self.session_token(email, ttl_seconds=300),
            ok=(302, 303, 307), follow_redirects=False,
        )
        location = response.headers.get("location") or ""
        if not location:
            raise _hub_error("the connector hub started no authorization")
        return location

    async def token_status(self, *, gateway_id: str, email: str) -> dict[str, Any]:
        payload = (await self._call(
            "GET", f"/oauth/status/{quote(gateway_id, safe='')}",
            token=self.session_token(email, ttl_seconds=300),
        )).json()
        status = payload.get("user_token_status") if isinstance(payload, dict) else None
        return dict(status) if isinstance(status, dict) else {"authorized": False, "status": "missing"}

    async def revoke(self, *, gateway_id: str, email: str) -> bool:
        payload = (await self._call(
            "DELETE", f"/oauth/token/{quote(gateway_id, safe='')}",
            token=self.session_token(email, ttl_seconds=300),
        )).json()
        return bool(payload.get("revoked")) if isinstance(payload, dict) else False


def _rpc_response(response: httpx.Response, request_id: Any) -> dict[str, Any]:
    """The JSON-RPC message answering ``request_id`` in a JSON or event-stream body."""

    if "text/event-stream" not in response.headers.get("content-type", ""):
        payload = response.json()
        if not isinstance(payload, dict):
            raise _hub_error(f"the connector hub answered with a non-object body ({response.status_code})")
        return payload
    messages = [
        json.loads(line[5:])
        for line in response.text.splitlines()
        if line.startswith("data:") and line[5:].strip()
    ]
    for candidate in reversed(messages):
        if isinstance(candidate, dict) and candidate.get("id") == request_id:
            return candidate
    raise _hub_error(f"the connector hub's event stream held no answer to request {request_id!r}")
