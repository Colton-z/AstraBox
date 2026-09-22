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


@dataclass(frozen=True, slots=True)
class EnvironmentNetworking:
    """The Environment-owned network intent before provider translation."""

    mode: Literal["unrestricted", "limited"]
    allowed_hosts: tuple[str, ...] = ()
    allow_mcp_servers: bool = False


def _normalize_host_target(value: Any, *, index: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"networking.allowed_hosts[{index}] must be a string")
    target = value.strip().lower().rstrip(".")
    if not target:
        raise ValueError(f"networking.allowed_hosts[{index}] cannot be empty")
    if any(char.isspace() for char in target) or "://" in target:
        raise ValueError(
            f"networking.allowed_hosts[{index}] must be a host, wildcard host, IP, or CIDR"
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
            f"networking.allowed_hosts[{index}] must not include a scheme, port, or path"
        )
    labels = hostname.split(".")
    if (
        len(hostname) > 253
        or any(not label or not _HOST_LABEL_RE.fullmatch(label) for label in labels)
    ):
        raise ValueError(
            f"networking.allowed_hosts[{index}] is not a valid host or leftmost wildcard"
        )
    return target


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
        target = _normalize_host_target(value, index=index)
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
    "normalized_environment_networking",
    "parse_environment_networking",
]
