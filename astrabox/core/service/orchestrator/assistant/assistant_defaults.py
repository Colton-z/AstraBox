"""The deployment's Assistant defaults: the layer every Assistant inherits.

An administrator decides here what the deployment's Assistants can do: the
Environment and model new Assistants use, instructions, and the Skills, MCP
servers (the deployment's own, or picked from the extension catalog),
packages, native options and Credential Vaults every Assistant gets. A layer
is checked the way an Assistant's own fields are: against what the engine of
its Environment reads, the Environment's network boundary, and the Vaults
that may be bound. ``assistant_layers`` says how layers combine.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.service.orchestrator.assistant.assistant_layers import (
    LAYER_FIELDS,
    assistant_environment_names,
    default_environment_name,
    inherited_layers,
)
from astrabox.core.service.orchestrator.agent_extension_service import (
    AGENT_EXTENSION_CATALOG_FIELD,
    compile_extension_selection,
    configured_extension_provider,
    extension_catalog_view,
)
from astrabox.core.service.orchestrator.mcp_assignments import (
    AGENT_MCP_ASSIGNMENTS_FIELD,
    replace_provider_assignments,
)
from astrabox.persistence.repository.assistant_defaults_repository import (
    DEPLOYMENT_SCOPE,
    AssistantDefaultsRepository,
)
from astrabox.persistence.repository.environment_repository import EnvironmentRepository

#: What a layer stores: its authored fields and its catalog selection.
_STORED_FIELDS = (*LAYER_FIELDS, AGENT_EXTENSION_CATALOG_FIELD, AGENT_MCP_ASSIGNMENTS_FIELD)


def _invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


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
        environments: EnvironmentRepository | None = None,
        assistant_service: Any = None,
    ) -> None:
        self._repo = repo or AssistantDefaultsRepository()
        self._environment_repo = environments or EnvironmentRepository()
        self._assistant_service = assistant_service

    def _assistants(self) -> Any:
        if self._assistant_service is None:
            from astrabox.core.service.orchestrator.service_registry import get_assistant_service

            self._assistant_service = get_assistant_service()
        return self._assistant_service

    async def _environments(self) -> list[dict[str, Any]]:
        return await self._environment_repo.list_all()

    async def view(self) -> dict[str, Any]:
        """The deployment's layer, the Environment new Assistants would use, and the choices."""

        layer = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
        environments = await self._environments()
        layers = await inherited_layers(repo=self._repo)
        return {
            **{field: layer.get(field) for field in LAYER_FIELDS},
            "effective_environment_name": await default_environment_name(layers, environments),
            "assistant_environment_names": await assistant_environment_names(environments),
            "updated_at": layer.get("updated_at"),
        }

    async def put(self, user: UserContext, fields: dict[str, Any]) -> dict[str, Any]:
        """Replace the deployment layer's authored fields; its catalog selection stays."""

        unknown = sorted(set(fields) - set(LAYER_FIELDS))
        if unknown:
            raise _invalid(f"Assistant defaults take no fields {unknown}")
        environments = await self._environments()
        environment_name = _text(fields.get("environment_name"), "environment_name")
        if environment_name is not None and environment_name not in await assistant_environment_names(environments):
            raise APIError(
                code="ASSISTANT_ENVIRONMENT_MISSING",
                message=f"environment {environment_name!r} is not an enabled Environment that runs Assistants",
                status_code=403,
            )
        layer: dict[str, Any] = {
            "environment_name": environment_name,
            "model_name": _text(fields.get("model_name"), "model_name"),
            "instructions": _text(fields.get("instructions"), "instructions"),
            "skills": list(fields.get("skills") or []),
            "mcp_servers": dict(fields.get("mcp_servers") or {}),
            "packages": list(fields.get("packages") or []),
            "engine_options": dict(fields.get("engine_options") or {}),
            "credential_vault_ids": list(fields.get("credential_vault_ids") or []),
        }
        await self._check_extensions(user, layer, environments)
        current = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
        stored = {field: current.get(field) for field in _STORED_FIELDS}
        stored.update(layer)
        await self._repo.put_layer(DEPLOYMENT_SCOPE, stored, updated_by=user.user_id)
        return await self.view()

    async def _check_extensions(
        self, user: UserContext, layer: dict[str, Any], environments: list[dict[str, Any]]
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
        name = await default_environment_name(layers, environments)
        environment = next((env for env in environments if env.get("name") == name), None)
        if environment is None:
            raise _invalid(
                "choose the Environment new Assistants use before adding Skills, MCP servers, "
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
        catalog = await provider.list_catalog(org_id=default_org_id())
        layer = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
        return extension_catalog_view(layer, catalog, provider.name)

    async def set_extensions(
        self, user: UserContext, *, mcp_server_ids: list[str], skill_ids: list[str]
    ) -> dict[str, Any]:
        """Select catalog MCP servers and Skills for every Assistant in the deployment."""

        provider = configured_extension_provider()
        catalog, snapshot, selected_mcp = await compile_extension_selection(
            provider, org_id=default_org_id(), mcp_server_ids=mcp_server_ids, skill_ids=skill_ids
        )
        current = await self._repo.get_layer(DEPLOYMENT_SCOPE) or {}
        stored = {field: current.get(field) for field in _STORED_FIELDS}
        stored[AGENT_EXTENSION_CATALOG_FIELD] = snapshot
        stored[AGENT_MCP_ASSIGNMENTS_FIELD] = replace_provider_assignments(
            current.get(AGENT_MCP_ASSIGNMENTS_FIELD), provider.name, selected_mcp
        )
        updated = await self._repo.put_layer(DEPLOYMENT_SCOPE, stored, updated_by=user.user_id)
        return extension_catalog_view(updated, catalog, provider.name)


__all__ = ["AssistantDefaultsService"]
