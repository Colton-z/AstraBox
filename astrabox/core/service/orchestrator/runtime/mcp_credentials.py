"""Direct MCP credential delivery through the sandbox egress Vault."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    extract_mcp_server_url,
    is_in_sandbox_loopback_mcp_url,
    is_platform_mcp_server,
    mcp_server_enabled,
    runtime_mcp_transport_type,
    template_mcp_servers,
)
from astrabox.seams.egress_credentials import (
    MCPHeaderEgressCredential,
    MCPOutboundCredentialResolution,
    SandboxEgressCredentialPlan,
)
from astrabox.seams.extensions import extension_provider_for_name


def _normalized_target(url: str) -> str:
    # The Vault owns this key contract. Import lazily so engine modules remain
    # importable while the runtime manager is registering providers.
    from astrabox.core.service.orchestrator.vault_service import (
        normalize_mcp_server_url,
    )

    return normalize_mcp_server_url(url)


def _request_destination_scope(url: str) -> str:
    """Canonicalize one remote request destination without provider rules.

    Explicit default ports are equivalent to the scheme default. Query strings
    remain part of the destination because a provider that can preserve them
    must not be constrained by a provider that cannot.
    """

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = str(parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=f"sandbox MCP server has an invalid port: {url!r}",
            status_code=400,
        ) from exc
    default_port = 80 if scheme == "http" else 443 if scheme == "https" else None
    host_token = f"[{host}]" if ":" in host else host
    authority = host_token if port in {None, default_port} else f"{host_token}:{port}"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{authority}{parsed.path or '/'}{query}"


def _gateway_header_value(header: str, value: str) -> tuple[str, str]:
    name = str(header or "").strip() or "Authorization"
    raw = str(value or "").strip()
    if not raw:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=f"MCP gateway credential for header {name!r} is empty",
            status_code=400,
        )
    if name.lower() == "authorization":
        return "Authorization", f"Bearer {raw}"
    return name, raw


def mcp_vault_scope_id(vault_ids: list[str]) -> str:
    """Fingerprint one ordered platform Vault binding without exposing its ids."""

    payload = json.dumps(
        vault_ids,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _claim_header(
    claimed: dict[str, tuple[str, str, set[str]]],
    *,
    name: str,
    value: str,
    source: str,
    destination: str,
) -> None:
    display = str(name or "").strip()
    key = display.lower()
    existing = claimed.get(key)
    if existing is not None and existing[1] != value:
        raise APIError(
            code="VAULT_CREDENTIAL_CONFLICT",
            message=(
                f"MCP destination {destination!r} has more than one value for "
                f"request header {display!r}"
            ),
            status_code=409,
        )
    if existing is not None:
        existing[2].add(source)
    else:
        claimed[key] = (display, value, {source})


def _validate_gateway_destination(
    *, provider_name: str, actual_url: str, gateway: Any
) -> None:
    actual = urlsplit(actual_url)
    base = urlsplit(str(getattr(gateway, "base_url", "") or "").strip())
    path_glob = str(getattr(gateway, "path_glob", "") or "").strip() or "/*/mcp"
    if (
        actual.scheme.lower() != base.scheme.lower()
        or actual.netloc.lower() != base.netloc.lower()
        or not fnmatchcase(actual.path or "/", path_glob)
    ):
        raise APIError(
            code="AGENT_EXTENSION_PROVIDER_INVALID",
            message=(
                f"MCP server {actual_url!r} resolved by provider {provider_name!r} "
                f"falls outside that provider's credential scope"
            ),
            status_code=500,
        )


async def _resolve_mcp_credential_plan(
    *,
    template: Any,
    vault_enabled: bool,
    credential_resolver: Callable[
        [list[str]], Awaitable[MCPOutboundCredentialResolution]
    ],
) -> SandboxEgressCredentialPlan | None:
    """Compose one non-ambiguous egress binding per direct MCP URL.

    Provider gateway authentication and the platform-resolved upstream
    credential are merged before translation. The caller supplies the owner of
    that platform credential scope: either a persisted Session snapshot or the
    Agent binding from which a prepared runtime is being built.
    """

    groups: dict[str, dict[str, Any]] = {}
    lookup_targets: list[str] = []
    for server_name, config in template_mcp_servers(
        getattr(template, "mcp_servers", None)
    ).items():
        if not mcp_server_enabled(config) or is_platform_mcp_server(config):
            continue
        if isinstance(config, dict) and str(config.get("command") or "").strip():
            continue
        actual_url = extract_mcp_server_url(config)
        if not actual_url or is_in_sandbox_loopback_mcp_url(actual_url):
            continue
        try:
            actual_key = _normalized_target(actual_url)
        except APIError as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"sandbox MCP server {server_name!r} has an invalid URL",
                status_code=500,
            ) from exc
        destination_scope = _request_destination_scope(actual_url)
        group = groups.setdefault(
            destination_scope,
            {
                "actual_url": actual_url,
                "actual_targets": set(),
                "configured_headers": set(),
                "headers": {},
                "providers": set(),
                "targets": [],
                "transports": set(),
            },
        )
        group["actual_targets"].add(actual_key)
        group["transports"].add(runtime_mcp_transport_type(config))
        configured_headers = config.get("headers") if isinstance(config, dict) else None
        if configured_headers is not None:
            if not isinstance(configured_headers, dict):
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message=(
                        f"sandbox MCP server {server_name!r} headers must be an object"
                    ),
                    status_code=500,
                )
            for header_name in configured_headers:
                name = str(header_name or "").strip()
                if name:
                    group["configured_headers"].add(name.lower())
        target_url = (
            str(config.get("credential_target_url") or "").strip()
            if isinstance(config, dict)
            else ""
        ) or actual_url
        try:
            target_key = _normalized_target(target_url)
        except APIError as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"sandbox MCP server {server_name!r} has an invalid "
                    "credential_target_url"
                ),
                status_code=500,
            ) from exc
        if target_key not in group["targets"]:
            group["targets"].append(target_key)
        if target_key not in lookup_targets:
            lookup_targets.append(target_key)

        provider_name = (
            str(config.get("provider") or "").strip().lower()
            if isinstance(config, dict)
            else ""
        )
        if provider_name:
            group["providers"].add(provider_name)
            gateway = extension_provider_for_name(
                provider_name
            ).mcp_gateway_credential()
            if gateway is not None:
                _validate_gateway_destination(
                    provider_name=provider_name,
                    actual_url=actual_url,
                    gateway=gateway,
                )
                header, value = _gateway_header_value(
                    str(gateway.header), str(gateway.value)
                )
                _claim_header(
                    group["headers"],
                    name=header,
                    value=value,
                    source=f"provider:{provider_name}",
                    destination=actual_url,
                )

    if not groups:
        return None

    resolution = await credential_resolver(lookup_targets)
    scope_id = str(getattr(resolution, "scope_id", "") or "").strip()
    if not scope_id:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="MCP credential resolution returned no platform Vault scope",
            status_code=500,
        )
    by_target: dict[str, Any] = {}
    for item in resolution.credentials:
        credential_id = str(getattr(item, "credential_id", "") or "").strip()
        if not credential_id:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="MCP credential resolution returned an unnamed credential",
                status_code=500,
            )
        by_target[_normalized_target(str(item.target_url))] = item
    for group in groups.values():
        matched_targets: list[str] = []
        for target in group["targets"]:
            credential = by_target.get(target)
            if credential is None:
                continue
            matched_targets.append(target)
            for header, value in credential.headers.items():
                _claim_header(
                    group["headers"],
                    name=str(header),
                    value=str(value),
                    source=f"credential:{credential.credential_id}",
                    destination=str(group["actual_url"]),
                )
        if len(matched_targets) > 1:
            raise APIError(
                code="VAULT_CREDENTIAL_CONFLICT",
                message=(
                    f"MCP destination {group['actual_url']!r} resolves more than "
                    "one upstream credential target; an outbound request cannot "
                    "identify which configured MCP alias selected it"
                ),
                status_code=409,
            )

    credentials: list[MCPHeaderEgressCredential] = []
    for destination_scope, group in groups.items():
        headers = {
            display: value
            for display, value, _sources in group["headers"].values()
        }
        overlapping_headers = sorted(
            display
            for display in headers
            if display.lower() in group["configured_headers"]
        )
        if overlapping_headers:
            raise APIError(
                code="VAULT_CREDENTIAL_CONFLICT",
                message=(
                    f"MCP destination {group['actual_url']!r} declares request "
                    "headers that are also owned by its egress credential: "
                    + ", ".join(overlapping_headers)
                ),
                status_code=409,
            )
        header_sources = {
            display: "\0".join(sorted(sources))
            for display, _value, sources in group["headers"].values()
        }
        if headers and not vault_enabled:
            raise APIError(
                code="SANDBOX_CREDENTIAL_VAULT_DISABLED",
                message=(
                    "This Agent or Assistant has an authenticated MCP server, "
                    "which requires protected egress delivery. Set "
                    "ASTRABOX_SANDBOX_CREDENTIAL_VAULT=1, or remove that server "
                    "credential from the managed binding."
                ),
                status_code=400,
            )
        if not vault_enabled:
            continue
        destination_id = hashlib.sha256(
            destination_scope.encode("utf-8")
        ).hexdigest()[:24]
        scope_payload = json.dumps(
            {
                "session_vault_scope": scope_id,
                "actual_targets": sorted(group["actual_targets"]),
                "credential_targets": sorted(group["targets"]),
                "providers": sorted(group["providers"]),
                "transports": sorted(group["transports"]),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        source_payload = json.dumps(
            {
                name.lower(): source
                for name, source in sorted(header_sources.items())
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        scope_hash = hashlib.sha256(scope_payload.encode("utf-8")).hexdigest()[:16]
        source_hash = hashlib.sha256(source_payload.encode("utf-8")).hexdigest()[:16]
        binding_name = (
            f"astrabox-mcp-{destination_id}-v-{scope_hash}-i-{source_hash}"
        )
        credentials.append(
            MCPHeaderEgressCredential(
                name=binding_name,
                server_url=str(group["actual_url"]),
                headers=headers,
                header_sources=header_sources,
                transports=tuple(sorted(group["transports"])),
            )
        )
    # ``None`` means this template has no direct MCP destination. A plan with
    # an empty-header entry means it does, and gives a provider enough identity
    # to remove a binding whose credential was archived.
    return SandboxEgressCredentialPlan(mcp=tuple(credentials))


async def resolve_mcp_credential_plan(
    platform: Any,
    *,
    session_id: str,
    template: Any,
    vault_enabled: bool,
) -> SandboxEgressCredentialPlan | None:
    """Resolve direct MCP credentials from one persisted Session snapshot."""

    if platform is None:
        raise TypeError(
            "resolve_mcp_credential_plan requires a Session platform; "
            "prepared runtimes use resolve_agent_mcp_credential_plan"
        )

    async def resolve(
        targets: list[str],
    ) -> MCPOutboundCredentialResolution:
        return await platform.resolve_session_mcp_credentials(session_id, targets)

    return await _resolve_mcp_credential_plan(
        template=template,
        vault_enabled=vault_enabled,
        credential_resolver=resolve,
    )


async def resolve_agent_mcp_credential_plan(
    template: Any,
    *,
    vault_enabled: bool,
) -> SandboxEgressCredentialPlan | None:
    """Resolve a prepared runtime's MCP plan from its owning Agent binding."""

    vault_ids = [
        str(item).strip()
        for item in (getattr(template, "credential_vault_ids", None) or [])
        if str(item or "").strip()
    ]

    async def resolve(
        targets: list[str],
    ) -> MCPOutboundCredentialResolution:
        credentials: list[Any] = []
        if targets and vault_ids:
            from astrabox.core.service.orchestrator.vault_service import VaultService

            credentials = await VaultService().resolve_mcp_credentials(
                vault_ids,
                targets,
            )
        return MCPOutboundCredentialResolution(
            scope_id=mcp_vault_scope_id(vault_ids),
            credentials=tuple(credentials),
        )

    return await _resolve_mcp_credential_plan(
        template=template,
        vault_enabled=vault_enabled,
        credential_resolver=resolve,
    )


