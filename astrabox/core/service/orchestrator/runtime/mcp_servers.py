from __future__ import annotations

import hashlib
import posixpath
from typing import Any
from urllib.parse import urlparse

from astrabox.common.utils.errors import APIError

EXPOSE_PORT_PLATFORM_SERVER = "expose_port"
ARTIFACTS_PLATFORM_SERVER = "artifacts"
CONNECTIONS_PLATFORM_SERVER = "connections"
AGENTS_PLATFORM_SERVER = "agents"
#: The A2A task outcome tool (``agent/a2a_task_outcome.py``), in conversations an A2A context created.
A2A_PLATFORM_SERVER = "a2a"
#: The Session provenance of a conversation an A2A context created.
A2A_SOURCE_TYPE = "a2a"
#: A work-app connector, relayed to the connector hub as the Session's person.
CONNECTOR_PLATFORM_SERVER = "connector"

_LOOPBACK_MCP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SSE_ALIASES = frozenset({"sse", "server_sent_events"})
_HTTP_ALIASES = frozenset({"streamable_http", "streamablehttp", "http"})


def template_mcp_servers(mcp_servers: Any) -> dict[str, Any]:
    """Normalize an agent's ``mcp_servers`` into a name→server-def map.

    The stored/AgentView shape is a flat name-keyed map (``mcp_servers`` absorbs
    ``mcp_config``; see ``docs/domain-model.md``). A ``{"mcp_servers": {...}}`` wrapper is
    still unwrapped for safety, since an assistant override may nest the
    servers under that key."""
    if not isinstance(mcp_servers, dict):
        return {}
    inner = mcp_servers.get("mcp_servers")
    servers = inner if isinstance(inner, dict) else mcp_servers
    return {
        str(name).strip(): config
        for name, config in servers.items()
        if str(name).strip()
    }


def a2a_task_tools_enabled(source_type: str | None) -> bool:
    """Whether a Session of this provenance holds the ``a2a`` task outcome tool.

    Only a conversation an A2A context created answers A2A tasks, so only its
    engine can report a task's outcome.
    """
    return source_type == A2A_SOURCE_TYPE


def prepared_runtime_serves(template: Any) -> bool:
    """Whether a prepared native process holds the platform servers a Session is given.

    A prepared process froze its platform MCP servers at spawn, before any
    Session existed: the servers its Agent gives a Session with no
    provenance. The Agent's runtime generation already keeps a process
    prepared from another configuration from being claimed, so a Session may
    take one exactly when its own servers are the same. An A2A conversation is
    also given the ``a2a`` server and starts a runtime of its own.
    """

    def servers(source_type: str | None) -> dict[str, Any]:
        return binding_mcp_server_definitions(
            getattr(template, "mcp_servers", None),
            session_source_type=source_type,
            agent_id=getattr(template, "agent_id", None),
            assistant_id=getattr(template, "assistant_id", None),
            artifacts_enabled=getattr(template, "artifacts_enabled", True),
        )

    return servers(getattr(template, "session_source_type", None)) == servers(None)


