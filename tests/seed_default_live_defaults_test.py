"""Seeded defaults stay live references; explicitly authored values stay pins."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.api import app as app_module
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_extension_service import (
    AgentExtensionService,
)
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    RuntimeConfigResolver,
)
from astrabox.persistence import repository as repository_module
from astrabox.seams import sandbox as sandbox_module
from astrabox.seams.extensions import (
    ExtensionCatalog,
    ExtensionCatalogItem,
    ExtensionProvider,
    ExtensionRuntimeSelection,
)
from astrabox.seams.model import (
    ModelEndpoint,
    ModelEndpointProvider,
    register_model_endpoint,
)


class _SeedAgentRepository:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def list_all_agents(self) -> list[dict[str, Any]]:
        return []

    async def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload)
        self.rows.append(row)
        return row


class _SeedEnvironmentRepository:
    def __init__(self) -> None:
        self.name = ""
        self.row: dict[str, Any] = {}

    async def upsert_by_name(self, name: str, doc: dict[str, Any]) -> None:
        self.name = name
        self.row = dict(doc)


class _PinnedOrgAgentRepository:
    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        if agent_id != "agent-pinned":
            return None
        return {
            "agent_id": agent_id,
            "created_by": "owner",
            "org_id": "operator-org",
        }


class _CapturingExtensionProvider(ExtensionProvider):
    name = "capture-pinned-org"

    def __init__(self) -> None:
        self.requested_orgs: list[str] = []

    async def list_catalog(self, *, org_id: str) -> ExtensionCatalog:
        self.requested_orgs.append(org_id)
        return ExtensionCatalog()

    def materialize(
        self,
        *,
        mcp_servers: tuple[ExtensionCatalogItem, ...],
        skills: tuple[ExtensionCatalogItem, ...],
    ) -> ExtensionRuntimeSelection:
        _ = (mcp_servers, skills)
        return ExtensionRuntimeSelection()


class _PassthroughModelEndpoint(ModelEndpointProvider):
    name = "seed-default-passthrough"

    def resolve(
        self, *, requested: ModelEndpoint, settings: Any = None
    ) -> ModelEndpoint:
        _ = (requested, settings)
        return ModelEndpoint()


register_model_endpoint(_PassthroughModelEndpoint())


@pytest.mark.asyncio
async def test_seeded_rows_do_not_copy_deployment_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh store follows later deployment changes until someone chooses a pin."""
    agents = _SeedAgentRepository()
    environment = _SeedEnvironmentRepository()
    provider = SimpleNamespace(
        runtime_defaults=lambda: SimpleNamespace(runtime_image="deployment-image")
    )
    monkeypatch.setattr(repository_module, "AgentRepository", lambda: agents)
    monkeypatch.setattr(
        repository_module, "EnvironmentRepository", lambda: environment
    )
    monkeypatch.setattr(
        sandbox_module, "default_sandbox_backend", lambda: "deployment-backend"
    )
    monkeypatch.setattr(sandbox_module, "sandbox_for_name", lambda _name: provider)
    monkeypatch.setattr(
        app_module, "_seed_default_model", lambda: "deployment-model"
    )

    await app_module._seed_default_agent_if_empty()

    assert environment.name == "claude-code"
    assert len(agents.rows) == 2
    assert "sandbox_backend" not in environment.row, (
        "the seeded Environment must resolve the current deployment backend, "
        "not freeze the seed-time backend"
    )
    assert all("org_id" not in row for row in agents.rows), (
        "seeded Agents must resolve catalogs in the current request/deployment "
        "organization, not the hardcoded default tenant"
    )
    assert all("model" not in row for row in agents.rows), (
        "seeded Agents must resolve the current deployment model, not preserve "
        "the model selected when the store was first created"
    )
    research = next(row for row in agents.rows if row["name"] == "Investment Research")
    assert research["engine_options"] == {"sdk_options": {"strict_mcp_config": True}}, (
        "the example must load Plugin content without also starting its commercial MCP declarations"
    )
    assert research["plugin_repos"] == [
        {
            "url": "https://github.com/anthropics/financial-services.git",
            "protocol": "https",
            "sha": "286f9068951335bcabd99525d5197752866ee05f",
            "plugin_paths": [
                "plugins/vertical-plugins/financial-analysis",
                "plugins/vertical-plugins/equity-research",
            ],
        }
    ], "the example must pin both reviewed official Plugins instead of following upstream main"
    assert research["mcp_servers"] == {
        "keyvex": {
            "type": "http",
            "url": "https://mcp.keyvex.com",
        }
    }, (
        "the example must include a usable public-data MCP, not only Plugin "
        "metadata, and it must be exactly a vendor entry: equality is the "
        "assertion, so a key no reader consumes cannot be added back quietly"
    )


@pytest.mark.asyncio
async def test_an_explicit_agent_org_remains_the_catalog_pin() -> None:
    provider = _CapturingExtensionProvider()
    service = AgentExtensionService(
        agent_repo=_PinnedOrgAgentRepository(),  # type: ignore[arg-type]
        provider=provider,
    )

    await service.get_catalog(
        UserContext("owner", org_id="request-org"), "agent-pinned"
    )

    assert provider.requested_orgs == ["operator-org"], (
        "an Agent-authored organization must outrank the request and deployment "
        "defaults"
    )


def test_an_explicit_environment_backend_remains_the_sandbox_pin() -> None:
    assert (
        sandbox_module.sandbox_name_for_template(
            {"sandbox_backend": "operator-backend"}
        )
        == "operator-backend"
    ), "an Environment-authored backend must outrank the deployment default"


def test_an_explicit_agent_model_remains_the_runtime_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_MODEL_NAME", "deployment-model")
    resolver = RuntimeConfigResolver(
        SimpleNamespace(
            model_endpoint_provider="seed-default-passthrough",
            model_base_url="",
            model_name="deployment-model",
            model_api_key_secret_name="",
        )
    )

    resolved = resolver.resolve_model_config({"model_name": "operator-model"})

    assert resolved["model_name"] == "operator-model", (
        "an Agent-authored model must outrank the deployment model"
    )