def mcp_credential_refresher(
    platform: Any,
    *,
    session_id: str,
    template: Any,
    backend_adapter: Any,
    sandbox: Any,
    initial_credential_plan: SandboxEgressCredentialPlan,
    vault_enabled: bool,
) -> Callable[[], Awaitable[None]]:
    """Return the per-root-turn refresh for one running sandbox sidecar."""

    managed_binding_names = {
        item.name
        for item in initial_credential_plan.mcp
        if item.name.startswith("astrabox-mcp-")
    }

    async def refresh() -> None:
        plan = await resolve_mcp_credential_plan(
            platform,
            session_id=session_id,
            template=template,
            vault_enabled=vault_enabled,
        )
        if vault_enabled and plan is not None and not plan.is_empty:
            await backend_adapter.apply_credential_vault(
                sandbox,
                vault_write=plan,
                managed_binding_names=tuple(sorted(managed_binding_names)),
                create_if_missing=False,
            )
            managed_binding_names.update(
                item.name
                for item in plan.mcp
                if item.name.startswith("astrabox-mcp-")
            )

    return refresh


__all__ = [
    "mcp_vault_scope_id",
    "mcp_credential_refresher",
    "resolve_agent_mcp_credential_plan",
    "resolve_mcp_credential_plan",
]