def binding_mcp_server_definitions(
    mcp_config: Any,
    *,
    session_source_type: str | None = None,
    agent_id: str | None = None,
    assistant_id: str | None = None,
    artifacts_enabled: bool,
) -> dict[str, Any]:
    """The servers one Session's engine is given.

    The Agent's configuration decides them. A messaging-channel conversation
    runs as its deployment's execution account and is given the same servers
    as that account's other conversations of the Agent; every call is still
    authorized against the Session's account and the Agent when it arrives.
    Where connectors are offered, the platform's connections tools come with
    them. The ``artifacts`` server follows the Agent's ``artifacts_enabled``
    switch (an Assistant has none and passes True), and a removal also drops
    authored aliases of it, so no name can reach those tools on a binding that
    has none. An Agent conversation an A2A context created also gets the
    ``a2a`` server (:func:`a2a_task_tools_enabled`), which no authored
    definition reaches. All of this is decided here, at the engine, rather
    than in the resolved view: the Agent's runtime generation and its box's
    egress are the same for every Session of the Agent.
    """
    servers = template_mcp_servers(mcp_config)
    if assistant_id:
        if AGENTS_PLATFORM_SERVER in servers:
            raise APIError(
                code="AGENT_MCP_NAME_CONFLICT",
                message="the MCP server name agents belongs to Assistant coordination tools",
                status_code=409,
            )
        servers = {**servers, AGENTS_PLATFORM_SERVER: {"platform_server": AGENTS_PLATFORM_SERVER}}
    elif not agent_id:
        servers = {name: config for name, config in servers.items()
                   if platform_mcp_server_id(name, config) != AGENTS_PLATFORM_SERVER}
    # The task outcome tool is the platform's alone: no author definition reaches it.
    servers = {name: config for name, config in servers.items()
               if platform_mcp_server_id(name, config) != A2A_PLATFORM_SERVER}
    if agent_id and not assistant_id and a2a_task_tools_enabled(session_source_type):
        if A2A_PLATFORM_SERVER in servers:
            raise APIError(
                code="AGENT_MCP_NAME_CONFLICT",
                message=(
                    f"the MCP server name {A2A_PLATFORM_SERVER!r} is the platform's A2A task "
                    "outcome tool; rename the server"
                ),
                status_code=409,
            )
        servers = {**servers, A2A_PLATFORM_SERVER: {"platform_server": A2A_PLATFORM_SERVER}}
    if any(platform_mcp_server_id(name, config) == CONNECTOR_PLATFORM_SERVER for name, config in servers.items()):
        # The engine is told which offered apps the person has connected and
        # can ask them to connect one; it decides when.
        if CONNECTIONS_PLATFORM_SERVER in servers:
            raise APIError(
                code="AGENT_MCP_NAME_CONFLICT",
                message=(
                    f"the MCP server name {CONNECTIONS_PLATFORM_SERVER!r} is the platform's "
                    "connections tools, which come with connectors; rename the server"
                ),
                status_code=409,
            )
        servers = {**servers, CONNECTIONS_PLATFORM_SERVER: {"platform_server": CONNECTIONS_PLATFORM_SERVER}}
    if not artifacts_enabled:
        return {name: config for name, config in servers.items()
                if name != ARTIFACTS_PLATFORM_SERVER and platform_mcp_server_id(name, config) != ARTIFACTS_PLATFORM_SERVER}
    declared = servers.get(ARTIFACTS_PLATFORM_SERVER)
    if declared is not None and platform_mcp_server_id(ARTIFACTS_PLATFORM_SERVER, declared) != ARTIFACTS_PLATFORM_SERVER:
        raise APIError(
            code="AGENT_MCP_NAME_CONFLICT",
            message=(
                f"the MCP server name {ARTIFACTS_PLATFORM_SERVER!r} is the platform's artifact "
                "tools; rename the server"
            ),
            status_code=409,
        )
    return {**servers, ARTIFACTS_PLATFORM_SERVER: {"platform_server": ARTIFACTS_PLATFORM_SERVER}}


def prepared_slot_mcp_deployment_id(slot_id: str) -> str:
    """The platform-MCP deployment id a prepared slot's URLs are fixed under.

    A prepared Claude process freezes its MCP URLs at spawn, before any
    platform Session exists, so the URL must carry an address the platform can
    bind later — never a Session id. Prepare and activation both derive the
    same value from the slot id: prepare writes it into the child's URLs, and
    activation records the binding the proxy resolves it through.
    """
    normalized = str(slot_id or "").strip()
    if not normalized:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="prepared slot MCP addressing requires a slot id",
            status_code=500,
        )
    return make_mcp_deployment_id(
        scope_kind="prepared_slot",
        owner_id=normalized,
    )


def make_mcp_deployment_id(
    *,
    scope_kind: str,
    owner_id: str,
    user_id: str | None = None,
) -> str:
    raw = "|".join(
        [
            "platform-mcp",
            str(scope_kind or "").strip(),
            str(owner_id or "").strip(),
            str(user_id or "").strip(),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"mcp_{digest}"


def mcp_server_enabled(config: Any) -> bool:
    if not isinstance(config, dict):
        return True
    raw = config.get("enabled")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def platform_mcp_server_id(name: str, config: Any) -> str:
    """The AstraBox capability this definition asks for, or "" for none.

    Naming a platform server is how a definition says "answer this call here"
    rather than "dial this URL from the box", so the id must be explicit: a
    value inferred from the server's own key would mark every definition.
    """

    if not isinstance(config, dict):
        return ""
    return str(
        config.get("platform_server") or config.get("platformServer") or ""
    ).strip()


def is_platform_mcp_server(config: Any) -> bool:
    """Whether this server terminates on an AstraBox capability route.

    Platform servers act on platform-owned resources: sandbox port publication
    and durable artifacts. Other servers are reached by the sandbox's own MCP
    client, with credentials attached at its egress seam.
    """

    return bool(platform_mcp_server_id("", config))


def extract_mcp_server_url(config: Any) -> str:
    if not isinstance(config, dict):
        return ""
    for key in ("url", "endpoint", "server_url", "serverUrl"):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_in_sandbox_loopback_mcp_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
    except Exception:
        return False
    return (parsed.hostname or "").lower() in _LOOPBACK_MCP_HOSTS


def runtime_mcp_transport_type(config: Any, *, default: str = "http") -> str:
    raw = ""
    if isinstance(config, dict):
        raw = str(config.get("type") or config.get("transport") or "").strip()
    normalized = raw.lower().replace("-", "_")
    if normalized in _SSE_ALIASES:
        return "sse"
    if normalized in _HTTP_ALIASES:
        return "http"
    return default


def normalize_sandbox_mcp_entry(config: Any) -> dict[str, Any]:
    if isinstance(config, dict) and str(config.get("command") or "").strip():
        command = str(config["command"]).strip()
        stdio_result: dict[str, Any] = {"type": "stdio", "command": command}
        if "args" in config:
            args = config.get("args")
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox stdio MCP server args must be a list of strings",
                    status_code=500,
                )
            stdio_result["args"] = list(args)
        if "env" in config:
            env = config.get("env")
            if not isinstance(env, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox stdio MCP server env must be a string map",
                    status_code=500,
                )
            stdio_result["env"] = dict(env)
        return stdio_result
    url = extract_mcp_server_url(config)
    if not url:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="sandbox/internal MCP server requires url",
            status_code=500,
        )
    http_result: dict[str, Any] = {
        "type": runtime_mcp_transport_type(config),
        "url": url,
    }
    if isinstance(config, dict) and "headers" in config:
        headers = config.get("headers")
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox HTTP MCP server headers must be a string map",
                status_code=500,
            )
        http_result["headers"] = dict(headers)
    return http_result


