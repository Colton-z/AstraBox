"""What an Agent's or Assistant's own fields may make its sandbox reach and use.

Anyone who may create an Agent or an Assistant writes fields that decide what
its sandbox clones and connects to: Skill and Plugin Git origins and its own
remote MCP servers join a limited Environment's allowed hosts, a repository
deploy key is named by its secret, and an MCP server definition decides which
requests the egress sidecar authenticates. An author may use only what an
administrator made available:

* A host from those fields joins a limited Environment's policy only when the
  Environment already names it, or when it is a public internet address. A
  private, loopback, link-local or otherwise non-public address is the
  deployment's own network — the Docker bridge gateway the platform API is
  published on, sibling sandboxes, the cloud metadata service — and only an
  administrator opens it, by naming it in the Environment.
* A deploy key resolves only from a secret name listed in
  ``ASTRABOX_DEPLOY_KEY_SECRET_NAMES``. Every other server environment variable
  is out of an author's reach.
* ``provider`` and ``credential_target_url`` on an MCP definition select the
  platform's gateway credential and point a Vault credential at the server's
  URL. Only a catalog assignment an administrator made writes them.

The checks run where an author writes, refusing with a 4xx before anything is
saved, and again where the runtime reads a stored definition (the MCP fields
when the Agent is resolved, the hosts when a sandbox policy is built), because
a definition may predate the checks and the Environment may have changed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Mapping
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.secrets import secret_env_key
from astrabox.core.service.orchestrator.environment_networking import (
    EnvironmentNetworking,
    environment_admits_address,
    environment_admits_host,
    non_public_address_reason,
    normalize_host_target,
    parse_environment_networking,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    sandbox_mcp_egress_hosts,
    template_mcp_servers,
)
from astrabox.core.service.orchestrator.runtime.plugin_repos import (
    normalize_plugin_repos,
    plugin_repo_egress_hosts,
)
from astrabox.seams.sandbox import SANDBOX_NETWORK_LIMITED

#: MCP definition fields only a catalog assignment writes. ``provider`` makes
#: the sidecar attach that provider's gateway credential; ``credential_target_url``
#: makes it attach the Vault credential bound to another URL.
RESERVED_MCP_FIELDS: tuple[str, ...] = ("provider", "credential_target_url")

#: How long an author's save waits for one host name to resolve before the
#: host is refused as unproven.
_RESOLVE_TIMEOUT_SECONDS = 5.0


def refuse_reserved_mcp_fields(servers: Any, *, owner: str) -> None:
    """Refuse an author's own MCP definitions that set a catalog-only field.

    ``servers`` are the author's own definitions only: a catalog assignment
    resolves its servers with these fields set, after this check.
    """
    for name, config in template_mcp_servers(servers).items():
        if not isinstance(config, dict):
            continue
        present = [field for field in RESERVED_MCP_FIELDS if field in config]
        if present:
            raise APIError(
                code="AGENT_MCP_FIELD_RESERVED",
                message=(
                    f"MCP server {name!r} of {owner} sets {', '.join(present)}, "
                    "which only an administrator's catalog assignment may set. "
                    "Remove the field; assign the server from the catalog to use "
                    "the platform's gateway credential."
                ),
                status_code=403,
            )


def author_egress_hosts(
    *,
    skills: Any,
    plugin_repos: Any,
    mcp_servers: Any,
) -> list[str]:
    """The network hosts an author's own Skills, Plugins and MCP servers add."""
    from astrabox.core.service.orchestrator.runtime.conversation_identity import (
        skill_repo_egress_hosts,
    )

    if isinstance(skills, str):
        skill_items = [skills]
    else:
        skill_items = [str(item) for item in (skills or []) if str(item or "").strip()]
    hosts: list[str] = []
    for host in (
        *skill_repo_egress_hosts(skill_items),
        *plugin_repo_egress_hosts({"plugin_repos": plugin_repos}),
        *sandbox_mcp_egress_hosts(mcp_servers),
    ):
        if host not in hosts:
            hosts.append(host)
    return hosts


def _refused(host: str, reason: str) -> APIError:
    return APIError(
        code="AGENT_EGRESS_HOST_REFUSED",
        message=(
            f"network host {host!r} from this Agent's Skills, Plugins or MCP "
            f"servers is {reason}. Its limited Environment does not name it, and "
            "only an administrator can open the deployment's own network to a "
            "sandbox: use a public host, or ask an administrator to add this "
            "host to the Environment's allowed hosts."
        ),
        status_code=403,
    )


