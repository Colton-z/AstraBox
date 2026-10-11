"""A deployment that runs no extension catalog (``ASTRABOX_EXTENSION_PROVIDER=none``).

Agents, Assistants and the Assistant defaults keep the catalog selection they
made while a catalog ran. Without one nothing can resolve it, so the selection
is named at startup and refused where it would be used, never quietly left out
of a runtime that would then hold fewer tools than the record says. What the
console and a conversation do with such a record is the live journey in
``tests/e2e-ui/specs/*.no-extension-catalog.spec.ts``; these cases pin the
deterministic decisions behind it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from astrabox.bootstrap import BootstrapConfigError, _assert_extension_provider_is_registered
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator import agent_extension_service as extensions
from astrabox.core.service.orchestrator.mcp_registry_service import MCPRegistryService
from astrabox.persistence.repository import assistant_catalog_repository, assistant_defaults_repository
from astrabox.providers import litellm_extensions, register_builtin_providers
from astrabox.seams.extensions import selected_extension_provider


def test_none_selects_no_provider_and_a_name_nothing_registers_refuses_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_builtin_providers()

    assert selected_extension_provider("none") is None
    assert selected_extension_provider(" None ") is None
    default = selected_extension_provider("")
    assert default is not None and default.name == "litellm", (
        "an empty setting keeps the provider that declares itself the default"
    )

    monkeypatch.setenv("ASTRABOX_EXTENSION_PROVIDER", "none")
    _assert_extension_provider_is_registered()

    # A misspelt `none` must stop the process, not surface later as a broken
    # catalog on the first Agent page someone opens.
    monkeypatch.setenv("ASTRABOX_EXTENSION_PROVIDER", "nonne")
    with pytest.raises(BootstrapConfigError) as refused:
        _assert_extension_provider_is_registered()
    assert "ASTRABOX_EXTENSION_PROVIDER='nonne'" in str(refused.value)
    assert "'none'" in str(refused.value)


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def list_catalog_selections(self) -> list[dict[str, Any]]:
        return self._rows


class _Layer:
    def __init__(self, layer: dict[str, Any] | None) -> None:
        self._layer = layer

    async def get_layer(self, scope: str) -> dict[str, Any] | None:
        _ = scope
        return self._layer


class _Warnings(logging.Handler):
    """Each warning the module's own logger emits, once per emission."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def test_startup_names_each_record_that_still_selects_from_a_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = _Rows([
        {
            "agent_id": "ag-research",
            "name": "Research",
            "enabled": True,
            "mcp_assignments": [{"provider": "litellm", "item_id": "srv-search"}],
        },
        # Disabled: it starts nothing, so it is not named.
        {
            "agent_id": "ag-off",
            "name": "Off",
            "enabled": False,
            "mcp_assignments": [{"provider": "litellm", "item_id": "srv-search"}],
        },
        # A selection saved empty is no selection.
        {
            "agent_id": "ag-inline",
            "name": "Inline",
            "mcp_assignments": [],
            "extension_catalog": {"skill_ids": [], "skills": []},
        },
    ])
    assistants = _Rows([
        {
            "assistant_id": "as-mine",
            "display_name": "Mine",
            "extension_catalog": {
                "skill_ids": ["sk-review"],
                "skills": ["https://github.com/example/skills.git#review"],
            },
        },
    ])
    layer = _Layer({"mcp_assignments": [{"provider": "builtin", "item_id": "mcp_docs"}]})
    monkeypatch.setattr(extensions, "AgentRepository", lambda: agents)
    monkeypatch.setattr(
        assistant_catalog_repository, "AssistantCatalogRepository", lambda: assistants
    )
    monkeypatch.setattr(
        assistant_defaults_repository, "AssistantDefaultsRepository", lambda: layer
    )
    # Counted on the module's own logger: what reaches the root logger depends
    # on the application logger's propagation and on how many capture handlers
    # the test runner has installed there.
    warnings = _Warnings()
    extensions.logger.addHandler(warnings)
    try:
        monkeypatch.setenv("ASTRABOX_EXTENSION_PROVIDER", "")
        await extensions.warn_about_catalog_selections_without_a_catalog()
        assert warnings.messages == [], "with a catalog running, every selection still resolves"

        monkeypatch.setenv("ASTRABOX_EXTENSION_PROVIDER", "none")
        await extensions.warn_about_catalog_selections_without_a_catalog()
    finally:
        extensions.logger.removeHandler(warnings)
    named = warnings.messages
    assert len(named) == 3, named
    assert all("EXTENSION_CATALOG_DISABLED" in message for message in named)
    assert "Agent 'Research' (ag-research) selected MCP servers from 'litellm'" in named[0]
    assert "Assistant 'Mine' (as-mine) selected catalog Skills" in named[1]
    assert "Assistant defaults selected MCP servers from 'builtin'" in named[2]
    assert "every Assistant's new conversations fail" in named[2]


class _NoRepository:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the refusal must come before any read ({name})")


async def test_no_built_in_server_is_assigned_while_no_catalog_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_EXTENSION_PROVIDER", "none")
    service = MCPRegistryService(repo=_NoRepository(), agent_repo=_NoRepository())  # type: ignore[arg-type]

    with pytest.raises(APIError) as refused:
        await service.set_agent_assignment(
            UserContext("admin", roles=["admin"]), "agent-1", ["mcp_docs"]
        )

    assert refused.value.code == "EXTENSION_CATALOG_DISABLED"
    assert refused.value.status_code == 409
    assert refused.value.category == "configuration"


async def test_litellm_management_pages_close_unless_it_is_the_selected_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_EXTENSION_PROVIDER", "none")
    monkeypatch.setattr(litellm_extensions, "_registered_on", None)
    app = FastAPI()
    litellm_extensions.register_litellm_extension_routes(app)

    async with httpx.AsyncClient(
        base_url="http://astrabox.test", transport=httpx.ASGITransport(app=app)
    ) as client:
        # A grant minted while LiteLLM was selected must not keep its pages
        # open, so the refusal comes before any cookie is read.
        with pytest.raises(APIError) as refused:
            await client.get("/extension-console/open")

    assert refused.value.code == "EXTENSION_CATALOG_DISABLED"
