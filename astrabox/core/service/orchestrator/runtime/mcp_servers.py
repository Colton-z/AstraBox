from __future__ import annotations

import hashlib
import posixpath
from typing import Any
from urllib.parse import urlparse

from astrabox.common.utils.errors import APIError

HTML_PREVIEW_PLATFORM_SERVER = "html_preview"

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
    """Whether answering this server's calls requires acting on the sandbox.

    Only AstraBox can publish a port of the box that is asking, or a preview
    for its session, so those calls terminate on an HTTP route here. Every
    other server — a process inside the box, or an endpoint on the network — is
    reached by the sandbox's own MCP client, and AstraBox's whole part in it is
    writing the URL into the engine's config and admitting the host to the
    egress allowlist.

    There is no third kind: an upstream server's credential is attached by the
    egress sidecar's vault, which keeps it out of the sandbox, so relaying a
    call through this server buys nothing that would justify the hop.
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


def extract_mcp_service_code(config: Any) -> str:
    if isinstance(config, str):
        return config.strip()
    if not isinstance(config, dict):
        return ""
    for key in ("service_code", "serviceCode", "server_code", "serverCode"):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    url = extract_mcp_server_url(config)
    if url:
        try:
            parsed = urlparse(url)
        except Exception:
            return ""
        for part in parsed.query.split("&"):
            if not part:
                continue
            key, _, value = part.partition("=")
            if key == "serverCode" and value:
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
) -> dict[str, dict[str, Any]]:
    servers = template_mcp_servers(mcp_config)
    if not servers:
        return {}
    base = str(proxy_base_url or "").strip().rstrip("/")
    bid = str(deployment_id or "").strip()
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
            "url": f"{base}/api/v1/platform-mcp/{bid}/{name}/mcp",
        }

    return result


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


def exposed_port_url_path(deployment_id: str, port: int) -> str:
    return f"/api/v1/exposed-ports/{str(deployment_id).strip()}/{int(port)}/url"
