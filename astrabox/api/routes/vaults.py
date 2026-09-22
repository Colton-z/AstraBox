"""Admin REST surface for managed credentials and runtime bindings.

All Vault CRUD lives below ``/api/v1/admin`` so the identity middleware applies
the admin-role gate. Agent/Assistant bindings are managed here as a separate
policy resource. Ordinary conversation routes never accept ``vault_ids``.

Secret payload fields (``token`` / ``access_token`` / ``refresh_token`` /
``client_secret`` / ``secret_value``) are WRITE-ONLY: accepted on create/update,
never present in any response.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.credential_delivery import (
    credential_delivery_overview,
)
from astrabox.core.service.orchestrator.credential_binding_service import (
    CredentialBindingService,
)
from astrabox.core.service.orchestrator.vault_service import VaultService

_registered_on: int | None = None


class CreateVaultRequest(BaseModel):
    """Body for ``POST /api/v1/admin/vaults``."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    metadata: dict[str, Any] | None = None


class CreateCredentialRequest(BaseModel):
    """Body for ``POST /api/v1/admin/vaults/{vault_id}/credentials``.

    ``auth`` is the (open) credential document — the vault service validates its
    ``type`` and shape and splits out the write-only secret fields.
    """

    model_config = ConfigDict(extra="forbid")

    auth: dict[str, Any]
    display_name: str | None = None


class UpdateCredentialRequest(BaseModel):
    """Body for ``PATCH /api/v1/admin/vaults/{vault_id}/credentials/{credential_id}``."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    auth: dict[str, Any] | None = None


class SetCredentialBindingRequest(BaseModel):
    """Ordered Vault ids selected by an administrator for one runtime."""

    model_config = ConfigDict(extra="forbid")

    vault_ids: list[str]


# ── response payloads ───────────────────────────────────────────────────────
#
# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring a response model are documented in
# :mod:`astrabox.api.routes.response_envelope`.
#
# Field order mirrors the service views, because pydantic serializes in
# declaration order. A credential view emits a key only when the credential
# carries a value for it, which is what the defaults below and
# ``response_model_exclude_unset=True`` on every route together preserve.


class CredentialNetworking(BaseModel):
    """Egress policy for one credential; ``allowed_hosts`` only when limited."""

    model_config = ConfigDict(extra="allow")

    type: str
    allowed_hosts: list[str] | None = None


class CredentialInjectionLocation(BaseModel):
    """Where egress placeholder substitution is allowed; at least one is true."""

    model_config = ConfigDict(extra="allow")

    header: bool
    body: bool


class CredentialAllowedRequests(BaseModel):
    """Method / path limits on the requests a credential may authenticate."""

    model_config = ConfigDict(extra="allow")

    methods: list[str] | None = None
    paths: list[str] | None = None


class CredentialRefresh(BaseModel):
    """Non-secret half of an OAuth refresh configuration."""

    model_config = ConfigDict(extra="allow")

    token_endpoint: str | None = None
    client_id: str | None = None
    auth_method: str | None = None
    scope: str | None = None


class CredentialAuthSummary(BaseModel):
    """Key fields and rules of one credential — never a secret payload."""

    model_config = ConfigDict(extra="allow")

    type: str | None
    mcp_server_url: str | None = None
    header_name: str | None = None
    secret_name: str | None = None
    url: str | None = None
    username: str | None = None
    expires_at: str | None = None
    networking: CredentialNetworking | None = None
    injection_location: CredentialInjectionLocation | None = None
    allowed_requests: CredentialAllowedRequests | None = None
    allow_insecure_http: bool | None = None
    refresh: CredentialRefresh | None = None


class VaultCredential(BaseModel):
    """One saved credential, as ``VaultService._credential_view`` renders it."""

    model_config = ConfigDict(extra="allow")

    credential_id: str | None
    vault_id: str | None
    display_name: str | None
    auth: CredentialAuthSummary
    archived_at: str | None
    created_at: str | None
    updated_at: str | None


class Vault(BaseModel):
    """One Vault. ``credentials`` accompanies the catalog and binding reads."""

    model_config = ConfigDict(extra="allow")

    vault_id: str | None
    display_name: str | None
    metadata: dict[str, Any]
    archived_at: str | None
    created_at: str | None
    updated_at: str | None
    credentials: list[VaultCredential] | None = None


class CredentialDelivery(BaseModel):
    """How saved values reach a sandbox — mechanisms, never the values."""

    model_config = ConfigDict(extra="allow")

    deployment_mode: str
    model_credentials: str
    mcp_credentials: str
    environment_credentials: str


class VaultCatalog(BaseModel):
    """Every Vault in the organization, plus how this deployment delivers them."""

    model_config = ConfigDict(extra="allow")

    vaults: list[Vault]
    credential_delivery: CredentialDelivery


class VaultCredentialList(BaseModel):
    """One Vault's credentials, archived ones included."""

    model_config = ConfigDict(extra="allow")

    credentials: list[VaultCredential]


