"""Provider-neutral Environment networking configuration.

An Environment decides ordinary outbound reachability. Credential storage and
injection are a separate sandbox capability and therefore do not appear in this
module. The runtime later adds destinations the platform itself requires
(model, callback, tracing, declared Plugin repositories, and optionally Agent
MCP). The provider boundary separately admits exact destinations authorized by
attached Vault bindings without writing them into the Environment document.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Literal

from astrabox.seams.sandbox import (
    SANDBOX_NETWORK_LIMITED,
    SANDBOX_NETWORK_MODES,
    SANDBOX_NETWORK_UNRESTRICTED,
)

_NETWORKING_KEYS = frozenset({"type", "allowed_hosts", "allow_mcp_servers"})
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


@dataclass(frozen=True, slots=True)
class EnvironmentNetworking:
    """The Environment-owned network intent before provider translation."""

    mode: Literal["unrestricted", "limited"]
    allowed_hosts: tuple[str, ...] = ()
    allow_mcp_servers: bool = False


def normalize_host_target(value: Any, *, label: str) -> str:
    """Canonicalize one destination: an exact host, leftmost wildcard, IP or CIDR.

    An administrator's allowed host and a host an Agent's own fields add to the
    policy (a Skill or Plugin Git origin, a remote MCP server) pass through this
    one function, so the two cannot disagree about what a destination is.
    ``label`` names the value in the refusal.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    target = value.strip().lower().rstrip(".")
    if not target:
        raise ValueError(f"{label} cannot be empty")
    if any(char.isspace() for char in target) or "://" in target:
        raise ValueError(
            f"{label} must be a host, wildcard host, IP, or CIDR"
        )

    try:
        return str(ipaddress.ip_address(target))
    except ValueError:
        if "/" in target:
            try:
                return str(ipaddress.ip_network(target, strict=False))
            except ValueError:
                pass

    hostname = target[2:] if target.startswith("*.") else target
    if "/" in hostname or ":" in hostname:
        raise ValueError(
            f"{label} must not include a scheme, port, or path"
        )
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or any(not part or not _HOST_LABEL_RE.fullmatch(part) for part in labels)
    ):
        raise ValueError(
            f"{label} is not a valid host or leftmost wildcard"
        )
    return target


def non_public_address_reason(address: str) -> str | None:
    """Why ``address`` belongs to the deployment's own network, or None.

    Only a globally routable unicast address is the public internet. Private
    space (which holds the Docker bridge, its gateway where the platform API is
    published, and sibling sandboxes), loopback, link-local (which holds the
    169.254.169.254 cloud metadata service), shared, reserved, multicast and
    unspecified space all reach something the deployment owns. An IPv6 address
    that carries an IPv4 one (mapped, 6to4, or the NAT64 well-known prefix) is
    judged by that IPv4 address as well, because that is where it leads.
    """
    ip = ipaddress.ip_address(address)
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            candidates.append(ip.sixtofour)
        if ip in _NAT64_WELL_KNOWN_PREFIX:
            candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    for candidate in candidates:
        if candidate.is_loopback:
            return "a loopback address"
        if candidate.is_link_local:
            return "a link-local address (this range holds the cloud metadata service)"
        if candidate.is_private:
            return "a private address"
        if candidate.is_multicast:
            return "a multicast address"
        if not candidate.is_global:
            return "not a publicly routable address"
    return None


def environment_admits_host(networking: EnvironmentNetworking, host: str) -> bool:
    """Whether the Environment's own allowed hosts already name ``host``.

    ``host`` is canonical (see :func:`normalize_host_target`). A leftmost
    wildcard names every host beneath its domain, not the domain itself.
    """
    for target in networking.allowed_hosts:
        if target == host:
            return True
        if target.startswith("*.") and host.endswith(target[1:]):
            return True
    return False


def environment_admits_address(networking: EnvironmentNetworking, address: str) -> bool:
    """Whether an allowed IP or CIDR of the Environment already covers ``address``."""
    ip = ipaddress.ip_address(address)
    for target in networking.allowed_hosts:
        try:
            network = ipaddress.ip_network(target, strict=False)
        except ValueError:
            continue
        if network.version == ip.version and ip in network:
            return True
    return False


def parse_environment_networking(raw: Any) -> EnvironmentNetworking:
    """Validate one Environment networking document without provider vocabulary.

    An omitted document has a secure, deterministic default: limited networking
    with no operator-authored destinations and Agent MCP auto-admission off.
    Platform-required destinations are composed later from runtime facts.
    """

    if raw is None or raw == {}:
        return EnvironmentNetworking(mode=SANDBOX_NETWORK_LIMITED)
    if not isinstance(raw, dict):
        raise ValueError("networking must be an object")
    unknown = sorted(set(raw) - _NETWORKING_KEYS)
    if unknown:
        raise ValueError(
            "networking contains unsupported fields: " + ", ".join(unknown)
        )

    mode = str(raw.get("type") or "").strip().lower()
    if mode not in SANDBOX_NETWORK_MODES:
        raise ValueError(
            "networking.type must be "
            f"{SANDBOX_NETWORK_UNRESTRICTED!r} or {SANDBOX_NETWORK_LIMITED!r}"
        )

    allowed_raw = raw.get("allowed_hosts", [])
    if not isinstance(allowed_raw, list):
        raise ValueError("networking.allowed_hosts must be a list of hosts")
    allowed: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(allowed_raw):
        target = normalize_host_target(
            value, label=f"networking.allowed_hosts[{index}]"
        )
        if target not in seen:
            seen.add(target)
            allowed.append(target)

    allow_mcp = raw.get("allow_mcp_servers", False)
    if not isinstance(allow_mcp, bool):
        raise ValueError("networking.allow_mcp_servers must be a boolean")

    if mode == SANDBOX_NETWORK_UNRESTRICTED:
        return EnvironmentNetworking(mode=SANDBOX_NETWORK_UNRESTRICTED)
    return EnvironmentNetworking(
        mode=SANDBOX_NETWORK_LIMITED,
        allowed_hosts=tuple(allowed),
        allow_mcp_servers=allow_mcp,
    )


def normalized_environment_networking(raw: Any) -> dict[str, Any]:
    """Return the canonical stored shape for one networking document."""

    spec = parse_environment_networking(raw)
    if spec.mode == SANDBOX_NETWORK_UNRESTRICTED:
        return {"type": SANDBOX_NETWORK_UNRESTRICTED}
    return {
        "type": SANDBOX_NETWORK_LIMITED,
        "allowed_hosts": list(spec.allowed_hosts),
        "allow_mcp_servers": spec.allow_mcp_servers,
    }


__all__ = [
    "EnvironmentNetworking",
    "environment_admits_address",
    "environment_admits_host",
    "non_public_address_reason",
    "normalize_host_target",
    "normalized_environment_networking",
    "parse_environment_networking",
]
