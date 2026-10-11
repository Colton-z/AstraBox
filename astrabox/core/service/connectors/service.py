"""People's connections to work apps, and the connectors Agents and Assistants offer.

A connection is a person's consent at the vendor, held by the connector hub
(:mod:`astrabox.core.service.connectors.contextforge`): the hub runs the OAuth
grant, stores and refreshes the person's token, and calls the vendor's server
with it. Each AstraBox user is a hub user derived from their user id.

An Agent or Assistant offers a connector as a platform MCP server: its engine
reaches the connector only through the platform's MCP route, which relays each
request to the hub as the Session's person (an Assistant's owner, or whoever
started the Agent conversation). Neither the vendor's token nor the hub's
enters the sandbox, and Sessions sharing one box each act as their own person.

An administrator sets a connector up once: enters the deployment's OAuth app
where the vendor needs one, then connects their own account so the hub can
list the server's tools (it lists an authorization-code server's tools only
with a stored token), and publishes them.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.runtime.mcp_servers import CONNECTIONS_PLATFORM_SERVER
from astrabox.core.service.connectors.contextforge import ContextForge, hub_from_env
from astrabox.core.service.connectors.oauth import discover, require_redirect_uri
from astrabox.core.service.connectors.presets import (
    CONNECTOR_PRESETS,
    ConnectorPreset,
    connector_preset,
)
from astrabox.deploy.public_path import public_path

logger = get_logger(__name__)

#: Lifetime of the hub session token an Assistant Session carries. Longer than
#: the expiration watcher's default interval, which re-resolves an Assistant's
#: credentials between turns, so a token is replaced before it lapses.


def _not_found(connector_id: str) -> APIError:
    return APIError(
        code="CONNECTOR_NOT_FOUND", message=f"connector {connector_id!r} not found", status_code=404
    )


def _not_available(preset: ConnectorPreset) -> APIError:
    return APIError(
        code="CONNECTOR_NOT_AVAILABLE",
        message=f"{preset.name} has not been set up on this deployment",
        status_code=409,
    )


class ConnectorService:
    def __init__(self, *, hub: ContextForge | None = None) -> None:
        self._hub_override = hub

    def _hub_or_none(self) -> ContextForge | None:
        return self._hub_override if self._hub_override is not None else hub_from_env()

    def _hub(self) -> ContextForge:
        hub = self._hub_or_none()
        if hub is None:
            raise APIError(
                code="CONNECTOR_HUB_NOT_CONFIGURED",
                message="this deployment runs no connector hub",
                status_code=409,
            )
        return hub

    @staticmethod
    def _preset(connector_id: str) -> ConnectorPreset:
        preset = connector_preset(connector_id)
        if preset is None:
            raise _not_found(connector_id)
        return preset

    @staticmethod
    def _describe(preset: ConnectorPreset) -> dict[str, Any]:
        return {
            "connector_id": preset.connector_id,
            "name": preset.name,
            "registration": preset.registration,
            "docs_url": preset.docs_url or None,
        }

    # ── catalog ──────────────────────────────────────────────────────────
    async def list_for_user(self, user: UserContext) -> list[dict[str, Any]]:
        """Every connector, with whether it is set up and this user is connected."""

        hub = self._hub_or_none()
        servers: dict[str, dict[str, Any]] = {}
        gateways: dict[str, dict[str, Any]] = {}
        if hub is not None:
            servers = {str(row.get("name")): row for row in await hub.list_servers()}
            gateways = {str(row.get("name")): row for row in await hub.list_gateways()}
        email = ""
        if hub is not None and any(name in servers for name in gateways):
            email = await hub_member(hub, user.user_id, user.display_name)
        rows = []
        for preset in CONNECTOR_PRESETS:
            gateway = gateways.get(preset.connector_id)
            available = gateway is not None and preset.connector_id in servers
            status = "disconnected"
            if hub is not None and available and email:
                token = await hub.token_status(gateway_id=str(gateway["id"]), email=email)
                if token.get("authorized"):
                    status = "connected"
                elif token.get("status") == "expired":
                    status = "needs_reauth"
            rows.append({**self._describe(preset), "available": available, "status": status})
        return rows

    async def list_for_admin(self) -> list[dict[str, Any]]:
        hub = self._hub_or_none()
        servers: dict[str, dict[str, Any]] = {}
        gateways: dict[str, dict[str, Any]] = {}
        if hub is not None:
            servers = {str(row.get("name")): row for row in await hub.list_servers()}
            gateways = {str(row.get("name")): row for row in await hub.list_gateways()}
        rows = []
        for preset in CONNECTOR_PRESETS:
            rows.append(
                {
                    **self._describe(preset),
                    # The deployment runs the server this connector forwards to.
                    "deployed": bool(preset.upstream_url()),
                    "configured": preset.connector_id in gateways,
                    "published": preset.connector_id in servers,
                    "redirect_uri": hub.callback_url() if hub is not None else None,
                }
            )
        return rows

    # ── administrator set-up ─────────────────────────────────────────────
    async def configure(
        self, connector_id: str, *, client_id: str | None = None, client_secret: str | None = None
    ) -> dict[str, Any]:
        """Register the connector's server with the hub, with how to authorize."""

        preset = self._preset(connector_id)
        hub = self._hub()
        upstream = preset.upstream_url()
        if not upstream:
            raise APIError(
                code="CONNECTOR_SERVER_NOT_DEPLOYED",
                message=f"{preset.name} needs its server deployed; set {preset.url_env}",
                status_code=409,
            )
        callback = hub.callback_url()
        # Vendors register only HTTPS or loopback redirect URIs.
        require_redirect_uri(callback)
        oauth_config = await self._oauth_config(
            preset, callback=callback, client_id=client_id, client_secret=client_secret
        )
        await hub.put_gateway(name=preset.connector_id, url=upstream, oauth_config=oauth_config)
        # Replacing a gateway's OAuth configuration drops its tools until they
        # are published again.
        return {**self._describe(preset), "configured": True, "published": False}

    async def _oauth_config(
        self,
        preset: ConnectorPreset,
        *,
        callback: str,
        client_id: str | None,
        client_secret: str | None,
    ) -> dict[str, Any]:
        hub = self._hub()
        config: dict[str, Any] = {
            "grant_type": "authorization_code",
            "redirect_uri": callback,
            # Back to the person's connections once the vendor answers.
            "redirect_uri_after_oauth": (
                f"{_origin(hub.config.public_url)}{public_path('/connections')}"
                f"?connected={preset.connector_id}"
            ),
        }
        if preset.registration == "operator":
            client_id = str(client_id or "").strip()
            if not client_id:
                raise APIError(
                    code="INVALID_REQUEST",
                    message=f"{preset.name} needs this deployment's OAuth app: client_id is required",
                    status_code=400,
                )
            config["client_id"] = client_id
            secret = str(client_secret or "").strip()
            if secret:
                config["client_secret"] = secret
            if preset.authorization_endpoint:
                config["authorization_url"] = preset.authorization_endpoint
                config["token_url"] = preset.token_endpoint
                scopes = list(preset.scopes)
            else:
                server = await discover(preset.url)
                config["authorization_url"] = server.authorization_endpoint
                config["token_url"] = server.token_endpoint
                scopes = list(preset.scopes or server.resource_scopes)
        else:
            if client_id:
                raise APIError(
                    code="INVALID_REQUEST",
                    message=f"{preset.name} registers its own client; no client_id is entered",
                    status_code=400,
                )
            server = await discover(preset.url)
            config["issuer"] = server.issuer
            scopes = list(preset.scopes or server.resource_scopes)
        if scopes:
            config["scopes"] = scopes
        if preset.authorize_params:
            config["extra_auth_params"] = dict(preset.authorize_params)
        if preset.omit_resource:
            config["omit_resource"] = True
        return config

    async def verify(self, connector_id: str) -> dict[str, str]:
        """Start the administrator's own consent, which lets the hub list tools."""

        preset = self._preset(connector_id)
        hub = self._hub()
        gateway = await hub.find_gateway(preset.connector_id)
        if gateway is None:
            raise _not_available(preset)
        return {
            "authorize_url": await hub.authorize_url(
                gateway_id=str(gateway["id"]), email=hub.config.admin_email
            )
        }

    async def publish(self, connector_id: str) -> dict[str, Any]:
        """Serve the connector's tools to Assistants."""

        preset = self._preset(connector_id)
        hub = self._hub()
        gateway = await hub.find_gateway(preset.connector_id)
        if gateway is None:
            raise _not_available(preset)
        server_id = await hub.publish_tools(
            name=preset.connector_id,
            gateway_id=str(gateway["id"]),
            operator_email=hub.config.admin_email,
        )
        return {**self._describe(preset), "configured": True, "published": True, "server_id": server_id}

    async def remove(self, connector_id: str) -> None:
        preset = self._preset(connector_id)
        await self._hub().delete_gateway(preset.connector_id)

    # ── one user's consent ───────────────────────────────────────────────
    async def start(self, user: UserContext, connector_id: str) -> dict[str, str]:
        """Begin a consent; the browser is sent to the returned ``authorize_url``."""

        preset = self._preset(connector_id)
        hub = self._hub()
        gateway = await hub.find_gateway(preset.connector_id)
        if gateway is None or await hub.find_server(preset.connector_id) is None:
            raise _not_available(preset)
        email = await hub_member(hub, user.user_id, user.display_name)
        return {"authorize_url": await hub.authorize_url(gateway_id=str(gateway["id"]), email=email)}

    async def hub_callback(self, query: str) -> Any:
        """The hub's answer to a vendor's browser redirect."""

        return await self._hub().oauth_callback(query)

    async def connected(self, user: UserContext) -> list[str]:
        """The apps this user has connected, of those set up for connecting."""

        return [
            str(row["connector_id"])
            for row in await self.list_for_user(user)
            if row.get("available") and row.get("status") == "connected"
        ]

    async def disconnect(self, user: UserContext, connector_id: str) -> bool:
        preset = self._preset(connector_id)
        hub = self._hub()
        gateway = await hub.find_gateway(preset.connector_id)
        if gateway is None:
            return False
        email = await hub_member(hub, user.user_id, user.display_name)
        return await hub.revoke(gateway_id=str(gateway["id"]), email=email)


async def hub_member(hub: ContextForge, user_id: str, display_name: str | None) -> str:
    """The user's hub email, once the hub knows them.

    The hub refuses a session token for a user it has no record of, for every
    request: listing their connections, starting or removing one, and calling
    a connector's tools. AstraBox creates hub users itself, so each of those
    makes the user a member first rather than only a connect doing it.
    """

    email = hub.member_email(user_id)
    await hub.ensure_member(email=email, full_name=str(display_name or user_id))
    return email


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class _ConnectorSwitch:
    """Which connectors an Agent or Assistant offers, recorded on it.

    Enabling a connector records the hub's virtual server for it
    (``connector_servers``); the runtime view adds each as a platform MCP
    server, which the platform relays to the hub as the Session's person and
    which needs no egress from the sandbox. Changing the set changes the
    definition, so its runtime is prepared again. Subclasses name the record
    and how to reach it.
    """

    #: What the record is called in a refusal.
    kind = ""

    def __init__(self, *, service_getter: Any, hub: ContextForge | None = None) -> None:
        self._service = service_getter
        self._hub_override = hub

    async def _record(self, user: UserContext, target_id: str) -> dict[str, Any]:
        raise NotImplementedError


    async def _write(self, user: UserContext, target_id: str, servers: dict[str, str]) -> None:
        raise NotImplementedError

    @staticmethod
    def _own_servers(record: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def enabled(self, user: UserContext, target_id: str) -> list[str]:
        servers = (await self._record(user, target_id)).get("connector_servers") or {}
        return [preset.connector_id for preset in CONNECTOR_PRESETS if preset.connector_id in servers]

    async def set_enabled(
        self, user: UserContext, target_id: str, connector_id: str, *, enabled: bool
    ) -> list[str]:
        preset = connector_preset(connector_id)
        if preset is None:
            raise _not_found(connector_id)
        record = await self._record(user, target_id)
        servers = dict(record.get("connector_servers") or {})
        if enabled:
            own = self._own_servers(record)
            taken = next(
                (name for name in (preset.connector_id, CONNECTIONS_PLATFORM_SERVER) if name in own), None
            )
            if taken is not None:
                # The connections tools come with the first connector under
                # their own name, so that name is as taken as the app's.
                raise APIError(
                    code="AGENT_MCP_NAME_CONFLICT",
                    message=(
                        f"this {self.kind} already has an MCP server named "
                        f"'{taken}'; rename it to use the {preset.name} connector"
                    ),
                    status_code=409,
                )
            hub = self._hub_override if self._hub_override is not None else hub_from_env()
            server = await hub.find_server(preset.connector_id) if hub is not None else None
            if hub is None or server is None:
                raise _not_available(preset)
            # The hub's virtual server; the platform's MCP route relays to it.
            servers[preset.connector_id] = str(server["id"])
        else:
            servers.pop(preset.connector_id, None)
        await self._write(user, target_id, servers)
        return await self.enabled(user, target_id)


def _mcp_map(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("mcp_servers") if isinstance(raw.get("mcp_servers"), dict) else raw
    return {str(name): config for name, config in inner.items()}


class AssistantConnectors(_ConnectorSwitch):
    """An Assistant's connectors: its owner's connections, in all its conversations.

    The apps the owner connects are offered by default: a new Assistant starts
    with the apps its owner has connected, and an app the owner connects is
    turned on in each of their Assistants (:meth:`adopt`). Turning an app off
    on one Assistant is recorded there (``connectors_off``), and a later
    connection leaves it off. Disconnecting turns nothing off: the app's calls
    then say it is not connected, and the agent can ask to reconnect it.
    """

    kind = "Assistant"

    async def _record(self, user: UserContext, target_id: str) -> dict[str, Any]:
        return await self._service().get_assistant(user, target_id)


    async def _write(self, user: UserContext, target_id: str, servers: dict[str, str]) -> None:
        await self._service().set_connector_servers(user, target_id, servers)

    async def set_enabled(
        self, user: UserContext, target_id: str, connector_id: str, *, enabled: bool
    ) -> list[str]:
        result = await super().set_enabled(user, target_id, connector_id, enabled=enabled)
        record = await self._record(user, target_id)
        off = set(record.get("connectors_off") or [])
        off = off - {connector_id} if enabled else off | {connector_id}
        await self._service().set_connector_servers(
            user, target_id, dict(record.get("connector_servers") or {}), connectors_off=sorted(off)
        )
        return result

    async def adopt(
        self, user: UserContext, connector_ids: list[str], *, assistant_ids: list[str] | None = None
    ) -> list[str]:
        """Turn the given apps on in the user's Assistants that have not turned them off.

        ``assistant_ids`` narrows it to those Assistants. The caller has
        established that the user connected each app. An Assistant with its
        own MCP server under an app's name keeps it, and does not get the app.
        Returns the Assistants that changed.
        """

        owned = await self._service().list_assistants(user)
        changed: list[str] = []
        for assistant in owned:
            assistant_id = str(assistant.get("assistant_id") or "")
            if assistant_ids is not None and assistant_id not in assistant_ids:
                continue
            off = set(assistant.get("connectors_off") or [])
            servers = assistant.get("connector_servers") or {}
            wanted = [c for c in connector_ids if c not in off and c not in servers]
            for connector_id in wanted:
                try:
                    await _ConnectorSwitch.set_enabled(self, user, assistant_id, connector_id, enabled=True)
                except APIError as exc:
                    if exc.code != "AGENT_MCP_NAME_CONFLICT":
                        raise
                    continue
                if assistant_id not in changed:
                    changed.append(assistant_id)
        return changed

    @staticmethod
    def _own_servers(record: dict[str, Any]) -> dict[str, Any]:
        return _mcp_map(record.get("mcp_servers"))


class AgentConnectors(_ConnectorSwitch):
    """An Agent's connectors: each caller's own connections, in conversations they start.

    Conversations a channel, schedule or webhook starts have no such caller
    and are offered no connectors.
    """

    kind = "Agent"

    async def _record(self, user: UserContext, target_id: str) -> dict[str, Any]:
        return await self._service().get_agent(user, target_id)


    async def _write(self, user: UserContext, target_id: str, servers: dict[str, str]) -> None:
        await self._service().set_connector_servers(user, target_id, servers)

    @staticmethod
    def _own_servers(record: dict[str, Any]) -> dict[str, Any]:
        return _mcp_map(record.get("mcp_servers"))