class CredentialBinding(BaseModel):
    """The Vaults an administrator bound to one Agent or Assistant."""

    model_config = ConfigDict(extra="allow")

    target_type: str
    target_id: str
    vault_ids: list[str]
    vaults: list[Vault]


class VaultBindingHolder(BaseModel):
    """One Agent or Assistant currently assigned to a Vault."""

    target_type: str
    target_id: str
    target_name: str
    vault_ids: list[str]


def register_vault_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    _service = VaultService()
    _bindings = CredentialBindingService(vault_service=_service)

    # ── vaults ──────────────────────────────────────────────────────────────
    @app.post(
        "/api/v1/admin/vaults",
        response_model=ApiEnvelope[Vault],
        response_model_exclude_unset=True,
    )
    async def create_vault(request: Request, body: CreateVaultRequest):
        user = await get_current_user_context(request)
        vault = await _service.create_vault(
            user,
            display_name=str(body.display_name or ""),
            metadata=body.metadata,
        )
        return success_response(vault)

    @app.get(
        "/api/v1/admin/vaults",
        response_model=ApiEnvelope[VaultCatalog],
        response_model_exclude_unset=True,
    )
    async def list_vaults(request: Request):
        user = await get_current_user_context(request)
        return success_response(
            {
                "vaults": await _service.list_vaults(user),
                "credential_delivery": credential_delivery_overview(),
            }
        )

    @app.get(
        "/api/v1/admin/vaults/{vault_id}",
        response_model=ApiEnvelope[Vault],
        response_model_exclude_unset=True,
    )
    async def get_vault(vault_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.get_vault(user, vault_id))

    @app.post(
        "/api/v1/admin/vaults/{vault_id}/archive",
        response_model=ApiEnvelope[Vault],
        response_model_exclude_unset=True,
    )
    async def archive_vault(vault_id: str, request: Request):
        user = await get_current_user_context(request)
        # Authorize before inspecting bindings so a cross-org id cannot reveal
        # whether another organization's runtime uses the Vault.
        await _service.get_vault(user, vault_id)
        await _bindings.ensure_vault_unbound(vault_id, action="archive")
        return success_response(await _service.archive_vault(user, vault_id))

    # 204 is declared, not defaulted: the handler answers 204 with no body, and
    # the default 200 documents a body no caller ever receives.
    @app.delete("/api/v1/admin/vaults/{vault_id}", status_code=204)
    async def delete_vault(vault_id: str, request: Request):
        user = await get_current_user_context(request)
        await _service.get_vault(user, vault_id)
        await _bindings.ensure_vault_unbound(vault_id, action="delete")
        await _service.delete_vault(user, vault_id)
        return Response(status_code=204)

    # ── credentials ─────────────────────────────────────────────────────────
    @app.post(
        "/api/v1/admin/vaults/{vault_id}/credentials",
        response_model=ApiEnvelope[VaultCredential],
        response_model_exclude_unset=True,
    )
    async def create_credential(vault_id: str, request: Request, body: CreateCredentialRequest):
        user = await get_current_user_context(request)
        credential = await _service.create_credential(
            user,
            vault_id,
            display_name=body.display_name,
            auth=body.auth,
        )
        return success_response(credential)

    @app.get(
        "/api/v1/admin/vaults/{vault_id}/credentials",
        response_model=ApiEnvelope[VaultCredentialList],
        response_model_exclude_unset=True,
    )
    async def list_credentials(vault_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(
            {"credentials": await _service.list_credentials(user, vault_id)}
        )

    @app.patch(
        "/api/v1/admin/vaults/{vault_id}/credentials/{credential_id}",
        response_model=ApiEnvelope[VaultCredential],
        response_model_exclude_unset=True,
    )
    async def update_credential(
        vault_id: str, credential_id: str, request: Request, body: UpdateCredentialRequest
    ):
        user = await get_current_user_context(request)
        credential = await _service.update_credential(
            user,
            vault_id,
            credential_id,
            display_name=body.display_name,
            auth=body.auth,
        )
        return success_response(credential)

    @app.post(
        "/api/v1/admin/vaults/{vault_id}/credentials/{credential_id}/archive",
        response_model=ApiEnvelope[VaultCredential],
        response_model_exclude_unset=True,
    )
    async def archive_credential(vault_id: str, credential_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(
            await _service.archive_credential(user, vault_id, credential_id)
        )

    @app.delete(
        "/api/v1/admin/vaults/{vault_id}/credentials/{credential_id}", status_code=204
    )
    async def delete_credential(vault_id: str, credential_id: str, request: Request):
        user = await get_current_user_context(request)
        await _service.delete_credential(user, vault_id, credential_id)
        return Response(status_code=204)

    # ── managed runtime bindings ────────────────────────────────────────────
    @app.get(
        "/api/v1/admin/vaults/{vault_id}/bindings",
        response_model=ApiEnvelope[list[VaultBindingHolder]],
        response_model_exclude_unset=True,
    )
    async def list_vault_bindings(vault_id: str, request: Request):
        user = await get_current_user_context(request)
        bindings = await _bindings.list_vault_bindings(user, vault_id)
        return success_response(bindings)

    @app.get(
        "/api/v1/admin/agents/{agent_id}/credential-vaults",
        response_model=ApiEnvelope[CredentialBinding],
        response_model_exclude_unset=True,
    )
    async def get_agent_credential_binding(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _bindings.get_agent_binding(user, agent_id))

    @app.put(
        "/api/v1/admin/agents/{agent_id}/credential-vaults",
        response_model=ApiEnvelope[CredentialBinding],
        response_model_exclude_unset=True,
    )
    async def set_agent_credential_binding(
        agent_id: str,
        request: Request,
        body: SetCredentialBindingRequest,
    ):
        user = await get_current_user_context(request)
        return success_response(
            await _bindings.set_agent_binding(user, agent_id, body.vault_ids)
        )

    @app.get(
        "/api/v1/admin/assistants/{assistant_id}/credential-vaults",
        response_model=ApiEnvelope[CredentialBinding],
        response_model_exclude_unset=True,
    )
    async def get_assistant_credential_binding(
        assistant_id: str, request: Request
    ):
        user = await get_current_user_context(request)
        return success_response(
            await _bindings.get_assistant_binding(user, assistant_id)
        )

    @app.put(
        "/api/v1/admin/assistants/{assistant_id}/credential-vaults",
        response_model=ApiEnvelope[CredentialBinding],
        response_model_exclude_unset=True,
    )
    async def set_assistant_credential_binding(
        assistant_id: str,
        request: Request,
        body: SetCredentialBindingRequest,
    ):
        user = await get_current_user_context(request)
        return success_response(
            await _bindings.set_assistant_binding(
                user, assistant_id, body.vault_ids
            )
        )