def sandbox_mcp_egress_hosts(mcp_config: Any) -> list[str]:
    """Return remote hosts reached directly by sandbox-owned MCP clients.

    A server the sandbox does not dial over the network contributes no host: a
    stdio one is a process the engine starts inside the box, and a loopback URL
    is a listener already there. Demanding a URL from those refuses the whole
    session — an agent carrying one stdio MCP server never reached READY.
    """

    hosts: list[str] = []
    for name, config in template_mcp_servers(mcp_config).items():
        if not mcp_server_enabled(config) or is_platform_mcp_server(config):
            continue
        if isinstance(config, dict) and str(config.get("command") or "").strip():
            continue
        url = extract_mcp_server_url(config)
        if is_in_sandbox_loopback_mcp_url(url):
            continue
        parsed = urlparse(url)
        host = str(parsed.hostname or "").strip().lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"sandbox MCP server '{name}' requires an http(s) URL with a host",
                status_code=500,
            )
        if host not in hosts:
            hosts.append(host)
    return hosts


def runtime_mcp_servers_for_binding(
    mcp_config: Any,
    *,
    proxy_base_url: str,
    deployment_id: str,
    session_source_type: str | None = None,
    agent_id: str | None = None,
    assistant_id: str | None = None,
    artifacts_enabled: bool,
) -> dict[str, dict[str, Any]]:
    from astrabox.core.service.orchestrator.transcript_capability import mint_platform_mcp_capability

    base = str(proxy_base_url or "").strip().rstrip("/")
    bid = str(deployment_id or "").strip()
    servers = (binding_mcp_server_definitions(
                   mcp_config, session_source_type=session_source_type,
                   agent_id=agent_id, assistant_id=assistant_id, artifacts_enabled=artifacts_enabled,
               )
               if (base and bid) or not artifacts_enabled
               else template_mcp_servers(mcp_config))
    result: dict[str, dict[str, Any]] = {}

    for name, config in servers.items():
        if not mcp_server_enabled(config):
            continue
        if not is_platform_mcp_server(config):
            result[name] = normalize_sandbox_mcp_entry(config)
            continue
        if not base or not bid:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"mcp_proxy_base_url and deployment_id are required for MCP server '{name}'",
                status_code=500,
            )
        result[name] = {
            "type": "http",
            "url": f"{base}/api/v1/platform-mcp/{bid}/{name}/{mint_platform_mcp_capability(bid, name)}/mcp",
        }

    return result


def author_mcp_servers(mcp_config: Any) -> dict[str, dict[str, Any]]:
    """The author's enabled servers, normalized; platform servers are not the author's.

    Each is ``{"type": "stdio", "command", "args"?, "env"?}`` or
    ``{"type": "http" | "sse", "url", "headers"?}``, which an engine adapter
    renders into its own configuration.
    """
    return {
        name: normalize_sandbox_mcp_entry(config)
        for name, config in template_mcp_servers(mcp_config).items()
        if mcp_server_enabled(config) and not is_platform_mcp_server(config)
    }


def validate_direct_mcp_servers(mcp_config: Any) -> dict[str, Any]:
    servers = template_mcp_servers(mcp_config)
    result: dict[str, Any] = {}
    for name, config in servers.items():
        if not mcp_server_enabled(config):
            continue
        if is_platform_mcp_server(config):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"platform MCP server '{name}' requires mcp_proxy_base_url and deployment_id",
                status_code=500,
            )
        result[name] = normalize_sandbox_mcp_entry(config)
    return result