def check_author_egress_hosts(
    networking: EnvironmentNetworking, hosts: Iterable[str]
) -> list[str]:
    """Refuse author hosts a limited Environment does not already admit.

    Checks what needs no lookup: the host's syntax, whether the Environment
    names it, and a literal IP. Returns the host names still to be resolved;
    an unrestricted Environment admits everything and returns none.
    """
    if networking.mode != SANDBOX_NETWORK_LIMITED:
        return []
    pending: list[str] = []
    for raw in hosts:
        try:
            host = normalize_host_target(raw, label=f"network host {raw!r}")
        except ValueError as exc:
            raise APIError(
                code="AGENT_EGRESS_HOST_REFUSED",
                message=str(exc),
                status_code=403,
            ) from exc
        if "/" in host or host.startswith("*."):
            raise _refused(host, "not a single host")
        if environment_admits_host(networking, host):
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pending.append(host)
            continue
        reason = non_public_address_reason(host)
        if reason and not environment_admits_address(networking, host):
            raise _refused(host, reason)
    return pending


async def resolve_host_addresses(host: str) -> list[str]:
    """Every address the platform's resolver answers for ``host``.

    An empty list means the name did not resolve. OpenSandbox's egress sidecar
    resolves the name again whenever the sandbox connects and admits whatever
    answers it gets then, so this proves only what the name points at now.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
            timeout=_RESOLVE_TIMEOUT_SECONDS,
        )
    except (OSError, UnicodeError, asyncio.TimeoutError):
        return []
    return sorted({str(info[4][0]).split("%", 1)[0] for info in infos})


async def refuse_unadmitted_author_hosts(
    networking: EnvironmentNetworking, hosts: Iterable[str]
) -> None:
    """Refuse author hosts that would open the deployment's own network.

    A host name is resolved, and every address it answers must be public or
    already covered by one of the Environment's allowed IPs or CIDRs. A name
    that does not resolve cannot be shown to be public and is refused.
    """
    for name in check_author_egress_hosts(networking, hosts):
        addresses = await resolve_host_addresses(name)
        if not addresses:
            raise _refused(name, "a name the platform could not resolve")
        for address in addresses:
            reason = non_public_address_reason(address)
            if reason and not environment_admits_address(networking, address):
                raise _refused(name, f"resolved to {address}, {reason}")


def allowed_deploy_key_env_keys() -> frozenset[str]:
    """The environment keys of the deploy keys an administrator made available."""
    from astrabox.common.utils.settings import load_astrabox_settings

    raw = str(load_astrabox_settings().deploy_key_secret_names or "")
    return frozenset(
        secret_env_key(name.strip()) for name in raw.split(",") if name.strip()
    )


def require_allowed_deploy_key(secret_name: str, *, label: str) -> None:
    """Refuse a deploy-key secret name no administrator listed.

    Names are compared by the environment variable they resolve to, so a
    different spelling of a listed name is the same key and an unlisted
    variable stays unreachable however it is spelled.
    """
    if secret_env_key(secret_name) in allowed_deploy_key_env_keys():
        return
    raise APIError(
        code="AGENT_DEPLOY_KEY_NOT_ALLOWED",
        message=(
            f"{label}.deploy_key_secret_name {secret_name!r} is not a deploy key "
            "an administrator made available. An administrator lists the deploy "
            "keys Agents may use in ASTRABOX_DEPLOY_KEY_SECRET_NAMES."
        ),
        status_code=403,
    )


def _deploy_key_names(
    *, default_repo: Any, plugin_repos: Any
) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    if isinstance(default_repo, Mapping):
        name = str(default_repo.get("deploy_key_secret_name") or "").strip()
        if name:
            names.append(("default_repo", name))
    for index, repo in enumerate(normalize_plugin_repos(plugin_repos)):
        name = str(repo.get("deploy_key_secret_name") or "").strip()
        if name:
            names.append((f"plugin_repos[{index}]", name))
    return names


async def validate_author_declarations(
    *,
    owner: str,
    networking: Any,
    skills: Any,
    plugin_repos: Any,
    mcp_servers: Any,
    default_repo: Any = None,
) -> None:
    """Refuse an Agent's or Assistant's fields before they are saved.

    ``networking`` is the stored networking document of the Environment the
    fields will run in, and ``owner`` names the resource in the refusal.
    """
    refuse_reserved_mcp_fields(mcp_servers, owner=owner)
    for label, name in _deploy_key_names(
        default_repo=default_repo, plugin_repos=plugin_repos
    ):
        require_allowed_deploy_key(name, label=label)
    try:
        spec = parse_environment_networking(networking)
    except ValueError as exc:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"Environment networking is invalid: {exc}",
            status_code=500,
        ) from exc
    await refuse_unadmitted_author_hosts(
        spec,
        author_egress_hosts(
            skills=skills, plugin_repos=plugin_repos, mcp_servers=mcp_servers
        ),
    )


__all__ = [
    "RESERVED_MCP_FIELDS",
    "allowed_deploy_key_env_keys",
    "author_egress_hosts",
    "check_author_egress_hosts",
    "refuse_reserved_mcp_fields",
    "refuse_unadmitted_author_hosts",
    "require_allowed_deploy_key",
    "resolve_host_addresses",
    "validate_author_declarations",
]
