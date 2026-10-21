"""The deployment's Assistant defaults: the layer every Assistant inherits.

An administrator decides here what the deployment's Assistants can do: the
RuntimeProfile and model new Assistants use, instructions, and the Skills, MCP
servers (the deployment's own, or picked from the extension catalog),
packages, native options and Credential Vaults every Assistant gets. A layer
is checked the way an Assistant's own fields are: against what the engine of
its RuntimeProfile reads, the RuntimeProfile's network boundary, and the Vaults
that may be bound. ``assistant_layers`` says how layers combine.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.service.orchestrator.assistant.assistant_layers import (
    LAYER_FIELDS,
    assistant_runtime_profile_names,
    default_runtime_profile_name,
    inherited_layers,
)
from astrabox.core.service.orchestrator.agent_extension_service import (
    AGENT_EXTENSION_CATALOG_FIELD,
    catalog_selection,
    clear_catalog_selection,
    compile_extension_selection,
    configured_extension_provider,
    extension_catalog_view,
    refuse_catalog_read,
)
from astrabox.core.service.orchestrator.mcp_assignments import (
    AGENT_MCP_ASSIGNMENTS_FIELD,
    replace_provider_assignments,
)
from astrabox.persistence.repository.assistant_defaults_repository import (
    DEPLOYMENT_SCOPE,
    AssistantDefaultsRuntimeProfileMissing,
    AssistantDefaultsRepository,
    AssistantDefaultsVaultMissing,
)
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.seams.extensions import ExtensionCatalog

#: What a layer stores: its authored fields and its catalog selection.
_STORED_FIELDS = (*LAYER_FIELDS, AGENT_EXTENSION_CATALOG_FIELD, AGENT_MCP_ASSIGNMENTS_FIELD)


def _invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def _conflict() -> APIError:
    return APIError(
        code="ASSISTANT_DEFAULTS_VERSION_CONFLICT",
        message="the Assistant defaults were saved concurrently; retry with the latest version",
        status_code=409,
    )


def _text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"{field} must be a string")
    return value.strip() or None


class AssistantDefaultsService:
    def __init__(
        self,
        *,
        repo: AssistantDefaultsRepository | None = None,
        runtime_profiles: RuntimeProfileRepository | None = None,
        assistant_service: Any = None,
    ) -> None:
        self._repo = repo or AssistantDefaultsRepository()
        self._runtime_profile_repo = runtime_profiles or RuntimeProfileRepository()
        self._assistant_service = assistant_service

    def _assistants(self) -> Any:
        if self._assistant_service is None:
            from astrabox.core.service.orchestrator.service_registry import get_assistant_service

            self._assistant_service = get_assistant_service()
        return self._assistant_service

    async def _runtime_profiles(self) -> list[dict[str, Any]]:
        return await self._runtime_profile_repo.list_all()

    async def view(self) -> dict[str, Any]:
        """The deployment's layer, the RuntimeProfile new Assistants would use, and the choices.

        The layer's catalog selection is reported as stored, so a deployment
        that runs no extension catalog can still show what the layer selected.
        """

        layer = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
        runtime_profiles = await self._runtime_profiles()
        layers = await inherited_layers(repo=self._repo)
        return {
            **{field: layer.get(field) for field in LAYER_FIELDS},
            AGENT_MCP_ASSIGNMENTS_FIELD: layer.get(AGENT_MCP_ASSIGNMENTS_FIELD),
            AGENT_EXTENSION_CATALOG_FIELD: layer.get(AGENT_EXTENSION_CATALOG_FIELD),
            "effective_runtime_profile_name": await default_runtime_profile_name(layers, runtime_profiles),
            "assistant_runtime_profile_names": await assistant_runtime_profile_names(runtime_profiles),
            "updated_at": layer.get("updated_at"),
        }

    async def put(self, user: UserContext, fields: dict[str, Any]) -> dict[str, Any]:
        """Replace the deployment layer's authored fields; its catalog selection stays."""

        unknown = sorted(set(fields) - set(LAYER_FIELDS))
        if unknown:
            raise _invalid(f"Assistant defaults take no fields {unknown}")
        runtime_profiles = await self._runtime_profiles()
        runtime_profile_name = _text(fields.get("runtime_profile_name"), "runtime_profile_name")
        if runtime_profile_name is not None and runtime_profile_name not in await assistant_runtime_profile_names(runtime_profiles):
            raise APIError(
                code="ASSISTANT_RUNTIME_PROFILE_MISSING",
                message=f"environment {runtime_profile_name!r} is not an enabled RuntimeProfile that runs Assistants",
                status_code=403,
            )
        layer: dict[str, Any] = {
            "runtime_profile_name": runtime_profile_name,
            "model_name": _text(fields.get("model_name"), "model_name"),
            "instructions": _text(fields.get("instructions"), "instructions"),
            "skills": list(fields.get("skills") or []),
            "mcp_servers": dict(fields.get("mcp_servers") or {}),
            "packages": list(fields.get("packages") or []),
            "engine_options": dict(fields.get("engine_options") or {}),
            "credential_vault_ids": list(fields.get("credential_vault_ids") or []),
        }
        await self._check_extensions(user, layer, runtime_profiles)
        current = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
        stored = {field: current.get(field) for field in _STORED_FIELDS}
        stored.update(layer)
        await self._save(user, current, stored)
        return await self.view()

    async def _save(self, user: UserContext, current: dict[str, Any], stored: dict[str, Any]) -> None:
        """Replace the layer read as ``current`` with ``stored``.

        The save is a compare-and-set on the version ``current`` was read at,
        so a save computed from a layer another save has since replaced is
        refused rather than undoing that save.
        """
        try:
            saved = await self._repo.put_layer(
                DEPLOYMENT_SCOPE,
                stored,
                expected_version=current.get("version"),
                updated_by=user.user_id,
            )
        except AssistantDefaultsRuntimeProfileMissing:
            raise APIError(
                code="ASSISTANT_RUNTIME_PROFILE_MISSING",
                message=(
                    f"environment {stored.get('runtime_profile_name')!r} is not an enabled "
                    "RuntimeProfile that runs Assistants"
                ),
                status_code=403,
            ) from None
        except AssistantDefaultsVaultMissing:
            raise APIError(
                code="VAULT_NOT_FOUND",
                message="a Credential Vault the Assistant defaults bind was deleted",
                status_code=404,
            ) from None
        if not saved:
            raise _conflict()

    async def _check_extensions(
        self, user: UserContext, layer: dict[str, Any], runtime_profiles: list[dict[str, Any]]
    ) -> None:
        """Refuse what the Assistants' engine would not read or may not reach."""

        from astrabox.core.service.orchestrator.author_boundary import validate_author_declarations
        from astrabox.core.service.orchestrator.runtime.packages import normalize_packages
        from astrabox.core.service.orchestrator.vault_service import VaultService

        layer["packages"] = normalize_packages(layer["packages"])
        if layer["credential_vault_ids"]:
            layer["credential_vault_ids"] = await VaultService().validate_binding_ids(
                user, layer["credential_vault_ids"], target_type="assistant"
            )
        authored = any(layer[field] for field in ("skills", "mcp_servers", "packages", "engine_options"))
        if not authored:
            return
        layers = [("the deployment's Assistant defaults", layer)]
        name = await default_runtime_profile_name(layers, runtime_profiles)
        environment = next((env for env in runtime_profiles if env.get("name") == name), None)
        if environment is None:
            raise _invalid(
                "choose the RuntimeProfile new Assistants use before adding Skills, MCP servers, "
                "packages or native options: what they may hold depends on its engine"
            )
        engine_kind = str(environment.get("engine_kind") or "").strip()
        self._assistants()._validate_engine_configuration(
            engine_kind,
            {"mcp_servers": layer["mcp_servers"], "skills": layer["skills"], "packages": layer["packages"]},
            layer["engine_options"],
        )
        await validate_author_declarations(
            owner="the Assistant defaults",
            networking=environment.get("networking"),
            skills=layer["skills"],
            packages=layer["packages"],
            mcp_servers=layer["mcp_servers"],
        )

    async def extensions(self) -> dict[str, Any]:
        """The extension catalog with the deployment layer's selection marked."""

        provider = configured_extension_provider()
        if provider is None:
            raise refuse_catalog_read()
        catalog = await provider.list_catalog(org_id=default_org_id())
        layer = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
        return extension_catalog_view(layer, catalog, provider.name)

    async def set_extensions(
        self, user: UserContext, *, mcp_server_ids: list[str], skill_ids: list[str]
    ) -> dict[str, Any]:
        """Select catalog MCP servers and Skills for every Assistant in the deployment.

        Without an extension catalog only an empty selection is accepted, and it
        removes every catalog selection the layer holds.
        """

        provider = configured_extension_provider()
        if provider is None:
            cleared = clear_catalog_selection(mcp_server_ids=mcp_server_ids, skill_ids=skill_ids)
            current = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
            if not catalog_selection(current):
                return extension_catalog_view(current, ExtensionCatalog(), "")
            stored = {field: current.get(field) for field in _STORED_FIELDS}
            stored.update(cleared)
            await self._save(user, current, stored)
            updated = await self._repo.get_layer(DEPLOYMENT_SCOPE) or stored
            return extension_catalog_view(updated, ExtensionCatalog(), "")
        catalog, snapshot, selected_mcp = await compile_extension_selection(
            provider, org_id=default_org_id(), mcp_server_ids=mcp_server_ids, skill_ids=skill_ids
        )
        current = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
        stored = {field: current.get(field) for field in _STORED_FIELDS}
        stored[AGENT_EXTENSION_CATALOG_FIELD] = snapshot
        stored[AGENT_MCP_ASSIGNMENTS_FIELD] = replace_provider_assignments(
            current.get(AGENT_MCP_ASSIGNMENTS_FIELD), provider.name, selected_mcp
        )
        await self._save(user, current, stored)
        updated = await self._repo.get_layer(DEPLOYMENT_SCOPE) or stored
        return extension_catalog_view(updated, catalog, provider.name)


__all__ = ["AssistantDefaultsService"]
