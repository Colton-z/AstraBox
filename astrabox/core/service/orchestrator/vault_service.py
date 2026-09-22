"""Administrator-managed credential stores for agent sessions.

Mirrors the Claude Managed Agents vault semantics on the self-hosted runtime:

* A **vault** is an organization-scoped collection of credentials maintained by
  administrators. Agent and Assistant management binds Vaults ahead of time;
  conversation callers never see or choose ``vault_ids``. Where several bound
  Vaults carry a matching credential, the **first vault with a match wins**.
* **Credential types** (the ``auth.type`` field):
  - ``mcp_oauth`` — OAuth for an MCP server (keyed by immutable
    ``mcp_server_url``), with optional automatic refresh (token endpoint +
    client id + auth method); the access token is refreshed on use when expired.
  - ``static_bearer`` — a fixed bearer token for an MCP server (same key).
  - ``mcp_static_header`` — a fixed value for one named MCP request header
    (same key), for services that authenticate with headers such as
    ``apikey`` instead of ``Authorization``.
  - ``environment_variable`` — a secret keyed by immutable ``secret_name``,
    scoped by ``networking.allowed_hosts`` and ``injection_location``
    (header/body). Requires a sandbox backend with egress credential
    substitution (``SandboxProvider.supports_egress_credential_injection``);
    the built-in local backend does not support it yet — session create fails
    loud instead of silently shipping the secret into the sandbox.
* **Structural secrecy**: sensitive values never live in metadata rows and are
  never returned by any API (write-only). MCP credentials are written to the
  sandbox egress sidecar, which attaches the resolved request headers only to
  the matching destination, so the sandbox and model context never hold them.
* **Lifecycle**: key fields are immutable after create (rotate by archiving and
  re-creating); secret payloads and descriptive fields are mutable;
  ``injection_location`` updates merge per-field. **Archive** purges secret
  payloads from the secret store but keeps the metadata for audit; **delete**
  removes everything. A running engine re-resolves MCP credentials before its
  next root input, so rotation does not require a sandbox restart.

Storage split: metadata in :class:`VaultRepository`; values in the
:class:`astrabox.seams.secrets.SecretStore` (``local`` encrypted-at-rest by
default; an enterprise store plugs in at the ``astrabox.providers.secrets``
entry-point group).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from astrabox.persistence.repository.vault_repository import VaultRepository
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso, utcnow, utcnow_iso
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.seams.egress_credentials import (
    EgressCredential,
    HTTPBasicEgressCredential,
    HTTPBasicEgressCredentialSet,
    MCPOutboundCredential,
    mint_placeholder,
)
from astrabox.seams.secrets import secret_store_for_name

logger = get_logger(__name__)

MAX_CREDENTIALS_PER_VAULT = 20

#: Credential auth types (mirrors the managed-agents vault vocabulary).
AUTH_TYPE_MCP_OAUTH = "mcp_oauth"
AUTH_TYPE_STATIC_BEARER = "static_bearer"
AUTH_TYPE_MCP_STATIC_HEADER = "mcp_static_header"
AUTH_TYPE_ENVIRONMENT_VARIABLE = "environment_variable"
AUTH_TYPE_HTTP_BASIC = "http_basic"
_MCP_AUTH_TYPES = (
    AUTH_TYPE_MCP_OAUTH,
    AUTH_TYPE_STATIC_BEARER,
    AUTH_TYPE_MCP_STATIC_HEADER,
)
_ALL_AUTH_TYPES = (*_MCP_AUTH_TYPES, AUTH_TYPE_ENVIRONMENT_VARIABLE, AUTH_TYPE_HTTP_BASIC)
_EGRESS_AUTH_TYPES = (AUTH_TYPE_ENVIRONMENT_VARIABLE, AUTH_TYPE_HTTP_BASIC)

#: OAuth refresh auth methods.
_REFRESH_AUTH_METHODS = ("client_secret_basic", "client_secret_post", "none")

#: Seconds of clock skew under which an access token counts as expired.
_EXPIRY_SKEW_SECONDS = 60

_HTTP_METHOD = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_STATIC_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_MAX_REQUEST_MATCH_VALUES = 50

#: Secret-store field slots per credential type (everything sensitive).
_SECRET_FIELDS = {
    AUTH_TYPE_MCP_OAUTH: ("access_token", "refresh_token", "client_secret"),
    AUTH_TYPE_STATIC_BEARER: ("token",),
    AUTH_TYPE_MCP_STATIC_HEADER: ("value",),
    AUTH_TYPE_ENVIRONMENT_VARIABLE: ("secret_value",),
    AUTH_TYPE_HTTP_BASIC: ("password",),
}


def _invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def _not_found(kind: str, ident: str) -> APIError:
    return APIError(code=f"{kind.upper()}_NOT_FOUND", message=f"{kind} '{ident}' not found", status_code=404)


def normalize_mcp_server_url(url: str) -> str:
    """Match key for MCP server URLs: lowercase scheme+host, no trailing slash."""
    parts = urlsplit(str(url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise _invalid(f"mcp_server_url must be an absolute URL, got {url!r}")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )


class VaultService:
    """Vault + credential CRUD, and credential resolution for the proxies."""

    def __init__(
        self,
        *,
        vault_repo: VaultRepository | None = None,
        secret_store_name: str | None = None,
    ) -> None:
        self._repo = vault_repo or VaultRepository()
        self._secret_store_name = secret_store_name

    # The store is resolved per use (not captured at construction) so a
    # provider registered after service construction still wins by name.
    def _store(self):
        return secret_store_for_name(self._secret_store_name)

    @staticmethod
    def _scope(vault_id: str) -> str:
        return f"vault/{vault_id}"

    # ── vaults ──────────────────────────────────────────────────────────────

    async def create_vault(
        self,
        user: UserContext,
        *,
        display_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = str(display_name or "").strip()
        if not name:
            raise _invalid("display_name is required")
        if metadata is not None and not isinstance(metadata, dict):
            raise _invalid("metadata must be an object")
        doc = await self._repo.create_vault(
            {
                "org_id": getattr(user, "org_id", None) or default_org_id(),
                "created_by": user.user_id,
                "display_name": name,
                "metadata": dict(metadata or {}),
            }
        )
        return self._vault_view(doc)

    async def list_vaults(self, user: UserContext) -> list[dict[str, Any]]:
        docs = await self._repo.list_vaults(
            org_id=getattr(user, "org_id", None) or default_org_id()
        )
        views: list[dict[str, Any]] = []
        for doc in docs:
            view = self._vault_view(doc)
            credentials = await self._repo.list_credentials(
                str(doc.get("vault_id") or "")
            )
            view["credentials"] = [
                self._credential_view(credential)
                for credential in credentials
                if not credential.get("archived_at")
            ]
            views.append(view)
        return views

    async def get_vault(self, user: UserContext, vault_id: str) -> dict[str, Any]:
        return self._vault_view(await self._must_access_vault(user, vault_id))

    async def archive_vault(self, user: UserContext, vault_id: str) -> dict[str, Any]:
        """Purge every secret payload; keep metadata (read-only) for audit."""
        vault = await self._must_access_vault(user, vault_id)
        await self._store().purge_scope(scope=self._scope(vault_id))
        stamp = utcnow_iso()
        for credential in await self._repo.list_credentials(vault_id):
            if not credential.get("archived_at"):
                await self._repo.update_credential(
                    str(credential["credential_id"]), {"archived_at": stamp}
                )
        await self._repo.update_vault(vault_id, {"archived_at": stamp})
        vault["archived_at"] = stamp
        return self._vault_view(vault)

    async def delete_vault(self, user: UserContext, vault_id: str) -> None:
        """Hard delete: secrets, credentials, and the vault row (no audit trail)."""
        await self._must_access_vault(user, vault_id)
        await self._store().purge_scope(scope=self._scope(vault_id))
        await self._repo.delete_vault_credentials(vault_id)
        await self._repo.delete_vault(vault_id)

    # ── credentials ─────────────────────────────────────────────────────────

    async def create_credential(
        self,
        user: UserContext,
        vault_id: str,
        *,
        display_name: str | None = None,
        auth: dict[str, Any],
    ) -> dict[str, Any]:
        vault = await self._must_access_vault(user, vault_id)
        if vault.get("archived_at"):
            raise _invalid(f"vault '{vault_id}' is archived (read-only)")
        if not isinstance(auth, dict):
            raise _invalid("auth must be an object")
        auth_type = str(auth.get("type") or "").strip()
        if auth_type not in _ALL_AUTH_TYPES:
            raise _invalid(f"auth.type must be one of {list(_ALL_AUTH_TYPES)}")
        if auth_type in _EGRESS_AUTH_TYPES:
            # Capability-gate the API surface: this credential type only works
            # on a backend with egress credential substitution. With none
            # registered, creating it could never lead to a working session —
            # fail at create with the reason, not later at session attach.
            from astrabox.seams.sandbox import (
                any_backend_supports_egress_credential_injection,
            )

            if not any_backend_supports_egress_credential_injection():
                raise APIError(
                    code="VAULT_ENV_CREDENTIAL_UNSUPPORTED",
                    message=(
                        "environment_variable/http_basic credentials need a sandbox backend "
                        "with egress credential substitution "
                        "(SandboxProvider.supports_egress_credential_injection), "
                        "and no registered backend supports it in this deployment. "
                        "Use mcp_oauth/static_bearer/mcp_static_header credentials "
                        "with an egress-capable sandbox backend, or install a "
                        "backend plugin with an egress credential proxy."
                    ),
                    status_code=400,
                )

        existing = [
            c for c in await self._repo.list_credentials(vault_id) if not c.get("archived_at")
        ]
        if len(existing) >= MAX_CREDENTIALS_PER_VAULT:
            raise _invalid(
                f"vault '{vault_id}' already holds {MAX_CREDENTIALS_PER_VAULT} active "
                "credentials (the per-vault maximum)"
            )

        meta_auth, secret_values = self._split_auth_payload(auth_type, auth)
        self._require_unique_key(existing, auth_type, meta_auth)

        credential = await self._repo.create_credential(
            {
                "vault_id": vault_id,
                "display_name": str(display_name or "").strip() or None,
                "auth": meta_auth,
            }
        )
        credential_id = str(credential["credential_id"])
        for field, value in secret_values.items():
            await self._store().put(
                scope=self._scope(vault_id), key=f"{credential_id}/{field}", value=value
            )
        return self._credential_view(credential)

    async def list_credentials(self, user: UserContext, vault_id: str) -> list[dict[str, Any]]:
        await self._must_access_vault(user, vault_id)
        return [self._credential_view(c) for c in await self._repo.list_credentials(vault_id)]

    async def update_credential(
        self,
        user: UserContext,
        vault_id: str,
        credential_id: str,
        *,
        display_name: str | None = None,
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._must_access_vault(user, vault_id)
        credential = await self._must_get_credential(vault_id, credential_id)
        if credential.get("archived_at"):
            raise _invalid(f"credential '{credential_id}' is archived (read-only)")

        meta_auth = dict(credential.get("auth") or {})
        auth_type = str(meta_auth.get("type") or "")
        updates: dict[str, Any] = {}
        if display_name is not None:
            updates["display_name"] = str(display_name).strip() or None

        if auth is not None:
            if not isinstance(auth, dict):
                raise _invalid("auth must be an object")
            for immutable in (
                "type",
                "mcp_server_url",
                "header_name",
                "secret_name",
                "url",
                "username",
                "token_endpoint",
                "client_id",
            ):
                incoming = auth.get(immutable)
                if incoming is None:
                    continue
                current = meta_auth.get(immutable) or (meta_auth.get("refresh") or {}).get(immutable)
                if str(incoming) != str(current or ""):
                    raise _invalid(
                        f"auth.{immutable} is immutable after create — archive the "
                        "credential and create a new one to change it"
                    )
            # Rotate secret payloads (write-only fields).
            new_meta, secret_values = self._split_auth_payload(
                auth_type, {**meta_auth, **auth, "type": auth_type}, partial=True
            )
            # injection_location merges per-field; networking replaces whole.
            if "injection_location" in auth:
                merged = dict(meta_auth.get("injection_location") or {})
                merged.update(dict(auth.get("injection_location") or {}))
                new_meta["injection_location"] = self._validate_injection_location(merged)
            updates["auth"] = new_meta
            for field, value in secret_values.items():
                await self._store().put(
                    scope=self._scope(vault_id), key=f"{credential_id}/{field}", value=value
                )

        if updates:
            await self._repo.update_credential(credential_id, updates)
        refreshed = await self._must_get_credential(vault_id, credential_id)
        return self._credential_view(refreshed)

    async def archive_credential(
        self, user: UserContext, vault_id: str, credential_id: str
    ) -> dict[str, Any]:
        await self._must_access_vault(user, vault_id)
        credential = await self._must_get_credential(vault_id, credential_id)
        auth_type = str((credential.get("auth") or {}).get("type") or "")
        for field in _SECRET_FIELDS.get(auth_type, ()):
            await self._store().delete(
                scope=self._scope(vault_id), key=f"{credential_id}/{field}"
            )
        stamp = utcnow_iso()
        await self._repo.update_credential(credential_id, {"archived_at": stamp})
        credential["archived_at"] = stamp
        return self._credential_view(credential)

    async def delete_credential(
        self, user: UserContext, vault_id: str, credential_id: str
    ) -> None:
        await self._must_access_vault(user, vault_id)
        credential = await self._must_get_credential(vault_id, credential_id)
        auth_type = str((credential.get("auth") or {}).get("type") or "")
        for field in _SECRET_FIELDS.get(auth_type, ()):
            await self._store().delete(
                scope=self._scope(vault_id), key=f"{credential_id}/{field}"
            )
        await self._repo.delete_credential(credential_id)

    # ── session attach validation ───────────────────────────────────────────

    async def validate_binding_ids(
        self,
        user: UserContext,
        vault_ids: list[str],
        *,
        target_type: str,
    ) -> list[str]:
        """Validate an administrator's ordered binding for one managed target."""
        if target_type not in {"agent", "assistant"}:
            raise _invalid("target_type must be 'agent' or 'assistant'")
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in vault_ids:
            vault_id = str(raw or "").strip()
            if not vault_id or vault_id in seen:
                continue
            vault = await self._must_access_vault(user, vault_id)
            if vault.get("archived_at"):
                raise _invalid(f"vault '{vault_id}' is archived and cannot be bound")
            if target_type == "assistant":
                for credential in await self._repo.list_credentials(vault_id):
                    auth = credential.get("auth") or {}
                    if (
                        not credential.get("archived_at")
                        and str(auth.get("type") or "")
                        in _EGRESS_AUTH_TYPES
                    ):
                        raise APIError(
                            code="ASSISTANT_VAULT_CREDENTIAL_UNSUPPORTED",
                            message=(
                                f"vault '{vault_id}' contains an outbound "
                                f"credential of type '{auth.get('type')}'. Assistants "
                                "reuse a long-running workspace, so only MCP credentials "
                                "can be bound to an Assistant."
                            ),
                            status_code=400,
                        )
            cleaned.append(vault_id)
            seen.add(vault_id)
        return cleaned

    async def describe_binding(
        self, user: UserContext, vault_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Return ordered, secret-free Vault summaries for the admin console."""
        result: list[dict[str, Any]] = []
        for vault_id in vault_ids:
            vault = await self._must_access_vault(
                user, str(vault_id or "").strip()
            )
            view = self._vault_view(vault)
            view["credentials"] = [
                self._credential_view(item)
                for item in await self._repo.list_credentials(
                    str(vault["vault_id"])
                )
            ]
            result.append(view)
        return result

    async def validate_bound_vaults_for_session(
        self,
        vault_ids: list[str],
        *,
        backend_name: str,
        backend_supports_egress_injection: bool,
        egress_injection_unsupported_reason: str | None = None,
    ) -> list[str]:
        """Validate current managed bindings when a Session is created.

        Authorization was checked when an administrator saved the binding. This
        runtime check does not consult the conversation user's identity; it only
        verifies that each Vault still exists, remains active, and can be safely
        delivered by the selected sandbox backend.
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        from astrabox.common.utils.settings import load_astrabox_settings

        vault_enabled = bool(
            getattr(
                load_astrabox_settings(),
                "sandbox_credential_vault_enabled",
                False,
            )
        )
        for raw in vault_ids:
            vault_id = str(raw or "").strip()
            if not vault_id or vault_id in seen:
                continue
            vault = await self._repo.get_vault(vault_id)
            if not vault or vault.get("archived_at"):
                raise _invalid(
                    f"managed credential vault '{vault_id}' is missing or archived"
                )
            for credential in await self._repo.list_credentials(vault_id):
                if credential.get("archived_at"):
                    continue
                auth = credential.get("auth") or {}
                if str(auth.get("type") or "") not in _EGRESS_AUTH_TYPES:
                    continue
                if not vault_enabled:
                    raise APIError(
                        code="VAULT_ENV_CREDENTIAL_UNSUPPORTED",
                        message=(
                            f"managed credential vault '{vault_id}' contains an "
                            "outbound credential, which requires protected "
                            "delivery. Set ASTRABOX_SANDBOX_CREDENTIAL_VAULT=1 or "
                            "remove that credential from the managed binding."
                        ),
                        status_code=400,
                    )
                if not backend_supports_egress_injection:
                    raise APIError(
                        code="VAULT_ENV_CREDENTIAL_UNSUPPORTED",
                        message=(
                            egress_injection_unsupported_reason
                            or (
                                f"managed credential vault '{vault_id}' contains "
                                f"outbound credential of type '{auth.get('type')}', but "
                                f"sandbox backend '{backend_name}' cannot keep its real "
                                "value outside the sandbox."
                            )
                        ),
                        status_code=400,
                    )
            cleaned.append(vault_id)
            seen.add(vault_id)
        return cleaned

    # ── egress-side resolution ──────────────────────────────────────────────

    async def resolve_mcp_headers(
        self, vault_ids: list[str], server_url: str
    ) -> dict[str, str]:
        """Request headers for an upstream MCP server.

        No matching credential returns an empty map, which means an
        unauthenticated attempt.

        First vault with a match wins (attach order). The runtime calls this
        before each root input, so a rotated credential takes effect without a
        sandbox restart. An expired ``mcp_oauth`` access token is refreshed here when
        the credential carries refresh configuration; an expired token
        without one raises here, since forwarding it would only 401
        opaquely upstream.
        """
        resolved = await self.resolve_mcp_credentials(vault_ids, [server_url])
        return dict(resolved[0].headers) if resolved else {}

    async def resolve_mcp_credentials(
        self,
        vault_ids: list[str],
        server_urls: list[str],
    ) -> list[MCPOutboundCredential]:
        """Resolve the first-vault match for each requested MCP destination.

        This is the egress-side shape used by direct sandbox MCP clients. It
        preserves the credential id and normalized lookup URL so an engine can
        map a provider's upstream credential onto the gateway URL it actually
        dials without putting either the header or its value in engine config.
        Resolution is intentionally fresh: static rotations and OAuth refresh
        are picked up when the runtime prepares the next turn.
        """

        targets: list[str] = []
        for server_url in server_urls:
            try:
                target = normalize_mcp_server_url(server_url)
            except APIError:
                continue
            if target not in targets:
                targets.append(target)

        resolved: list[MCPOutboundCredential] = []
        for target in targets:
            match: MCPOutboundCredential | None = None
            for raw in vault_ids or []:
                vault_id = str(raw or "").strip()
                if not vault_id:
                    continue
                for credential in await self._repo.list_credentials(vault_id):
                    if credential.get("archived_at"):
                        continue
                    auth = credential.get("auth") or {}
                    auth_type = str(auth.get("type") or "")
                    if auth_type not in _MCP_AUTH_TYPES:
                        continue
                    if (
                        normalize_mcp_server_url(
                            str(auth.get("mcp_server_url") or "")
                        )
                        != target
                    ):
                        continue
                    match = MCPOutboundCredential(
                        credential_id=str(credential.get("credential_id") or ""),
                        target_url=target,
                        headers=await self._headers_for(vault_id, credential),
                    )
                    break
                if match is not None:
                    break
            if match is not None:
                resolved.append(match)
        return resolved

    async def resolve_env_credentials(
        self,
        vault_ids: list[str],
        *,
        placeholder_context: str | None = None,
    ) -> list[EgressCredential]:
        """Every ``environment_variable`` credential these vaults carry.

        The egress-side view: the real value, plus an opaque placeholder. With
        a private per-sandbox ``placeholder_context``, repeated resolution for
        that live box returns the same placeholder (the runner and a later
        terminal therefore address the same Vault entry). A new sandbox
        generation changes the placeholder, so one recovered from an old
        transcript or stale box matches nothing in the replacement box. Calls
        without a context preserve the fresh-per-call behavior.

        Two secrets claiming one ``secret_name`` raise instead of one silently
        winning: the box has a single environment, and a credential that lost a
        race it could not see would authenticate as the wrong identity.
        """
        resolved: list[EgressCredential] = []
        claimed: dict[str, str] = {}
        for raw in vault_ids or []:
            vault_id = str(raw or "").strip()
            if not vault_id:
                continue
            for credential in await self._repo.list_credentials(vault_id):
                if credential.get("archived_at"):
                    continue
                auth = dict(credential.get("auth") or {})
                if str(auth.get("type") or "") != AUTH_TYPE_ENVIRONMENT_VARIABLE:
                    continue
                credential_id = str(credential["credential_id"])
                secret_name = str(auth.get("secret_name") or "").strip()
                if secret_name in claimed:
                    raise APIError(
                        code="VAULT_CREDENTIAL_CONFLICT",
                        message=(
                            f"credentials {claimed[secret_name]!r} and {credential_id!r} "
                            f"both claim environment variable {secret_name!r}; the sandbox "
                            "has one environment, so detach one of the vaults"
                        ),
                        status_code=409,
                    )
                claimed[secret_name] = credential_id
                secret_value = await self._store().get(
                    scope=self._scope(vault_id), key=f"{credential_id}/secret_value"
                )
                if not secret_value:
                    raise APIError(
                        code="VAULT_CREDENTIAL_UNAVAILABLE",
                        message=(
                            f"vault credential {credential_id!r} has no secret_value payload"
                        ),
                        status_code=502,
                    )
                resolved.append(
                    EgressCredential(
                        credential_id=credential_id,
                        secret_name=secret_name,
                        secret_value=secret_value,
                        placeholder=mint_placeholder(
                            credential_id,
                            context=placeholder_context,
                        ),
                        networking=dict(auth.get("networking") or {}),
                        injection_location=dict(auth.get("injection_location") or {}),
                        allow_insecure_http=bool(auth.get("allow_insecure_http", False)),
                        allowed_requests=dict(auth.get("allowed_requests") or {}),
                    )
                )
        return resolved

    async def resolve_http_basic_credentials(
        self, vault_ids: list[str]
    ) -> list[HTTPBasicEgressCredential]:
        """Resolve HTTPS authentication without putting secrets in the workload.

        The first bound Vault with an exact destination wins, as for MCP.
        Overlapping destination paths remain subject to the provider's matching
        rules; the platform never guesses a credential from a repository URL.
        """
        resolved: dict[str, HTTPBasicEgressCredential] = {}
        for vault_id in vault_ids:
            for credential in await self._repo.list_credentials(vault_id):
                auth = credential.get("auth") or {}
                if credential.get("archived_at") or auth.get("type") != AUTH_TYPE_HTTP_BASIC:
                    continue
                url = str(auth["url"])
                if url in resolved:
                    continue
                credential_id = str(credential["credential_id"])
                password = await self._store().get(
                    scope=self._scope(vault_id), key=f"{credential_id}/password"
                )
                if not password:
                    raise APIError(
                        code="VAULT_CREDENTIAL_UNAVAILABLE",
                        message=f"vault credential {credential_id!r} has no password payload",
                        status_code=502,
                    )
                resolved[url] = HTTPBasicEgressCredential(
                    credential_id=credential_id,
                    url=url,
                    username=str(auth["username"]),
                    password=password,
                )
        return list(resolved.values())

    async def resolve_egress_credentials(
        self, vault_ids: list[str], *, placeholder_context: str | None = None
    ) -> list[EgressCredential | HTTPBasicEgressCredentialSet]:
        """Resolve the same assigned outbound credentials for cold and warm boxes."""
        from astrabox.core.service.orchestrator.runtime.mcp_credentials import mcp_vault_scope_id

        return [
            *await self.resolve_env_credentials(vault_ids, placeholder_context=placeholder_context),
            HTTPBasicEgressCredentialSet(
                scope_id=mcp_vault_scope_id(vault_ids),
                credentials=tuple(await self.resolve_http_basic_credentials(vault_ids)),
            ),
        ]

    async def _headers_for(
        self, vault_id: str, credential: dict[str, Any]
    ) -> dict[str, str]:
        credential_id = str(credential["credential_id"])
        auth = dict(credential.get("auth") or {})
        scope = self._scope(vault_id)

        if auth.get("type") == AUTH_TYPE_STATIC_BEARER:
            token = await self._store().get(scope=scope, key=f"{credential_id}/token")
            if not token:
                raise APIError(
                    code="VAULT_CREDENTIAL_UNAVAILABLE",
                    message=f"vault credential '{credential_id}' has no token payload",
                    status_code=502,
                )
            return {"Authorization": f"Bearer {token}"}

        if auth.get("type") == AUTH_TYPE_MCP_STATIC_HEADER:
            value = await self._store().get(
                scope=scope, key=f"{credential_id}/value"
            )
            if not value:
                raise APIError(
                    code="VAULT_CREDENTIAL_UNAVAILABLE",
                    message=f"vault credential '{credential_id}' has no value payload",
                    status_code=502,
                )
            return {str(auth["header_name"]): value}

        # mcp_oauth: refresh-on-use when expired.
        expires_at = str(auth.get("expires_at") or "").strip()
        expired = False
        if expires_at:
            try:
                expired = parse_iso(expires_at) <= utcnow() + timedelta(seconds=_EXPIRY_SKEW_SECONDS)
            except ValueError:
                expired = True
        if expired:
            await self._refresh_oauth(vault_id, credential)
        token = await self._store().get(scope=scope, key=f"{credential_id}/access_token")
        if not token:
            raise APIError(
                code="VAULT_CREDENTIAL_UNAVAILABLE",
                message=f"vault credential '{credential_id}' has no access token payload",
                status_code=502,
            )
        return {"Authorization": f"Bearer {token}"}

    async def _refresh_oauth(self, vault_id: str, credential: dict[str, Any]) -> None:
        credential_id = str(credential["credential_id"])
        auth = dict(credential.get("auth") or {})
        refresh = dict(auth.get("refresh") or {})
        token_endpoint = str(refresh.get("token_endpoint") or "").strip()
        scope_key = self._scope(vault_id)
        if not token_endpoint:
            raise APIError(
                code="VAULT_CREDENTIAL_EXPIRED",
                message=(
                    f"vault credential '{credential_id}' access token is expired and no "
                    "refresh configuration is set — rotate the credential"
                ),
                status_code=502,
            )
        refresh_token = await self._store().get(
            scope=scope_key, key=f"{credential_id}/refresh_token"
        )
        if not refresh_token:
            raise APIError(
                code="VAULT_CREDENTIAL_EXPIRED",
                message=f"vault credential '{credential_id}' is expired and has no refresh token",
                status_code=502,
            )

        auth_method = str(refresh.get("auth_method") or "none").strip()
        client_id = str(refresh.get("client_id") or "").strip()
        data: dict[str, str] = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        if str(refresh.get("scope") or "").strip():
            data["scope"] = str(refresh["scope"]).strip()
        request_auth = None
        if auth_method == "client_secret_basic":
            client_secret = await self._store().get(
                scope=scope_key, key=f"{credential_id}/client_secret"
            )
            request_auth = (client_id, client_secret or "")
        elif auth_method == "client_secret_post":
            client_secret = await self._store().get(
                scope=scope_key, key=f"{credential_id}/client_secret"
            )
            data["client_id"] = client_id
            data["client_secret"] = client_secret or ""
        else:  # public client
            data["client_id"] = client_id

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                response = await client.post(token_endpoint, data=data, auth=request_auth)
        except Exception as exc:
            raise APIError(
                code="VAULT_CREDENTIAL_REFRESH_FAILED",
                message=f"vault credential '{credential_id}' refresh request failed: {exc}",
                status_code=502,
            ) from exc
        if response.status_code >= 400:
            raise APIError(
                code="VAULT_CREDENTIAL_REFRESH_FAILED",
                message=(
                    f"vault credential '{credential_id}' refresh rejected by token endpoint "
                    f"(status={response.status_code}): {response.text[:300]}"
                ),
                status_code=502,
            )
        payload = response.json()
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise APIError(
                code="VAULT_CREDENTIAL_REFRESH_FAILED",
                message=f"vault credential '{credential_id}' refresh returned no access_token",
                status_code=502,
            )
        await self._store().put(
            scope=scope_key, key=f"{credential_id}/access_token", value=access_token
        )
        rotated_refresh = str(payload.get("refresh_token") or "").strip()
        if rotated_refresh:
            await self._store().put(
                scope=scope_key, key=f"{credential_id}/refresh_token", value=rotated_refresh
            )
        expires_in = payload.get("expires_in")
        new_expires_at = None
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            new_expires_at = (utcnow() + timedelta(seconds=int(expires_in))).isoformat()
        auth["expires_at"] = new_expires_at
        await self._repo.update_credential(credential_id, {"auth": auth})
        logger.info(
            "vault credential refreshed: credential=%s expires_at=%s", credential_id, new_expires_at
        )

    # ── payload shaping / validation ────────────────────────────────────────

    def _split_auth_payload(
        self, auth_type: str, auth: dict[str, Any], *, partial: bool = False
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Split an auth payload into (metadata doc, write-only secret values).

        ``partial`` (update path) lets secret fields be absent — absent means
        "keep the stored payload"; present means rotate.
        """
        secrets: dict[str, str] = {}

        def _take_secret(field: str, *, required: bool) -> None:
            value = auth.get(field)
            if value is None or str(value) == "":
                if required and not partial:
                    raise _invalid(f"auth.{field} is required for type={auth_type}")
                return
            secrets[field] = str(value)

        if auth_type == AUTH_TYPE_HTTP_BASIC:
            raw_url = str(auth.get("url") or "").strip()
            try:
                url = urlsplit(raw_url)
                port = url.port
            except ValueError as exc:
                raise _invalid("auth.url is not a valid HTTPS destination") from exc
            path = url.path.rstrip("/")
            if (
                url.scheme != "https" or not url.hostname or port not in (None, 443)
                or url.username is not None or url.password is not None
                or url.query or url.fragment or not path or path == "/"
                or any(c in raw_url for c in ("*", "?", "[", "]", "%", "\\", "\r", "\n", "\t", " "))
                or any(part in (".", "..", "") for part in path.lstrip("/").split("/"))
            ):
                raise _invalid("auth.url must be a clean HTTPS destination with a non-root path")
            username = str(auth.get("username") or "").strip()
            if not username or any(c in username for c in (":", "\r", "\n", "\x00")):
                raise _invalid("auth.username must be non-empty and contain no colon or control characters")
            _take_secret("password", required=True)
            return {
                "type": auth_type,
                "url": urlunsplit(("https", url.hostname.lower(), path, "", "")),
                "username": username,
            }, secrets

        if auth_type == AUTH_TYPE_STATIC_BEARER:
            meta = {
                "type": auth_type,
                "mcp_server_url": normalize_mcp_server_url(str(auth.get("mcp_server_url") or "")),
            }
            _take_secret("token", required=True)
            return meta, secrets

        if auth_type == AUTH_TYPE_MCP_STATIC_HEADER:
            header_name = str(auth.get("header_name") or "").strip()
            if not _HTTP_HEADER_NAME.fullmatch(header_name):
                raise _invalid(
                    "auth.header_name must be a valid HTTP request header name"
                )
            if header_name.lower() in _FORBIDDEN_STATIC_HEADERS:
                raise _invalid(
                    f"auth.header_name {header_name!r} is reserved; use the typed "
                    "OAuth or bearer credential for Authorization, and do not "
                    "override HTTP routing headers"
                )
            meta = {
                "type": auth_type,
                "mcp_server_url": normalize_mcp_server_url(
                    str(auth.get("mcp_server_url") or "")
                ),
                "header_name": header_name,
            }
            _take_secret("value", required=True)
            return meta, secrets

        if auth_type == AUTH_TYPE_MCP_OAUTH:
            meta = {
                "type": auth_type,
                "mcp_server_url": normalize_mcp_server_url(str(auth.get("mcp_server_url") or "")),
                "expires_at": str(auth.get("expires_at") or "").strip() or None,
            }
            _take_secret("access_token", required=True)
            refresh_in = auth.get("refresh")
            if refresh_in is not None:
                if not isinstance(refresh_in, dict):
                    raise _invalid("auth.refresh must be an object")
                auth_method = str(refresh_in.get("auth_method") or "none").strip()
                if auth_method not in _REFRESH_AUTH_METHODS:
                    raise _invalid(f"auth.refresh.auth_method must be one of {list(_REFRESH_AUTH_METHODS)}")
                token_endpoint = str(refresh_in.get("token_endpoint") or "").strip()
                if not token_endpoint:
                    raise _invalid("auth.refresh.token_endpoint is required when refresh is set")
                meta["refresh"] = {
                    "token_endpoint": token_endpoint,
                    "client_id": str(refresh_in.get("client_id") or "").strip(),
                    "auth_method": auth_method,
                    "scope": str(refresh_in.get("scope") or "").strip() or None,
                }
                refresh_token = refresh_in.get("refresh_token")
                if refresh_token:
                    secrets["refresh_token"] = str(refresh_token)
                elif not partial:
                    raise _invalid("auth.refresh.refresh_token is required when refresh is set")
                client_secret = refresh_in.get("client_secret")
                if client_secret:
                    secrets["client_secret"] = str(client_secret)
                elif auth_method in ("client_secret_basic", "client_secret_post") and not partial:
                    raise _invalid(f"auth.refresh.client_secret is required for {auth_method}")
            return meta, secrets

        # environment_variable
        secret_name = str(auth.get("secret_name") or "").strip()
        if not secret_name:
            raise _invalid("auth.secret_name is required for type=environment_variable")
        networking = auth.get("networking")
        if networking is None:
            raise _invalid(
                "auth.networking is required for type=environment_variable "
                '(use {"type": "limited", "allowed_hosts": [...]} — recommended — '
                'or {"type": "unrestricted"})'
            )
        meta = {
            "type": auth_type,
            "secret_name": secret_name,
            "networking": self._validate_networking(networking),
            "injection_location": self._validate_injection_location(
                auth.get("injection_location") or {"header": True, "body": True}
            ),
        }
        if "allowed_requests" in auth:
            allowed_requests = auth.get("allowed_requests")
            if allowed_requests is None and partial:
                # PATCH uses null as an explicit request to remove the optional
                # method/path limit. An empty object is rejected because it
                # looks configured while matching everything.
                pass
            else:
                meta["allowed_requests"] = self._validate_allowed_requests(
                    allowed_requests
                )
        if "allow_insecure_http" in auth:
            allow_insecure_http = auth.get("allow_insecure_http")
            if not isinstance(allow_insecure_http, bool):
                raise _invalid("auth.allow_insecure_http must be a boolean")
            meta["allow_insecure_http"] = allow_insecure_http
        _take_secret("secret_value", required=True)
        return meta, secrets

    @staticmethod
    def _validate_networking(networking: Any) -> dict[str, Any]:
        if not isinstance(networking, dict):
            raise _invalid("auth.networking must be an object")
        kind = str(networking.get("type") or "").strip()
        if kind == "unrestricted":
            return {"type": "unrestricted"}
        if kind == "limited":
            hosts = networking.get("allowed_hosts")
            if not isinstance(hosts, list) or not hosts or any(not str(h).strip() for h in hosts):
                raise _invalid("auth.networking.allowed_hosts must be a non-empty list of hosts")
            return {"type": "limited", "allowed_hosts": [str(h).strip().lower() for h in hosts]}
        raise _invalid('auth.networking.type must be "limited" or "unrestricted"')

    @staticmethod
    def _validate_injection_location(location: Any) -> dict[str, bool]:
        if not isinstance(location, dict):
            raise _invalid("auth.injection_location must be an object")
        header = bool(location.get("header", False))
        body = bool(location.get("body", False))
        if not header and not body:
            raise _invalid("auth.injection_location must enable header and/or body")
        return {"header": header, "body": body}

    @staticmethod
    def _validate_allowed_requests(value: Any) -> dict[str, list[str]]:
        """Validate optional HTTP method/path limits without silently widening them."""
        if not isinstance(value, dict):
            raise _invalid("auth.allowed_requests must be an object")
        unknown = sorted(set(value) - {"methods", "paths"})
        if unknown:
            raise _invalid(
                "auth.allowed_requests contains unknown fields: " + ", ".join(unknown)
            )

        result: dict[str, list[str]] = {}
        if "methods" in value:
            raw_methods = value.get("methods")
            if (
                not isinstance(raw_methods, list)
                or not raw_methods
                or len(raw_methods) > _MAX_REQUEST_MATCH_VALUES
            ):
                raise _invalid(
                    "auth.allowed_requests.methods must be a non-empty list with at "
                    f"most {_MAX_REQUEST_MATCH_VALUES} HTTP methods"
                )
            methods: list[str] = []
            for raw in raw_methods:
                method = str(raw or "").strip().upper()
                if not _HTTP_METHOD.fullmatch(method):
                    raise _invalid(
                        "auth.allowed_requests.methods contains an invalid HTTP method"
                    )
                if method not in methods:
                    methods.append(method)
            result["methods"] = methods

        if "paths" in value:
            raw_paths = value.get("paths")
            if (
                not isinstance(raw_paths, list)
                or not raw_paths
                or len(raw_paths) > _MAX_REQUEST_MATCH_VALUES
            ):
                raise _invalid(
                    "auth.allowed_requests.paths must be a non-empty list with at "
                    f"most {_MAX_REQUEST_MATCH_VALUES} absolute path patterns"
                )
            paths: list[str] = []
            for raw in raw_paths:
                path = str(raw or "").strip()
                if (
                    not path.startswith("/")
                    or "?" in path
                    or "#" in path
                    or any(char.isspace() for char in path)
                ):
                    raise _invalid(
                        "auth.allowed_requests.paths must contain absolute request "
                        "path patterns without a query, fragment, or whitespace"
                    )
                if path not in paths:
                    paths.append(path)
            result["paths"] = paths

        if not result:
            raise _invalid(
                "auth.allowed_requests must contain methods, paths, or both; omit "
                "the field when no HTTP request limit is needed"
            )
        return result

    @staticmethod
    def _require_unique_key(
        existing: list[dict[str, Any]], auth_type: str, meta_auth: dict[str, Any]
    ) -> None:
        """Duplicate ``mcp_server_url`` / ``secret_name`` in one vault → 409."""
        if auth_type in _MCP_AUTH_TYPES:
            key_field, new_key = "mcp_server_url", str(meta_auth.get("mcp_server_url") or "")
        elif auth_type == AUTH_TYPE_HTTP_BASIC:
            key_field, new_key = "url", str(meta_auth.get("url") or "")
        else:
            key_field, new_key = "secret_name", str(meta_auth.get("secret_name") or "")
        for credential in existing:
            auth = credential.get("auth") or {}
            if str(auth.get(key_field) or "") == new_key:
                raise APIError(
                    code="VAULT_CREDENTIAL_CONFLICT",
                    message=(
                        f"vault already holds an active credential for {key_field}="
                        f"{new_key!r}; {key_field} is unique per vault (archive the "
                        "existing credential to replace it)"
                    ),
                    status_code=409,
                )

    # ── views / guards ──────────────────────────────────────────────────────

    async def _must_access_vault(self, user: UserContext, vault_id: str) -> dict[str, Any]:
        vault = await self._repo.get_vault(str(vault_id or "").strip())
        org_id = getattr(user, "org_id", None) or default_org_id()
        if not vault or str(vault.get("org_id") or "") != org_id:
            raise _not_found("vault", vault_id)
        return vault

    async def _must_get_credential(self, vault_id: str, credential_id: str) -> dict[str, Any]:
        credential = await self._repo.get_credential(str(credential_id or "").strip())
        if not credential or str(credential.get("vault_id") or "") != vault_id:
            raise _not_found("vault_credential", credential_id)
        return credential

    @staticmethod
    def _vault_view(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "vault_id": doc.get("vault_id"),
            "display_name": doc.get("display_name"),
            "metadata": dict(doc.get("metadata") or {}),
            "archived_at": doc.get("archived_at"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }

    @staticmethod
    def _credential_view(doc: dict[str, Any]) -> dict[str, Any]:
        """Public credential shape — key fields + rules, NEVER secret payloads."""
        auth = dict(doc.get("auth") or {})
        view_auth: dict[str, Any] = {"type": auth.get("type")}
        for field in (
            "mcp_server_url",
            "header_name",
            "secret_name",
            "url",
            "username",
            "expires_at",
            "networking",
            "injection_location",
            "allowed_requests",
            "allow_insecure_http",
        ):
            if auth.get(field) is not None:
                view_auth[field] = auth[field]
        refresh = auth.get("refresh")
        if isinstance(refresh, dict):
            view_auth["refresh"] = {
                key: refresh.get(key)
                for key in ("token_endpoint", "client_id", "auth_method", "scope")
                if refresh.get(key) is not None
            }
        return {
            "credential_id": doc.get("credential_id"),
            "vault_id": doc.get("vault_id"),
            "display_name": doc.get("display_name"),
            "auth": view_auth,
            "archived_at": doc.get("archived_at"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }
